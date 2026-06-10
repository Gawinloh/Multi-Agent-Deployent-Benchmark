"""Build the hybrid RAG index: BM25 (keyword) + Chroma (dense vector).

Hybrid retrieval is required because exact parameter names like
"shared_buffers" are critical and pure vector retrieval blurs them;
BM25 catches the exact terms, the vector index catches paraphrases.

Usage:
    python -m src.rag.build_index [--corpus corpus/] [--out rag_index/]

Outputs under --out:
    bm25.pkl       pickled BM25Okapi index + chunk metadata
    chunks.json    chunk metadata (human-inspectable)
    chroma/        Chroma persistent collection ("corpus")
    manifest.json  build metadata (model, counts, dimensionality)

Rebuilding is idempotent: the output directory is wiped and recreated.
Heavy dependencies (chromadb, sentence-transformers) are imported
lazily so the loader stays unit-testable without them.
"""

from __future__ import annotations

import argparse
import json
import pickle
import re
import shutil
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.rag.loader import Chunk, chunk_document, load_corpus

logger = structlog.get_logger(__name__)

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
COLLECTION_NAME = "corpus"

_WORD = re.compile(r"\w+")


def _tokenize(text: str) -> list[str]:
    """BM25 tokenizer: lowercase word characters. Underscores survive, so
    'shared_buffers' stays one token — exactly the behaviour hybrid
    retrieval needs for config parameter names."""
    return _WORD.findall(text.lower())


def build_bm25(chunks: list[Chunk], out_dir: Path) -> None:
    """Build and persist the BM25 index (+ chunk metadata sidecars)."""
    from rank_bm25 import BM25Okapi

    tokenized = [_tokenize(chunk.text) for chunk in chunks]
    bm25 = BM25Okapi(tokenized)
    chunk_dicts = [asdict(chunk) for chunk in chunks]
    with (out_dir / "bm25.pkl").open("wb") as fh:
        pickle.dump({"bm25": bm25, "chunks": chunk_dicts}, fh)
    (out_dir / "chunks.json").write_text(
        json.dumps(chunk_dicts, indent=2), encoding="utf-8"
    )
    logger.info("bm25_built", chunks=len(chunks))


def build_chroma(chunks: list[Chunk], out_dir: Path) -> int:
    """Build and persist the Chroma vector index; returns embedding dim."""
    import chromadb
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBEDDING_MODEL)
    embeddings = model.encode(
        [chunk.text for chunk in chunks],
        batch_size=64,
        show_progress_bar=False,
        normalize_embeddings=True,
    )
    client = chromadb.PersistentClient(path=str(out_dir / "chroma"))
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:  # noqa: BLE001 — collection may not exist yet
        pass
    collection = client.create_collection(
        COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )
    collection.add(
        ids=[chunk.chunk_id for chunk in chunks],
        embeddings=embeddings.tolist(),
        documents=[chunk.text for chunk in chunks],
        # Chroma metadata values must not be None
        metadatas=[
            {
                "doc_id": chunk.doc_id,
                "service": chunk.service or "",
                "section": chunk.section or "",
                "source": chunk.source,
                "url": chunk.url,
            }
            for chunk in chunks
        ],
    )
    dim = int(embeddings.shape[1])
    logger.info("chroma_built", chunks=len(chunks), dimensionality=dim)
    return dim


def build_index(corpus_dir: Path, out_dir: Path) -> dict:
    """Load, chunk, and index the corpus. Returns the build manifest."""
    documents = load_corpus(corpus_dir)
    if not documents:
        raise SystemExit(f"no parseable documents found under {corpus_dir}")

    chunks: list[Chunk] = []
    for doc in documents:
        chunks.extend(chunk_document(doc))

    per_service = Counter(chunk.service or "unspecified" for chunk in chunks)
    logger.info(
        "corpus_chunked",
        documents=len(documents),
        total_chunks=len(chunks),
        per_service=dict(per_service),
    )

    # idempotent rebuild: wipe and recreate
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    build_bm25(chunks, out_dir)
    dim = build_chroma(chunks, out_dir)

    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "embedding_model": EMBEDDING_MODEL,
        "documents": len(documents),
        "total_chunks": len(chunks),
        "chunks_per_service": dict(per_service),
        "embedding_dimensionality": dim,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    logger.info("index_built", **manifest)
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the hybrid RAG index.")
    parser.add_argument("--corpus", type=Path, default=Path("corpus"))
    parser.add_argument("--out", type=Path, default=Path("rag_index"))
    args = parser.parse_args(argv)
    build_index(args.corpus, args.out)


if __name__ == "__main__":
    main()
