"""Tests for hybrid retrieval and the query_rag tool.

The fusion/filter/top-k tests run anywhere: they build a real BM25 index
over a synthetic corpus and stub the vector side, so the heavy
dependencies aren't needed. The full hybrid tests (semantic match via
real embeddings) require chromadb + sentence-transformers and are
skipped automatically when those aren't installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import src.tools.rag as rag_tool
from src.rag.build_index import build_bm25
from src.rag.loader import Chunk
from src.rag.query import RRF_K, HybridRetriever, RetrievedChunk


def make_chunk(chunk_id: str, service: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=f"doc-{chunk_id}",
        service=service,
        section="test",
        text=text,
        source=f"source-{chunk_id}",
        url=f"https://example.com/{chunk_id}",
    )


SYNTHETIC_CHUNKS = [
    make_chunk(
        "pg-mem", "postgres",
        "shared_buffers determines PostgreSQL shared memory cache size. "
        "pgtune recommends 25 percent of system RAM for shared_buffers.",
    ),
    make_chunk(
        "pg-wal", "postgres",
        "wal_level and checkpoint_completion_target control write ahead "
        "log behaviour and crash recovery durability.",
    ),
    make_chunk(
        "nginx-tls", "nginx",
        "ssl_protocols TLSv1.2 TLSv1.3 with a restricted cipher list per "
        "the Mozilla intermediate profile.",
    ),
    make_chunk(
        "redis-evict", "redis",
        "maxmemory-policy allkeys-lru evicts least recently used keys "
        "when Redis reaches its memory limit.",
    ),
]


@pytest.fixture
def index_dir(tmp_path: Path) -> Path:
    build_bm25(SYNTHETIC_CHUNKS, tmp_path)
    return tmp_path


@pytest.fixture
def retriever(index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> HybridRetriever:
    """HybridRetriever with the vector side stubbed to a fixed ranking
    that respects the service filter (so fusion logic is exercised
    without embeddings)."""
    instance = HybridRetriever(index_dir)

    def fake_vector_search(query: str, n: int, service_filter: str | None) -> list[str]:
        ids = [c.chunk_id for c in SYNTHETIC_CHUNKS
               if service_filter is None or c.service == service_filter]
        return ids[:n]

    monkeypatch.setattr(instance, "_vector_search", fake_vector_search)
    return instance


class TestHybridRetriever:
    def test_keyword_match(self, retriever: HybridRetriever) -> None:
        results = retriever.search("shared_buffers tuning", k=2)
        assert results[0].chunk_id == "pg-mem"
        assert "shared_buffers" in results[0].text

    def test_service_filter(self, retriever: HybridRetriever) -> None:
        results = retriever.search("memory configuration", k=4, service_filter="postgres")
        assert results, "filter must not empty the results"
        assert all(r.service == "postgres" for r in results)

    def test_top_k_respected(self, retriever: HybridRetriever) -> None:
        for k in (1, 2, 3):
            assert len(retriever.search("memory", k=k)) <= k

    def test_deterministic(self, retriever: HybridRetriever) -> None:
        first = retriever.search("shared_buffers memory", k=4)
        second = retriever.search("shared_buffers memory", k=4)
        assert first == second

    def test_rrf_scores(self, retriever: HybridRetriever) -> None:
        results = retriever.search("shared_buffers", k=4)
        by_id = {r.chunk_id: r for r in results}
        # pg-mem is rank 1 in BM25 and rank 1 in the stubbed vector list
        assert by_id["pg-mem"].score == pytest.approx(2 / (RRF_K + 1))
        # every fused score is a sum of 1/(60+rank) terms, so bounded
        assert all(0 < r.score <= 2 / (RRF_K + 1) for r in results)

    def test_missing_index_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="build_index"):
            HybridRetriever(tmp_path / "nowhere")

    def test_returns_retrieved_chunk_objects(self, retriever: HybridRetriever) -> None:
        result = retriever.search("redis eviction", k=1)[0]
        assert isinstance(result, RetrievedChunk)
        assert result.url.startswith("https://example.com/")


class TestQueryRagTool:
    def test_returns_serialisable_dicts(
        self, retriever: HybridRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rag_tool, "_retriever", retriever)
        results = rag_tool.query_rag("shared_buffers tuning", k=3)

        assert isinstance(results, list) and results
        assert set(results[0]) == {"text", "source", "url", "service", "score"}
        import json

        json.dumps(results)  # must be JSON-serialisable

    def test_service_filter_passthrough(
        self, retriever: HybridRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rag_tool, "_retriever", retriever)
        results = rag_tool.query_rag("configuration", k=4, service="nginx")
        assert all(r["service"] == "nginx" for r in results)

    def test_reset_retriever(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rag_tool, "_retriever", object())
        rag_tool.reset_retriever()
        assert rag_tool._retriever is None


# ---------------------------------------------------------------------------
# Full hybrid tests — need chromadb + sentence-transformers (auto-skipped)
# ---------------------------------------------------------------------------


@pytest.fixture
def full_index(tmp_path: Path) -> Path:
    pytest.importorskip("chromadb")
    pytest.importorskip("sentence_transformers")
    from src.rag.build_index import build_index

    corpus = tmp_path / "corpus"
    for chunk in SYNTHETIC_CHUNKS:
        service_dir = corpus / chunk.service
        service_dir.mkdir(parents=True, exist_ok=True)
        (service_dir / f"{chunk.chunk_id}.md").write_text(
            f"---\nsource: \"{chunk.source}\"\nurl: \"{chunk.url}\"\n"
            f"service: \"{chunk.service}\"\nsection: \"test\"\n---\n{chunk.text}\n",
            encoding="utf-8",
        )
    out = tmp_path / "rag_index"
    build_index(corpus, out)
    return out


class TestFullHybrid:
    def test_keyword_match_real_index(self, full_index: Path) -> None:
        results = HybridRetriever(full_index).search("shared_buffers", k=2)
        assert any("shared_buffers" in r.text for r in results)

    def test_semantic_match(self, full_index: Path) -> None:
        # paraphrase: no literal overlap with "maxmemory-policy allkeys-lru"
        results = HybridRetriever(full_index).search(
            "how should the cache discard old entries when it runs out of space",
            k=2,
        )
        assert any(r.service == "redis" for r in results)

    def test_service_filter_real_index(self, full_index: Path) -> None:
        results = HybridRetriever(full_index).search(
            "configuration", k=4, service_filter="postgres"
        )
        assert results and all(r.service == "postgres" for r in results)
