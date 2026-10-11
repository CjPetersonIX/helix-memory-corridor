"""
Semantic vector search over HELIX documents.

- Embeddings: sentence-transformers `all-MiniLM-L6-v2` (384 dims, CPU-friendly).
  Model is downloaded once on first use (~90MB) and cached locally.
- Storage: ChromaDB persistent client, one collection per HELIX division.
  Data lives under ~/.helix-memory/chroma/ — no server, no PostgreSQL.

Lazy imports: sentence_transformers and chromadb are only imported when
SemanticIndex is actually used, so the rest of the skill works without them.
"""

import os

from .collector import collect_documents
from .fusion import RetrievalResult

INDEX_DIR = os.path.expanduser("~/.helix-memory")
CHROMA_DIR = os.path.join(INDEX_DIR, "chroma")
MODEL_NAME = "all-MiniLM-L6-v2"


class SemanticIndex:
    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self.model_name = model_name
        self._model = None
        self._client = None
        self._collections: dict[str, object] = {}

    # ---- lazy deps ----

    def _get_model(self):
        if self._model is None:
            # httpx (used by huggingface_hub) chokes on bracketed IPv6 entries
            # in no_proxy (e.g. "[::1]") -> "Invalid port" errors. Sanitize.
            for _k in ("no_proxy", "NO_PROXY"):
                _v = os.environ.get(_k, "")
                if "[" in _v:
                    os.environ[_k] = "localhost,127.0.0.1"
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _get_client(self):
        if self._client is None:
            import chromadb

            os.makedirs(CHROMA_DIR, exist_ok=True)
            self._client = chromadb.PersistentClient(path=CHROMA_DIR)
        return self._client

    def _get_collection(self, division: str):
        if division not in self._collections:
            client = self._get_client()
            self._collections[division] = client.get_or_create_collection(
                name=f"helix-{division}",
                metadata={"hnsw:space": "cosine"},
            )
        return self._collections[division]

    # ---- build ----

    def build(
        self,
        divisions: list[str] | None = None,
        batch_size: int = 32,
        **collect_kw,
    ) -> "SemanticIndex":
        """Embed all documents and upsert into per-division collections."""
        from .collector import DIVISIONS

        divisions = divisions or DIVISIONS
        docs = collect_documents(divisions=divisions, **collect_kw)
        model = self._get_model()

        by_div: dict[str, list[dict]] = {}
        for d in docs:
            by_div.setdefault(d["division"], []).append(d)

        for div, dlist in by_div.items():
            col = self._get_collection(div)
            # Clear stale entries for a clean rebuild
            try:
                existing = col.get(limit=100000)["ids"]
                if existing:
                    col.delete(ids=existing)
            except Exception:
                pass
            texts = [d["text"][:4000] for d in dlist]  # cap length for the encoder
            ids = [d["id"] for d in dlist]
            for i in range(0, len(texts), batch_size):
                chunk_texts = texts[i : i + batch_size]
                chunk_ids = ids[i : i + batch_size]
                embs = model.encode(chunk_texts, show_progress_bar=False)
                col.upsert(
                    ids=chunk_ids,
                    embeddings=[e.tolist() for e in embs],
                    documents=chunk_texts,
                    metadatas=[{"division": div} for _ in chunk_ids],
                )
        return self

    # ---- search ----

    def search(
        self,
        query: str,
        top_n: int = 20,
        division: str | None = None,
    ) -> list[RetrievalResult]:
        from .collector import DIVISIONS

        model = self._get_model()
        q_emb = model.encode([query], show_progress_bar=False)[0].tolist()
        divisions = [division] if division else DIVISIONS

        out: list[RetrievalResult] = []
        for div in divisions:
            try:
                col = self._get_collection(div)
            except Exception:
                continue
            try:
                res = col.query(
                    query_embeddings=[q_emb],
                    n_results=top_n,
                    include=["documents", "distances"],
                )
            except Exception:
                continue
            ids = res.get("ids", [[]])[0]
            docs = res.get("documents", [[]])[0]
            dists = res.get("distances", [[]])[0]
            for doc_id, doc, dist in zip(ids, docs, dists):
                # cosine distance -> similarity
                sim = max(0.0, 1.0 - float(dist))
                out.append(
                    RetrievalResult(
                        id=doc_id,
                        text=doc[:1200] if doc else "",
                        score=sim,
                        source="semantic",
                        metadata={"division": div},
                    )
                )
        out.sort(key=lambda r: r.score, reverse=True)
        return out[:top_n]
