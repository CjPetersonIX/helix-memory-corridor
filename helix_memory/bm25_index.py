"""
BM25 keyword index over HELIX documents. Pure local, no network.

Uses rank-bm25. Index is rebuilt on demand and cached as JSON
(tokenized corpus + doc ids) so rebuilds are only needed when
the underlying files change.
"""

import json
import os
import re

from rank_bm25 import BM25Okapi

from .collector import collect_documents
from .fusion import RetrievalResult

INDEX_DIR = os.path.expanduser("~/.helix-memory")
BM25_CACHE = os.path.join(INDEX_DIR, "bm25-index.json")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, 2+ chars."""
    return re.findall(r"[a-z0-9][a-z0-9\-]*", text.lower())


class BM25Index:
    def __init__(self) -> None:
        self.doc_ids: list[str] = []
        self.doc_texts: dict[str, str] = {}
        self.doc_divisions: dict[str, str] = {}
        self._bm25: BM25Okapi | None = None
        self._tokenized: list[list[str]] = []

    def build(self, divisions: list[str] | None = None, **collect_kw) -> "BM25Index":
        docs = collect_documents(divisions=divisions, **collect_kw)
        self.doc_ids = [d["id"] for d in docs]
        self.doc_texts = {d["id"]: d["text"] for d in docs}
        self.doc_divisions = {d["id"]: d["division"] for d in docs}
        self._tokenized = [tokenize(d["text"]) for d in docs]
        self._bm25 = BM25Okapi(self._tokenized)
        return self

    def search(
        self,
        query: str,
        top_n: int = 20,
        division: str | None = None,
    ) -> list[RetrievalResult]:
        if self._bm25 is None:
            raise RuntimeError("BM25Index not built; call build() first")
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(
            enumerate(scores), key=lambda x: x[1], reverse=True
        )[: top_n * 3]  # oversample for division filter
        out: list[RetrievalResult] = []
        for idx, s in ranked:
            if s <= 0:
                continue
            doc_id = self.doc_ids[idx]
            if division and self.doc_divisions[doc_id] != division:
                continue
            text = self.doc_texts[doc_id]
            out.append(
                RetrievalResult(
                    id=doc_id,
                    text=text[:1200],
                    score=float(s),
                    source="bm25",
                    metadata={"division": self.doc_divisions[doc_id]},
                )
            )
            if len(out) >= top_n:
                break
        return out

    # ---- persistence ----

    def save(self, path: str = BM25_CACHE) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(
                {
                    "doc_ids": self.doc_ids,
                    "tokenized": self._tokenized,
                    "divisions": [self.doc_divisions[i] for i in self.doc_ids],
                },
                f,
            )
        # texts stay on disk (source files); reloaded via collector on load

    @classmethod
    def load(cls, path: str = BM25_CACHE, **collect_kw) -> "BM25Index":
        idx = cls()
        with open(path) as f:
            data = json.load(f)
        idx.doc_ids = data["doc_ids"]
        idx._tokenized = data["tokenized"]
        idx.doc_divisions = dict(zip(data["doc_ids"], data["divisions"]))
        # re-read current file contents for text
        docs = {d["id"]: d["text"] for d in collect_documents(**collect_kw)}
        idx.doc_texts = {
            i: docs.get(i, "") for i in idx.doc_ids
        }
        idx._bm25 = BM25Okapi(idx._tokenized)
        return idx

    @classmethod
    def load_or_build(
        cls, path: str = BM25_CACHE, divisions: list[str] | None = None, **collect_kw
    ) -> "BM25Index":
        if os.path.isfile(path):
            try:
                return cls.load(path, divisions=divisions, **collect_kw)
            except Exception:
                pass
        return cls().build(divisions=divisions, **collect_kw)
