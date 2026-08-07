# RAG corpus

Authoritative documentation for the catalog's services. The agent
queries this corpus during reasoning via the `query_rag` tool. Index it
with:

```bash
python -m src.rag.build_index --corpus corpus/ --out rag_index/
```

> **The committed `rag_index/` is frozen for Study 2 and must not be
> rebuilt.** Retrieval is held identical to Study 1 so that the only
> difference between the two studies is the service-selection task; a
> re-indexed corpus would confound that comparison. `corpus/rabbitmq/`
> is therefore present on disk but absent from the live index, and
> `rabbitmq` is deliberately not in the `_SERVICE_ALIASES` table in
> `src/rag/query.py`. Rebuild only as a deliberate, recorded decision.

## Directory structure

```
corpus/
  postgres/        PostgreSQL official docs (selected chapters), pgtune source/notes
  nginx/           nginx official docs (selected pages)
  redis/           Redis official docs (selected pages)
  rabbitmq/        RabbitMQ official docs (selected pages)
  cis-benchmarks/  CIS PostgreSQL, nginx, Redis benchmarks (markdown extracts).
                   No CIS benchmark exists for RabbitMQ; its controls come
                   from the vendor docs under rabbitmq/ instead.
  tuning-guides/   Crunchy/Percona/EDB PG guides, Mozilla SSL notes, Antirez Redis posts
```

Documents are Markdown files (`.md`). Files named `README.md` are
ignored by the indexer, as is any file without valid front matter
(skipped with a warning).

## Front matter schema

Every document must start with YAML front matter:

```yaml
---
source: "PostgreSQL Documentation: Resource Consumption"   # required
url: "https://www.postgresql.org/docs/16/runtime-config-resource.html"  # required
service: "postgres"      # optional: postgres | nginx | redis | rabbitmq (omit for cross-service docs)
section: "memory"        # optional: free-form topic tag
license: "PostgreSQL License"  # recommended; defaults to "unknown"
---
```

- `source` and `url` are required; documents missing either are skipped.
- `service` drives the `query_rag` service filter — set it whenever the
  document is service-specific.
- Keep one topic per file where practical; the chunker splits documents
  into ~500-word windows with 50-word overlap, so very long files are
  fine but focused files retrieve better.

## Current contents (26 documents, retrieved 2026-07-06)

- [x] postgres/ (8 files): Resource Consumption, Connections & Authentication, WAL,
      Client Authentication (pg_hba.conf), SSL/TLS, Logging, pgtune heuristics, pgAudit
- [x] nginx/ (6 files): Core module, HTTP core module, SSL module, Gzip module,
      Headers module (security headers), Mozilla SSL intermediate profile
- [x] redis/ (5 files): Memory/eviction, Persistence (RDB/AOF), Security,
      Networking, LRU/LFU eviction tuning notes
- [x] cis-benchmarks/ (3 files): CIS L1 control extracts for PostgreSQL (14 controls),
      nginx (10 controls), Redis (8 controls) — matched to validator code
- [x] tuning-guides/ (4 files): Crunchy PG tuning, Percona PG memory,
      Mozilla SSL intermediate profile rationale, Resource allocation guide (authored)
