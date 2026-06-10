"""RAG corpus loading, indexing, and retrieval.

The corpus (see ``corpus/README.md``) is indexed into a hybrid BM25 +
Chroma vector index by :mod:`src.rag.build_index`; the agent retrieves
from it via the ``query_rag`` tool.
"""

from src.rag.loader import Chunk, Document, chunk_document, load_corpus

__all__ = ["Chunk", "Document", "chunk_document", "load_corpus"]
