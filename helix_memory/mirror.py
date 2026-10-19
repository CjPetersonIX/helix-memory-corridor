"""Verified, non-destructive memory snapshots. Authored by [R2] SRVP2-CODEX.

These are explicit operations, not an automatic fleet/Drive synchronizer. A
generation preserves mapped relative paths and bytes; old generations are never
removed. SQLite/other database files require a supported consistent text export
(for example sqlite3.Connection.iterdump()); copying a live database is refused.
The optional rclone backend uses existing configuration, uploads an immutable
generation, downloads it again, and verifies every byte before moving current.
No rclone sync/delete/purge, credential discovery, or account setup is performed.
One state directory is the sole writer for each seat-specific remote namespace.
The local lock is not a distributed lock: different brains/state directories must
not publish into the same namespace. Older retries preserve history without
rewinding current; unsupported/missing current-pointer evidence fails closed.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import uuid

from ._io import atomic_write_json, file_lock

AUTHOR = "[R2] SRVP2-CODEX"
SCHEMA = 1


class MirrorError(ValueError):
    """Verification/persistence failed; inspect the receipt before claiming sync."""

    def __init__(self, message, receipt=None, *, returncode=None):
        super().__init__(message)
        self.receipt = receipt
        self.returncode = returncode


def _now():
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as error:
        raise MirrorError("Snapshot timestamp must be an ISO timestamp with a timezone") from error
    if parsed.tzinfo is None:
        raise MirrorError("Snapshot timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _logical(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise MirrorError("Logical paths must be nonempty relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise MirrorError("Unsafe logical path: " + value)
    return path.as_posix()


def _plain_path(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    for component in (path, *path.parents):
        if component.is_symlink():
            raise MirrorError("Symbolic links are not supported: " + str(component))
    return path


def _sources(sources):
    if not isinstance(sources, Mapping) or not sources:
        raise MirrorError("Provide an explicit nonempty logical-path/source mapping")
    result = {}
    for key, value in sources.items():
        logical = _logical(key)
        source = _plain_path(value)
        if not source.exists() or not (source.is_dir() or source.is_file()):
            raise MirrorError("Source is not a regular file or directory: " + str(source))
        for other in result:
            if logical == other or logical.startswith(other + "/") or other.startswith(logical + "/"):
                raise MirrorError("Overlapping logical paths: " + logical + " / " + other)
        result[logical] = source
    return result


def _separate(sources, target):
    for source in sources.values():
        if source.is_dir() and (target == source or source in target.parents):
            raise MirrorError("Mirror target must not be inside a source directory")


def _read_file(path):
    """Read only a regular, non-link file and detect mutation during that read."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise MirrorError("Only regular files are supported: " + str(path))
        data = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise MirrorError("Source changed while being read: " + str(path))
    name = path.name.lower()
    if (path.suffix.lower() in (".db", ".db3", ".sqlite", ".sqlite3", ".duckdb", ".mdb", ".lmdb") or
            name.endswith(("-wal", "-shm")) or data.startswith(b"SQLite format 3\x00")):
        raise MirrorError("Database copies are refused; supply a consistent text export: " + str(path))
    return data


def _scan(sources):
    entries, physical = {}, {}

    def add(logical, path):
        if path.is_symlink():
            raise MirrorError("Symbolic links are not supported: " + str(path))
        if path.is_dir():
            entries[logical] = {"kind": "directory"}
            for child in sorted(path.iterdir()):
                add(logical + "/" + child.name, child)
        elif path.is_file():
            data = _read_file(path)
            entries[logical] = {"kind": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            physical[logical] = path
        else:
            raise MirrorError("Only regular files/directories are supported: " + str(path))

    for logical, path in sorted(sources.items()):
        add(logical, path)
        # Include implicit parent directories so the manifest describes the tree exactly.
        for parent in PurePosixPath(logical).parents:
            if parent.as_posix() != ".":
                entries.setdefault(parent.as_posix(), {"kind": "directory"})
    return dict(sorted(entries.items())), physical


def _generation_id(value=None):
    value = value or (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + uuid.uuid4().hex[:12])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", value) or ".." in value:
        raise MirrorError("Invalid generation ID")
    return value


def _load_manifest(generation):
    generation = _plain_path(generation)
    manifest_path = generation / "manifest.json"
    if manifest_path.is_symlink():
        raise MirrorError("Manifest cannot be a symbolic link")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if manifest.get("schema_version") != SCHEMA or not isinstance(manifest.get("entries"), dict):
        raise MirrorError("Unsupported generation manifest")
    if manifest.get("generation_id") != generation.name:
        raise MirrorError("Generation directory and manifest ID differ")
    _generation_id(manifest["generation_id"])
    _timestamp(manifest.get("created_at"))
    for name, entry in manifest["entries"].items():
        _logical(name)
        if entry.get("kind") not in ("file", "directory"):
            raise MirrorError("Invalid manifest entry")
        if entry["kind"] == "file" and (not isinstance(entry.get("size"), int) or entry["size"] < 0 or
                not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))):
            raise MirrorError("Invalid file digest/size")
    return generation, manifest


def _verify_tree(tree, entries):
    tree = _plain_path(tree)
    if not tree.is_dir():
        raise MirrorError("Generation tree is missing")
    actual = {}
    for path in sorted(tree.rglob("*")):
        if path.is_symlink():
            raise MirrorError("Generation contains a symbolic link")
        logical = path.relative_to(tree).as_posix()
        if path.is_dir():
            actual[logical] = {"kind": "directory"}
        elif path.is_file():
            # Snapshot verification reads bytes without treating exported names as active DBs.
            data = path.read_bytes()
            actual[logical] = {"kind": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        else:
            raise MirrorError("Generation contains a nonregular entry")
    if actual != entries:
        missing = sorted(set(entries) - set(actual))
        extra = sorted(set(actual) - set(entries))
        changed = sorted(k for k in entries.keys() & actual.keys() if entries[k] != actual[k])
        raise MirrorError(f"Generation verification failed: missing={missing}, extra={extra}, changed={changed}")


def verify_generation(generation):
    """Read back every file, including detection of unmanifested files."""
    generation, manifest = _load_manifest(generation)
    if {p.name for p in generation.iterdir()} != {"tree", "manifest.json"}:
        raise MirrorError("Unexpected generation metadata/files")
    _verify_tree(generation / "tree", manifest["entries"])
    return {"verified": True, "generation_id": manifest["generation_id"],
            "files": sum(e["kind"] == "file" for e in manifest["entries"].values()),
            "directories": sum(e["kind"] == "directory" for e in manifest["entries"].values()),
            "manifest_sha256": hashlib.sha256((generation / "manifest.json").read_bytes()).hexdigest()}


def _receipt(root, receipt):
    root = _plain_path(root)
    receipts = _plain_path(root / "receipts")
    receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = receipts / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + uuid.uuid4().hex + ".json")
    atomic_write_json(path, receipt)
    return receipt


def _snapshot(sources, destination, generation_id):
    entries, physical = _scan(sources)
    generation = destination / "generations" / generation_id
    if generation.exists():
        verify_generation(generation)
        if _load_manifest(generation)[1]["entries"] != entries:
            raise MirrorError("Generation ID already records a different source snapshot")
        return generation
    _plain_path(generation.parent).mkdir(mode=0o700, parents=True, exist_ok=True)
    generation.mkdir(mode=0o700)  # Exclusive creation: never replace a generation.
    tree = generation / "tree"
    tree.mkdir(mode=0o700)
    for logical, entry in entries.items():
        target = tree / logical
        if entry["kind"] == "directory":
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            data = _read_file(physical[logical])
            if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise MirrorError("Source changed during snapshot: " + logical)
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
    after, _ = _scan(sources)
    if after != entries:
        raise MirrorError("Source changed during snapshot; current remains unchanged")
    # Flush directory entries as well as file contents before publishing the manifest.
    for directory in sorted((p for p in tree.rglob("*") if p.is_dir()), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(tree, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    atomic_write_json(generation / "manifest.json", {"schema_version": SCHEMA,
                      "generation_id": generation_id, "created_at": _now(),
                      "authored_by": AUTHOR, "entries": entries})
    descriptor = os.open(generation.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    verify_generation(generation)
    return generation


def mirror_to_directory(sources, destination, *, generation_id=None):
    """Snapshot an explicit mapping; atomically advance current only after verification.

    Mapping example: {'memory': '~/memory', 'handoff/Living_Handoff.html':
    '~/workspace/user/files/Living_Handoff.html'}. Include all required roots;
    this function cannot infer files omitted from the mapping. Reusing an ID is
    idempotent only for the same bytes. A failed generation remains for diagnosis.
    """
    sources, destination = _sources(sources), _plain_path(destination)
    _separate(sources, destination)
    generation_id = _generation_id(generation_id)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = {"generation_id": generation_id, "authored_by": AUTHOR,
               "local_generation": None, "remote_verified_generation": None,
               "remote_identity": None, "snapshot_created_at": None,
               "current_advanced": False, "local_current_generation": None,
               "status": "pending", "created_at": _now()}
    try:
        with file_lock(destination / ".mirror.lock"):
            _receipt(destination, receipt)
            generation = _snapshot(sources, destination, generation_id)
            verified = verify_generation(generation)
            captured = _load_manifest(generation)[1]["created_at"]
            current_path = _plain_path(destination / "current.json")
            previous = json.loads(current_path.read_text("utf-8")) if current_path.exists() else None
            if previous is not None:
                previous_id = _generation_id(previous["generation_id"])
                previous_capture = previous.get("snapshot_created_at")
                if previous_capture is None:
                    previous_capture = _load_manifest(destination / "generations" / previous_id)[1]["created_at"]
                advance = _timestamp(captured) > _timestamp(previous_capture)
            else:
                advance = True
            if advance:
                atomic_write_json(current_path, {**verified, "snapshot_created_at": captured,
                                  "remote_identity": None, "verified_at": _now()})
            receipt = {**receipt, **verified, "local_generation": str(generation),
                       "snapshot_created_at": captured, "status": "local_verified", "verified_at": _now(),
                       "current_advanced": advance, "local_current_generation": generation_id if advance else previous_id}
            _receipt(destination, receipt)
    except Exception as error:
        receipt = {**receipt, "status": "failed", "error": str(error), "failed_at": _now()}
        _receipt(destination, receipt)
        raise MirrorError(str(error), receipt) from error
    return receipt


def _remote(remote):
    if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9_-]+:[^\r\n\x00]*", remote):
        raise MirrorError("Use an explicitly configured rclone remote:path")
    if any(p == ".." for p in remote.split(":", 1)[1].split("/")):
        raise MirrorError("Remote path cannot traverse parents")
    return remote.rstrip("/")


def _run_rclone(arguments, *, executable, timeout):
    # Do not return provider output: it can contain signed URLs/credentials.
    result = subprocess.run([executable, *arguments], capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise MirrorError(f"rclone {arguments[0]} failed (exit {result.returncode})", returncode=result.returncode)


def _remote_pointer(remote, temporary, *, known, executable, timeout):
    """An unknown failure is never treated as an empty remote."""
    downloaded = Path(temporary) / "observed-current.json"
    try:
        _run_rclone(["copyto", remote + "/current.json", str(downloaded)],
                    executable=executable, timeout=timeout)
    except MirrorError as error:
        # Documented rclone missing-directory/file exit codes; only first publication.
        if error.returncode in (3, 4) and known is None:
            return None
        raise MirrorError("Cannot verify the remote current pointer; publication remains pending") from error
    try:
        current = json.loads(downloaded.read_text("utf-8"))
        _generation_id(current["generation_id"])
        if current.get("remote_identity") not in (None, remote):
            raise MirrorError("Remote current pointer identifies a different namespace")
        captured = current.get("snapshot_created_at")
        if captured is None:
            # Upgrade old pointer format by reading its referenced manifest, never
            # inferring time from an ID or a last-upload/verified timestamp.
            manifest_path = Path(temporary) / "observed-manifest.json"
            _run_rclone(["copyto", remote + "/generations/" + current["generation_id"] + "/manifest.json",
                         str(manifest_path)], executable=executable, timeout=timeout)
            raw = manifest_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != current["manifest_sha256"]:
                raise MirrorError("Current pointer's referenced manifest digest differs")
            old_manifest = json.loads(raw)
            if old_manifest.get("generation_id") != current["generation_id"]:
                raise MirrorError("Current pointer's referenced manifest ID differs")
            captured = old_manifest["created_at"]
        _timestamp(captured)
        current["snapshot_created_at"] = captured
        if known and _timestamp(captured) < _timestamp(known["remote_current_snapshot_created_at"]):
            raise MirrorError("Remote current pointer regressed; explicit reconciliation is required")
        return current
    except (KeyError, ValueError, TypeError) as error:
        raise MirrorError("Remote current pointer is invalid; publication remains pending") from error


def retry_to_rclone(generation, remote, *, executable="rclone", timeout=300):
    """Retry captured bytes; a seat's single state directory serializes publication.

    This is a process lock, not distributed coordination. Each brain/seat must use
    its own remote namespace and one state directory. An older verified upload
    returns remote_history_verified/current_advanced=False without rewinding current.
    """
    generation, manifest = _load_manifest(generation)
    verified = verify_generation(generation)
    remote = _remote(remote)
    root = generation.parent.parent
    receipt = {"generation_id": manifest["generation_id"], "authored_by": AUTHOR,
               "local_generation": str(generation), "remote_verified_generation": None,
               "remote_identity": remote, "snapshot_created_at": manifest["created_at"],
               "current_advanced": False, "remote_current_generation": None,
               "status": "remote_pending", "created_at": _now()}
    _receipt(root, receipt)
    try:
        remote_key = hashlib.sha256(remote.encode("utf-8")).hexdigest()
        lock = _plain_path(root / "remote-locks" / (remote_key + ".lock"))
        known_path = _plain_path(root / "remote-state" / (remote_key + ".json"))
        with file_lock(lock, timeout=timeout):
            known = json.loads(known_path.read_text("utf-8")) if known_path.exists() else None
            if known and known.get("remote_identity") != remote:
                raise MirrorError("Local remote-state namespace mismatch")
            target = remote + "/generations/" + manifest["generation_id"]
            _run_rclone(["copy", str(generation), target, "--immutable", "--create-empty-src-dirs"],
                        executable=executable, timeout=timeout)
            with tempfile.TemporaryDirectory(prefix="helix-mirror-readback-") as temporary:
                download = Path(temporary) / manifest["generation_id"]
                download.mkdir()
                _run_rclone(["copy", target, str(download), "--create-empty-src-dirs"],
                            executable=executable, timeout=timeout)
                downloaded = verify_generation(download)
                if downloaded["manifest_sha256"] != verified["manifest_sha256"]:
                    raise MirrorError("Remote manifest differs from the local snapshot")
                observed = _remote_pointer(remote, temporary, known=known, executable=executable, timeout=timeout)
                advance = observed is None or _timestamp(manifest["created_at"]) > _timestamp(observed["snapshot_created_at"])
                if observed and observed["generation_id"] == manifest["generation_id"]:
                    if observed.get("manifest_sha256") != verified["manifest_sha256"]:
                        raise MirrorError("Remote current generation has a conflicting manifest digest")
                    advance = False
                if advance:
                    pointer = Path(temporary) / "current.json"
                    observed = {**verified, "snapshot_created_at": manifest["created_at"],
                                "remote_identity": remote, "verified_at": _now()}
                    atomic_write_json(pointer, observed)
                    _run_rclone(["copyto", str(pointer), remote + "/current.json"], executable=executable, timeout=timeout)
                    readback = Path(temporary) / "pointer-readback.json"
                    _run_rclone(["copyto", remote + "/current.json", str(readback)], executable=executable, timeout=timeout)
                    if readback.read_bytes() != pointer.read_bytes():
                        raise MirrorError("Remote current-pointer readback mismatch")
                is_current = observed["generation_id"] == manifest["generation_id"]
                receipt = {**receipt, **verified, "status": "remote_verified" if is_current else "remote_history_verified",
                           "remote_verified_generation": manifest["generation_id"], "current_advanced": advance,
                           "remote_current_generation": observed["generation_id"],
                           "remote_current_snapshot_created_at": observed["snapshot_created_at"], "verified_at": _now()}
            atomic_write_json(known_path, receipt)
            atomic_write_json(root / "remote-current.json", receipt)
            _receipt(root, receipt)
            return receipt
    except Exception as error:
        receipt = {**receipt, "status": "remote_pending", "error": str(error), "failed_at": _now()}
        _receipt(root, receipt)
        raise MirrorError(str(error), receipt) from error


def mirror_to_rclone(sources, remote, *, state_dir, generation_id=None, executable="rclone", timeout=300):
    """Capture locally, then upload/read back using existing rclone configuration.

    No network is accessed unless explicitly calling this function. Remote
    unavailability leaves a local_verified snapshot and remote_pending receipt;
    retry_to_rclone(receipt['local_generation'], remote) reuses that exact snapshot.
    """
    remote = _remote(remote)
    local = mirror_to_directory(sources, state_dir, generation_id=generation_id)
    return retry_to_rclone(local["local_generation"], remote, executable=executable, timeout=timeout)


def restore_generation(generation, empty_target):
    """Restore into an absent/empty isolated directory; never replace existing files."""
    generation, manifest = _load_manifest(generation)
    verify_generation(generation)
    target = _plain_path(empty_target)
    if target == generation or generation in target.parents or target in generation.parents:
        raise MirrorError("Restore target must be separate from the generation")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise MirrorError("Restore target must be absent or empty")
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    for logical, entry in sorted(manifest["entries"].items()):
        output = target / logical
        if entry["kind"] == "directory":
            output.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with output.open("xb") as stream:
                stream.write((generation / "tree" / logical).read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
    _verify_tree(target, manifest["entries"])
    return {"restored": True, "generation_id": manifest["generation_id"],
            "target": str(target), "verified_at": _now(), "authored_by": AUTHOR}
