"""Corpus loading and chunking.

Documents are Markdown files with YAML front matter (schema in
corpus/README.md). Document IDs are deterministic (uuid5 of the path
relative to the corpus root) so rebuilding the index never reshuffles
identifiers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import frontmatter
import structlog

logger = structlog.get_logger(__name__)

#: Required front matter keys; documents missing any are skipped.
REQUIRED_KEYS = ("source", "url")


@dataclass(frozen=True)
class Document:
    """One corpus document with provenance metadata."""

    id: str
    source: str
    url: str
    service: str | None
    section: str | None
    license: str
    content: str


@dataclass(frozen=True)
class Chunk:
    """A retrievable window of a document, carrying its citation data."""

    chunk_id: str
    doc_id: str
    service: str | None
    section: str | None
    text: str
    source: str
    url: str


def _doc_id(corpus_dir: Path, path: Path) -> str:
    """Deterministic ID derived from the file's corpus-relative path."""
    relative = path.relative_to(corpus_dir).as_posix()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"corpus/{relative}"))


def load_corpus(corpus_dir: Path) -> list[Document]:
    """Load all parseable documents under ``corpus_dir``.

    Files named README.md are ignored. Files that fail front matter
    parsing or lack required keys are skipped with a warning.
    """
    corpus_dir = Path(corpus_dir)
    documents: list[Document] = []
    for path in sorted(corpus_dir.rglob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        try:
            post = frontmatter.load(path, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — bad file must not abort the build
            logger.warning("corpus_file_unparseable", path=str(path), error=str(exc))
            continue
        missing = [key for key in REQUIRED_KEYS if not post.metadata.get(key)]
        if missing:
            logger.warning(
                "corpus_file_missing_front_matter",
                path=str(path),
                missing=missing,
            )
            continue
        if not post.content.strip():
            logger.warning("corpus_file_empty", path=str(path))
            continue
        documents.append(
            Document(
                id=_doc_id(corpus_dir, path),
                source=str(post.metadata["source"]),
                url=str(post.metadata["url"]),
                service=post.metadata.get("service"),
                section=post.metadata.get("section"),
                license=str(post.metadata.get("license", "unknown")),
                content=post.content,
            )
        )
    logger.info("corpus_loaded", documents=len(documents), corpus_dir=str(corpus_dir))
    return documents


def chunk_document(
    doc: Document, chunk_size: int = 500, overlap: int = 50
) -> list[Chunk]:
    """Split a document into word windows of ``chunk_size`` with
    ``overlap`` words shared between consecutive chunks.

    The final window always reaches the end of the document; no
    degenerate trailing chunk that is a subset of its predecessor is
    produced.
    """
    if overlap >= chunk_size:
        raise ValueError(f"overlap ({overlap}) must be < chunk_size ({chunk_size})")
    words = doc.content.split()
    if not words:
        return []
    step = chunk_size - overlap
    chunks: list[Chunk] = []
    start = 0
    while True:
        window = words[start : start + chunk_size]
        chunks.append(
            Chunk(
                chunk_id=f"{doc.id}:{len(chunks)}",
                doc_id=doc.id,
                service=doc.service,
                section=doc.section,
                text=" ".join(window),
                source=doc.source,
                url=doc.url,
            )
        )
        if start + chunk_size >= len(words):
            break
        start += step
    return chunks
