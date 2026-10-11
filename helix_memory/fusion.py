"""
RRF fusion and related helpers, ported from Hindsight's engine/search/fusion.py.

Implements hybrid search fusion (semantic + BM25 + graph + temporal) without
any external dependencies.
"""

from dataclasses import dataclass, field


@dataclass
class RetrievalResult:
    """One hit from a single retrieval arm."""

    id: str  # document id (file path)
    text: str  # document text (or excerpt)
    score: float = 0.0  # raw arm score (similarity, bm25, etc.)
    source: str = ""  # arm name: semantic | bm25 | graph | temporal
    metadata: dict = field(default_factory=dict)


@dataclass
class ArmScores:
    """Raw per-arm scores for one document."""

    semantic: float | None = None
    keyword: float | None = None
    graph: float | None = None
    temporal: float | None = None


@dataclass
class MergedCandidate:
    """A document after fusion, with RRF score and per-arm provenance."""

    id: str
    text: str
    rrf_score: float
    rrf_rank: int
    source_ranks: dict  # e.g. {"semantic_rank": 2, "bm25_rank": 5}
    arm_scores: ArmScores
    metadata: dict = field(default_factory=dict)


def cap_per_source(results: list[RetrievalResult], cap: int) -> list[RetrievalResult]:
    """Truncate a single retrieval arm to its top-``cap`` results.

    Applied per arm (semantic, BM25, graph, temporal) before fusion so that
    one over-expanding arm cannot crowd out the others when the merged pool
    is later trimmed to the final top_k budget. The caller is responsible for
    sorting ``results`` by relevance first; this only slices.
    """
    if cap <= 0 or len(results) <= cap:
        return results
    return results[:cap]


def reciprocal_rank_fusion(
    result_lists: list[list[RetrievalResult]], k: int = 60
) -> list[MergedCandidate]:
    """
    Merge multiple ranked result lists using Reciprocal Rank Fusion.

    RRF formula: score(d) = sum_over_lists(1 / (k + rank(d)))

    Args:
        result_lists: List of result lists, one per arm, in arm-priority order
            (semantic, bm25, graph, temporal). Each list sorted best-first.
        k: Constant for RRF formula (default: 60, per Hindsight/Cormack et al.)

    Returns:
        Merged list of MergedCandidate objects, sorted by RRF score descending.
    """
    rrf_scores: dict[str, float] = {}
    source_ranks: dict[str, dict[str, int]] = {}
    first_seen: dict[str, RetrievalResult] = {}
    arm_scores: dict[str, ArmScores] = {}

    source_names = ["semantic", "bm25", "graph", "temporal"]

    for source_idx, results in enumerate(result_lists):
        source_name = (
            source_names[source_idx]
            if source_idx < len(source_names)
            else f"source_{source_idx}"
        )
        for rank, retrieval in enumerate(results, start=1):
            doc_id = retrieval.id
            if doc_id not in first_seen:
                first_seen[doc_id] = retrieval
            if doc_id not in rrf_scores:
                rrf_scores[doc_id] = 0.0
                source_ranks[doc_id] = {}
                arm_scores[doc_id] = ArmScores()

            rrf_scores[doc_id] += 1.0 / (k + rank)
            source_ranks[doc_id][f"{source_name}_rank"] = rank

            arm = arm_scores[doc_id]
            if source_name == "semantic":
                arm.semantic = retrieval.score
            elif source_name == "bm25":
                arm.keyword = retrieval.score
            elif source_name == "graph":
                arm.graph = retrieval.score
            elif source_name == "temporal":
                arm.temporal = retrieval.score

    merged: list[MergedCandidate] = []
    for rrf_rank, (doc_id, rrf_score) in enumerate(
        sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True), start=1
    ):
        first = first_seen[doc_id]
        merged.append(
            MergedCandidate(
                id=doc_id,
                text=first.text,
                rrf_score=rrf_score,
                rrf_rank=rrf_rank,
                source_ranks=source_ranks[doc_id],
                arm_scores=arm_scores[doc_id],
                metadata=first.metadata,
            )
        )
    return merged


def interleave_fusion(
    result_lists: list[list[RetrievalResult]],
) -> list[MergedCandidate]:
    """Round-robin (interleaved) fusion — an alternative to RRF for dedup-style recall.

    RRF scores a doc by the *sum* of its reciprocal ranks across arms, so a result
    that is #1 in one arm but absent/low in the others gets averaged down. For
    dedup (finding the near-identical existing memory to merge into), that is
    the failure mode: the twin is semantic rank #1, yet shares little lexical
    overlap, so RRF drops it below the cutoff and the writer creates a duplicate.

    Interleave instead *guarantees every arm's top hits a slot*: take each arm's
    #1, then each arm's #2, ... in arm-priority order, de-duplicating, until all
    results are placed. ``rrf_score`` is assigned strictly decreasing by final
    interleave position so downstream order-by-score sorts preserve the order.
    """
    source_names = ["semantic", "bm25", "graph", "temporal"]
    seen: dict[str, MergedCandidate] = {}
    source_ranks: dict[str, dict[str, int]] = {}
    first_seen: dict[str, RetrievalResult] = {}
    arm_scores: dict[str, ArmScores] = {}

    for source_idx, results in enumerate(result_lists):
        source_name = (
            source_names[source_idx]
            if source_idx < len(source_names)
            else f"source_{source_idx}"
        )
        for rank, retrieval in enumerate(results, start=1):
            doc_id = retrieval.id
            first_seen.setdefault(doc_id, retrieval)
            source_ranks.setdefault(doc_id, {})[f"{source_name}_rank"] = rank
            arm = arm_scores.setdefault(doc_id, ArmScores())
            if source_name == "semantic":
                arm.semantic = retrieval.score
            elif source_name == "bm25":
                arm.keyword = retrieval.score
            elif source_name == "graph":
                arm.graph = retrieval.score
            elif source_name == "temporal":
                arm.temporal = retrieval.score

    # Round-robin: arm 0 rank 1, arm 1 rank 1, ..., arm 0 rank 2, ...
    max_len = max((len(r) for r in result_lists), default=0)
    position = 0
    for rank in range(max_len):
        for results in result_lists:
            if rank < len(results):
                doc_id = results[rank].id
                if doc_id not in seen:
                    position += 1
                    first = first_seen[doc_id]
                    seen[doc_id] = MergedCandidate(
                        id=doc_id,
                        text=first.text,
                        rrf_score=1.0 / position,  # strictly decreasing
                        rrf_rank=position,
                        source_ranks=source_ranks[doc_id],
                        arm_scores=arm_scores[doc_id],
                        metadata=first.metadata,
                    )

    return sorted(seen.values(), key=lambda c: c.rrf_rank)
