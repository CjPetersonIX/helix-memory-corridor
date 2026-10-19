"""Collect lossless Markdown memory records from a bounded directory tree.

Root Markdown and category folders are included by default. ``category`` and
``division`` are compatibility names for the same retrieval scope; neither is
an agent's authority rank or a business-division assignment. First-line records
of the form ``<!-- helix-event: {JSON} -->`` carry explicit immutable event IDs
and timestamps. Plain Markdown remains supported. Authored by [R2] SRVP2-CODEX.
"""

import hashlib
import json
import os
from pathlib import Path
import re

from .temporal_filter import normalize_timestamp, timestamp_from_path

DIVISIONS: list[str] = []


def normalize_categories(categories=None, divisions=None, category=None, division=None):
    """Resolve singular/plural scope aliases, rejecting contradictory scopes.

    None means auto-detect; an explicit empty list means collect nothing.
    Scopes must be relative paths contained by the memory root.
    """
    supplied = []
    for name, value in (("categories", categories), ("divisions", divisions),
                        ("category", category), ("division", division)):
        if value is None:
            continue
        values = [value] if isinstance(value, str) else list(value)
        normalized = []
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{name} must contain non-empty relative paths")
            candidate = Path(item)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError(f"{name} cannot escape the memory root")
            clean = candidate.as_posix()
            if clean not in normalized:
                normalized.append(clean)
        supplied.append(normalized)
    if not supplied:
        return None
    if any(set(scope) != set(supplied[0]) for scope in supplied[1:]):
        raise ValueError("Conflicting category/division scopes")
    return supplied[0]


def _detect_divisions(memory_dir: str | None = None) -> list[str]:
    """Detect safe, visible immediate category directories."""
    root = Path(memory_dir or "~/memory").expanduser().resolve()
    if not root.is_dir():
        return []
    return [p.name for p in sorted(root.iterdir())
            if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")]


def doc_count_by_division(docs: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for document in docs:
        scope = document.get("category", document.get("division", "uncategorized"))
        counts[scope] = counts.get(scope, 0) + 1
    return counts


def _anchor(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "section"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def _split_sections(path: str, text: str) -> list[tuple[str, str]]:
    """Stable heading anchors instead of ordinal IDs; keep every source byte.

    Duplicate headings use their full section content as the disambiguator.
    Inserting an unrelated section cannot change an existing section's ID.
    Small files remain whole with a stable path ID.
    """
    matches = list(re.finditer(r"(?m)^## +([^\n]*)", text))
    if not matches or len(text) < 1500:
        return [(path, text)]
    headings = [match.group(1).strip() for match in matches]
    units = []
    if matches[0].start():
        units.append((f"{path}#preamble", text[:matches[0].start()]))
    for offset, match in enumerate(matches):
        end = matches[offset + 1].start() if offset + 1 < len(matches) else len(text)
        content = text[match.start():end]
        heading = match.group(1).strip()
        anchor = _anchor(heading if headings.count(heading) == 1 else content)
        units.append((f"{path}#{anchor}", content))
    return units


def _event_metadata(text: str, source: Path) -> dict:
    first = text.split("\n", 1)[0].rstrip("\r") if text else ""
    if not first.startswith("<!-- helix-event:"):
        return {}
    match = re.fullmatch(r"<!-- helix-event:\s*(\{.*\})\s*-->", first)
    if not match:
        raise ValueError(f"Malformed helix-event metadata in {source}")
    try:
        metadata = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed helix-event JSON in {source}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"helix-event metadata must be an object in {source}")
    if "schema_version" in metadata or "payload_sha256" in metadata:
        # A recognized immutable-store record must pass the same integrity
        # checks as read_event. Never downgrade malformed store records into
        # unchecked legacy Markdown during collection or recall.
        from .memory_store import validate_event_text

        validated = validate_event_text(text)
        metadata = {key: value for key, value in validated.items() if key != "text"}
    event_id = metadata.get("event_id")
    if event_id is not None and (not isinstance(event_id, str) or not event_id.strip()):
        raise ValueError(f"event_id must be a non-empty string in {source}")
    supersedes = metadata.get("supersedes", [])
    targets = [supersedes] if isinstance(supersedes, str) else supersedes
    if not isinstance(targets, list) or any(not isinstance(x, str) or not x for x in targets):
        raise ValueError(f"supersedes must contain explicit event IDs in {source}")
    return metadata


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def collect_documents(memory_dir=None, categories=None, recursive=True, *,
                      divisions=None, category=None, division=None) -> list[dict]:
    """Collect full Markdown chunks with stable IDs and provenance.

    Symlink directories are never traversed, and files resolving outside the
    memory root are never read. Explicit path traversal scopes are rejected.
    Legacy filename timestamps have a documented UTC fallback; no timestamp is
    invented from file modification time. Malformed event/time metadata raises
    a visible error instead of silently omitting memories.
    """
    scopes = normalize_categories(categories, divisions, category, division)
    root = Path(memory_dir or "~/memory").expanduser().resolve()
    if not root.is_dir():
        return []
    selected = (["."] if recursive else [".", *_detect_divisions(str(root))]) if scopes is None else scopes
    docs = []
    seen_paths = set()
    seen_ids = {}
    for scope in selected:
        scope_root = root / scope
        if not _inside(scope_root, root):
            raise ValueError("Category path resolves outside the memory root")
        if not scope_root.is_dir() or scope_root.is_symlink():
            continue
        walker = os.walk(scope_root, followlinks=False) if recursive else [
            (str(scope_root), [], sorted(os.listdir(scope_root)))
        ]
        for directory, directories, filenames in walker:
            directories[:] = sorted(name for name in directories
                                    if not name.startswith(".")
                                    and not (Path(directory) / name).is_symlink())
            for filename in sorted(filenames):
                source = Path(directory) / filename
                if filename.startswith(".") or source.suffix.lower() != ".md":
                    continue
                if not _inside(source, root) or not source.is_file():
                    continue
                canonical = source.resolve()
                if canonical in seen_paths:
                    continue
                seen_paths.add(canonical)
                try:
                    # read_text uses universal-newline translation. Immutable
                    # body hashes cover the exact LF/CRLF text originally saved.
                    text = source.read_bytes().decode("utf-8")
                except OSError as exc:
                    raise OSError(f"Cannot read memory source: {source}") from exc
                if not text.strip():
                    continue
                relative = source.relative_to(root).as_posix()
                actual_scope = relative.split("/", 1)[0] if "/" in relative else "."
                if scope != ".":
                    actual_scope = scope
                metadata = _event_metadata(text, source)
                supplied_time = normalize_timestamp(metadata.get("timestamp"))
                recorded_time = normalize_timestamp(metadata.get("recorded_at"), field_name="recorded_at")
                effective_time = normalize_timestamp(metadata.get("valid_from"), field_name="valid_from")
                # Explicit event metadata takes precedence over legacy filename dates.
                base_time = supplied_time or recorded_time or effective_time or timestamp_from_path(source)
                recorded = recorded_time or base_time
                valid_from = effective_time or base_time or recorded
                timestamp = base_time or recorded or valid_from
                # Events are indivisible; splitting would make supersedes IDs ambiguous.
                chunks = [(metadata["event_id"], text)] if metadata.get("event_id") else _split_sections(relative, text)
                for identifier, content in chunks:
                    if identifier in seen_ids:
                        if seen_ids[identifier] != (str(source), content):
                            raise ValueError(f"Duplicate memory/event ID: {identifier}")
                        continue
                    seen_ids[identifier] = (str(source), content)
                    docs.append({
                        "id": identifier,
                        "event_id": metadata.get("event_id"),
                        "category": actual_scope,
                        "division": actual_scope,
                        "text": content,
                        "source_path": str(source),
                        "metadata": metadata,
                        "timestamp": timestamp.isoformat() if timestamp else None,
                        "recorded_at": recorded.isoformat() if recorded else None,
                        "valid_from": valid_from.isoformat() if valid_from else None,
                        "supersedes": metadata.get("supersedes", []),
                        "task_id": metadata.get("task_id"),
                        "date": timestamp.date().isoformat() if timestamp else None,
                    })
    return docs
