# Pre-Study-4 retrieval index (built 2026-07-07)

This is a verbatim copy of `rag_index/` **as Studies 1, 2 and 3 actually ran
against it**, taken 2026-08-25 immediately before the index was rebuilt for
Study 4. `rag_index/` itself is gitignored (`.gitignore:66`) and has never been
tracked, so without this copy the three collected studies would not be
reproducible against their own retrieval state.

Do not rebuild or overwrite this directory. To use it, point `RAG_INDEX_DIR` at
it rather than copying it back over `rag_index/`:

```bash
RAG_INDEX_DIR=rag_index_prestudy4_2026-07-07 python scripts/run_matrix.py ...
```

## Manifest

```json
{
  "built_at": "2026-07-07T18:03:31.859562+00:00",
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "documents": 26,
  "total_chunks": 48,
  "chunks_per_service": {"nginx": 11, "postgres": 21, "redis": 12, "unspecified": 4},
  "embedding_dimensionality": 384
}
```

**26 documents, no `rabbitmq` key.** The corpus holds 31 documents excluding
`README.md`; the five in `corpus/rabbitmq/` carry `retrieved: "2026-08-07"` and
postdate this build by a month. Studies 2 and 3 were collected 2026-08-08, so
no RabbitMQ document was retrievable during any run of any study. That gap is
the confound Study 4 exists to remove, and it is discussed at Section 6.4 of the
dissertation.

## Pre-registration commitment

The Study 4 predictions were written and hashed before the index was rebuilt
and before any Study 4 run was launched. The documents live in the private docs
repository (`docs/` is gitignored here); their SHA-256 digests are recorded so
the content is fixed and verifiable from this public repository:

```
3f7dab722d9552df6a6bc1e8cb515cce2c8dd5639f439e3bb8140e77ea6c8e07  docs/planning/study4-prereg.md
005eb29ea4ceef6515b0f5c7e86d3cef34a5579ea081c0563e8ea17c89a731ef  docs/planning/study4-provenance.md
```

Verify with `shasum -a 256 docs/planning/study4-prereg.md`.

Recorded at source commit `4e2bbb2`, working tree clean.
