# Attribution: Hindsight

The retrieval architecture in `helix-memory` — four parallel retrieval arms
(semantic, BM25, graph, temporal) fused with Reciprocal Rank Fusion — is
adapted from the [Hindsight](https://github.com/vectorize-io/hindsight)
project by [Vectorize](https://vectorize.io).

## What we took

1. **The 4-arm retrieval concept.** Hindsight's `engine/search/retrieval.py`
   runs semantic, BM25, graph, and temporal retrieval in parallel. We kept the
   same four arms, reimplemented against file-based storage instead of
   PostgreSQL.

2. **Reciprocal Rank Fusion.** Our `helix_memory/fusion.py` is a direct port
   of Hindsight's `engine/search/fusion.py` (`reciprocal_rank_fusion`,
   `interleave_fusion`, `cap_per_source`), including the RRF constant k=60
   and the documented rationale for interleave fusion in dedup scenarios.

3. **Design insights** from reading their codebase:
   - Per-arm caps before fusion (no single arm crowds out the rest)
   - Temporal arm abstains when the query has no date constraint
   - Interleave (not RRF) for dedup, so each arm's #1 is never buried
   - The selectivity principle in fact extraction ("would this be useful
     to recall in 6 months?")

## What we did NOT take

Hindsight is a full memory platform (118K+ lines): PostgreSQL with pgvector,
LLM-per-write fact extraction, a 3,629-line consolidation engine, a 1,659-line
entity resolver, cross-encoder reranking, an MCP server, and multi-provider
LLM abstraction. We took the retrieval architecture only. Our write path
remains HELIX's structured qpulse format — no LLM calls, no database server,
fully local and free.

## Hindsight license

Hindsight is open source. See their repository for the current license text:
https://github.com/vectorize-io/hindsight

Our `fusion.py` port retains Hindsight's algorithmic structure with original
expression adapted to this codebase. If their license requires specific
attribution or copyleft compliance for derivative works, this file serves as
the attribution record; verify the upstream license before redistributing.

## Thank you

To the Vectorize team: the 4-arm + RRF design is the right abstraction, and
publishing the code made this adaptation possible.
