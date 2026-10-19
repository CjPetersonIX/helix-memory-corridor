# Agent-host capture and recovery contract

Authored by [R2] SRVP2-CODEX. These hooks must be connected in the agent's actual
execution environment. Instructions in MEMORY.md alone do not enforce them.

## Before changing startup memory

Preserve the complete original memory, source layout, wrapper, handoff, and
configuration. Verify a full backup and restoration in a separate directory.
Identify the real auto-loaded MEMORY.md and executable; do not infer them from
a similarly named file. Install this version into a separate virtual environment
for verification, rather than replacing a working wrapper blindly.

## After meaningful inputs and actions

Call `MemoryStore.remember` after a decision, correction, verified result,
delegation or task transition. Use an exact task ID, the full actor identifier,
a source/message reference, verification status and an idempotency key. The
receipt must say durable/readback verified before reporting retention.
Record consequential action intent before execution and outcome afterward.
An unresolved intent on recovery means inspect actual outcome before retrying.

Update the one existing handoff's Resume block using revision checks. Record
goal, last verified step/evidence, next action, blocker, approvals and last event
ID. All handoff renderers must preserve this block and cooperate with its lock.
A failed update must be shown as pending; do not invent a successful handoff.

Enqueue a mirror of the **whole configured corpus**, including out-of-tree
handoff/planning state. Show local save and remote verification separately.
Retain the exact local generation on outage; retry without recapturing history.

## Before/after context compaction or session restart

Before compaction: flush decisions/results, update resume, and verify readback.
After restart: load the small startup map, read resume and `latest_events` for
its task ID, then use recall for relevant deeper context. Open source evidence;
do not reconstruct a step from vague recollection or a stale summary.

If the provider offers no capture/compaction hooks, integrate these operations
at each task boundary through its available tools and report that enforcement
is procedural. A timer/research pulse is a backstop, not a capture hook.

## Startup memory contents

Keep identity, scope/approval boundaries, stable preferences, file pointers and
recovery steps in a compact startup file. Preserve detailed cases, dated account
states, completed work and old incidents in topic history. Keep active deadlines
in current task state. Never remove evidence or permissions during a cleanup.

## Acceptance before claiming a live fix

Prove an exact new decision can be recalled immediately and after ten minutes;
recover a task across a fresh process/context reset without repeating a finished
action; order two same-day corrections; preserve an unrelated same-actor rule;
show degraded optional-index/Drive status; and restore a complete verified
remote generation into an empty directory. Repository tests do not prove the
hosted provider has these hooks installed.
