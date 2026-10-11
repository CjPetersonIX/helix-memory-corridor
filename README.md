# Helix Memory Corridor

4-arm hybrid memory recall for AI agents, enhanced with bi-temporal validity,
Personalized PageRank, and multiplicative re-ranking.
File-based, free, no PostgreSQL, no LLM calls, no API keys.

> **Start here:** [`helix-memory-corridor-master-skill.md`](helix-memory-corridor-master-skill.md)
> is the all-in-one guide. It covers installation, usage, Google Drive
> memory mirroring, and the Living Handoff pattern. Everything below is
> a summary.

## Why this exists

AI agents forget. Context windows compact, sessions end, and yesterday's
decisions vanish. If you store durable memory as structured markdown files
(decision logs, journal entries, project notes), plain keyword search misses
too much: paraphrases, related concepts, time-bounded questions, and,
critically, *which version of a corrected fact is current*.

The Helix Memory Corridor adds hybrid retrieval on top of your memory files:

1. **Semantic** — vector search finds paraphrases and conceptual matches
2. **BM25** — keyword search finds exact terms and names
3. **Graph** — Personalized PageRank over the entity graph, seeded from query
   entities and weighted by node specificity. Finds multi-hop connections
   through shared people, projects, and systems
4. **Temporal** — date-window filter boosts documents inside a detected time
   range ("last week", "in October", "since Monday")

Plus:
- **Bi-temporal validity** (Graphiti-style): corrections close the old fact's
  validity window. `recall()` defaults to "what's true now".
- **Multiplicative re-rank** (mem0-style):
  `final = rrf x importance x recency_decay`.

No single arm is sufficient. Semantic misses exact keywords. BM25 misses
paraphrases. The graph finds connections neither sees. Temporal cuts noise.
Reciprocal Rank Fusion combines them:

```
score(d) = sum_over_arms( 1 / (60 + rank(d)) )
```

Documents appearing in multiple arms outrank documents that are #1 in only
one arm. See `docs/ARCHITECTURE.md` for the full design.

## Install

```bash
pip install git+https://github.com/CjPetersonIX/helix-memory-corridor.git
```

For the semantic (vector) arm, also install the heavy optional deps
(~2GB, CPU-only torch is fine):

```bash
pip install "helix-memory-corridor[semantic] @ git+https://github.com/CjPetersonIX/helix-memory-corridor.git"
```

Without the semantic deps, `recall()` still works with BM25 + graph +
temporal arms. The semantic arm degrades gracefully.

## Quickstart

```python
from helix_memory import recall, build_index

# First run: index your memory directory (takes a minute or two)
# Point it at any folder of markdown files
build_index(memory_dir="~/memory")

# Ask about prior work
results = recall("What did the team decide about the project timeline?")
for r in results:
    print(f"#{r.rrf_rank} score={r.rrf_score:.4f} {r.id}")
    print("   arms:", r.source_ranks)   # which arms found it, at which rank
    print("   " + r.text[:200])
```

More options:

```python
# Restrict to one category
recall("Project approval status", category="work")

# Skip the embedding model (faster, no torch)
recall("Budget review", include_semantic=False)

# Dedup mode: find near-duplicates (uses interleave fusion so each arm's
# top hit is guaranteed a slot — ideal for consolidation). Validity filter
# and re-rank are skipped: the goal is finding the old twin, even superseded.
recall("new memory text...", dedup_mode=True)

# Bi-temporal query: what was true on Oct 1?
recall("Project budget", as_of="2026-10-01")

# Skip the multiplicative re-rank (pure RRF order)
recall("Project status", rerank=False)

# Rebuild after many new files land
build_index()
```

## CLI

```bash
# Build/rebuild all indexes
python scripts/build_index.py [--no-semantic] [--categories work,personal]

# Smoke-test recall on real data
python scripts/test_recall.py [--no-semantic]

# Unit + integration tests for validity, PPR, re-rank
python scripts/test_enhancements.py [--integration]
```

## How it works

| Module | Job |
|---|---|
| `collector.py` | Walks your memory directory, splits files on H2 sections |
| `bm25_index.py` | Keyword index via `rank-bm25`, cached as JSON |
| `semantic_index.py` | `all-MiniLM-L6-v2` embeddings + ChromaDB, one collection per category |
| `entity_graph.py` | File-based co-occurrence graph, heuristic entity extraction, no deps |
| `ppr_ranking.py` | Personalized PageRank + node specificity (HippoRAG-style) |
| `temporal_filter.py` | Date extraction from queries, 30-day decay scoring |
| `temporal_validity.py` | Bi-temporal validity windows (Graphiti-style) |
| `fusion.py` | RRF + interleave fusion + per-arm caps (ported from Hindsight) |
| `recall.py` | Runs 4 arms in parallel threads, validity filter, re-rank, top-k |

Indexes cache under `~/.helix-memory/` (rebuilt on demand, never committed).

Design notes:

- RRF constant k=60 (Cormack et al., via Hindsight). Don't tune it.
- Per-arm cap (default 25) before fusion stops one arm crowding out the rest.
- Temporal arm abstains when the query has no date constraint.
- We deliberately skip: PostgreSQL, LLM-per-write, cross-encoder reranking,
  and heavyweight entity resolution. Structured markdown is already clean;
  the retrieval was the gap, not the write path.

## Memory file format

The collector expects markdown files organized however you like. It splits
on H2 (`##`) headers. Each section becomes a retrievable document with:

- The section text
- Its source file path and header
- Any dates found in the text (for temporal filtering)
- Entities extracted from capitalized phrases (for graph building)

Corrections work by writing a new section that references the old one.
The bi-temporal validity tracker closes the old fact's window automatically
when it detects correction language ("corrected:", "update:", "superseded").

## Attribution

The 4-arm retrieval architecture and RRF fusion are adapted from
[Hindsight](https://github.com/vectorize-io/hindsight) by Vectorize.
Personalized PageRank retrieval adapts [HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG)
(OSU NLP Group). Bi-temporal validity edges adapt
[Graphiti](https://github.com/getzep/graphiti) (Zep). The multiplicative
re-rank pattern follows [mem0](https://github.com/mem0ai/mem0).
See `docs/HINDSIGHT_CREDIT.md`.

## License

MIT. See `LICENSE`.
