# Retention repair validation

Authored by [R2] SRVP2-CODEX, 2026-10-19.

Baseline: public commit `d5df14f30d429535f9bf1d47921c35c8481d93a4`.
Original source remains in Git history. No personal memory, production handoff,
remote repository, or provider account was changed during these checks.

- **165 regression/integration tests passed** on Python 3.12 using synthetic
  records and temporary directories. Includes warm-cache append/edit, exact
  task recovery, intraday/known-at correction order, pre-ranking eligibility,
  cross-corpus scope, preserved original events, idempotency across processes,
  record integrity, CRLF, handoff CAS/history, mirror readback and safe restore.
- **12 installed-wheel checks passed** outside the checkout in a fresh virtual
  environment with no SciPy, Chroma, Torch, or sentence-transformers. Installed
  `hmc` capture, index, recall, latest, resume, mirror and restore work; missing
  SciPy produces a reported graph-walk fallback. Corrupted event recovery fails
  explicitly instead of returning modified evidence.
- The existing enhancement demonstration now uses explicit supersession and
  isolated integration data: **30 checks passed** with `--integration`.
- Optional semantic tests use in-memory fakes. Optional rclone remote tests use
  a fake transport, including corrupted/missing readback and stale retries.
  These do not establish live Google Drive or actual embedding performance.
- Python 3.10 grammar was checked; execution was tested on Python 3.12.

Deployment acceptance remains separate: connect the real host's capture and
recovery boundaries, inventory every memory/state path, verify a live complete
Drive generation and restore, and test real session/compaction recovery.
No provider hooks, scheduler, OAuth, fleet coordinator, or MemPalace export
integration is installed merely by installing this package.
