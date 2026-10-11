# Architecture: 4-Arm Hybrid Retrieval

## The problem

A single retrieval method is never enough for agent memory:

- **Keyword search** misses paraphrases ("cert grind" vs "certification progress").
- **Vector search** misses exact names and IDs ("AGENT-01", "doc-abc123").
- Neither finds **related** memories that share entities but not wording.
- Neither handles **"what happened last week?"** without a date filter.

## The four arms

All four run in parallel (threads) on every `recall()` call. Each returns a
ranked list of `RetrievalResult(id, text, score, source, metadata)`.

### 1. Semantic arm (`semantic_index.py`)

- Embeddings: `sentence-transformers` `all-MiniLM-L6-v2` (384 dims, CPU-friendly,
  ~90MB model downloaded once and cached).
- Storage: ChromaDB persistent client, one collection per HELIX division,
  cosine space. Lives under `~/.helix-memory/chroma/`. No server, no PostgreSQL.
- Query: embed the query, cosine-search each division collection, merge.
- Abstains gracefully when deps aren't installed (arm returns `[]`).

### 2. BM25 arm (`bm25_index.py`)

- `rank-bm25` (pure Python) over tokenized qpulse sections.
- Tokenizer: lowercase alphanumeric tokens, 2+ chars.
- Index cached as JSON (`~/.helix-memory/bm25-index.json`): tokenized corpus +
  doc IDs. Texts are re-read from source files on load so content stays fresh.
- Oversamples 3x before the division filter so filtered results stay full.

### 3. Graph arm (`entity_graph.py` + `ppr_ranking.py`)

- Zero heavy dependencies (`networkx` is pure Python). Entities extracted
  heuristically:
  - Multi-word capitalized phrases: "Project Alpha", "Team Beta"
  - ALL-CAPS tokens: "ADMIN", "AGENT-01", "MEMORY"
  - Quoted phrases: "the morning brief"
- Edges are co-occurrence links: two entities in the same document get an
  undirected edge weighted by co-occurrence count.
- Retrieval: **Personalized PageRank** (HippoRAG-style). Query substrings
  match entities (case-insensitive); those become PPR seed nodes on a
  bipartite entity+document graph. Damping 0.85. Documents ranked by
  stationary probability x **node specificity** (`1 / log(1 + degree)`),
  so rare entities count more than hub entities like "IX".
- Falls back to the 1-2 hop BFS walk if `networkx` is unavailable.
- Storage: one JSON file per division (`graph-<division>.json`).

### 4. Temporal arm (`temporal_filter.py`)

- Query time: detect temporal constraints —
  explicit dates (`2026-10-10`, `Oct 10`), relative (`yesterday`, `last week`,
  `past 30 days`, `since Monday`), month names (`in October`) via regex +
  `dateparser` fallback.
- Index time: dates parsed from qpulse filenames (`2026-10-10-1030-hourly.md`).
- Scoring: inside the window = 1.0, outside decays over 30 days, undated = 0.5.
- **Abstains** (returns `[]`) when the query has no temporal constraint.

## Fusion (`fusion.py`)

### Reciprocal Rank Fusion (default)

Ported from Hindsight's `engine/search/fusion.py`:

```python
score(d) = sum_over_arms( 1 / (k + rank(d)) )   # k = 60
```

Why RRF instead of score averaging: the four arms produce incomparable raw
scores (cosine similarity vs BM25 vs graph weight vs temporal decay). Ranks
are the common currency. A document ranked #2 in three arms
(3 x 1/62 = 0.048) beats a document ranked #1 in one arm only (1/61 = 0.016).
Consensus wins. k=60 per Cormack et al.; Hindsight uses the same value.

Before fusion, `cap_per_source()` truncates each arm (default 25) so one
over-expanding arm can't crowd out the others in the merged pool.

### Interleave fusion (dedup mode)

Round-robin: arm 0 rank 1, arm 1 rank 1, arm 2 rank 1, arm 3 rank 1, then all
rank 2s, de-duplicating. Guarantees every arm's top hit a slot.

Why: for dedup (finding the near-identical existing memory to merge into),
RRF has a failure mode — the "twin" is semantic #1 but shares little lexical
overlap, so RRF averages it down below the cutoff and the writer creates a
duplicate. Interleave never buries an arm's #1. (This insight is also from
Hindsight's `interleave_fusion`, used in their consolidation path.)

## Post-processing

Two post-fusion stages run after RRF (skipped in `dedup_mode`, where the
goal is finding the old twin even if superseded):

### Bi-temporal validity filter (`temporal_validity.py`, Graphiti-style)

Every memory carries `valid_from` / `valid_until`. When a new qpulse entry
in the same division shares 2+ entities with an older memory AND contains a
correction keyword ("corrected", "supersedes", "replaces", "updated",
"was wrong", ...), the old window closes at the new entry's date.

`recall()` takes `as_of` (date/datetime/ISO string, default = now) and
drops candidates whose window doesn't cover it. Corrections automatically
hide superseded facts; pass an old date to ask "what was true then".

Validity index cached at `~/.helix-memory/validity.json`, rebuilt by
`build_index()`.

### Multiplicative re-rank (mem0-style)

```
final_score = rrf_score x importance x recency_decay
importance    = 1 + log(1 + distinct_entity_count)
recency_decay = exp(-age_days / 180)
```

A correction from today beats a status update from last month even if the
old one ranked higher semantically. Undated docs get recency 1.0 (no
penalty). Components are stamped into result metadata
(`importance`, `recency_decay`, `final_score`, `final_rank`).
Disable with `rerank=False`.

## Data flow

```
recall("What did IX decide about crypto?")
  |
  +-- ThreadPoolExecutor (4 workers)
  |     |-- semantic: embed query -> chroma cosine -> top 25
  |     |-- bm25:     tokenize -> BM25Okapi scores -> top 25
  |     |-- graph:    query entities -> PPR seeds -> top 25
  |     +-- temporal: extract window -> date scoring -> top 25 (or abstain)
  |
  +-- cap_per_source(each arm, 25)
  +-- reciprocal_rank_fusion([semantic, bm25, graph, temporal])
  +-- validity filter (as_of=now; skipped in dedup_mode)
  +-- multiplicative re-rank (skipped in dedup_mode / rerank=False)
  +-- return top_k MergedCandidate(id, text, rrf_score, rrf_rank,
                                  source_ranks, arm_scores, metadata)
```

## What we deliberately skip (vs Hindsight)

| Hindsight component | Why we skip it |
|---|---|
| PostgreSQL + pgvector + pg_trgm + pg_search | ChromaDB + rank-bm25 + JSON files are free and local |
| LLM per write (fact extraction) | HELIX qpulse entries are already structured; extraction is the cost center we don't need |
| Cross-encoder reranking | v1 ships without it; add if recall quality proves insufficient |
| 1,659-line entity resolver | Our data doesn't have ambiguous-entity problems |
| Consolidation/dedup engine | `dedup_mode` covers the 80% case with interleave fusion |
| MCP server, multi-provider LLM abstraction | Not needed for a retrieval library |

## Index lifecycle

- First `recall()` or `build_index()` builds and caches everything under
  `~/.helix-memory/`.
- BM25 cache stores tokenized corpus; texts re-read from source on load.
- ChromaDB persists vectors; rebuild clears stale entries per collection.
- Graphs rebuild from scratch (fast: heuristic extraction, no models).
- Rebuild when recall feels stale (new qpulse files land hourly).
