"""Local lexical retrieval with content-verified, corpus-scoped caches.

Caches contain a complete document snapshot. Loading never combines old token
scores with newly read text. load_or_build verifies the current corpus on every
call, including edits that preserve file size and modification time.
"""

import hashlib
import json
import os
import re

from rank_bm25 import BM25Okapi

from ._io import atomic_write_json
from .collector import collect_documents
from .fusion import RetrievalResult

INDEX_DIR = os.path.expanduser("~/.helix-memory")
BM25_CACHE = os.path.join(INDEX_DIR, "bm25-index.json")  # legacy explicit path
CACHE_VERSION = 2


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9\-]*", text.lower())


def corpus_scope(memory_dir=None, categories=None, recursive=True, **aliases):
    """Canonical scope shared by keyword and semantic cache manifests."""
    from .collector import normalize_categories

    chosen = normalize_categories(categories=categories, **aliases)
    return {
        "memory_dir": os.path.realpath(os.path.expanduser(memory_dir or "~/memory")),
        "categories": sorted(chosen) if chosen is not None else None,
        "recursive": bool(recursive),
    }


def normalize_documents(documents):
    """Keep full source and provenance; accept legacy document dictionaries."""
    docs = []
    seen = set()
    for value in documents:
        # Collector values are JSON primitives. Historical callers may provide
        # date/Path metadata; normalize it consistently for manifest persistence.
        d = json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))
        if d["id"] in seen:
            raise ValueError(f"Duplicate document id: {d['id']}")
        seen.add(d["id"])
        d["category"] = d.get("category", d.get("division", "uncategorized"))
        d["division"] = d["category"]
        d["text"] = d.get("text", "")
        docs.append(d)
    return sorted(docs, key=lambda d: d["id"])


def corpus_fingerprint(documents, scope):
    payload = {"scope": scope, "documents": normalize_documents(documents)}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _default_cache_path(scope):
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:24]
    return os.path.join(INDEX_DIR, f"bm25-{digest}.json")


class BM25Index:
    def __init__(self):
        self.doc_ids = []
        self.doc_texts = {}
        self.doc_divisions = {}
        self.doc_metadata = {}
        self.documents = []
        self.scope = None
        self.fingerprint = None
        self.cache_path = None
        self._bm25 = None
        self._tokenized = []
        self._built = False

    def build(self, divisions=None, *, categories=None, documents=None,
              memory_dir=None, recursive=True, category=None, division=None):
        self.scope = corpus_scope(memory_dir, categories, recursive,
                                  divisions=divisions, category=category, division=division)
        if documents is None:
            documents = collect_documents(memory_dir=memory_dir,
                                          categories=self.scope["categories"], recursive=recursive)
        self.documents = normalize_documents(documents)
        self.doc_ids = [d["id"] for d in self.documents]
        self.doc_texts = {d["id"]: d["text"] for d in self.documents}
        self.doc_divisions = {d["id"]: d["category"] for d in self.documents}
        self.doc_metadata = {
            d["id"]: {k: v for k, v in d.items() if k not in {"id", "text"}}
            for d in self.documents
        }
        self._tokenized = [tokenize(d["text"]) for d in self.documents]
        # rank-bm25 divides by zero for an empty/all-empty-token corpus.
        self._bm25 = BM25Okapi(self._tokenized) if any(self._tokenized) else None
        self.fingerprint = corpus_fingerprint(self.documents, self.scope)
        self.cache_path = _default_cache_path(self.scope)
        self._built = True
        return self

    def search(self, query, top_n=20, division=None, category=None):
        if not self._built:
            raise RuntimeError("BM25Index not built; call build() first")
        if division is not None and category is not None and division != category:
            raise ValueError("division and category specify conflicting filters")
        selected = category if category is not None else division
        terms = set(tokenize(query))
        if not terms or top_n <= 0 or self._bm25 is None:
            return []
        raw_scores = self._bm25.get_scores(list(terms))
        ranked = []
        for index, raw in enumerate(raw_scores):
            doc_id = self.doc_ids[index]
            if selected is not None and self.doc_divisions[doc_id] != selected:
                continue
            overlap = terms.intersection(self._tokenized[index])
            if not overlap:
                continue
            # BM25 IDF can be zero/negative for tiny corpora or common terms.
            # Exact lexical matches must still be retrievable in those cases.
            score = float(raw) if raw > 0 else len(overlap) / (len(terms) * (1 + len(self._tokenized[index])))
            ranked.append((score, doc_id))
        ranked.sort(key=lambda hit: (-hit[0], hit[1]))
        return [RetrievalResult(id=doc_id, text=self.doc_texts[doc_id], score=score,
                                source="bm25", metadata=dict(self.doc_metadata[doc_id]))
                for score, doc_id in ranked[:top_n]]

    def save(self, path=None):
        if not self._built:
            raise RuntimeError("Cannot save an unbuilt index")
        path = os.fspath(path or self.cache_path)
        atomic_write_json(path, {"version": CACHE_VERSION, "scope": self.scope,
                                 "fingerprint": self.fingerprint, "documents": self.documents})
        self.cache_path = path

    @classmethod
    def load(cls, path=None, **collect_kw):
        if path is None:
            options = {key: value for key, value in collect_kw.items() if key != "documents"}
            path = _default_cache_path(corpus_scope(**options))
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        if data.get("version") != CACHE_VERSION:
            raise ValueError("Legacy index lacks a source manifest; rebuild required")
        saved_scope = data["scope"]
        saved_docs = normalize_documents(data["documents"])
        if corpus_fingerprint(saved_docs, saved_scope) != data["fingerprint"]:
            raise ValueError("Index manifest integrity check failed")
        if collect_kw:
            options = dict(collect_kw)
            provided = options.pop("documents", None)
            scope = corpus_scope(**options)
            current = provided if provided is not None else collect_documents(**options)
            if corpus_fingerprint(current, scope) != data["fingerprint"]:
                raise ValueError("Memory corpus changed; rebuild required")
        idx = cls().build(documents=saved_docs, **saved_scope)
        idx.cache_path = os.fspath(path)
        return idx

    @classmethod
    def load_or_build(cls, path=None, divisions=None, *, categories=None,
                      memory_dir=None, recursive=True, category=None, division=None,
                      documents=None):
        scope = corpus_scope(memory_dir, categories, recursive, divisions=divisions,
                             category=category, division=division)
        path = os.fspath(path or _default_cache_path(scope))
        docs = documents if documents is not None else collect_documents(**scope)
        try:
            return cls.load(path, documents=docs, **scope)
        except (OSError, ValueError, TypeError, KeyError):
            idx = cls().build(documents=docs, **scope)
            idx.save(path)
            return idx
