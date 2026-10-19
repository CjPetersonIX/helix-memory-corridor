"""
Helix Memory Corridor: 4-arm hybrid memory recall for AI agent memory.

Searches the explicitly selected memory corpus using retrieval arms fused with
Reciprocal Rank Fusion:

  1. semantic — sentence-transformers + ChromaDB vector search
  2. bm25     — rank-bm25 keyword search
  3. graph    — Personalized PageRank over the entity co-occurrence graph
                (HippoRAG-style; falls back to 1-2 hop walk)
  4. temporal — date-window filter/boost

Plus:
  - Bi-temporal validity windows (Graphiti-style): corrections close the
    old fact's window; recall() defaults to as_of=now.
  - Multiplicative re-rank (mem0-style):
    final = rrf x (1 + log(1 + entity_count)) x exp(-age_days / 180).

Quickstart:
    from helix_memory import recall, build_index

    build_index(memory_dir="~/memory")  # optional explicit derived-index build
    results = recall("What did the team decide about the project?")
    for r in results:
        print(r.rrf_rank, f"{r.rrf_score:.4f}", r.id)

    # Bi-temporal query: what was true on Oct 1?
    results = recall("project deadline", as_of="2026-10-01")

The retrieval architecture (4-arm + RRF fusion) is adapted from the
Hindsight project (github.com/vectorize-io/hindsight). PPR ranking adapts
HippoRAG (OSU-NLP-Group/HippoRAG). Bi-temporal edges adapt Graphiti
(getzep/graphiti). See docs/HINDSIGHT_CREDIT.md for full attribution.
"""

from .fusion import (
    ArmScores,
    MergedCandidate,
    RetrievalResult,
    cap_per_source,
    interleave_fusion,
    reciprocal_rank_fusion,
)
from .ppr_ranking import ppr_rank
from .recall import recall, rebuild_all
from .recall import latest_events
from .memory_store import MemoryStore
from .temporal_validity import (
    build_validity_index,
    filter_valid,
    is_valid,
    load_validity,
    save_validity,
)

# Friendly alias: build_index() is the primary name for (re)building indexes.
build_index = rebuild_all

__version__ = "0.3.0"

__all__ = [
    "recall",
    "build_index",
    "rebuild_all",
    "latest_events",
    "MemoryStore",
    "reciprocal_rank_fusion",
    "interleave_fusion",
    "cap_per_source",
    "ppr_rank",
    "build_validity_index",
    "filter_valid",
    "is_valid",
    "load_validity",
    "save_validity",
    "RetrievalResult",
    "MergedCandidate",
    "ArmScores",
    "__version__",
]
