"""Unit tests for src.rag.loader using a synthetic corpus directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.rag.build_index import _tokenize
from src.rag.loader import Document, chunk_document, load_corpus

POSTGRES_DOC = """\
---
source: "PostgreSQL Documentation: Resource Consumption"
url: "https://www.postgresql.org/docs/16/runtime-config-resource.html"
service: "postgres"
section: "memory"
license: "PostgreSQL License"
---
shared_buffers determines how much memory PostgreSQL dedicates to its
shared buffer cache. pgtune recommends roughly 25% of system RAM on a
dedicated host. Setting it too high starves the OS page cache.
"""

NGINX_DOC = """\
---
source: "Mozilla SSL Configuration Generator"
url: "https://ssl-config.mozilla.org/"
service: "nginx"
section: "tls"
---
The intermediate profile enables TLSv1.2 and TLSv1.3 only, with a
restricted cipher list and ssl_prefer_server_ciphers off.
"""

NO_FRONT_MATTER = "just some markdown with no front matter at all\n"

MISSING_URL = """\
---
source: "Some doc without a url"
service: "redis"
---
content here
"""


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    (tmp_path / "postgres").mkdir()
    (tmp_path / "nginx").mkdir()
    (tmp_path / "redis").mkdir()
    (tmp_path / "postgres" / "memory.md").write_text(POSTGRES_DOC, encoding="utf-8")
    (tmp_path / "nginx" / "tls.md").write_text(NGINX_DOC, encoding="utf-8")
    (tmp_path / "redis" / "broken.md").write_text(NO_FRONT_MATTER, encoding="utf-8")
    (tmp_path / "redis" / "no_url.md").write_text(MISSING_URL, encoding="utf-8")
    (tmp_path / "README.md").write_text("# corpus readme", encoding="utf-8")
    return tmp_path


class TestLoadCorpus:
    def test_front_matter_parsed(self, corpus: Path) -> None:
        docs = {d.service: d for d in load_corpus(corpus)}
        pg = docs["postgres"]
        assert pg.source == "PostgreSQL Documentation: Resource Consumption"
        assert pg.url.startswith("https://www.postgresql.org")
        assert pg.section == "memory"
        assert pg.license == "PostgreSQL License"
        assert "shared_buffers" in pg.content

    def test_license_defaults_to_unknown(self, corpus: Path) -> None:
        docs = {d.service: d for d in load_corpus(corpus)}
        assert docs["nginx"].license == "unknown"

    def test_unparseable_and_incomplete_files_skipped(self, corpus: Path) -> None:
        docs = load_corpus(corpus)
        assert len(docs) == 2  # broken.md and no_url.md skipped
        assert all(d.url for d in docs)

    def test_readme_ignored(self, corpus: Path) -> None:
        sources = [d.source for d in load_corpus(corpus)]
        assert not any("readme" in s.lower() for s in sources)

    def test_ids_deterministic(self, corpus: Path) -> None:
        first = {d.source: d.id for d in load_corpus(corpus)}
        second = {d.source: d.id for d in load_corpus(corpus)}
        assert first == second

    def test_empty_corpus_returns_empty(self, tmp_path: Path) -> None:
        assert load_corpus(tmp_path) == []


def make_doc(n_words: int, service: str | None = "postgres") -> Document:
    return Document(
        id="doc-1",
        source="synthetic",
        url="https://example.com",
        service=service,
        section="test",
        license="CC0",
        content=" ".join(f"word{i}" for i in range(n_words)),
    )


class TestChunkDocument:
    def test_short_doc_is_single_chunk(self) -> None:
        chunks = chunk_document(make_doc(100), chunk_size=500, overlap=50)
        assert len(chunks) == 1
        assert chunks[0].chunk_id == "doc-1:0"

    def test_chunking_respects_overlap(self) -> None:
        chunks = chunk_document(make_doc(1200), chunk_size=500, overlap=50)
        first_words = chunks[0].text.split()
        second_words = chunks[1].text.split()
        # last 50 words of chunk 0 == first 50 words of chunk 1
        assert first_words[-50:] == second_words[:50]

    def test_all_words_covered(self) -> None:
        doc = make_doc(1234)
        chunks = chunk_document(doc, chunk_size=500, overlap=50)
        assert chunks[-1].text.split()[-1] == "word1233"
        # no degenerate trailing chunk: each chunk adds new words
        seen: set[str] = set()
        for chunk in chunks:
            words = set(chunk.text.split())
            assert words - seen, "chunk adds no new words"
            seen |= words

    def test_service_tag_preserved_through_chunking(self) -> None:
        for service in ("postgres", None):
            chunks = chunk_document(make_doc(1200, service=service))
            assert all(c.service == service for c in chunks)
            assert all(c.source == "synthetic" for c in chunks)
            assert all(c.url == "https://example.com" for c in chunks)

    def test_empty_doc_returns_no_chunks(self) -> None:
        assert chunk_document(make_doc(0)) == []

    def test_overlap_must_be_less_than_chunk_size(self) -> None:
        with pytest.raises(ValueError):
            chunk_document(make_doc(100), chunk_size=50, overlap=50)


class TestTokenizer:
    def test_parameter_names_stay_whole(self) -> None:
        # underscores must survive tokenization for BM25 exact matching
        assert "shared_buffers" in _tokenize("Set shared_buffers to 4GB.")

    def test_lowercases(self) -> None:
        assert _tokenize("TLSv1.2") == ["tlsv1", "2"]
