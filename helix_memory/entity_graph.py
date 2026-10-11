"""
File-based entity graph per HELIX division.

No NLP models, no dependencies. Entities are extracted with a lightweight
heuristic tuned for qpulse/memory text:

- Multi-word capitalized phrases: "Project Alpha", "Team Beta", "System Gamma"
- ALL-CAPS tokens: "ADMIN", "AGENT-01", "MEMORY"
- Quoted phrases: "the morning brief"

Graph edges are co-occurrence links: two entities appearing in the same
document get an edge weighted by co-occurrence count. Graph retrieval walks
1-2 hops from query entities and returns linked documents.

Storage: one JSON file per division at <index_dir>/graph-<division>.json:
{
  "entities": {"Project Alpha": {"docs": ["path1", "path2"], "count": 5}, ...},
  "edges": {"Project Alpha": {"ADMIN": 3, "Jane Smith": 2}, ...}
}
"""

import json
import os
import re

# Capitalized words/phrases, 2+ chars, allowing internal hyphens/digits/caps runs
_PHRASE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*){0,3})\b"
)
_ALLCAPS_RE = re.compile(r"\b([A-Z][A-Z0-9-]{1,})\b")
_QUOTED_RE = re.compile(r'"([^"]{3,60})"')

# Noise: common sentence-start words and section headers to ignore
_STOP = {
    "The", "This", "That", "What", "When", "Where", "Which", "How", "Why",
    "From", "With", "After", "Before", "During", "Monday", "Tuesday",
    "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January",
    "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December", "Today", "Yesterday",
    "Morning", "Evening", "All", "No", "Yes", "Not", "For", "And",
}


def extract_entities(text: str, max_phrase_words: int = 4) -> list[str]:
    """Extract candidate entities from text. Returns deduplicated list."""
    found: list[str] = []

    for m in _PHRASE_RE.finditer(text):
        phrase = m.group(1).strip()
        words = phrase.split()
        if len(words) > max_phrase_words:
            continue
        if words[0] in _STOP and len(words) == 1:
            continue
        # skip pure section-header noise like "What happened"
        if all(w in _STOP or w[0].islower() for w in words):
            continue
        found.append(phrase)

    for m in _ALLCAPS_RE.finditer(text):
        tok = m.group(1)
        if tok not in _STOP:
            found.append(tok)

    for m in _QUOTED_RE.finditer(text):
        found.append(m.group(1).strip())

    # Deduplicate preserving order; normalize internal whitespace
    seen: set[str] = set()
    out: list[str] = []
    for e in found:
        e = re.sub(r"\s+", " ", e)
        key = e.lower()
        if key not in seen and len(e) >= 2:
            seen.add(key)
            out.append(e)
    return out


class EntityGraph:
    """Co-occurrence entity graph for one division (or all divisions)."""

    def __init__(self) -> None:
        # entity -> {"docs": set, "count": int}
        self.entities: dict[str, dict] = {}
        # entity -> {other_entity: cooccur_count}
        self.edges: dict[str, dict[str, int]] = {}

    def add_document(self, doc_id: str, text: str) -> None:
        ents = extract_entities(text)
        for e in ents:
            rec = self.entities.setdefault(e, {"docs": set(), "count": 0})
            rec["docs"].add(doc_id)
            rec["count"] += 1
        # co-occurrence edges (undirected)
        for i, a in enumerate(ents):
            for b in ents[i + 1 :]:
                if a == b:
                    continue
                self.edges.setdefault(a, {}).setdefault(b, 0)
                self.edges[a][b] += 1
                self.edges.setdefault(b, {}).setdefault(a, 0)
                self.edges[b][a] += 1

    def query_entities(self, query: str) -> list[str]:
        """Find graph entities matching the query (substring, case-insensitive)."""
        q = query.lower()
        scored: list[tuple[int, str]] = []
        for ent in self.entities:
            el = ent.lower()
            if q == el:
                scored.append((100, ent))
            elif q in el:
                scored.append((50 + len(q), ent))
            elif el in q and len(el) >= 3:
                scored.append((30 + len(el), ent))
        scored.sort(reverse=True)
        return [e for _, e in scored[:10]]

    def walk(
        self, seed_entities: list[str], hops: int = 2, max_docs: int = 50
    ) -> list[tuple[str, float]]:
        """Walk hops from seeds, return (doc_id, score) sorted by score desc.

        Score = sum over seeds of (edge weight product along path) *
        normalized entity doc frequency.
        """
        if hops < 1:
            hops = 1
        doc_scores: dict[str, float] = {}
        # BFS with decay
        frontier: dict[str, float] = {s: 1.0 for s in seed_entities if s in self.entities}
        visited: set[str] = set(seed_entities)
        for _ in range(hops):
            nxt: dict[str, float] = {}
            for ent, w in frontier.items():
                # docs containing this entity get its weight
                rec = self.entities.get(ent)
                if rec:
                    boost = w * (1.0 + 0.1 * min(rec["count"], 10))
                    for d in rec["docs"]:
                        doc_scores[d] = doc_scores.get(d, 0.0) + boost
                for nb, ew in self.edges.get(ent, {}).items():
                    if nb in visited:
                        continue
                    visited.add(nb)
                    nxt[nb] = nxt.get(nb, 0.0) + w * (0.5 + 0.1 * min(ew, 5))
            frontier = nxt
            if not frontier:
                break
        ranked = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:max_docs]

    # ---- persistence ----

    def to_networkx(self, _cache: dict | None = None):
        """Build (and optionally cache) a networkx graph for PPR ranking.

        Nodes: "e:<entity>" and "d:<doc_id>". See ppr_ranking.build_nx_graph.
        Pass a dict as _cache to memoize across calls: the graph is stored
        under key "nx" and reused.
        """
        if _cache is not None and "nx" in _cache:
            return _cache["nx"]
        from .ppr_ranking import build_nx_graph

        G = build_nx_graph(self)
        if _cache is not None:
            _cache["nx"] = G
        return G

    def to_dict(self) -> dict:
        return {
            "entities": {
                e: {"docs": sorted(r["docs"]), "count": r["count"]}
                for e, r in self.entities.items()
            },
            "edges": self.edges,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EntityGraph":
        g = cls()
        for e, r in data.get("entities", {}).items():
            g.entities[e] = {"docs": set(r["docs"]), "count": r["count"]}
        g.edges = data.get("edges", {})
        return g

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f)

    @classmethod
    def load(cls, path: str) -> "EntityGraph":
        with open(path) as f:
            return cls.from_dict(json.load(f))
