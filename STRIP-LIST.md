# Strip List — Helix Memory Corridor Public Release

Audit trail of every private value removed or replaced during sanitization.
Original: `~/workspace/helix-memory-enhanced` (private, untouched)
Sanitized copy: `~/workspace/helix-memory-public`

## Personal identifiers

| Original | Replacement | Location |
|----------|-------------|----------|
| "Chaz" | "Alex" | `helix_memory/ppr_ranking.py` (docstring example) |
| "MusiCares" | "Project Alpha" / "project" | `helix_memory/__init__.py`, `entity_graph.py`, `ppr_ranking.py`, `scripts/test_*.py`, `docs/ARCHITECTURE.md` |
| "EDD appeal" | "Q3 planning" / "budget review" | `helix_memory/ppr_ranking.py` |
| "Candress Suber" | "Jane Smith" | `helix_memory/entity_graph.py`, `scripts/test_enhancements.py` |
| "Rose" | "Sam" | `scripts/test_enhancements.py` |
| "Tesla" (example) | "budget" | `helix_memory/ppr_ranking.py` |

## Project / infrastructure identifiers

| Original | Replacement | Location |
|----------|-------------|----------|
| "BrainFrame" | "Team Beta" (examples) / removed | `helix_memory/entity_graph.py`, `docs/ARCHITECTURE.md` |
| "HELIX memory spine" | "AI agent memory" | `helix_memory/__init__.py`, `pyproject.toml` |
| "HELIX" (all-caps token example) | "MEMORY" | `helix_memory/entity_graph.py`, `docs/ARCHITECTURE.md` |
| "IX" | "ADMIN" / "admin" | `helix_memory/entity_graph.py`, `helix_memory/ppr_ranking.py`, `docs/ARCHITECTURE.md`, `scripts/test_recall.py` |
| "MUSE-03" | "AGENT-01" | `helix_memory/entity_graph.py`, `docs/ARCHITECTURE.md`, `scripts/test_recall.py` |
| "SRVP2-CODEX" | "System Gamma" | `helix_memory/entity_graph.py` |
| "Muse" (hub entity example) | "system" | `helix_memory/ppr_ranking.py` |
| Drive file ID `1wzLeV_TxTPy96zHyN1D3zne5JoWMnvYN` | `doc-abc123` | `docs/ARCHITECTURE.md` |

## Paths and structure

| Original | Replacement | Location |
|----------|-------------|----------|
| `~/memory/helix/` | `~/memory/` (configurable via `memory_dir`) | `helix_memory/__init__.py`, `README.md` |
| `~/memory/helix/<division>/qpulse/` | Any `<category>/*.md` | `helix_memory/collector.py` (rewritten) |
| `~/.helix-hybrid/` | `~/.helix-memory/` | `docs/ARCHITECTURE.md`, `README.md` |
| Hardcoded `DIVISIONS = ["00-system", ...]` | Auto-detected from `memory_dir` | `helix_memory/collector.py` (rewritten) |
| `division=` parameter | `category=` parameter | `scripts/test_*.py`, `README.md` |

## Repository references

| Original | Replacement | Location |
|----------|-------------|----------|
| `helix-memory-enhanced` (repo name) | `helix-memory-corridor` | `pyproject.toml`, `README.md` |

## Removed entirely

- `build/` directory (build artifacts with local paths)
- `helix_memory.egg-info/` (generated metadata)
- `.git/` history (fresh single-commit history for public repo)

## Verified clean

- No API keys, tokens, passwords, or credentials found in original scan
- No Tailscale IPs, hostnames, or internal DNS
- No email addresses (gmail, icloud, duck.com)
- No financial figures tied to real accounts
- Final grep for `chaz|peterson|ninjaloc|duck.com|icloud.com|brainframe|musi[c]?ares|edd appeal|SRVP|MUSE-0|Candress` returns only GitHub URLs (necessary, public-facing)

## Structural changes (not just find/replace)

1. **`helix_memory/collector.py` rewritten**: was hardcoded to walk
   `~/memory/helix/<division>/qpulse/`. Now accepts any `memory_dir`,
   auto-detects category subdirectories, walks recursively. The HELIX
   7-division layout is no longer required.

2. **`helix_memory/recall.py` updated**: removed `DIVISIONS` import
   dependency. Uses `_detect_divisions(memory_dir)` for auto-detection.

3. **`README.md` rewritten**: was written for HELIX spine users.
   Now generic — any markdown memory directory.

4. **`helix-memory-corridor-master-skill.md` created**: all-in-one guide
   covering install, usage, Drive mirror setup, and Living Handoff pattern.

5. **`docs/DRIVE-MIRROR.md` created**: standalone Google Drive
   one-to-one mirror setup guide (rclone).
