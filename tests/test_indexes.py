"""Regression tests for index freshness. Semantic tests use in-memory fakes only.

No embedding models, network services, or semantic dependencies are downloaded.
Authored by [R2] SRVP2-CODEX.
"""

import builtins
import json
import math
import os
import sys
from types import ModuleType
from pathlib import Path

import pytest

from helix_memory import bm25_index
from helix_memory.bm25_index import BM25Index
from helix_memory.collector import collect_documents
from helix_memory.entity_graph import EntityGraph
from helix_memory.fusion import RetrievalResult, interleave_fusion, reciprocal_rank_fusion
from helix_memory.ppr_ranking import ppr_rank
from helix_memory.semantic_index import SemanticIndex


def test_optional_chroma_client_disables_telemetry(tmp_path, monkeypatch):
    chroma = ModuleType("chromadb")
    config = ModuleType("chromadb.config")
    captured = {}

    class Settings:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    def persistent_client(**kwargs):
        captured["settings"] = kwargs["settings"]
        return object()

    config.Settings = Settings
    chroma.PersistentClient = persistent_client
    monkeypatch.setitem(sys.modules, "chromadb", chroma)
    monkeypatch.setitem(sys.modules, "chromadb.config", config)
    index = SemanticIndex(index_dir=tmp_path)
    index.scope = {"memory_dir": str(tmp_path), "categories": None, "recursive": True}
    index._get_client()
    assert captured["anonymized_telemetry"] is False
    assert isinstance(captured["settings"], Settings)


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


@pytest.fixture
def memory(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    write(root, "MEMORY.md", "Canonical directory contains a copper compass.")
    write(root, "00-system/a.md", "Preserve all historical records. Silver continuity protocol.")
    return root


def test_bm25_accepts_categories_and_old_divisions_alias(memory):
    modern = BM25Index().build(memory_dir=memory, categories=["00-system"])
    legacy = BM25Index().build(memory_dir=memory, divisions=["00-system"])
    assert modern.fingerprint == legacy.fingerprint
    assert modern.search("continuity", category="00-system")[0].score > 0
    assert legacy.search("continuity", division="00-system")[0].metadata["category"] == "00-system"


def test_bm25_includes_root_and_returns_full_matching_source(memory):
    text = "leading " * 500 + "rare_needle exact ending"
    write(memory, "MEMORY.md", text)
    result = BM25Index().build(memory_dir=memory).search("needle")[0]
    assert result.text == text
    assert result.metadata["source_path"] == str(memory / "MEMORY.md")
    assert result.metadata["category"] == "."


def test_bm25_exact_token_matches_survive_negative_single_doc_idf(tmp_path):
    idx = BM25Index().build(documents=[{"id": "single", "text": "copper compass", "category": "."}])
    assert idx.search("copper")[0].score > 0
    assert idx.search("unrelated") == []


@pytest.mark.parametrize("documents", [[], [{"id": "empty", "text": "---", "division": "."}]])
def test_empty_indexes_are_searchable(documents):
    assert BM25Index().build(documents=documents).search("hello") == []


def test_empty_explicit_categories_means_no_documents(memory):
    assert BM25Index().build(memory_dir=memory, categories=[]).search("copper") == []


def test_filter_happens_before_top_n(memory):
    for i in range(50):
        write(memory, f"noise/{i}.md", "copper")
    write(memory, "00-system/target.md", "copper compass")
    hits = BM25Index().build(memory_dir=memory).search("copper", top_n=1, category="00-system")
    assert len(hits) == 1 and hits[0].metadata["category"] == "00-system"


def test_bm25_cache_rebuilds_for_same_size_same_mtime_edit(memory, tmp_path):
    source = memory / "MEMORY.md"
    source.write_text("copper compass", encoding="utf-8")
    original_stat = source.stat()
    cache = tmp_path / "cache.json"
    first = BM25Index.load_or_build(cache, memory_dir=memory)
    source.write_text("silver compass", encoding="utf-8")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    second = BM25Index.load_or_build(cache, memory_dir=memory)
    assert first.fingerprint != second.fingerprint
    assert "silver compass" in [h.text for h in second.search("silver")]
    assert not any(h.metadata["category"] == "." for h in second.search("copper"))


def test_bm25_cache_rebuilds_for_new_file_and_scope(memory, tmp_path):
    cache = tmp_path / "cache.json"
    BM25Index.load_or_build(cache, memory_dir=memory, categories=["00-system"])
    write(memory, "00-system/new.md", "newly persisted indigo decision")
    fresh = BM25Index.load_or_build(cache, memory_dir=memory, categories=["00-system"])
    assert fresh.search("indigo")
    root_only = BM25Index.load_or_build(cache, memory_dir=memory, categories=["."], recursive=False)
    assert root_only.search("copper")
    assert root_only.search("indigo") == []


def test_bm25_default_cache_path_is_corpus_specific(tmp_path, monkeypatch):
    monkeypatch.setattr(bm25_index, "INDEX_DIR", str(tmp_path / "index"))
    left, right = tmp_path / "left", tmp_path / "right"
    write(left, "a.md", "copper")
    write(right, "a.md", "silver")
    a = BM25Index.load_or_build(memory_dir=left)
    b = BM25Index.load_or_build(memory_dir=right)
    assert a.cache_path != b.cache_path
    assert Path(a.cache_path).is_file() and Path(b.cache_path).is_file()
    assert b.search("copper") == []


def test_default_save_load_share_corpus_scoped_path(memory, tmp_path, monkeypatch):
    monkeypatch.setattr(bm25_index, "INDEX_DIR", str(tmp_path / "index"))
    BM25Index().build(memory_dir=memory).save()
    assert BM25Index.load(memory_dir=memory).search("copper")


def test_load_rejects_changed_sources_instead_of_mixing_scores(memory, tmp_path):
    cache = tmp_path / "cache.json"
    BM25Index().build(memory_dir=memory).save(cache)
    write(memory, "MEMORY.md", "Replacement violet decision")
    with pytest.raises(ValueError, match="changed"):
        BM25Index.load(cache, memory_dir=memory)
    # Loading without a source scope is an explicit coherent historical snapshot.
    old = BM25Index.load(cache)
    assert old.search("copper") and not old.search("violet")


def test_legacy_cache_is_rebuilt(memory, tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"doc_ids": ["obsolete"], "tokenized": [["old"]], "divisions": ["."]}))
    fresh = BM25Index.load_or_build(cache, memory_dir=memory)
    assert fresh.search("copper") and "obsolete" not in fresh.doc_ids


def test_manifest_integrity_is_checked(memory, tmp_path):
    cache = tmp_path / "cache.json"
    BM25Index().build(memory_dir=memory).save(cache)
    data = json.loads(cache.read_text())
    data["documents"][0]["text"] = "tampered"
    cache.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="integrity"):
        BM25Index.load(cache)


def test_provided_document_metadata_is_preserved():
    doc = {"id": "event-1", "text": "copper", "category": "00-system", "source_path": "/memory/a.md",
           "recorded_at": "2026-10-19T10:12:00Z", "task_id": "repair-1", "supersedes": ["event-0"]}
    hit = BM25Index().build(documents=[doc]).search("copper")[0]
    assert hit.metadata["task_id"] == "repair-1"
    assert hit.metadata["supersedes"] == ["event-0"]


class FakeModel:
    def encode(self, texts, show_progress_bar=False):
        return [[float(text.lower().count("copper")), float(text.lower().count("silver")), 0.1]
                for text in texts]


class FakeCollection:
    def __init__(self):
        self.records = {}

    def upsert(self, ids, embeddings, documents, metadatas):
        for values in zip(ids, embeddings, documents, metadatas):
            self.records[values[0]] = values[1:]

    def query(self, query_embeddings, n_results, include):
        query = query_embeddings[0]
        def distance(record):
            vector = record[0]
            return 1 - sum(a*b for a, b in zip(query, vector)) / math.sqrt(sum(a*a for a in query) * sum(a*a for a in vector))
        hits = sorted(self.records.items(), key=lambda item: distance(item[1]))[:n_results]
        return {"ids": [[identifier for identifier, _ in hits]],
                "distances": [[distance(record) for _, record in hits]]}

    def delete(self, **kwargs):
        raise AssertionError("A generation rebuild must never clear an existing collection")


class FakeClient:
    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, name, metadata):
        return self.collections.setdefault(name, FakeCollection())


def semantic(tmp_path, client=None):
    idx = SemanticIndex(index_dir=tmp_path / "semantic")
    idx._model = FakeModel()
    idx._client = client or FakeClient()
    return idx


def test_semantic_auto_detects_root_and_categories_without_DIVISIONS(memory, tmp_path):
    idx = semantic(tmp_path).build(memory_dir=memory)
    hits = idx.search("copper")
    assert any(hit.metadata["category"] == "." for hit in hits)
    assert any(hit.metadata["category"] == "00-system" for hit in hits)
    assert idx.search("copper", division=".")[0].text.startswith("Canonical")


def test_semantic_generation_refresh_keeps_old_collections(memory, tmp_path):
    idx = semantic(tmp_path).build(memory_dir=memory)
    old_generation = idx.generation
    old_collections = dict(idx._client.collections)
    write(memory, "MEMORY.md", "silver replacement memory")
    fresh_hits = idx.search("silver", category=".")
    assert idx.generation != old_generation
    assert fresh_hits[0].text == "silver replacement memory"
    assert all(name in idx._client.collections for name in old_collections)
    assert set(idx._collections.values()).isdisjoint(set(old_collections.values()))


def test_semantic_can_search_one_explicitly_frozen_snapshot(memory, tmp_path):
    idx = semantic(tmp_path).build(memory_dir=memory)
    before = idx.generation
    write(memory, "MEMORY.md", "silver replacement memory")
    frozen = idx.search("copper", category=".", refresh_sources=False)
    assert idx.generation == before and "copper compass" in frozen[0].text
    current = idx.search("silver", category=".")
    assert idx.generation != before and current[0].text == "silver replacement memory"


def test_semantic_ignores_stale_ids_not_in_active_generation(tmp_path):
    idx = semantic(tmp_path).build(documents=[{"id": "current", "text": "copper", "category": "."}])
    collection = idx._collections["."]
    collection.upsert(ids=["stale"], embeddings=[[1., 0., 0.1]], documents=["fabrication"], metadatas=[{}])
    # Fake storage corruption never becomes authoritative returned text.
    assert all(hit.id == "current" for hit in idx.search("copper", top_n=10))


def test_semantic_separates_roots_even_for_identical_document_ids(tmp_path):
    client = FakeClient()
    a = semantic(tmp_path, client).build(documents=[{"id": "same", "text": "copper", "category": "."}], memory_dir=tmp_path / "a")
    b = semantic(tmp_path, client).build(documents=[{"id": "same", "text": "copper", "category": "."}], memory_dir=tmp_path / "b")
    assert a.generation != b.generation
    assert set(a._collections.values()).isdisjoint(set(b._collections.values()))


def test_empty_semantic_build_does_not_import_optional_stack(tmp_path, monkeypatch):
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name in {"chromadb", "sentence_transformers", "torch"}:
            raise AssertionError("Optional stack must not be imported for an empty corpus")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked)
    assert SemanticIndex(index_dir=tmp_path).build(documents=[]).search("anything") == []


def test_ppr_falls_back_to_real_graph_walk_if_scipy_is_missing(monkeypatch):
    import networkx as nx
    graph = EntityGraph()
    graph.add_document("memory-a", "Project Alpha meets Team Beta")
    graph.add_document("memory-b", "Team Beta pursues Project Gamma")
    seeds = graph.query_entities("Project Alpha")
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name == "scipy" or name.startswith("scipy."):
            raise ModuleNotFoundError("No module named scipy")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked)
    def requires_scipy(*args, **kwargs):
        __import__("scipy")
    monkeypatch.setattr(nx, "pagerank", requires_scipy)
    assert ppr_rank(graph, seeds, top_n=10) == graph.walk(seeds, max_docs=10)
    assert ppr_rank(graph, seeds, top_n=10)
    ranked, method = ppr_rank(graph, seeds, top_n=10, return_method=True)
    assert ranked and method == "walk-fallback"


@pytest.mark.parametrize("fuse", [reciprocal_rank_fusion, interleave_fusion])
def test_fusion_prefers_newer_text_and_metadata_over_semantic_first(fuse):
    old = RetrievalResult("doc", "old text", source="semantic", metadata={"recorded_at": "2026-10-19T10:00:00Z"})
    new = RetrievalResult("doc", "current text", source="bm25", metadata={"recorded_at": "2026-10-19T10:10:00Z", "source_path": "/source"})
    hit = fuse([[old], [new], [], []])[0]
    assert hit.text == "current text" and hit.metadata == new.metadata
    assert hit.arm_scores.semantic == old.score and hit.arm_scores.keyword == new.score
