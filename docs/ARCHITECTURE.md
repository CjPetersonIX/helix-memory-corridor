# Architecture and boundaries

Authored by [R2] SRVP2-CODEX. Source events are evidence; indexes and rendered
handoffs are projections. Memory content does not grant execution authority.

1. `MemoryStore.remember` exclusively creates an immutable Markdown event,
   fsyncs it, reads it back, and updates an idempotency lookup under a local lock.
   Interrupted lookup updates can recover from the event, without duplication.
2. `collector` reads root Markdown and recursively scoped categories. Explicit
   event IDs and stable section anchors prevent ordinal-ID drift. Records carry
   source paths, full text, precise timestamps and explicit correction targets.
3. `recall` rereads the corpus every call. Its content-and-scope generation keys
   bounded caches, so warm indexes cannot hide an appended fact. Keyword,
   graph, time and opt-in semantic scores are fused with RRF. Candidate text is
   always hydrated from this call's source snapshot. Large returned passages
   should be budgeted by the integrating host, without dropping the matched fact.
4. `temporal_validity` resolves only explicit supersedes IDs. Effective time and
   recorded time are separate; `known_at` prevents future-recorded corrections
   leaking into a historical view. Naive legacy times use UTC. New captures
   require an explicit offset. Undated evidence remains undated.
5. `latest_events` bypasses optional indexes and returns newest exact-task events
   for recovery. `MemoryStore.read_resume` restores the marked JSON block in
   the single existing handoff. Neither function executes its text.
6. `mirror` captures a complete immutable generation, hashes it, verifies copied
   bytes, and advances a current pointer after verification. Local and remote
   receipts are separate. Prior generations are preserved.

## Retrieval details

- BM25 handles empty/tiny corpora and supplements zero/negative scores for exact
  token matches. Cache manifests include full source content and scope.
- Graph relations are text co-occurrence, not evidence of agent activity or
  actual memory traversal. PPR uses NetworkX+SciPy when available and reports
  graph-walk fallback otherwise.
- Time windows score dated candidates; exact validity and recency use full
  timestamps. A time-only query is not a semantic proof of relevance.
- Semantic models are opt-in. Generation-specific Chroma collections preserve
  old derived state; the source snapshot, not a cached excerpt, supplies text.
  No MemPalace bridge is included.
- RRF uses k=60. Importance and 180-day recency reranking are retained. Immediate
  recovery uses latest events rather than relying on a small relevance boost.
- Missing roots/scopes and failed arms have explicit health diagnostics. An
  empty result cannot establish that no memory exists.

## Concurrency, performance, and limits

Content validation favors correctness over mtime-only shortcuts. Large corpora
may need a journal-driven incremental collector later; measure before adding
complexity. Cache scope is per resolved root/category/options/generation.

Local locks/CAS coordinate cooperating Linux/macOS writers. They do not fence
unrelated handoff renderers, replicate fleet state, or establish a global lock.
A hosted conversation must call capture and recovery explicitly. Installing a
skill or this package does not inject it into a provider's context window.

Markdown is indexed. HTML/JSON handoff state uses explicit resume/state APIs.
Category-scoped queries only see corrections in that scope; keep corrections
with their subject, or query the whole corpus. Legacy heuristic validity caches
are not reused as current truth. Originals remain in place.

No conical renderer, geometric storage layout, traversal-strengthened veins,
automatic authorization ledger, or autonomous scheduler is implied by these
retrieval primitives. Those require separately reviewed integration.
