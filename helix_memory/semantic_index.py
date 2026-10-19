"""Optional semantic retrieval using immutable, corpus-specific generations.

A generation is activated only after every document has been indexed. Rebuilds
never clear an existing collection. Optional model/storage packages are imported
only when a nonempty semantic build or query is explicitly requested.
"""

import hashlib
import json
import os

from ._io import atomic_write_json
from .bm25_index import corpus_fingerprint, corpus_scope, normalize_documents
from .collector import collect_documents
from .fusion import RetrievalResult

INDEX_DIR = os.path.expanduser("~/.helix-memory")
CHROMA_DIR = os.path.join(INDEX_DIR, "chroma")
MODEL_NAME = "all-MiniLM-L6-v2"


class SemanticIndex:
    def __init__(self, model_name=MODEL_NAME, index_dir=None):
        self.model_name = model_name
        self.index_dir = os.fspath(index_dir or INDEX_DIR)
        self._model = None
        self._client = None
        self._client_path = None
        self._collections = {}
        self._documents = []
        self._by_id = {}
        self._source_options = None
        self._built = False
        self.scope = None
        self.fingerprint = None
        self.generation = None
        self.diagnostics = []

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _get_client(self):
        scope_id = hashlib.sha256(json.dumps(self.scope, sort_keys=True).encode()).hexdigest()[:24]
        path = os.path.join(self.index_dir, "chroma", scope_id)
        if self._client is None or (self._client_path is not None and self._client_path != path):
            import chromadb
            from chromadb.config import Settings

            os.makedirs(path, exist_ok=True)
            self._client = chromadb.PersistentClient(
                path=path, settings=Settings(anonymized_telemetry=False))
        self._client_path = path
        return self._client

    def _get_collection(self, category):
        """Return only a collection belonging to the current verified generation."""
        if not self.generation:
            raise RuntimeError("No verified semantic generation; call build() first")
        if category not in self._collections:
            suffix = hashlib.sha256(category.encode()).hexdigest()[:12]
            self._collections[category] = self._get_client().get_or_create_collection(
                name=f"hmc-{self.generation[:24]}-{suffix}",
                metadata={"hnsw:space": "cosine", "generation": self.generation,
                          "category": category, "model": self.model_name},
            )
        return self._collections[category]

    def build(self, divisions=None, batch_size=32, *, categories=None,
              documents=None, memory_dir=None, recursive=True, category=None, division=None):
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        scope = corpus_scope(memory_dir, categories, recursive, divisions=divisions,
                             category=category, division=division)
        source_options = scope if documents is None or memory_dir is not None else None
        docs = normalize_documents(documents if documents is not None else collect_documents(**scope))
        fingerprint = corpus_fingerprint(docs, scope)
        generation = hashlib.sha256(f"{self.model_name}:{fingerprint}".encode()).hexdigest()
        if self._built and generation == self.generation:
            self._source_options = source_options
            return self
        self._built = False
        self.scope = scope
        self.fingerprint = fingerprint
        self.generation = generation
        self._collections = {}
        self._documents = docs
        self._by_id = {d["id"]: d for d in docs}
        self._source_options = source_options
        by_category = {}
        for doc in docs:
            by_category.setdefault(doc["category"], []).append(doc)
        if docs:
            model = self._get_model()
            for cat, group in by_category.items():
                collection = self._get_collection(cat)
                for start in range(0, len(group), batch_size):
                    batch = group[start:start + batch_size]
                    texts = [d["text"] for d in batch]
                    embeddings = model.encode(texts, show_progress_bar=False)
                    vectors = [e.tolist() if hasattr(e, "tolist") else list(e) for e in embeddings]
                    collection.upsert(
                        ids=[d["id"] for d in batch], embeddings=vectors, documents=texts,
                        metadatas=[{"category": cat, "division": cat, "generation": generation}
                                   for _ in batch],
                    )
        # The activation record is written only after all upserts succeeded.
        scope_id = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:24]
        atomic_write_json(os.path.join(self.index_dir, f"semantic-{scope_id}.json"),
                          {"version": 2, "model": self.model_name, "scope": scope,
                           "fingerprint": fingerprint, "generation": generation,
                           "categories": sorted(by_category), "document_ids": sorted(self._by_id)})
        self._built = True
        return self

    def search(self, query, top_n=20, division=None, category=None, *, refresh_sources=True):
        """Search current sources, or a caller's explicitly frozen transaction.

        ``refresh_sources=False`` is for recall's already-collected source
        snapshot. It keeps all arms on one corpus generation during fusion.
        """
        if division is not None and category is not None and division != category:
            raise ValueError("division and category specify conflicting filters")
        selected = category if category is not None else division
        if top_n <= 0 or not query.strip():
            return []
        if not self._built:
            self.build()
        elif refresh_sources and self._source_options is not None:
            current = collect_documents(**self._source_options)
            if corpus_fingerprint(current, self.scope) != self.fingerprint:
                self.build(documents=current, **self._source_options)
        if not self._documents:
            return []
        model = self._get_model()
        vector = model.encode([query], show_progress_bar=False)[0]
        q_emb = vector.tolist() if hasattr(vector, "tolist") else list(vector)
        available = sorted({d["category"] for d in self._documents})
        categories = [selected] if selected in available else ([] if selected is not None else available)
        out = []
        for cat in categories:
            collection = self._collections.get(cat)
            if collection is None:
                # Never auto-create a missing collection during retrieval.
                self.diagnostics.append(f"Missing active semantic collection: {cat}")
                continue
            count = sum(d["category"] == cat for d in self._documents)
            result = collection.query(query_embeddings=[q_emb], n_results=min(top_n, count),
                                      include=["distances"])
            ids = result.get("ids", [[]])[0]
            distances = result.get("distances", [[]])[0]
            for doc_id, distance in zip(ids, distances):
                doc = self._by_id.get(doc_id)
                if doc is None or doc["category"] != cat:
                    self.diagnostics.append(f"Ignored document outside active semantic generation: {doc_id}")
                    continue
                out.append(RetrievalResult(
                    id=doc_id, text=doc["text"], score=max(0., 1. - float(distance)), source="semantic",
                    metadata={k: v for k, v in doc.items() if k not in {"id", "text"}},
                ))
        out.sort(key=lambda r: (-r.score, r.id))
        return out[:top_n]
