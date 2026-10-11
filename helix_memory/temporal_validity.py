"""
Bi-temporal validity windows for HELIX memories (adapted from Graphiti/Zep).

Every memory gets a validity window:
  - valid_from:  when the fact became true (doc date from qpulse path)
  - valid_until: when it was superseded (None = still current)

When a new memory contradicts or supersedes an old one, we CLOSE the old
window instead of just appending. Queries default to as_of=now, so
corrections automatically hide superseded facts — "latest truth" becomes a
first-class query instead of a recency heuristic.

Supersede detection (heuristic, no LLM):
  A new doc supersedes older docs when ALL of:
    1. same HELIX division,
    2. shares 2+ entities with the older doc,
    3. its text contains a correction keyword
       ("corrected", "supersedes", "replaces", "updated", "was wrong", ...)

This matches our correction-heavy workflow: "IX corrected X on Oct 10"
closes the window on the earlier (now-wrong) version of X.

Storage: <index_dir>/validity.json
  {doc_id: {"valid_from": "2026-10-10"|null,
            "valid_until": "2026-10-12"|null,
            "superseded_by": doc_id|null}}
"""

import json
import os
from datetime import date, datetime

INDEX_DIR = os.path.expanduser("~/.helix-memory")
VALIDITY_PATH = os.path.join(INDEX_DIR, "validity.json")

# Correction keywords: a doc containing one of these (case-insensitive)
# is a candidate for superseding older memories it overlaps with.
CORRECTION_KEYWORDS = [
    "corrected",
    "correction",
    "supersedes",
    "superseded",
    "supersede",
    "replaces",
    "replaced",
    "replacement",
    "updated",
    "update:",
    "was wrong",
    "were wrong",
    "is wrong",
    "fix:",
    "fixed",
    "amended",
    "amendment",
    "retracted",
    "retraction",
    "override",
    "overrides",
    "overridden",
    "no longer",
    "instead of",
    "disregard",
]


def _has_correction_signal(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in CORRECTION_KEYWORDS)


def _as_date(value) -> date | None:
    """Normalize date/datetime/ISO-string/None to a date or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def build_validity_index(docs: list[dict]) -> dict[str, dict]:
    """Build the validity map from a list of document dicts.

    Each doc: {"id": str, "division": str, "text": str, "date": date|None}.
    Returns: {doc_id: {"valid_from": iso|None, "valid_until": iso|None,
                       "superseded_by": doc_id|None}}.

    Algorithm:
      1. Group docs by division, sort chronologically (undated first).
      2. Maintain an entity -> set(doc_id) inverted index of docs seen so far.
      3. For each doc with a correction signal, find older docs in the same
         division sharing 2+ entities. Those get their window closed at this
         doc's valid_from.
    """
    from .entity_graph import extract_entities

    validity: dict[str, dict] = {}

    # Group by division
    by_div: dict[str, list[dict]] = {}
    for d in docs:
        by_div.setdefault(d.get("division", ""), []).append(d)

    for division, dlist in by_div.items():
        # Chronological order. Undated docs sort last and stay always-valid:
        # without a date we can neither close their window at a known point
        # nor let them close others, so the conservative choice is to keep them.
        def sort_key(d):
            dt = _as_date(d.get("date"))
            return (dt is None, dt or date.min)

        ordered = sorted(dlist, key=sort_key)

        # doc_id -> set of entities (lowercased for matching)
        doc_entities: dict[str, set[str]] = {}
        # entity -> set of doc_ids already processed (older docs)
        entity_index: dict[str, set[str]] = {}
        # doc_id -> date
        doc_date: dict[str, date | None] = {}

        for d in ordered:
            doc_id = d["id"]
            dt = _as_date(d.get("date"))
            doc_date[doc_id] = dt
            validity[doc_id] = {
                "valid_from": dt.isoformat() if dt else None,
                "valid_until": None,
                "superseded_by": None,
            }
            ents = {e.lower() for e in extract_entities(d.get("text", ""))}
            doc_entities[doc_id] = ents

            # Supersede check: only docs WITH a date can close windows
            # (we need a valid_from to close the old window at).
            if dt is not None and _has_correction_signal(d.get("text", "")):
                # Candidate older docs: share at least one entity
                candidates: dict[str, int] = {}
                for e in ents:
                    for older_id in entity_index.get(e, ()): 
                        candidates[older_id] = candidates.get(older_id, 0) + 1
                for older_id, shared in candidates.items():
                    if shared >= 2:
                        old = validity[older_id]
                        # Don't re-close an already-closed window; keep the
                        # earliest supersession (first correction wins).
                        if old["valid_until"] is None:
                            old["valid_until"] = dt.isoformat()
                            old["superseded_by"] = doc_id

            # Index this doc for future (newer) docs
            for e in ents:
                entity_index.setdefault(e, set()).add(doc_id)

    return validity


def save_validity(validity: dict[str, dict], path: str = VALIDITY_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(validity, f)


def load_validity(path: str = VALIDITY_PATH) -> dict[str, dict]:
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return json.load(f)


def is_valid(entry: dict, as_of: date | datetime | str | None) -> bool:
    """True if the validity entry covers the as_of timestamp.

    as_of=None means "now". Undated valid_from (None) means always-valid
    from the beginning of time.
    """
    moment = _as_date(as_of) or date.today()
    vf = _as_date(entry.get("valid_from"))
    vu = _as_date(entry.get("valid_until"))
    if vf is not None and moment < vf:
        return False
    if vu is not None and moment >= vu:
        return False
    return True


def filter_valid(
    doc_ids: list[str],
    validity: dict[str, dict],
    as_of: date | datetime | str | None = None,
) -> list[str]:
    """Keep only doc_ids whose validity window covers as_of (default now)."""
    return [d for d in doc_ids if is_valid(validity.get(d, {}), as_of)]


def validity_for(doc_id: str, validity: dict[str, dict]) -> dict:
    """Return the validity entry for a doc (defaults to always-valid)."""
    return validity.get(
        doc_id, {"valid_from": None, "valid_until": None, "superseded_by": None}
    )
