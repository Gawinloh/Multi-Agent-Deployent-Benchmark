"""Hybrid retrieval over the indexed corpus.

Combines BM25 (exact term matching — critical for parameter names like
``shared_buffers``) with Chroma dense-vector similarity (paraphrase
matching), fused via reciprocal rank fusion:

    score(c) = sum over retrievers of 1 / (60 + rank_in_retriever)

with rank starting at 1. Ties break on chunk_id, so results are fully
deterministic for a given query and index.

Note: the BM25 index loads eagerly at construction; the Chroma
collection and the sentence-transformers encoder load lazily on first
vector search. This is functionally identical to loading everything at
init but lets the fusion logic be unit-tested without the heavy
dependencies installed.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from src.rag.build_index import COLLECTION_NAME, EMBEDDING_MODEL, _tokenize

logger = structlog.get_logger(__name__)

#: RRF dampening constant (Cormack et al. 2009 use 60).
RRF_K = 60

#: Service-name aliases the LLM produces that do not match the corpus.
#:
#: Corpus chunks are tagged ``postgres``, ``nginx`` or ``redis``, but models
#: routinely ask for ``postgresql`` or ``PostgreSQL``. An unrecognised
#: filter previously matched no chunks, returned zero results, and left the
#: agent retrying the same failing query until its budget ran out. Filters
#: are therefore normalised, and an unknown one degrades to an unfiltered
#: search rather than an empty one.
_SERVICE_ALIASES: dict[str, str] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "postgre": "postgres",
    "psql": "postgres",
    "pg": "postgres",
    "redis": "redis",
    "nginx": "nginx",
}


@dataclass(frozen=True)
class RetrievedChunk:
    """One retrieved chunk with its citation data and fused score."""

    chunk_id: str
    service: str | None
    section: str | None
    text: str
    source: str
    url: str
    score: float


class HybridRetriever:
    """BM25 + dense vector retrieval with reciprocal rank fusion."""

    def __init__(self, index_dir: Path) -> None:
        self._index_dir = Path(index_dir)
        bm25_path = self._index_dir / "bm25.pkl"
        if not bm25_path.exists():
            raise FileNotFoundError(
                f"No BM25 index at {bm25_path}; run python -m src.rag.build_index first"
            )
        with bm25_path.open("rb") as fh:
            data = pickle.load(fh)
        self._bm25 = data["bm25"]
        self._chunks: list[dict[str, Any]] = data["chunks"]
        self._chunks_by_id = {chunk["chunk_id"]: chunk for chunk in self._chunks}
        self._collection: Any = None
        self._encoder: Any = None
        self._known_services = {
            chunk["service"] for chunk in self._chunks if chunk.get("service")
        }
        logger.info(
            "hybrid_retriever_loaded",
            index_dir=str(self._index_dir),
            chunks=len(self._chunks),
        )

    # ------------------------------------------------------------------
    # Individual retrievers (each returns chunk_ids, best first)
    # ------------------------------------------------------------------

    def _bm25_search(
        self, query: str, n: int, service_filter: str | None
    ) -> list[str]:
        scores = self._bm25.get_scores(_tokenize(query))
        candidates = [
            (chunk["chunk_id"], score)
            for chunk, score in zip(self._chunks, scores, strict=True)
            if service_filter is None or chunk["service"] == service_filter
        ]
        # deterministic: score desc, then chunk_id asc
        candidates.sort(key=lambda pair: (-pair[1], pair[0]))
        return [chunk_id for chunk_id, _ in candidates[:n]]

    def _ensure_vector_backend(self) -> None:
        if self._collection is None:
            import chromadb
            from sentence_transformers import SentenceTransformer

            client = chromadb.PersistentClient(path=str(self._index_dir / "chroma"))
            self._collection = client.get_collection(COLLECTION_NAME)
            self._encoder = SentenceTransformer(EMBEDDING_MODEL)

    def _vector_search(
        self, query: str, n: int, service_filter: str | None
    ) -> list[str]:
        self._ensure_vector_backend()
        embedding = self._encoder.encode([query], normalize_embeddings=True)
        result = self._collection.query(
            query_embeddings=embedding.tolist(),
            n_results=min(n, len(self._chunks)),
            where={"service": service_filter} if service_filter else None,
        )
        return list(result["ids"][0])

    # ------------------------------------------------------------------
    # Fusion
    # ------------------------------------------------------------------

    def _normalise_service(self, service_filter: str | None) -> str | None:
        """Map an LLM-supplied service name onto a corpus service tag.

        Returns ``None`` (unfiltered search) when the name cannot be
        resolved, because an empty result set gives the agent nothing to
        act on and it tends to retry the identical query until the budget
        is gone.
        """
        if service_filter is None:
            return None
        candidate = _SERVICE_ALIASES.get(service_filter.strip().lower())
        if candidate in self._known_services:
            return candidate
        logger.warning(
            "rag_service_filter_unrecognised",
            requested=service_filter,
            known=sorted(self._known_services),
            action="searching_unfiltered",
        )
        return None

    def search(
        self, query: str, k: int = 5, service_filter: str | None = None
    ) -> list[RetrievedChunk]:
        """Top-k chunks by reciprocal-rank-fused hybrid score."""
        service_filter = self._normalise_service(service_filter)
        fetch = k * 3
        ranked_lists = [
            self._bm25_search(query, fetch, service_filter),
            self._vector_search(query, fetch, service_filter),
        ]
        fused: dict[str, float] = {}
        for ranked in ranked_lists:
            for rank, chunk_id in enumerate(ranked, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)

        # deterministic: fused score desc, then chunk_id asc
        top = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))[:k]
        results = []
        for chunk_id, score in top:
            chunk = self._chunks_by_id[chunk_id]
            results.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    service=chunk["service"],
                    section=chunk["section"],
                    text=chunk["text"],
                    source=chunk["source"],
                    url=chunk["url"],
                    score=score,
                )
            )
        logger.info(
            "hybrid_search_complete",
            query=query[:80],
            k=k,
            service_filter=service_filter,
            results=len(results),
        )
        return results
