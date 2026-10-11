"""
Unified 4-arm recall over HELIX memory.

Arms (run in parallel via threads):
  1. semantic — ChromaDB cosine similarity (sentence-transformers)
  2. bm25     — rank-bm25 keyword search
  3. graph    — Personalized PageRank over the entity co-occurrence graph
                (HippoRAG-style; falls back to 1-2 hop walk if networkx
                is unavailable)
  4. temporal — date-window filter/boost

Post-processing:
  - Bi-temporal validity filter (Graphiti-style): hides superseded facts
    unless dedup_mode is on. as_of defaults to now.
  - Multiplicative re-rank (mem0-style):
    final = rrf * (1 + log(1 + entity_count)) * exp(-age_days / 180).
    Disabled in dedup_mode (interleave order is deliberate there).

Fusion: reciprocal_rank_fusion (default) or interleave_fusion (dedup mode).

Usage:
    from recall import recall
    results = recall("What did IX decide about crypto?", top_k=5)
    for r in results:
        print(r.rrf_rank, r.rrf_score, r.id, r.source_ranks)
"""

import concurrent.futures
import math
import os
import re
from datetime import date

from .bm25_index import BM25Index
from .collector import collect_documents
from .entity_graph import EntityGraph, extract_entities
from .fusion import (
    MergedCandidate,
    RetrievalResult,
    cap_per_source,
    interleave_fusion,
    reciprocal_rank_fusion,
)
from .temporal_filter import extract_temporal_window, filter_by_window

INDEX_DIR = os.path.expanduser("~/.helix-memory")

# Module-level caches so repeated recall() calls don't rebuild indexes.
_bm25: BM25Index | None = None
_graphs: dict[str, EntityGraph] = {}
_nx_cache: dict[str, dict] = {}  # division -> {"nx": networkx graph}
_doc_texts: dict[str, str] = {}
_doc_dates: dict[str, object] = {}
_doc_entity_counts: dict[str, int] = {}
_validity: dict | None = None


def _ensure_bm25(divisions: list[str] | None = None, **collect_kw) -> BM25Index:
    global _bm25
    if _bm25 is None:
        _bm25 = BM25Index.load_or_build(divisions=divisions, **collect_kw)
    return _bm25


def _ensure_graph(division: str, **collect_kw) -> EntityGraph:
    if division not in _graphs:
        path = os.path.join(INDEX_DIR, f"graph-{division}.json")
        if os.path.isfile(path):
            _graphs[division] = EntityGraph.load(path)
        else:
            g = EntityGraph()
            for d in collect_documents(divisions=[division], **collect_kw):
                g.add_document(d["id"], d["text"])
            g.save(path)
            _graphs[division] = g
    return _graphs[division]


def _ensure_doc_meta(**collect_kw) -> None:
    global _doc_texts, _doc_dates, _doc_entity_counts
    if not _doc_texts:
        from .temporal_filter import date_from_qpulse_path

        for d in collect_documents(**collect_kw):
            _doc_texts[d["id"]] = d["text"][:1200]
            _doc_dates[d["id"]] = date_from_qpulse_path(d["id"])
            _doc_entity_counts[d["id"]] = len(extract_entities(d["text"]))


def _ensure_validity(**collect_kw) -> dict:
    """Load or build the bi-temporal validity index (Graphiti-style)."""
    global _validity
    if _validity is None:
        from .temporal_filter import date_from_qpulse_path
        from .temporal_validity import (
            VALIDITY_PATH,
            build_validity_index,
            load_validity,
            save_validity,
        )

        if os.path.isfile(VALIDITY_PATH):
            _validity = load_validity()
        else:
            docs = []
            for d in collect_documents(**collect_kw):
                docs.append(
                    {
                        "id": d["id"],
                        "division": d["division"],
                        "text": d["text"],
                        "date": date_from_qpulse_path(d["id"]),
                    }
                )
            _validity = build_validity_index(docs)
            save_validity(_validity)
    return _validity


def _apply_validity(
    merged: list[MergedCandidate], validity: dict, as_of
) -> list[MergedCandidate]:
    """Drop candidates whose validity window does not cover as_of.

    as_of=None means "now". Survivors get valid_from/valid_until annotated
    in metadata.
    """
    from .temporal_validity import filter_valid, validity_for

    valid_ids = set(filter_valid([c.id for c in merged], validity, as_of))
    out: list[MergedCandidate] = []
    for c in merged:
        if c.id not in valid_ids:
            continue  # superseded as of the query time
        v = validity_for(c.id, validity)
        c.metadata["valid_from"] = v["valid_from"]
        c.metadata["valid_until"] = v["valid_until"]
        if v.get("superseded_by"):
            c.metadata["superseded_by"] = v["superseded_by"]
        out.append(c)
    return out


def _apply_rerank(merged: list[MergedCandidate]) -> list[MergedCandidate]:
    """Multiplicative re-rank (mem0-style).

    final_score = rrf_score x importance x recency_decay, where
      importance    = 1 + log(1 + distinct_entity_count)
      recency_decay = exp(-age_days / 180)   (180-day decay constant)

    Sorts in place by final_score desc and stamps final_rank + components
    into metadata. Undated docs get recency 1.0 (no penalty).
    """
    today = date.today()
    for c in merged:
        ecount = _doc_entity_counts.get(c.id, 0)
        importance = 1.0 + math.log1p(ecount)
        d = _doc_dates.get(c.id)
        age_days = (today - d).days if d is not None else 0
        recency = math.exp(-age_days / 180.0)
        final = c.rrf_score * importance * recency
        c.metadata["importance"] = round(importance, 4)
        c.metadata["recency_decay"] = round(recency, 4)
        c.metadata["final_score"] = final
    merged.sort(key=lambda c: c.metadata["final_score"], reverse=True)
    for i, c in enumerate(merged, start=1):
        c.metadata["final_rank"] = i
    return merged


def _graph_arm(
    query: str, division: str | None, top_n: int, **collect_kw
) -> list[RetrievalResult]:
    from .collector import _detect_divisions

    if division:
        divisions = [division]
    else:
        divisions = _detect_divisions(collect_kw.get("memory_dir"))
        if not divisions:
            divisions = []
    out: list[RetrievalResult] = []
    for div in divisions:
        g = _ensure_graph(div, **collect_kw)
        seeds = g.query_entities(query)
        if not seeds:
            continue
        # Personalized PageRank (HippoRAG-style); fall back to the simple
        # 1-2 hop walk if networkx is unavailable.
        try:
            from .ppr_ranking import ppr_rank

            cache = _nx_cache.setdefault(div, {})
            nx_g = g.to_networkx(cache)
            ranked = ppr_rank(g, seeds, top_n=top_n, nx_graph=nx_g)
            method = "ppr"
        except Exception as e:
            print(f"[helix-memory] PPR unavailable, falling back to walk: {e}")
            ranked = g.walk(seeds, hops=2, max_docs=top_n)
            method = "walk"
        for doc_id, score in ranked:
            out.append(
                RetrievalResult(
                    id=doc_id,
                    text=_doc_texts.get(doc_id, "")[:1200],
                    score=score,
                    source="graph",
                    metadata={
                        "division": div,
                        "seed_entities": seeds[:3],
                        "graph_method": method,
                    },
                )
            )
    out.sort(key=lambda r: r.score, reverse=True)
    return out[:top_n]


def _temporal_arm(
    query: str, division: str | None, top_n: int, **collect_kw
) -> list[RetrievalResult]:
    _ensure_doc_meta(**collect_kw)
    window = extract_temporal_window(query)
    if window is None:
        return []  # no temporal constraint -> arm abstains
    scores = filter_by_window(_doc_dates, window)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    out: list[RetrievalResult] = []
    for doc_id, s in ranked:
        if s <= 0:
            continue
        # division from path: memory/helix/<division>/...
        if division:
            m = re.match(r"memory/helix/([^/]+)/", doc_id)
            if m and m.group(1) != division:
                continue
        out.append(
            RetrievalResult(
                id=doc_id,
                text=_doc_texts.get(doc_id, "")[:1200],
                score=s,
                source="temporal",
                metadata={"window": [str(window[0]), str(window[1])]},
            )
        )
        if len(out) >= top_n:
            break
    return out


def _semantic_arm(
    query: str, division: str | None, top_n: int
) -> list[RetrievalResult]:
    try:
        from .semantic_index import SemanticIndex

        idx = SemanticIndex()
        return idx.search(query, top_n=top_n, division=division)
    except Exception as e:
        # semantic deps not installed -> arm abstains gracefully
        print(f"[helix-memory] semantic arm unavailable: {e}")
        return []


def recall(
    query: str,
    division: str | None = None,
    top_k: int = 10,
    per_arm_cap: int = 25,
    dedup_mode: bool = False,
    include_semantic: bool = True,
    rerank: bool = True,
    as_of=None,
    **collect_kw,
) -> list[MergedCandidate]:
    """Run 4-arm hybrid recall and return fused top_k candidates.

    Args:
        query: natural-language query.
        division: restrict to one HELIX division, or None for all.
        top_k: number of fused results to return.
        per_arm_cap: cap each arm's raw hits before fusion.
        dedup_mode: use interleave fusion (for finding near-duplicates).
            Validity filtering and re-ranking are skipped in this mode:
            the goal is finding the old twin, even if superseded.
        include_semantic: set False to skip the embedding arm (faster, no model).
        rerank: apply multiplicative re-rank
            (rrf x importance x recency_decay) after fusion. Default True.
        as_of: bi-temporal query time (date/datetime/ISO string). Only
            memories valid at that time are returned. None (default) = now,
            so corrections automatically hide superseded facts.
    """
    _ensure_doc_meta(**collect_kw)
    bm25 = _ensure_bm25(
        divisions=[division] if division else None, **collect_kw
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            "semantic": ex.submit(
                _semantic_arm, query, division, per_arm_cap
            )
            if include_semantic
            else None,
            "bm25": ex.submit(bm25.search, query, per_arm_cap, division),
            "graph": ex.submit(_graph_arm, query, division, per_arm_cap, **collect_kw),
            "temporal": ex.submit(
                _temporal_arm, query, division, per_arm_cap, **collect_kw
            ),
        }
        arms: dict[str, list[RetrievalResult]] = {}
        for name, fut in futs.items():
            if fut is None:
                arms[name] = []
                continue
            try:
                arms[name] = cap_per_source(fut.result(timeout=120), per_arm_cap)
            except Exception as e:
                print(f"[helix-memory] {name} arm failed: {e}")
                arms[name] = []

    result_lists = [arms["semantic"], arms["bm25"], arms["graph"], arms["temporal"]]
    if dedup_mode:
        merged = interleave_fusion(result_lists)
    else:
        merged = reciprocal_rank_fusion(result_lists)
        # Bi-temporal validity (Graphiti-style): hide superseded facts.
        merged = _apply_validity(merged, _ensure_validity(**collect_kw), as_of)
        # Multiplicative re-rank (mem0-style).
        if rerank:
            merged = _apply_rerank(merged)
    return merged[:top_k]


def rebuild_all(**collect_kw) -> dict:
    """Rebuild every index from scratch. Returns stats."""
    global _bm25, _graphs, _nx_cache, _doc_texts, _doc_dates, _doc_entity_counts, _validity
    _bm25 = None
    _graphs = {}
    _nx_cache = {}
    _doc_texts = {}
    _doc_dates = {}
    _doc_entity_counts = {}
    _validity = None

    from .collector import _detect_divisions, collect_documents, doc_count_by_division
    from .temporal_filter import date_from_qpulse_path
    from .temporal_validity import VALIDITY_PATH, build_validity_index, save_validity

    docs = collect_documents(**collect_kw)
    stats: dict = {"documents": len(docs), "by_division": doc_count_by_division(docs)}

    # BM25
    bm25 = BM25Index().build(**collect_kw)
    bm25.save()
    _bm25 = bm25
    stats["bm25"] = "built"

    # Graphs (per category)
    _divs = _detect_divisions(collect_kw.get("memory_dir"))
    for div in _divs:
        g = EntityGraph()
        for d in collect_documents(divisions=[div], **collect_kw):
            g.add_document(d["id"], d["text"])
        path = os.path.join(INDEX_DIR, f"graph-{div}.json")
        g.save(path)
        _graphs[div] = g
        stats[f"graph_{div}"] = f"{len(g.entities)} entities"

    # Bi-temporal validity index (Graphiti-style)
    vdocs = [
        {
            "id": d["id"],
            "division": d["division"],
            "text": d["text"],
            "date": date_from_qpulse_path(d["id"]),
        }
        for d in docs
    ]
    _validity = build_validity_index(vdocs)
    save_validity(_validity)
    superseded = sum(1 for v in _validity.values() if v["valid_until"] is not None)
    stats["validity"] = f"built ({superseded} superseded windows)"

    # Semantic (slow; optional)
    try:
        from .semantic_index import SemanticIndex

        SemanticIndex().build(**collect_kw)
        stats["semantic"] = "built"
    except Exception as e:
        stats["semantic"] = f"skipped: {e}"

    return stats
