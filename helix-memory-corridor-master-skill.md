# Helix Memory Corridor — Master Skill

> 4-arm hybrid memory recall for AI agents. File-based, free, no
> PostgreSQL, no LLM calls, no API keys. Your agent remembers what
> matters, finds it fast, and never shows you a stale fact.

---

## What this is

AI agents forget. Context windows compact, sessions end, and yesterday's
decisions vanish. The Helix Memory Corridor gives your agent a durable
memory system with smart recall:

1. **Semantic** — vector search finds paraphrases and conceptual matches
2. **BM25** — keyword search finds exact terms and names
3. **Graph** — Personalized PageRank over the entity graph. Finds
   multi-hop connections through shared people, projects, and systems
4. **Temporal** — date-window filter boosts documents inside a detected
   time range ("last week", "in October", "since Monday")

Plus:
- **Bi-temporal validity**: corrections close the old fact's validity
  window. `recall()` defaults to "what's true now".
- **Multiplicative re-rank**: `final = rrf x importance x recency_decay`.

Reciprocal Rank Fusion combines all four arms:
```
score(d) = sum_over_arms( 1 / (60 + rank(d)) )
```

---

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

### Requirements

- Python 3.10+
- `rank-bm25` (auto-installed)
- `networkx` optional (for full PPR; falls back to simple graph walk)
- `sentence-transformers`, `torch`, `chromadb` (only for semantic arm)

---

## Quickstart

```python
from helix_memory import recall, build_index

# First run: index your memory directory (takes a minute or two)
build_index(memory_dir="~/memory")

# Ask about prior work
results = recall("What did the team decide about the project timeline?")
for r in results:
    print(f"#{r.rrf_rank} score={r.rrf_score:.4f} {r.id}")
    print("   arms:", r.source_ranks)
    print("   " + r.text[:200])
```

### Common options

```python
# Restrict to one category
recall("Project approval status", category="work")

# Skip the embedding model (faster, no torch)
recall("Budget review", include_semantic=False)

# Dedup mode: find near-duplicates for consolidation
recall("new memory text...", dedup_mode=True)

# Bi-temporal query: what was true on Oct 1?
recall("Project budget", as_of="2026-10-01")

# Skip the multiplicative re-rank (pure RRF order)
recall("Project status", rerank=False)

# Rebuild after many new files land
build_index(memory_dir="~/memory")
```

### CLI

```bash
# Build/rebuild all indexes
python scripts/build_index.py --memory-dir ~/memory [--no-semantic]

# Smoke-test recall
python scripts/test_recall.py [--no-semantic]

# Unit + integration tests
python scripts/test_enhancements.py [--integration]
```

---

## Memory file format

The collector walks your memory directory and treats each subdirectory
as a category. It splits markdown files on H2 (`##`) headers. Each
section becomes a retrievable document with its text, source path,
detected dates, and extracted entities.

Example layout:
```
~/memory/
    work/
        project-notes.md
        meeting-log.md
    personal/
        journal.md
        goals.md
```

### Writing corrections

When a fact changes, write a new section that references the old one.
The bi-temporal validity tracker closes the old fact's window
automatically when it detects correction language:

- "Corrected:"
- "Update:"
- "Superseded:"
- "Previous value was X, now Y"

`recall()` returns only currently-valid facts by default. Use
`as_of="YYYY-MM-DD"` to query what was true at a past date.

---

## Google Drive as your memory mirror

Your local folder stays the working copy. Drive holds an identical
mirror. If your computer dies, your memories survive.

```
Local:  ~/memory/
            ├── work/
            ├── personal/
            └── journal.md

Drive:  My Drive/Memory/
            ├── work/          (identical copy)
            ├── personal/      (identical copy)
            └── journal.md     (identical copy)
```

### Setup

**1. Create the Drive folder**

1. Go to [drive.google.com](https://drive.google.com)
2. Click **New** → **New folder**, name it `Memory`
3. Note the folder ID from the URL:
   `https://drive.google.com/drive/folders/1ABC123xyz...`

**2. Install rclone** (free, open source)

```bash
# Mac
brew install rclone

# Linux
sudo apt install rclone
```

**3. Connect rclone to Google Drive**

```bash
rclone config
```

Follow the prompts: `n` for new remote, name it `gdrive`, choose
`drive`, leave client ID/secret blank, choose full access, type `y`
for auto config, sign in via browser, confirm.

Test it: `rclone lsd gdrive:`

**4. First sync**

```bash
rclone sync ~/memory gdrive:Memory --drive-root-folder-id YOUR_FOLDER_ID -v
```

### Daily use

**Push changes:**
```bash
rclone sync ~/memory gdrive:Memory --drive-root-folder-id YOUR_FOLDER_ID
```

**Pull/restore:**
```bash
rclone sync gdrive:Memory ~/memory --drive-root-folder-id YOUR_FOLDER_ID
```

**Automate** (hourly via cron):
```bash
crontab -e
# Add:
0 * * * * /usr/bin/rclone sync ~/memory gdrive:Memory --drive-root-folder-id YOUR_FOLDER_ID --log-file ~/.rclone-memory.log
```

### Tips

- **Exclude the index cache.** Don't sync `~/.helix-memory/`. It
  rebuilds automatically. Only sync your source markdown files.
- **Version history is free backup.** Drive keeps every version.
  Right-click any file → **Manage versions** to restore.
- **Use `copy` instead of `sync`** if you never want Drive deletions
  to happen automatically.

---

## The Living Handoff

The Living Handoff is a single HTML file that shows the current state
of everything: what's active, what's done, what's blocked, what's next.
Your agent updates it every time something changes. You open it to see
where things stand without asking.

### The one-file rule

There is **ONE** living handoff file. It lives at:
```
~/memory/living-handoff.html
```
(or wherever you keep it — but only one copy, always updated in place).
Never create duplicates, dated copies, or "v2" files. The file is the
truth. Update it, don't replace it.

### Structure

```html
<!DOCTYPE html>
<html>
<head>
  <title>Living Handoff</title>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="60">
  <style>
    body { font-family: system-ui; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
    .section { margin: 1.5rem 0; }
    .done { color: #666; }
    .blocked { color: #c00; }
    .active { color: #060; }
    .timestamp { font-size: 0.8em; color: #999; }
  </style>
</head>
<body>
  <h1>Living Handoff</h1>
  <p class="timestamp">Last updated: 2026-10-11 10:30 PDT</p>

  <div class="section">
    <h2>Active</h2>
    <ul>
      <li class="active">Project Alpha launch — waiting on design review</li>
      <li class="active">Budget proposal — draft in progress</li>
    </ul>
  </div>

  <div class="section">
    <h2>Blocked</h2>
    <ul>
      <li class="blocked">Vendor contract — need legal sign-off</li>
    </ul>
  </div>

  <div class="section">
    <h2>Done</h2>
    <ul>
      <li class="done">Q3 planning session — completed Oct 10</li>
    </ul>
  </div>
</body>
</html>
```

### How your agent maintains it

1. **After every significant action**, the agent updates the handoff:
   - Move completed items to Done
   - Add new items to Active
   - Flag blockers with what's needed to unblock

2. **Active sections show only active work.** Completed items move
   to the Done log at the bottom. No progress bars on done items.

3. **Always include a timestamp.** You should be able to glance at
   the file and know how fresh it is.

4. **The agent reads it before starting work.** It's the first thing
   checked at the beginning of a session, so nothing falls through
   the cracks.

### Connecting it to memory

Each handoff update should also write a brief memory entry. The
handoff shows *current state*; the memory files hold *history*.
Together they give you both "where are we now" and "how did we
get here."

```markdown
## Handoff Update — 2026-10-11 10:30

- Moved "Q3 planning" to Done (completed, notes in work/planning.md)
- Added "Vendor contract" to Blocked (waiting on legal)
- Active: Project Alpha launch, Budget proposal
```

### Sharing the handoff

Since it's a single HTML file, sharing is easy:

- **Local:** Open `~/memory/living-handoff.html` in any browser
- **Drive:** Sync it with the rest of your memory folder (see Drive
  Mirror section above), then open from Drive
- **Phone:** If Drive sync is on, open the HTML file from the Drive
  mobile app

---

## Architecture notes

| Module | Job |
|---|---|
| `collector.py` | Walks memory dir, splits files on H2 sections |
| `bm25_index.py` | Keyword index via `rank-bm25`, cached as JSON |
| `semantic_index.py` | `all-MiniLM-L6-v2` embeddings + ChromaDB |
| `entity_graph.py` | File-based co-occurrence graph, no deps |
| `ppr_ranking.py` | Personalized PageRank + node specificity |
| `temporal_filter.py` | Date extraction, 30-day decay scoring |
| `temporal_validity.py` | Bi-temporal validity windows |
| `fusion.py` | RRF + interleave fusion + per-arm caps |
| `recall.py` | Runs 4 arms in parallel, validity filter, re-rank |

Indexes cache under `~/.helix-memory/` (rebuilt on demand, never
committed).

Design decisions:
- RRF constant k=60 (don't tune it)
- Per-arm cap (default 25) before fusion
- Temporal arm abstains when the query has no date constraint
- Deliberately skipped: PostgreSQL, LLM-per-write, cross-encoder
  reranking, heavyweight entity resolution

---

## Attribution

The 4-arm retrieval architecture and RRF fusion are adapted from
[Hindsight](https://github.com/vectorize-io/hindsight) by Vectorize.
Personalized PageRank retrieval adapts
[HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG) (OSU NLP Group).
Bi-temporal validity edges adapt [Graphiti](https://github.com/getzep/graphiti)
(Zep). The multiplicative re-rank pattern follows
[mem0](https://github.com/mem0ai/mem0).

---

## License

MIT.
