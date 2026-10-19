"""Explicit, timezone-aware validity for preserved HELIX memory events.

Only an explicit ``supersedes`` event ID closes an earlier validity window.
Entity overlap and words such as "updated" never establish that authority.
``recorded_at`` describes when a record entered memory; ``valid_from`` is when
its claim became effective. ``known_at`` optionally reconstructs the knowledge
available at a particular moment. Authored by [R2] SRVP2-CODEX.
"""

import json
import os
import tempfile
from datetime import date, datetime, timezone

from .temporal_filter import normalize_timestamp

INDEX_DIR = os.path.expanduser("~/.helix-memory")
VALIDITY_PATH = os.path.join(INDEX_DIR, "validity.json")


def _as_date(value) -> date | None:
    """Legacy date view. Invalid input raises rather than meaning 'today'."""
    moment = normalize_timestamp(value)
    return moment.date() if moment else None


def _field(document: dict, key: str):
    value = document.get(key)
    if value is None:
        metadata = document.get("metadata") or {}
        value = metadata.get(key)
    return value


def _supersedes(document: dict) -> list[str]:
    value = _field(document, "supersedes") or []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or any(not isinstance(item, str) or not item for item in values):
        raise ValueError("supersedes must be an event ID or a list of event IDs")
    return list(dict.fromkeys(values))


def build_validity_index(docs: list[dict]) -> dict[str, dict]:
    """Build explicit validity windows without altering source documents.

    Unknown supersedes IDs remain unresolved references, never fuzzy matches.
    Each closing record retains its own recorded/effective timestamps, so a
    later-recorded correction cannot erase what was known before it arrived.
    """
    validity = {}
    identities = {}
    for document in docs:
        identifier = document["id"]
        if identifier in validity:
            raise ValueError(f"Duplicate document ID in validity index: {identifier}")
        effective = normalize_timestamp(_field(document, "valid_from"), field_name="valid_from")
        if effective is None:
            effective = normalize_timestamp(_field(document, "timestamp") or document.get("date"))
        recorded = normalize_timestamp(_field(document, "recorded_at"), field_name="recorded_at")
        if recorded is None:
            recorded = normalize_timestamp(_field(document, "timestamp") or document.get("date")) or effective
        validity[identifier] = {
            "valid_from": effective.isoformat() if effective else None,
            "recorded_at": recorded.isoformat() if recorded else None,
            "valid_until": None,
            "superseded_by": None,
            "supersessions": [],
        }
        if identifier in identities and identities[identifier] != identifier:
            raise ValueError(f"Document ID collides with event ID: {identifier}")
        identities[identifier] = identifier
        event_id = _field(document, "event_id")
        if event_id:
            if event_id in identities and identities[event_id] != identifier:
                raise ValueError(f"Duplicate event ID in validity index: {event_id}")
            identities[event_id] = identifier
    for document in docs:
        identifier = document["id"]
        closing = validity[identifier]
        effective = normalize_timestamp(closing["valid_from"])
        if effective is None:
            # Do not invent a point at which an undated correction took effect.
            continue
        for target in _supersedes(document):
            older_id = identities.get(target)
            if older_id is None or older_id == identifier:
                continue
            older = validity[older_id]
            beginning = normalize_timestamp(older["valid_from"])
            if beginning is not None and effective < beginning:
                raise ValueError(f"Supersession predates target {target}")
            older["supersessions"].append({
                "valid_from": closing["valid_from"],
                "recorded_at": closing["recorded_at"],
                "superseded_by": identifier,
            })
    for entry in validity.values():
        entry["supersessions"].sort(
            key=lambda item: (item["valid_from"], item["recorded_at"] or "", item["superseded_by"])
        )
        if entry["supersessions"]:
            earliest = entry["supersessions"][0]
            entry["valid_until"] = earliest["valid_from"]
            entry["superseded_by"] = earliest["superseded_by"]
    return validity


def save_validity(validity: dict[str, dict], path: str = VALIDITY_PATH) -> None:
    """Atomically replace the derived index, preserving immutable source events."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".validity-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(validity, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_validity(path: str = VALIDITY_PATH) -> dict[str, dict]:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def is_valid(entry: dict, as_of: date | datetime | str | None = None, *,
             known_at: date | datetime | str | None = None) -> bool:
    """Check a half-open [valid_from, valid_until) window at full precision.

    ``as_of=None`` means now in UTC. Optional ``known_at`` additionally excludes
    records and corrections not recorded by that timestamp. Invalid query
    timestamps always raise ValueError, including an otherwise empty entry.
    """
    moment = normalize_timestamp(as_of, field_name="as_of") or datetime.now(timezone.utc)
    knowledge = normalize_timestamp(known_at, field_name="known_at")
    beginning = normalize_timestamp(entry.get("valid_from"), field_name="valid_from")
    recorded = normalize_timestamp(entry.get("recorded_at"), field_name="recorded_at")
    if beginning is not None and moment < beginning:
        return False
    if knowledge is not None and recorded is not None and knowledge < recorded:
        return False
    supersessions = entry.get("supersessions")
    if supersessions is not None:
        for correction in supersessions:
            corrected_at = normalize_timestamp(correction.get("valid_from"))
            learned_at = normalize_timestamp(correction.get("recorded_at"))
            if knowledge is not None and learned_at is not None and knowledge < learned_at:
                continue
            if corrected_at is not None and moment >= corrected_at:
                return False
    else:
        # Older persisted explicit-window indexes remain readable. Callers must
        # rebuild legacy heuristic indexes before using authoritative recall.
        ending = normalize_timestamp(entry.get("valid_until"), field_name="valid_until")
        if ending is not None and moment >= ending:
            return False
    return True


def filter_valid(doc_ids: list[str], validity: dict[str, dict],
                 as_of: date | datetime | str | None = None, *,
                 known_at: date | datetime | str | None = None) -> list[str]:
    """Filter document IDs, validating query timestamps even for empty lists."""
    moment = normalize_timestamp(as_of, field_name="as_of") or datetime.now(timezone.utc)
    knowledge = normalize_timestamp(known_at, field_name="known_at")
    return [identifier for identifier in doc_ids
            if is_valid(validity.get(identifier, {}), moment, known_at=knowledge)]


def validity_for(doc_id: str, validity: dict[str, dict], *,
                 known_at: date | datetime | str | None = None) -> dict:
    """Return validity metadata, optionally projected to knowledge available then.

    A known-at query must not leak a later-recorded correction through returned
    ``superseded_by`` metadata even when its effective date is backdated.
    The persisted audit index is never changed by this projection.
    """
    entry = validity.get(doc_id, {
        "valid_from": None, "recorded_at": None, "valid_until": None,
        "superseded_by": None, "supersessions": [],
    })
    knowledge = normalize_timestamp(known_at, field_name="known_at")
    if knowledge is None or "supersessions" not in entry:
        return entry
    result = dict(entry)
    visible = [dict(correction) for correction in entry["supersessions"]
               if (normalize_timestamp(correction.get("recorded_at")) is None
                   or normalize_timestamp(correction.get("recorded_at")) <= knowledge)]
    result["supersessions"] = visible
    result["valid_until"] = visible[0]["valid_from"] if visible else None
    result["superseded_by"] = visible[0]["superseded_by"] if visible else None
    return result
