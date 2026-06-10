# RAG corpus

Authoritative documentation for the three target services. The agent
queries this corpus during reasoning via the `query_rag` tool. Index it
with:

```bash
python -m src.rag.build_index --corpus corpus/ --out rag_index/
```

## Directory structure

```
corpus/
  postgres/        PostgreSQL official docs (selected chapters), pgtune source/notes
  nginx/           nginx official docs (selected pages)
  redis/           Redis official docs (selected pages)
  cis-benchmarks/  CIS PostgreSQL, nginx, Redis benchmarks (markdown extracts)
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
service: "postgres"      # optional: postgres | nginx | redis (omit for cross-service docs)
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

## Current contents

Placeholder — to be populated from primary sources during W3:

- [ ] postgres/: Resource Consumption, Connections & Authentication, WAL,
      SSL/TLS, logging chapters; pgtune heuristics notes
- [ ] nginx/: core module, http module, ssl module docs
- [ ] redis/: eviction, persistence, security pages
- [ ] cis-benchmarks/: L1 control extracts for all three services
- [ ] tuning-guides/: Crunchy/Percona PG tuning, Mozilla SSL config
      (intermediate profile), Antirez on LRU/LFU

Record the retrieval date and license for every document you add.
