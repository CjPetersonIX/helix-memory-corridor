"""Fresh, corpus-scoped hybrid recall over HELIX source documents.

Every call reads the authoritative files before using an index.  Optional arms
may degrade, but their status is always exposed.  ``return_diagnostics=True``
returns ``{"results": candidates, "diagnostics": receipt}``; the default remains
an ordinary candidate list.  ``latest_events`` bypasses optional indexes for
reliable resumption after a context loss.

Authored by [R2] SRVP2-CODEX, 2026-10-19.
"""

from collections import OrderedDict
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
import threading

from .bm25_index import BM25Index
from .collector import collect_documents, normalize_categories
from .entity_graph import EntityGraph, extract_entities
from .fusion import MergedCandidate, RetrievalResult, cap_per_source, interleave_fusion, reciprocal_rank_fusion
from .temporal_filter import extract_temporal_window, filter_by_window, normalize_timestamp
from .temporal_validity import build_validity_index, filter_valid, save_validity, validity_for

INDEX_DIR = os.path.expanduser("~/.helix-memory")
# Bounded, source-generation keyed caches.  Never used to skip a source read.
_snapshots = OrderedDict()
_snapshot_lock = threading.RLock()
_MAX_SNAPSHOTS = 8
_MAX_VISIBILITY_VIEWS = 4
# Compatibility inputs for the original public reranking demonstration only.
# Production recall passes its own corpus metadata; it never reads these globals.
_doc_dates = {}
_doc_entity_counts = {}


def _now():
    return datetime.now(timezone.utc)


def _selection(categories=None, divisions=None, category=None, division=None):
    return normalize_categories(categories=categories, divisions=divisions, category=category, division=division)


def _source_snapshot(memory_dir=None, categories=None, recursive=True):
    root = os.path.realpath(os.path.expanduser(os.fspath(memory_dir or "~/memory")))
    docs = collect_documents(memory_dir=root, categories=categories, recursive=recursive)
    # Include all metadata, not only mtime: timestamp/correction edits and appends
    # must invalidate indexes even if size and mtime are deliberately preserved.
    docs = sorted(docs, key=lambda d: d["id"])
    scope = {"memory_dir": root, "categories": categories, "recursive": bool(recursive)}
    digest = hashlib.sha256(json.dumps({"scope": scope, "documents": docs}, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    key = (root, tuple(categories) if categories is not None else None, bool(recursive), digest)
    with _snapshot_lock:
        state = _snapshots.get(key)
        if state is None:
            state = {"scope": scope, "documents": docs, "by_id": {d["id"]: d for d in docs}, "graphs": {}, "graph_nx": {}, "bm25": None, "validity": build_validity_index(docs)}
            _snapshots[key] = state
            while len(_snapshots) > _MAX_SNAPSHOTS:
                _snapshots.popitem(last=False)
        else:
            _snapshots.move_to_end(key)
    missing_categories = [group for group in (categories or []) if not os.path.isdir(os.path.join(root, group))]
    receipt = {"source_generation": digest, "source_observed_at": _now().isoformat(), "memory_dir": root, "categories": categories, "document_count": len(docs), "source_status": "read" if os.path.isdir(root) else "missing", "missing_categories": missing_categories, "author": "[R2] SRVP2-CODEX"}
    return state, receipt


def _timestamp(doc):
    for key in ("timestamp", "recorded_at", "valid_from", "date"):
        value = doc.get(key)
        if value is not None:
            return normalize_timestamp(value, field_name=key)
    return None


def _visible_state(source_state, as_of, known_at=None):
    """Restrict every retrieval arm before ranking, using full-source validity.

    Filtering only a truncated arm's output can falsely hide an eligible memory
    behind many future or superseded matches.  A view keeps the unfiltered
    validity map, while indexes and graph topology contain eligible records only.
    The source snapshot and generation receipt remain unchanged.
    """
    ids = tuple(filter_valid(list(source_state["by_id"]), source_state["validity"], as_of, known_at=known_at))
    if len(ids) == len(source_state["documents"]):
        return source_state
    with _snapshot_lock:
        views = source_state.setdefault("visibility_views", OrderedDict())
        view = views.get(ids)
        if view is None:
            documents = [source_state["by_id"][identifier] for identifier in ids]
            view = {"scope": source_state["scope"], "documents": documents,
                    "by_id": {d["id"]: d for d in documents}, "bm25": None,
                    "graphs": {}, "graph_nx": {}, "validity": source_state["validity"]}
            views[ids] = view
            while len(views) > _MAX_VISIBILITY_VIEWS:
                views.popitem(last=False)
        else:
            views.move_to_end(ids)
    return view


def _newest_key(doc):
    # Stable ID tie-breaker gives reproducible results for identical timestamps.
    return (_timestamp(doc) or datetime.min.replace(tzinfo=timezone.utc), doc["id"])


def _apply_rerank(merged, *, doc_dates=None, entity_counts=None, reference=None):
    """Apply the existing importance/180-day recency formula using full time.

    Explicit metadata keeps separate corpora isolated; legacy module inputs are
    retained solely for callers of this helper in the original demo script.
    """
    dates = _doc_dates if doc_dates is None else doc_dates
    counts = _doc_entity_counts if entity_counts is None else entity_counts
    now = normalize_timestamp(reference) if reference is not None else _now()
    for candidate in merged:
        importance = 1.0 + math.log1p(counts.get(candidate.id, 0))
        stamp = normalize_timestamp(dates.get(candidate.id))
        raw_stamp = dates.get(candidate.id)
        if isinstance(raw_stamp, date) and not isinstance(raw_stamp, datetime):
            # Preserve the documented day-based helper for legacy callers.
            age_days = max(0, (now.date() - raw_stamp).days)
        else:
            age_days = max(0.0, (now - stamp).total_seconds() / 86400.0) if stamp else 0.0
        recency = math.exp(-age_days / 180.0)
        candidate.metadata.update(importance=round(importance, 4), recency_decay=round(recency, 4), final_score=candidate.rrf_score * importance * recency)
    merged.sort(key=lambda c: (c.metadata["final_score"], normalize_timestamp(dates.get(c.id)) or datetime.min.replace(tzinfo=timezone.utc), c.id), reverse=True)
    for rank, candidate in enumerate(merged, 1):
        candidate.metadata["final_rank"] = rank
    return merged


def _bm25_arm(state, query, cap):
    with _snapshot_lock:
        if state["bm25"] is None:
            scope = state["scope"]
            state["bm25"] = BM25Index().build(documents=state["documents"], memory_dir=scope["memory_dir"], categories=scope["categories"], recursive=scope["recursive"])
        index = state["bm25"]
    return index.search(query, top_n=cap)


def _graph_arm(state, query, cap):
    groups = {}
    for doc in state["documents"]:
        groups.setdefault(doc.get("category", doc.get("division", ".")), []).append(doc)
    out, fallbacks = [], []
    for category, docs in groups.items():
        with _snapshot_lock:
            graph = state["graphs"].get(category)
            if graph is None:
                graph = EntityGraph()
                for doc in docs:
                    graph.add_document(doc["id"], doc["text"])
                state["graphs"][category] = graph
            nx_cache = state["graph_nx"].setdefault(category, {})
        seeds = graph.query_entities(query)
        if not seeds:
            continue
        try:
            from .ppr_ranking import ppr_rank
            ranked, method = ppr_rank(graph, seeds, top_n=cap, nx_graph=graph.to_networkx(nx_cache), return_method=True)
            if method != "ppr":
                fallbacks.append({"category": category, "reason": getattr(graph, "_ppr_fallback_reason", "PPR unavailable"), "fallback": method})
        except Exception as exc:
            ranked = graph.walk(seeds, hops=2, max_docs=cap)
            method = "walk"
            fallbacks.append({"category": category, "reason": str(exc), "fallback": "walk"})
        for doc_id, score in ranked:
            doc = state["by_id"][doc_id]
            out.append(RetrievalResult(doc_id, doc["text"], score, "graph", {"category": category, "division": category, "graph_method": method, "seed_entities": seeds[:3]}))
    out.sort(key=lambda r: (r.score, _newest_key(state["by_id"][r.id])), reverse=True)
    return out[:cap], fallbacks


def _temporal_arm(state, query, cap, reference):
    window = extract_temporal_window(query, reference=reference.date())
    if window is None:
        return [], "abstained"
    # Date windows intentionally remain calendar windows, while validity and
    # recency retain full timestamps and never truncate intraday corrections.
    dates = {d["id"]: (_timestamp(d).date() if _timestamp(d) else None) for d in state["documents"]}
    scores = filter_by_window(dates, window)
    ranked = sorted(scores.items(), key=lambda pair: (pair[1], _newest_key(state["by_id"][pair[0]])), reverse=True)
    out = [RetrievalResult(doc_id, state["by_id"][doc_id]["text"], score, "temporal", {"window": [str(window[0]), str(window[1])]}) for doc_id, score in ranked if score > 0]
    return out[:cap], "ok"


def _semantic_arm(state, query, cap, *, memory_dir, categories, recursive, index_dir):
    from .semantic_index import SemanticIndex
    index = SemanticIndex(index_dir=index_dir) if index_dir is not None else SemanticIndex()
    # Explicit opt-in permits model loading.  A fresh generation is built before
    # querying; never query globally shared, stale embedding collections.
    index.build(documents=state["documents"], memory_dir=memory_dir, categories=categories, recursive=recursive)
    hits = index.search(query, top_n=cap, refresh_sources=False)
    return hits, list(getattr(index, "diagnostics", []))


def recall(query, division=None, top_k=10, per_arm_cap=25, dedup_mode=False, include_semantic=False, rerank=True, as_of=None, *, memory_dir=None, categories=None, divisions=None, category=None, recursive=True, return_diagnostics=False, index_dir=None, known_at=None):
    """Return fresh, source-backed candidates, optionally with a health receipt.

    All category/division spellings are aliases; contradictory selections error.
    Core recall is offline and never imports embedding/model dependencies.
    ``dedup_mode`` includes superseded records for finding previous versions,
    while explicit as-of/known-at values are still validated before source reads.
    An empty result is a search outcome, never proof that no memories exist.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a nonempty string")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")
    if not isinstance(per_arm_cap, int) or isinstance(per_arm_cap, bool) or per_arm_cap <= 0:
        raise ValueError("per_arm_cap must be a positive integer")
    moment = normalize_timestamp(as_of, field_name="as_of") or _now()
    known_moment = normalize_timestamp(known_at, field_name="known_at")
    selected = _selection(categories, divisions, category, division)
    source_state, receipt = _source_snapshot(memory_dir, selected, recursive)
    state = source_state if dedup_mode else _visible_state(source_state, moment, known_moment)
    arms, health = {}, {}
    for name in ("semantic", "bm25", "graph", "temporal"):
        if name == "semantic" and not include_semantic:
            arms[name] = []
            health[name] = {"status": "disabled", "reason": "optional embeddings not requested"}
            continue
        try:
            details = []
            status = "ok"
            if name == "bm25":
                hits = _bm25_arm(state, query, per_arm_cap)
            elif name == "graph":
                hits, details = _graph_arm(state, query, per_arm_cap)
                if details:
                    status = "degraded"
            elif name == "temporal":
                hits, status = _temporal_arm(state, query, per_arm_cap, moment)
            else:
                hits, details = _semantic_arm(state, query, per_arm_cap, memory_dir=receipt["memory_dir"], categories=selected, recursive=recursive, index_dir=index_dir)
                if details:
                    status = "degraded"
            # Trust relevance scores only.  Hydrate all source text and metadata
            # from this call's authoritative snapshot, never an index's copy.
            current = []
            for hit in hits:
                doc = state["by_id"].get(hit.id)
                if doc is None:
                    continue
                hit.text = doc["text"]
                hit.metadata = {**hit.metadata, **{k: v for k, v in doc.items() if k != "text"}}
                current.append(hit)
            arms[name] = cap_per_source(current, per_arm_cap)
            health[name] = {"status": status, "hits": len(arms[name])}
            if details:
                health[name]["fallbacks"] = details
        except Exception as exc:
            arms[name] = []
            health[name] = {"status": "failed", "error_type": type(exc).__name__, "reason": str(exc), "hits": 0}
    ordered = [arms[name] for name in ("semantic", "bm25", "graph", "temporal")]
    merged = interleave_fusion(ordered) if dedup_mode else reciprocal_rank_fusion(ordered)
    if not dedup_mode:
        valid_ids = set(filter_valid([c.id for c in merged], state["validity"], moment, known_at=known_moment))
        merged = [c for c in merged if c.id in valid_ids]
    for candidate in merged:
        doc = state["by_id"][candidate.id]
        candidate.text = doc["text"]
        candidate.metadata.update({k: v for k, v in doc.items() if k != "text"})
        candidate.metadata.update(validity_for(candidate.id, state["validity"], known_at=known_moment))
    if rerank and not dedup_mode:
        _apply_rerank(merged, doc_dates={d["id"]: _timestamp(d) for d in state["documents"]}, entity_counts={d["id"]: len(extract_entities(d["text"])) for d in state["documents"]}, reference=moment)
    elif not dedup_mode:
        merged.sort(key=lambda c: (c.rrf_score, _newest_key(state["by_id"][c.id])), reverse=True)
    diagnostics = {**receipt, "eligible_document_count": len(state["documents"]), "excluded_by_validity": len(source_state["documents"]) - len(state["documents"]), "arms": health, "degraded": receipt["source_status"] != "read" or bool(receipt["missing_categories"]) or any(h["status"] in ("failed", "degraded") for h in health.values()), "as_of": moment.isoformat(), "known_at": known_moment.isoformat() if known_moment else None, "empty_result_meaning": "No matching candidates from the available retrieval arms; not proof of absent memory."}
    candidates = merged[:top_k]
    for rank, candidate in enumerate(candidates, 1):
        candidate.rrf_rank = rank
        candidate.metadata.update(source_generation=receipt["source_generation"], source_observed_at=receipt["source_observed_at"], retrieval_health=diagnostics)
    return {"results": candidates, "diagnostics": diagnostics} if return_diagnostics else candidates


def latest_events(memory_dir=None, task_id=None, categories=None, limit=10, as_of=None, *, divisions=None, category=None, division=None, recursive=True, known_at=None):
    """Read newest source records directly, independent of optional indexes.

    Returns full document dictionaries with provenance, generation and validity.
    This is the deterministic resume route; task_id is an exact metadata match.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    moment = normalize_timestamp(as_of, field_name="as_of") or _now()
    known_moment = normalize_timestamp(known_at, field_name="known_at")
    selected = _selection(categories, divisions, category, division)
    state, receipt = _source_snapshot(memory_dir, selected, recursive)
    docs = [doc for doc in state["documents"] if task_id is None or doc.get("task_id") == task_id]
    valid = set(filter_valid([d["id"] for d in docs], state["validity"], moment, known_at=known_moment))
    docs = sorted((d for d in docs if d["id"] in valid), key=_newest_key, reverse=True)
    return [{**doc, "validity": validity_for(doc["id"], state["validity"], known_at=known_moment), "source_generation": receipt["source_generation"], "source_observed_at": receipt["source_observed_at"], "source_status": "read"} for doc in docs[:limit]]


def rebuild_all(memory_dir=None, categories=None, include_semantic=False, *, divisions=None, category=None, division=None, recursive=True, index_dir=None):
    """Build current source-generation indexes with explicit, separated options.

    No deletion or source mutation occurs.  Semantic models are opt-in.  Derived
    caches remain corpus-scoped; source documents are always reread at recall.
    """
    selected = _selection(categories, divisions, category, division)
    state, receipt = _source_snapshot(memory_dir, selected, recursive)
    index = BM25Index().build(documents=state["documents"], memory_dir=receipt["memory_dir"], categories=selected, recursive=recursive)
    state["bm25"] = index
    if index_dir is not None:
        path = os.path.join(os.fspath(index_dir), "bm25-" + receipt["source_generation"] + ".json")
        index.save(path)
    else:
        index.save()
    generation_dir = os.path.join(os.fspath(index_dir or INDEX_DIR), "generations", receipt["source_generation"])
    counts = {}
    for doc in state["documents"]:
        group = doc.get("category", doc.get("division", "."))
        counts[group] = counts.get(group, 0) + 1
    for group in counts:
        graph = EntityGraph()
        for doc in state["documents"]:
            if doc.get("category", doc.get("division", ".")) == group:
                graph.add_document(doc["id"], doc["text"])
        state["graphs"][group] = graph
        group_hash = hashlib.sha256(group.encode()).hexdigest()[:24]
        graph.save(os.path.join(generation_dir, "graph-" + group_hash + ".json"))
    state["graph_nx"] = {}
    save_validity(state["validity"], os.path.join(generation_dir, "validity.json"))
    stats = {**receipt, "documents": len(state["documents"]), "by_division": counts, "by_category": counts, "bm25": "built", "bm25_cache": index.cache_path, "generation_dir": generation_dir, "validity": "built", "semantic": "disabled"}
    for group, graph in state["graphs"].items():
        stats["graph_" + group] = f"{len(graph.entities)} entities"
    if include_semantic:
        try:
            from .semantic_index import SemanticIndex
            semantic = SemanticIndex(index_dir=index_dir) if index_dir is not None else SemanticIndex()
            semantic.build(documents=state["documents"], memory_dir=receipt["memory_dir"], categories=selected, recursive=recursive)
            stats["semantic"] = "built"
        except Exception as exc:
            stats["semantic"] = f"unavailable: {type(exc).__name__}: {exc}"
    return stats


build_index = rebuild_all
