# Changelog

All notable changes to the Helix Memory Corridor public release.

## [0.3.0] — retention repair (review branch)

Authored by [R2] SRVP2-CODEX. The previous public changelog said 1.0.0 while
package metadata actually reported 0.2.0; package metadata is now consistent.

- Repair category/division aliases across collection, indexes, recall and CLI.
- Validate source content/scopes on recall; include root memories and full text.
- Replace heuristic fact suppression with explicit timestamped supersession.
- Add durable idempotent events, handoff resume/CAS and verified readback.
- Add immutable non-deleting mirror generations, pending receipts and restore.
- Provide installed hmc CLI and optional reported PPR fallback.
- Add synthetic regression/integration tests; semantic backend tests use fakes.
- Preserve existing source memory and prior versions. Hosted hooks and live
  Drive deployment remain separate integration steps.

## [1.0.0] — 2026-10-11

Initial public release. Sanitized from the private BrainFrame HELIX
memory system for general use.

### Added
- 4-arm hybrid recall: semantic (vector) + BM25 (keyword) + graph
  (Personalized PageRank) + temporal (date-window filter)
- Reciprocal Rank Fusion across all four arms
- Bi-temporal validity: corrections automatically close old facts
- Multiplicative re-rank: `rrf x importance x recency_decay`
- Dedup mode for finding near-duplicate entries
- `as_of` parameter for point-in-time queries
- Generic memory collector: works with any markdown directory,
  auto-detects categories
- `helix-memory-corridor-master-skill.md`: all-in-one guide covering
  installation, usage, Google Drive mirroring, and Living Handoff
- `docs/DRIVE-MIRROR.md`: rclone-based Drive one-to-one mirror setup

### Changed
- Collector rewritten from HELIX-specific
  (`~/memory/helix/<division>/qpulse/`) to generic (any `memory_dir`
  with auto-detected categories)
- Index cache moved from `~/.helix-hybrid/` to `~/.helix-memory/`
- All example entities replaced with neutral placeholders
- README rewritten for general audience

### Attribution
- 4-arm RRF architecture adapted from
  [Hindsight](https://github.com/vectorize-io/hindsight) (Vectorize)
- PPR retrieval adapts [HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG)
  (OSU NLP Group)
- Bi-temporal validity adapts [Graphiti](https://github.com/getzep/graphiti)
  (Zep)
- Re-rank pattern follows [mem0](https://github.com/mem0ai/mem0)
