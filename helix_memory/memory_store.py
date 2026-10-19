"""Durable, append-only capture and an explicit resume block in one handoff.

Authored by [R2] SRVP2-CODEX, 2026-10-19 UTC.

This module does not install a provider hook, schedule a job, execute task text,
or grant an agent authority. Call ``remember`` at the integration boundary where
a meaningful decision/result is available, and explicitly restore ``read_resume``
when a session resumes. A successful receipt follows fsync and byte readback.

The Markdown events are primary evidence. ``.helix-state`` is rebuildable lookup
state. Historical handoff bytes are content-addressed ``.blob`` files, not new
living handoffs. Mutable handoff updates require all writers to use this API's
lock/CAS protocol; an unrelated writer cannot be fenced by a Python file lock.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import re
from typing import Any
from datetime import datetime, timezone
import uuid

from ._io import atomic_write_bytes, atomic_write_json, canonical_json, file_lock


EVENT_PREFIX = "<!-- helix-event: "
EVENT_SUFFIX = " -->"
RESUME_START = b"<!-- HELIX-RESUME-START -->"
RESUME_END = b"<!-- HELIX-RESUME-END -->"
_ACTOR = re.compile(r"\[R[0-8]\] [^\r\n]{2,}\Z")
_AREA = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class MemoryIntegrityError(ValueError):
    """An event, lookup ledger, or resume block failed integrity validation."""


class IdempotencyConflict(ValueError):
    """An idempotency key was already used for a different capture payload."""


class RevisionConflict(ValueError):
    """The existing handoff revision differs from the expected revision."""


def _json_copy(value: Any) -> Any:
    # Reject NaN/Infinity and non-JSON Python objects before any filesystem write.
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_object(value: Any) -> str:
    return _hash_bytes(canonical_json(value).encode("utf-8"))


def _single_line(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a nonempty string without edge whitespace")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} must be a single line without control characters")
    return value


def _actor(value: Any) -> str:
    value = _single_line(value, "actor")
    if not _ACTOR.fullmatch(value):
        raise ValueError("actor must use its full '[R#] REGISTERED-IDENTIFIER' form")
    return value


def _timestamp(value: str | datetime) -> str:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("timestamp must be an ISO datetime with an explicit UTC offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset, not a date or naive datetime")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_durable(path: Path, *, mode: int = 0o700) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    # Fsync the created directory entries as well as their child contents.
    for created in reversed(missing):
        _fsync_directory(created)
        _fsync_directory(created.parent)


def _exclusive_write(path: Path, data: bytes) -> None:
    """Create an immutable file; never truncate an existing path."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)
    if path.read_bytes() != data:
        raise MemoryIntegrityError(f"readback failed for {path}")


def _event_payload(meta: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "text": text,
        "area": meta["area"],
        "actor": meta["actor"],
        "task_id": meta["task_id"],
        "kind": meta["kind"],
        "supersedes": meta["supersedes"],
        "valid_from": meta["valid_from"] if meta["valid_from_explicit"] else None,
        "next_step": meta["next_step"],
        "metadata": meta["metadata"],
    }


def _record_hash(meta: dict[str, Any], text: str) -> str:
    """Cover identity/capture metadata separately from idempotency's payload."""
    return _hash_object({"metadata": {key: value for key, value in meta.items() if key != "record_sha256"}, "text": text})


def _id_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list of explicit event IDs")
    result = [_single_line(item, field) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must not contain duplicate IDs")
    return result


def validate_event_text(text: str) -> dict[str, Any]:
    """Purely validate a schema-1 event and return its metadata plus exact body.

    No filesystem, ledger, or network access occurs here. Newline characters in
    the body are significant checksum input: callers must decode source bytes
    without universal-newline normalization. Both LF and CRLF header separators
    are accepted while the body remains byte-equivalent after UTF-8 encoding.
    Unsupported/incomplete store schemas fail closed; callers may separately
    support legacy lightweight headers that do not declare a store schema.

    The record checksum covers metadata and exact body; the payload fingerprint
    separately supports idempotency. Neither authenticates an adversary able to
    recompute hashes. ``MemoryStore.read_event`` also checks the ledger's file hash.
    """
    try:
        if not isinstance(text, str):
            raise ValueError("event source must be decoded UTF-8 text")
        start = re.match(r"\A([^\r\n]+)(?:\r\n|\n)(?:\r\n|\n)", text)
        if start is None:
            raise ValueError("missing first-line event header and blank separator")
        header, body = start.group(1), text[start.end():]
        if not header.startswith(EVENT_PREFIX) or not header.endswith(EVENT_SUFFIX):
            raise ValueError("missing exact event header")
        meta = json.loads(header[len(EVENT_PREFIX):-len(EVENT_SUFFIX)])
        required = {"schema_version", "event_id", "recorded_at", "valid_from", "valid_from_explicit", "area", "actor", "task_id", "kind", "supersedes", "next_step", "metadata", "payload_sha256", "idempotency_key_hash", "record_sha256"}
        if not isinstance(meta, dict) or not required.issubset(meta) or type(meta["schema_version"]) is not int or meta["schema_version"] != 1:
            raise ValueError("unsupported or incomplete event schema")
        _json_copy(meta)
        identifier = _single_line(meta["event_id"], "event_id")
        if str(uuid.UUID(identifier)) != identifier:
            raise ValueError("event_id must be a canonical UUID")
        _actor(meta["actor"])
        _single_line(meta["task_id"], "task_id")
        _single_line(meta["kind"], "kind")
        if not isinstance(meta["area"], str) or not _AREA.fullmatch(meta["area"]):
            raise ValueError("unsafe area")
        if type(meta["valid_from_explicit"]) is not bool or not isinstance(meta["metadata"], dict):
            raise ValueError("invalid source metadata")
        if not isinstance(meta["recorded_at"], str) or not isinstance(meta["valid_from"], str):
            raise ValueError("event timestamps must be ISO strings")
        recorded_at = _timestamp(meta["recorded_at"])
        valid_from = _timestamp(meta["valid_from"])
        if not meta["valid_from_explicit"] and valid_from != recorded_at:
            raise ValueError("implicit valid_from must equal the capture timestamp")
        if not isinstance(meta["supersedes"], list):
            raise ValueError("supersedes must be an explicit list")
        _id_list(meta["supersedes"], "supersedes")
        if meta["next_step"] is not None and not isinstance(meta["next_step"], str):
            raise ValueError("next_step must be text or null")
        for key in ("payload_sha256", "idempotency_key_hash", "record_sha256"):
            value = meta[key]
            if value is None and key == "idempotency_key_hash":
                continue
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError(f"{key} must be a lowercase SHA-256 digest")
        if not body.strip() or _hash_object(_event_payload(meta, body)) != meta["payload_sha256"]:
            raise ValueError("event payload checksum mismatch")
        if _record_hash(meta, body) != meta["record_sha256"]:
            raise ValueError("event record checksum mismatch")
    except (ValueError, KeyError, TypeError, UnicodeError, AttributeError) as exc:
        raise MemoryIntegrityError("invalid immutable event text: " + str(exc)) from exc
    return {**meta, "text": body}


class MemoryStore:
    """Capture immutable memories under ``<memory_dir>/<area>/qpulse/``."""

    def __init__(self, memory_dir: str | os.PathLike[str], *, lock_timeout: float = 10):
        self.memory_dir = Path(memory_dir).expanduser().resolve()
        self.lock_timeout = lock_timeout
        self._state_dir = self.memory_dir / ".helix-state"
        self._ledger_path = self._state_dir / "events.json"

    def _prepare(self) -> None:
        if not self._state_dir.resolve().is_relative_to(self.memory_dir):
            raise ValueError("memory state resolves outside the memory directory")
        _mkdir_durable(self.memory_dir)
        _mkdir_durable(self._state_dir)
        _fsync_directory(self.memory_dir)

    def _ledger(self) -> dict[str, Any]:
        if not self._ledger_path.exists():
            return {"schema_version": 1, "events": {}, "idempotency": {}}
        try:
            value = json.loads(self._ledger_path.read_text(encoding="utf-8"))
            if value.get("schema_version") != 1 or not isinstance(value.get("events"), dict) or not isinstance(value.get("idempotency"), dict):
                raise ValueError("unsupported ledger shape")
            return value
        except (OSError, UnicodeError, ValueError, AttributeError) as exc:
            raise MemoryIntegrityError("memory lookup ledger is unreadable; primary events remain untouched") from exc

    def _entry_path(self, entry: dict[str, Any]) -> Path:
        try:
            path = (self.memory_dir / entry["relative_path"]).resolve()
            path.relative_to(self.memory_dir)
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryIntegrityError("event lookup path escapes memory directory or is malformed") from exc
        return path

    def _read_path(self, path: Path, expected_sha256: str | None = None) -> dict[str, Any]:
        data = path.read_bytes()
        digest = _hash_bytes(data)
        if expected_sha256 is not None and digest != expected_sha256:
            raise MemoryIntegrityError(f"event bytes changed: {path}")
        try:
            meta = validate_event_text(data.decode("utf-8"))
            relative = path.resolve().relative_to(self.memory_dir).as_posix()
            if Path(relative).parts[:2] != (meta["area"], "qpulse") or len(Path(relative).parts) != 3:
                raise ValueError("event location disagrees with area")
        except (UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise MemoryIntegrityError(f"invalid immutable event: {path}") from exc
        return {**meta, "path": str(path), "relative_path": relative, "sha256": digest}

    def _event_paths(self):
        if self.memory_dir.exists():
            for area in sorted(self.memory_dir.iterdir()):
                if area.is_dir() and not area.name.startswith("."):
                    if not (area / "qpulse").resolve().is_relative_to(self.memory_dir):
                        raise MemoryIntegrityError("qpulse directory resolves outside the memory directory")
                    for path in sorted((area / "qpulse").glob("*.md")):
                        # Existing legacy qpulse Markdown is not a new-style event.
                        with path.open("rb") as stream:
                            if stream.readline().startswith(EVENT_PREFIX.encode("utf-8")):
                                yield path

    @staticmethod
    def _entry(event: dict[str, Any]) -> dict[str, Any]:
        return {"relative_path": event["relative_path"], "sha256": event["sha256"]}

    @staticmethod
    def _receipt(event: dict[str, Any]) -> dict[str, Any]:
        return {key: event[key] for key in ("event_id", "path", "relative_path", "sha256", "recorded_at", "valid_from", "actor", "task_id", "kind")} | {"durable": True, "readback_verified": True}

    def remember(
        self,
        text: str,
        *,
        area: str | None = None,
        category: str | None = None,
        actor: str,
        task_id: str,
        kind: str = "decision",
        idempotency_key: str | None = None,
        supersedes: list[str] | tuple[str, ...] | None = None,
        valid_from: str | datetime | None = None,
        next_step: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save and read back one event, idempotently when an explicit key is supplied.

        ``metadata`` preserves the caller's source/verification labels exactly;
        an assertion is never promoted to independent verification. ``valid_from``
        is distinct from the capture time and must carry an explicit UTC offset.
        A repeated key with changed content raises ``IdempotencyConflict``.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a nonempty string")
        if area is not None and category is not None and area != category:
            raise ValueError("area and category must agree when both are supplied")
        area = area if area is not None else category
        if not isinstance(area, str) or not _AREA.fullmatch(area) or area in {".", ".."}:
            raise ValueError("area/category must be one non-hidden directory name")
        actor = _actor(actor)
        task_id = _single_line(task_id, "task_id")
        kind = _single_line(kind, "kind")
        if next_step is not None and not isinstance(next_step, str):
            raise ValueError("next_step must be a string or None")
        metadata = _json_copy({} if metadata is None else metadata)
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object")
        supersedes = _id_list(supersedes, "supersedes")
        explicit_from = _timestamp(valid_from) if valid_from is not None else None
        payload = {"text": text, "area": area, "actor": actor, "task_id": task_id, "kind": kind, "supersedes": supersedes, "valid_from": explicit_from, "next_step": next_step, "metadata": metadata}
        payload_hash = _hash_object(payload)
        key_hash = None
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key:
                raise ValueError("idempotency_key must be a nonempty string")
            key_hash = _hash_bytes(idempotency_key.encode("utf-8"))
        self._prepare()
        with file_lock(self._state_dir / "memory.lock", timeout=self.lock_timeout):
            ledger = self._ledger()
            if key_hash is not None:
                keyed = ledger["idempotency"].get(key_hash)
                if keyed is not None:
                    if keyed["payload_sha256"] != payload_hash:
                        raise IdempotencyConflict("idempotency key already refers to a different capture payload")
                    entry = ledger["events"].get(keyed["event_id"])
                    if entry is None:
                        raise MemoryIntegrityError("idempotency ledger points to an unknown event")
                    event = self._read_path(self._entry_path(entry), entry["sha256"])
                    if event["event_id"] != keyed["event_id"] or event["payload_sha256"] != payload_hash or event["idempotency_key_hash"] != key_hash:
                        raise MemoryIntegrityError("idempotency ledger disagrees with the immutable event")
                    return self._receipt(event)
                # Recover a crash between durable event creation and lookup commit.
                matches = []
                for path in self._event_paths():
                    candidate = self._read_path(path)
                    if candidate["idempotency_key_hash"] == key_hash:
                        matches.append(candidate)
                if len(matches) > 1:
                    raise MemoryIntegrityError("multiple immutable events carry the same idempotency key")
                if matches:
                    event = matches[0]
                    if event["payload_sha256"] != payload_hash:
                        raise IdempotencyConflict("idempotency key already refers to a different capture payload")
                    ledger["events"][event["event_id"]] = self._entry(event)
                    ledger["idempotency"][key_hash] = {"event_id": event["event_id"], "payload_sha256": payload_hash}
                    atomic_write_json(self._ledger_path, ledger)
                    return self._receipt(event)
            event_dir = self.memory_dir / area / "qpulse"
            if not event_dir.resolve().is_relative_to(self.memory_dir):
                raise ValueError("area resolves outside the memory directory")
            _mkdir_durable(event_dir)
            _fsync_directory(event_dir.parent)
            now = datetime.now(timezone.utc)
            recorded_at = now.isoformat(timespec="microseconds")
            event_id = str(uuid.uuid4())
            meta = {"schema_version": 1, "event_id": event_id, "recorded_at": recorded_at, "valid_from": explicit_from or recorded_at, "valid_from_explicit": explicit_from is not None, "area": area, "actor": actor, "task_id": task_id, "kind": kind, "supersedes": supersedes, "next_step": next_step, "metadata": metadata, "payload_sha256": payload_hash, "idempotency_key_hash": key_hash}
            meta["record_sha256"] = _record_hash(meta, text)
            header_json = canonical_json(meta).replace("<", "\\u003c").replace(">", "\\u003e")
            data = (EVENT_PREFIX + header_json + EVENT_SUFFIX + "\n\n" + text).encode("utf-8")
            path = event_dir / f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{event_id}.md"
            _exclusive_write(path, data)
            event = self._read_path(path, _hash_bytes(data))
            ledger["events"][event_id] = self._entry(event)
            if key_hash is not None:
                ledger["idempotency"][key_hash] = {"event_id": event_id, "payload_sha256": payload_hash}
            atomic_write_json(self._ledger_path, ledger)
            if self._ledger()["events"].get(event_id) != self._entry(event):
                raise MemoryIntegrityError("event lookup commit failed readback")
            return self._receipt(event)

    def read_event(self, event_id: str) -> dict[str, Any]:
        """Read exact source text plus metadata; never reinterpret its authority."""
        event_id = _single_line(event_id, "event_id")
        ledger = self._ledger()
        entry = ledger["events"].get(event_id)
        if entry is not None:
            result = self._read_path(self._entry_path(entry), entry["sha256"])
            if result["event_id"] != event_id:
                raise MemoryIntegrityError("event lookup resolves to a different event")
            return result
        matches = [event for path in self._event_paths() if (event := self._read_path(path))["event_id"] == event_id]
        if len(matches) > 1:
            raise MemoryIntegrityError("duplicate immutable event ID")
        if not matches:
            raise KeyError(event_id)
        return matches[0]

    @staticmethod
    def _resume_bounds(data: bytes) -> tuple[int, int] | None:
        starts, ends = data.count(RESUME_START), data.count(RESUME_END)
        if starts == ends == 0:
            return None
        if starts != 1 or ends != 1:
            raise MemoryIntegrityError("handoff must have zero or exactly one complete HELIX resume block")
        start, end = data.index(RESUME_START), data.index(RESUME_END) + len(RESUME_END)
        if end - len(RESUME_END) < start:
            raise MemoryIntegrityError("HELIX resume markers are out of order")
        return start, end

    @staticmethod
    def _resume_state(state: Any) -> dict[str, Any]:
        state = _json_copy(state)
        if not isinstance(state, dict):
            raise ValueError("resume state must be a JSON object")
        _single_line(state.get("task_id"), "task_id")
        if not isinstance(state.get("goal"), str) or not state["goal"].strip():
            raise ValueError("resume state requires a nonempty goal")
        for field in ("last_verified_step", "next_step", "blocker", "last_event_id"):
            if field in state and state[field] is not None and not isinstance(state[field], str):
                raise ValueError(f"resume {field} must be a string or None")
        if "actor" in state:
            _actor(state["actor"])
        if "event_ids" in state:
            state["event_ids"] = _id_list(state["event_ids"], "event_ids")
        return state

    @classmethod
    def _parse_resume(cls, data: bytes) -> dict[str, Any] | None:
        bounds = cls._resume_bounds(data)
        if bounds is None:
            return None
        block = data[bounds[0]:bounds[1]].decode("utf-8")
        # Only this module's JSON script is read; no script is ever executed.
        scripts = re.findall(r"<script\b(?=[^>]*\bid\s*=\s*[\"']helix-resume[\"'])(?=[^>]*\btype\s*=\s*[\"']application/json[\"'])[^>]*>(.*?)</script\s*>", block, re.IGNORECASE | re.DOTALL)
        if len(scripts) != 1:
            raise MemoryIntegrityError("HELIX resume block must contain exactly one JSON state script")
        try:
            value = json.loads(scripts[0])
            if value.get("schema_version") != 1 or type(value.get("revision")) is not int or value["revision"] < 1:
                raise ValueError("invalid resume revision/schema")
            cls._resume_state(value["state"])
            _timestamp(value["updated_at"])
            _id_list(value["event_ids"], "event_ids")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise MemoryIntegrityError("invalid HELIX resume JSON") from exc
        return value

    def read_resume(self, handoff_path: str | os.PathLike[str]) -> dict[str, Any] | None:
        """Read the resume state from the existing authoritative HTML handoff."""
        path = Path(handoff_path).expanduser().resolve(strict=True)
        return self._parse_resume(path.read_bytes())

    @staticmethod
    def _resume_block(value: dict[str, Any]) -> bytes:
        state = value["state"]
        fields = (("Task", "task_id"), ("Goal", "goal"), ("Last verified step", "last_verified_step"), ("Evidence", "evidence"), ("Next step", "next_step"), ("Blocker", "blocker"), ("Pending approval", "pending_approval"))
        rows = []
        for label, key in fields:
            content = state.get(key)
            if content is None:
                content = "Not recorded"
            elif not isinstance(content, str):
                content = canonical_json(content)
            rows.append(f"<dt>{label}</dt><dd>{html.escape(content, quote=True)}</dd>")
        safe_json = canonical_json(value).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        return (RESUME_START.decode() + '\n<section id="helix-resume-state" aria-label="Current task resume">\n<h2>Current task resume</h2>\n<dl>' + "".join(rows) + '</dl>\n<script type="application/json" id="helix-resume">' + safe_json + '</script>\n</section>\n' + RESUME_END.decode()).encode("utf-8")

    def update_resume(self, handoff_path: str | os.PathLike[str], state: dict[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
        """Update only our marked block, preserving the original handoff bytes.

        The caller must pass an existing handoff, not a new filename. Every prior
        full file is retained as a content-addressed blob before a mutable write.
        Pass ``expected_revision=0`` when the file has no resume block yet.
        """
        state = self._resume_state(state)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("expected_revision must be a nonnegative integer")
        path = Path(handoff_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("handoff_path must be an existing file")
        self._prepare()
        # Adjacent lock coordinates writers even if their memory roots differ.
        lock = path.parent / (".helix-resume-" + _hash_bytes(str(path).encode())[:20] + ".lock")
        with file_lock(lock, timeout=self.lock_timeout):
            previous = path.read_bytes()
            old = self._parse_resume(previous)
            revision = old["revision"] if old else 0
            if expected_revision is not None and revision != expected_revision:
                raise RevisionConflict(f"expected handoff revision {expected_revision}, found {revision}")
            event_ids = list(state.get("event_ids", []))
            if state.get("last_event_id") and state["last_event_id"] not in event_ids:
                event_ids.append(state["last_event_id"])
            value = {"schema_version": 1, "revision": revision + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"), "event_ids": event_ids, "state": state}
            block = self._resume_block(value)
            bounds = self._resume_bounds(previous)
            if bounds:
                current = previous[:bounds[0]] + block + previous[bounds[1]:]
            else:
                closing = list(re.finditer(br"</body\s*>", previous, re.IGNORECASE))
                insert_at = closing[-1].start() if closing else len(previous)
                current = previous[:insert_at] + block + previous[insert_at:]
            history_dir = self.memory_dir / ".helix-history" / "handoffs"
            if not history_dir.resolve().is_relative_to(self.memory_dir):
                raise ValueError("handoff history resolves outside the memory directory")
            _mkdir_durable(history_dir)
            previous_hash = _hash_bytes(previous)
            history_path = history_dir / (previous_hash + ".blob")
            if history_path.exists():
                if history_path.read_bytes() != previous:
                    raise MemoryIntegrityError("handoff history blob failed integrity check")
            else:
                try:
                    _exclusive_write(history_path, previous)
                except FileExistsError:
                    # Two different handoffs may preserve identical bytes at once.
                    if history_path.read_bytes() != previous:
                        raise MemoryIntegrityError("concurrent handoff history blob disagrees")
            # Detect non-cooperating changes observed before atomic replacement.
            if path.read_bytes() != previous:
                raise RevisionConflict("handoff changed outside the resume lock; no update written")
            atomic_write_bytes(path, current)
            readback = path.read_bytes()
            if readback != current or self._parse_resume(readback) != value:
                raise MemoryIntegrityError("handoff update failed readback")
            return {"path": str(path), "revision": value["revision"], "sha256": _hash_bytes(readback), "previous_sha256": previous_hash, "history_path": str(history_path), "event_ids": event_ids, "durable": True, "readback_verified": True}
