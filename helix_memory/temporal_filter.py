"""
Temporal filter: extract date references from queries and filter/index by date.

Two jobs:
1. At index time: normalize relative date expressions in qpulse filenames/content
   ("yesterday" -> absolute date) so temporal retrieval works on clean dates.
2. At query time: detect temporal constraints in the query ("last week", "in
   October", "since Monday") and return a (start, end) date range to filter
   candidates, plus a recency boost signal.
"""

import re
from datetime import date, datetime, timedelta

try:
    import dateparser

    _HAS_DATEPARSER = True
except ImportError:
    _HAS_DATEPARSER = False


# Matches explicit dates like 2026-10-10, 10/10/2026, Oct 10 2026, October 10th
_EXPLICIT_DATE_RES = [
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),  # 2026-10-10
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),  # 10/10/2026
    re.compile(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
        re.IGNORECASE,
    ),  # Oct 10 2026
]

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_RELATIVE_RES = [
    (re.compile(r"\btoday\b", re.IGNORECASE), 0, 0),
    (re.compile(r"\byesterday\b", re.IGNORECASE), -1, -1),
    (re.compile(r"\blast\s+week\b", re.IGNORECASE), -7, 0),
    (re.compile(r"\bthis\s+week\b", re.IGNORECASE), -6, 0),
    (re.compile(r"\blast\s+month\b", re.IGNORECASE), -30, 0),
    (re.compile(r"\bthis\s+month\b", re.IGNORECASE), -29, 0),
    (re.compile(r"\blast\s+(\d+)\s+days?\b", re.IGNORECASE), None, 0),  # group 1 = n
    (re.compile(r"\bpast\s+(\d+)\s+days?\b", re.IGNORECASE), None, 0),
    (re.compile(r"\bsince\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE), None, 0),
]

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def extract_explicit_dates(text: str) -> list[date]:
    """Pull explicit calendar dates out of text."""
    found: list[date] = []
    m = _EXPLICIT_DATE_RES[0].search(text)
    if m:
        try:
            found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    m = _EXPLICIT_DATE_RES[1].search(text)
    if m:
        try:
            found.append(date(int(m.group(3)), int(m.group(1)), int(m.group(2))))
        except ValueError:
            pass
    m = _EXPLICIT_DATE_RES[2].search(text)
    if m:
        try:
            month = _MONTH_MAP[m.group(1).lower()[:3]]
            day = int(m.group(2))
            year = int(m.group(3)) if m.group(3) else date.today().year
            found.append(date(year, month, day))
        except (ValueError, KeyError):
            pass
    return found


def extract_temporal_window(
    query: str, reference: date | None = None
) -> tuple[date, date] | None:
    """Detect a temporal constraint in a query.

    Returns (start_date, end_date) inclusive, or None if no temporal
    constraint was detected.
    """
    ref = reference or date.today()
    q = query.lower()

    # "since <weekday>" -> that weekday .. today
    m = re.search(
        r"\bsince\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", q
    )
    if m:
        target = _WEEKDAYS[m.group(1)]
        delta = (ref.weekday() - target) % 7
        start = ref - timedelta(days=delta)
        return (start, ref)

    # "last N days" / "past N days"
    m = re.search(r"\b(?:last|past)\s+(\d+)\s+days?\b", q)
    if m:
        n = int(m.group(1))
        return (ref - timedelta(days=n), ref)

    for pattern, start_off, end_off in _RELATIVE_RES:
        if start_off is None:  # handled above with groups
            continue
        if pattern.search(q):
            return (
                ref + timedelta(days=start_off),
                ref + timedelta(days=end_off),
            )

    # dateparser fallback for anything else ("in October", "last Tuesday", ...)
    if _HAS_DATEPARSER:
        parsed = dateparser.parse(
            query, settings={"RELATIVE_BASE": datetime.combine(ref, datetime.min.time())}
        )
        if parsed:
            d = parsed.date()
            # If the query names a month without a year, treat as that month
            if re.search(r"\b(in|during)\s+(month\s+of\s+)?[a-z]+\b", q) and not re.search(
                r"\b\d{4}\b", q
            ):
                start = d.replace(day=1)
                if start.month == 12:
                    end = start.replace(year=start.year + 1, month=1) - timedelta(days=1)
                else:
                    end = start.replace(month=start.month + 1) - timedelta(days=1)
                return (start, end)
            return (d, d)

    explicit = extract_explicit_dates(query)
    if explicit:
        d = explicit[0]
        return (d, d)

    return None


def date_from_qpulse_path(path: str) -> date | None:
    """Extract the date from a qpulse filename like 2026-10-10-1030-hourly.md."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", path)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def filter_by_window(
    doc_dates: dict[str, date | None],
    window: tuple[date, date] | None,
) -> dict[str, float]:
    """Score documents by temporal fit. Returns doc_id -> temporal score (0..1).

    Inside the window: 1.0. Outside: decays with distance in days.
    No window: every doc scores 1.0 (no temporal signal).
    Undated docs: 0.5 (neutral).
    """
    if window is None:
        return {doc_id: 1.0 for doc_id in doc_dates}
    start, end = window
    scores: dict[str, float] = {}
    for doc_id, d in doc_dates.items():
        if d is None:
            scores[doc_id] = 0.5
        elif start <= d <= end:
            scores[doc_id] = 1.0
        else:
            dist = min(abs((d - start).days), abs((d - end).days))
            scores[doc_id] = max(0.0, 1.0 - dist / 30.0)  # 30-day decay
    return scores
