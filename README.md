# Helix Memory Corridor

Durable agent-memory capture, fresh hybrid recall, explicit corrections, resume
state, and verified non-deleting backup generations. Free, local-first, Python
3.10+. No LLM calls or API keys on the core path.

Repair authored by [R2] SRVP2-CODEX. Original source/history is retained in Git.
Version 0.3.0 repairs the incomplete category migration in the earlier public
release. See [CHANGELOG](CHANGELOG.md).

## Install and run

```bash
python -m pip install .
hmc --help
hmc doctor --memory-dir ~/memory
```

`python -m helix_memory` is the same CLI. Semantic models are **opt-in**; core
recall never downloads a model. Install `.[semantic]` only if wanted. `.[graph]`
adds SciPy for full NetworkX PageRank; without it, graph walk is an explicit,
reported fallback. Linux/macOS file locking protects cooperating local writers.

## Save first, then recall

```python
from helix_memory import MemoryStore, recall, latest_events

store = MemoryStore("~/memory")
receipt = store.remember(
    "Project Orion uses the blue design. Next: review the draft.",
    area="work", actor="[R2] EXAMPLE-AGENT", task_id="orion-design",
    idempotency_key="orion-design-decision-1",
    metadata={"status": "reported", "source": "conversation-message-123"},
)
assert receipt["durable"] and receipt["readback_verified"]

result = recall("Orion design", memory_dir="~/memory", return_diagnostics=True)
print(result["results"])
print(result["diagnostics"])
print(latest_events(memory_dir="~/memory", task_id="orion-design"))
```

Events are immutable Markdown with structured metadata and full author identity.
A repeated idempotency key returns the existing event; changed content under
that key raises an error. Capture and readback are explicit operations: your
agent host must invoke them after meaningful decisions, corrections, results,
and task transitions. This package cannot intercept a hosted chat by itself.

```bash
hmc remember --memory-dir ~/memory --area work \
  --actor '[R2] EXAMPLE-AGENT' --task-id orion-design \
  --idempotency-key orion-design-decision-1 \
  --text 'Project Orion uses the blue design. Next: review the draft.'
hmc 'What is the Orion design?' --memory-dir ~/memory
hmc latest --memory-dir ~/memory --task-id orion-design
hmc index --memory-dir ~/memory --categories work --no-semantic
```

`category`, `categories`, `division`, and `divisions` are compatible API scope
aliases. Conflicting selections raise an error. The collector includes root
Markdown and category subdirectories, with stable IDs and explicit source paths.
HTML/JSON handoff data uses the separate resume interface rather than silently
being treated as Markdown.

## Corrections retain the original

```python
correction = store.remember(
    "Project Orion now uses the green design.", area="work",
    actor="[R2] EXAMPLE-AGENT", task_id="orion-design", kind="correction",
    supersedes=[receipt["event_id"]], idempotency_key="orion-design-decision-2",
)
```

Only explicit `supersedes` IDs close an earlier fact. Shared names or words such
as “updated” never invalidate unrelated records. Keep a correction in its
subject's category; a category-scoped query cannot see corrections stored
outside that scope. Full-corpus recall sees all available correction records.

`as_of="2026-10-18T10:00:00-07:00"` queries effective time; `known_at` optionally
limits what had been recorded by that time. Use explicit offsets. Date-only and
legacy filename times use documented UTC boundaries; new captures always carry
UTC timestamps. Historical undated records stay undated, not silently current.

## Resume without repeating work

Keep one existing handoff HTML file. `MemoryStore.update_resume(path, state,
expected_revision=...)` adds/updates only its marked Resume block and preserves
previous bytes as content-addressed history. `read_resume(path)` restores the
goal, last verified step, evidence, next action, blocker, approvals, and event ID.
The content is data, never an execution or authorization grant.

```bash
hmc resume set --memory-dir ~/memory --handoff /path/Living_Handoff.html \
  --actor '[R2] EXAMPLE-AGENT' --task-id orion-design --goal 'Finish Orion design' \
  --last-verified-step 'Decision saved and read back' --next-step 'Review draft' \
  --expected-revision 0
hmc resume show --memory-dir ~/memory --handoff /path/Living_Handoff.html
```

The handoff must already exist. All writers, including its renderer, must
preserve the marked block and coordinate revision updates. Local locks do not
fence unrelated writers or provide distributed fleet coordination.

## Freshness and search health

Every recall rereads source content and fingerprints the selected corpus.
Appending or changing a memory invalidates cached relevance state. Four arms
(keyword, graph, time, and optional semantic) use RRF fusion; returned text is
hydrated from that current snapshot, not stale cached excerpts. Full matching
text, source paths, generation and arm-health diagnostics accompany results.

`latest_events()` reads source events directly for current-task recovery.
A missing directory or failed arm is reported as degraded. An empty result does
not prove that an event was never recorded. Capture receipts establish durable
storage, not that the source assertion is independently true.

Optional semantic indexing preserves separate generations. It uses its own
Chroma store; **it is not automatically connected to MemPalace**. Models and
optional dependencies are not needed for capture, core recall, or restore.

## Exact memory mirror

See [Drive mirror](docs/DRIVE-MIRROR.md). Include every canonical memory path,
including out-of-tree handoff and planning state. The mirror preserves complete
immutable generations, relative paths, empty directories, bytes and SHA256
manifests. Current advances only after full readback verification; prior
generations remain available. No destructive sync/delete/purge operations.

Live databases are refused: obtain a supported consistent export first.
The package does not install OAuth, a cron job, or a provider capture hook.
A successful local save and a verified Drive save are separate states.

## Verification

```bash
python -m pip install '.[dev]'
python -m pytest -q
python scripts/test_recall.py --no-semantic
python scripts/test_enhancements.py
```

Tests use synthetic data and isolated directories. No personal memories, live
Drive, paid services, or model downloads are required. Optional semantic tests
use explicitly identified fakes; they are not live backend benchmarks. Models
have finite input windows; keep individual events small for semantic coverage.

See [architecture](docs/ARCHITECTURE.md) and [integration](docs/INTEGRATION.md).

## Attribution and license

RRF/four-arm design adapts [Hindsight](https://github.com/vectorize-io/hindsight).
Graph ranking adapts [HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG);
validity concepts reference [Graphiti](https://github.com/getzep/graphiti);
reranking references [mem0](https://github.com/mem0ai/mem0). See
[attribution](docs/HINDSIGHT_CREDIT.md) and the MIT [LICENSE](LICENSE).
