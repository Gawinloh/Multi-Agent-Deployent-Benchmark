# Multi-Agent LLM Systems for Multi-Service Software Deployment

**Author:** Gawin Lohaburananont
**Programme:** MSc Applied AI, WMG, University of Warwick
**Supervisor:** Young Saeng Park
**Submission deadline:** 2026-08-20

## Research question

> *"To what extent does a multi-agent LLM architecture improve the correctness, safety, and cost-efficiency of natural-language-driven multi-service deployment compared to a single-agent baseline under token-budget parity?"*

Empirical test bed: a containerised web stack consisting of **nginx + PostgreSQL + Redis**.

## Repository structure

| Path | Purpose |
|---|---|
| `docs/planning/` | Project roadmap, scope decisions, risk register, supervisor meeting notes |
| `docs/lit-review/` | Deep-research prompts (v1–v3), raw outputs, consolidated findings |
| `docs/methodology/` | Methodology explanation, system walkthrough |
| `docs/chapters/` | Dissertation chapter drafts |
| `docs/ethics/` | Ethics approval form |
| `docs/references/` | Marking rubric, source PDFs |
| `src/` | All Python source code for the experimental system |
| `src/agents/` | Single-agent baseline and multi-agent system implementations |
| `src/tools/` | Shared tools (RAG, validator, config generator, finaliser) |
| `src/rag/` | RAG corpus indexing and retrieval |
| `src/validator/` | Docker harness, CIS checks, pgbench/wrk/redis-benchmark wrappers |
| `src/llm/` | LLM client abstraction + token-budget enforcement |
| `src/schemas/` | Pydantic models for structured outputs |
| `src/experiment/` | Experiment runner, metrics, analysis scripts |
| `corpus/` | RAG source documents (PostgreSQL docs, CIS benchmarks, tuning guides) |
| `benchmark/scenarios/` | Natural-language scenarios with ground truth |
| `results/runs/` | Raw experiment logs (gitignored) |
| `results/aggregated/` | Processed metrics |
| `results/figures/` | Plots used in the dissertation |
| `tests/` | Unit tests for system code |

## Getting started

1. Clone the repository.
2. Copy `.env.example` to `.env` and fill in any required API keys.
3. Install Python dependencies: `pip install -e .` (or `uv pip install -e .`).
4. Install Ollama and pull a model: `ollama pull qwen2.5:7b`.
5. Verify Docker is running.
6. Build the RAG index: `python -m src.rag.build_index`.
7. Run a smoke test: `python -m src.experiment.runner --scenario benchmark/scenarios/smoke_test.yaml --arch single`.

(Detailed setup instructions will be added as the build progresses.)

## Methodology in one paragraph

The dissertation follows Design Science Research. Two architectures (single-agent ReAct loop, multi-agent LangGraph state machine with 6 specialised agents) are compared on identical natural-language scenarios under strict token-budget parity. Both share the same RAG corpus, the same tool registry, and the same Docker-based validator harness. Evaluation metrics span configuration correctness, safety violations caught (CIS Level 1 across all three services), self-correction effectiveness, token cost, and wall-clock latency. Statistical analysis includes paired comparisons across scenarios and confidence intervals. Multi-agent failures are categorised against Cemri et al.'s MAST taxonomy.

## Key documents to read first

- `docs/planning/project-roadmap.md` — week-by-week plan
- `docs/methodology/methodology-explained.md` — methodology in plain language
- `docs/methodology/system-walkthrough.md` — concrete walkthrough of how the system works
- `docs/lit-review/consolidated-findings.md` — synthesised literature review

## Reproducibility

The benchmark scenarios and ground-truth derivations are published under `benchmark/`. The full RAG corpus is in `corpus/`. Aggregated experimental results are in `results/aggregated/` and figures in `results/figures/`. Raw run logs (large, regenerable) are excluded from version control but can be reproduced by re-running the experimental matrix.

## License

To be decided before publication.
