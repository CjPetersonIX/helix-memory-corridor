# Verified memory mirror and recovery

Authored by [R2] SRVP2-CODEX. The working source and verified remote generation
have separate receipts. This is an explicit API/CLI operation, not a claim that
Drive access, a schedule, or an automatic chat hook is already installed.

## Define the whole corpus

Map every canonical file/directory to its intended relative path. A file omitted
from this mapping is not backed up. Include the memory root, the one existing
handoff, its authoritative planning state, and required memory configuration.
Keep credentials in the approved vault. Preserve existing Drive copies before
any layout migration; do not replace them with this example layout blindly.

```bash
hmc mirror \
  --source memory=/path/to/memory \
  --source workspace/user/files/Living_Handoff.html=/path/Living_Handoff.html \
  --source planning/board.json=/path/board.json \
  --destination /separate/location/memory-backup
```

The layout is:

```text
memory-backup/
  generations/<unique-id>/
    tree/<exact mapped relative paths>
    manifest.json
  current.json
  receipts/<timestamp-id>.json
```

Each generation contains an exact complete tree; history remains in earlier
generations. Removing or renaming a source never removes its previous backup.
Current is only a pointer to a verified generation, not a second memory source.

The implementation detects changed sources during capture, refuses symlinks and
path traversal, writes files exclusively, and verifies every path, byte count,
and SHA256 hash. A mutation/failed check leaves current unchanged. Keep the
backup/outbox outside all source roots to prevent recursive self-copy.

## Google Drive through existing rclone configuration

Use an already configured, private, least-privilege rclone Drive remote. This
library never discovers credentials, changes sharing, or performs OAuth setup.
Check the configured destination yourself before sending any private data.

```bash
hmc mirror \
  --source memory=/path/to/memory \
  --source workspace/user/files/Living_Handoff.html=/path/Living_Handoff.html \
  --rclone-remote gdrive:Memory/seat-example \
  --state-dir /separate/location/mirror-outbox
```

It captures locally, uploads an immutable generation using `rclone copy`,
downloads that complete generation into a separate temporary directory, and
checks its manifest and bytes. Only then can it advance and read back the
remote current pointer. It never invokes `sync`, `delete`, or `purge`.

`remote_verified` means the returned generation was read back successfully.
`remote_pending` means remote verification is incomplete; retain the local
generation and retry that exact generation. Do not claim mirrored from an
upload exit code. See receipt fields for whether the current pointer advanced;
an older verified retry must not replace a newer current generation.

```python
from helix_memory.mirror import retry_to_rclone
receipt = retry_to_rclone('/outbox/generations/GENERATION-ID',
                          'gdrive:Memory/seat-example')
```

One seat/remote must have one configured writer/state directory. Local locks do
not coordinate different machines. Do not share the same remote writer path
across independent brains or replicas without a separate coordinator.

## Databases and MemPalace

Raw `.db`, `.sqlite`, `.sqlite3`, SQLite-header, WAL and SHM files are refused.
Copying a changing database plus vector files is not a consistent backup. Use
its supported export or quiescence procedure first and include that output.
An isolated SQLite database can be exported through sqlite3's supported backup
or iterdump API; a full Chroma/MemPalace export needs its own verified contract.
A SQL text export can be mirrored byte-for-byte, but does not by itself prove a
complete Chroma restoration. No transparent live MemPalace backup is claimed.

## Restore rehearsal

```bash
hmc verify /backup/generations/GENERATION-ID
hmc restore /backup/generations/GENERATION-ID /new-empty/restore-check
```

Restoration refuses a nonempty target and verifies all restored hashes. Start
in a separate directory; never restore over live files. Verify the generation,
configuration, handoff state, and semantic database export before a deliberate
cutover. Keep the current working copy and all history available.

## Integration and cadence

After a durable event capture, enqueue a mirror operation. Before compaction,
verify the event and resume cursor are saved. A local readback can succeed while
Drive is offline: retain pending receipts and show last verified remote time.
A periodic reconciliation checks backlog; an hourly job alone does not protect
a decision made ten minutes ago. Installing that host integration/schedule is a
separate deployment step. No cron is created by installing this package.
