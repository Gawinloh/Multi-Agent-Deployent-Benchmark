"""The ``query_rag`` tool: grounded retrieval from the indexed corpus.

The agent issues natural-language questions during reasoning; this tool
returns top-k chunks with citations as plain dicts (JSON-serialisable,
ready to drop into an observation).

A single :class:`~src.rag.query.HybridRetriever` is loaded lazily on
first use and reused across calls (index loading is expensive).
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from src.rag.query import HybridRetriever

logger = structlog.get_logger(__name__)

_retriever: HybridRetriever | None = None


def _get_retriever(index_dir: Path | None = None) -> HybridRetriever:
    """Lazy singleton. ``index_dir`` defaults to $RAG_INDEX_DIR or rag_index/."""
    global _retriever
    if _retriever is None:
        resolved = index_dir or Path(os.environ.get("RAG_INDEX_DIR", "rag_index"))
        _retriever = HybridRetriever(resolved)
    return _retriever


def reset_retriever() -> None:
    """Drop the singleton (for tests and index rebuilds)."""
    global _retriever
    _retriever = None


def query_rag(
    question: str, k: int = 5, service: str | None = None
) -> list[dict]:
    """Retrieve top-k corpus chunks relevant to ``question``.

    Args:
        question: natural-language query.
        k: maximum number of chunks to return.
        service: optional filter ("postgres" | "nginx" | "redis").

    Returns:
        List of dicts with keys: text, source, url, service, score.
    """
    chunks = _get_retriever().search(question, k=k, service_filter=service)
    logger.info("query_rag", question=question[:80], k=k, service=service,
                results=len(chunks))
    return [
        {
            "text": chunk.text,
            "source": chunk.source,
            "url": chunk.url,
            "service": chunk.service,
            "score": chunk.score,
        }
        for chunk in chunks
    ]
