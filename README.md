# Multi-Agent Deployment Benchmark

An empirical comparison of single-agent and multi-agent LLM architectures for translating natural-language requirements into deployed multi-service web stacks (nginx + PostgreSQL + Redis).

## What this is

Type a paragraph of English describing the web stack you need. The system reads it, looks up the relevant tuning rules and security baselines via RAG, decides on configuration parameters, generates the Docker Compose stack and service configs, starts the stack in containers, runs benchmarks (`pgbench`, `wrk`, `redis-benchmark`) and CIS Level 1 security checks against it, and either accepts the configuration or iteratively revises it.

The system is built **two ways**: a single-agent ReAct loop and a six-agent LangGraph state machine. Both share the same RAG corpus, the same tool registry, and the same Docker validator. They are evaluated against the same benchmark scenarios under strict token-budget parity.

The benchmark measures whether multi-agent decomposition actually outperforms a strong single-agent baseline for sequential, well-specified infrastructure provisioning — a question recent literature (Tran & Kiela 2026; Anthropic "Building Effective Agents") has begun to challenge.

## Research question

> *"To what extent does a multi-agent LLM architecture improve the correctness, safety, and cost-efficiency of natural-language-driven multi-service deployment compared to a single-agent baseline under token-budget parity?"*

Sub-questions:
- SQ1: Does decomposing the task across specialised agents (one per service domain) improve per-service configuration correctness?
- SQ2: Does an independent verifier (Critic + Validator agents) catch more safety violations than self-verification in a single agent?
- SQ3: What is the cost overhead (tokens, latency) of multi-agent decomposition for sequential infrastructure provisioning?
- SQ4: Under what task and architectural conditions does multi-agent decomposition justify its cost overhead?

## Architecture

**Single-agent baseline.** One LLM running a ReAct loop with tools for RAG retrieval, configuration generation, validator execution, and finalisation. Constrained-decoded output via Pydantic + Instructor.

**Multi-agent system.** Six specialised agents in a LangGraph state machine:

| Agent | Role |
|---|---|
| Requirements Analyst | Parse NL, build structured stack spec, apply SRA paradigm for ambiguities |
| Sizing & Allocation | Distribute finite host resources (RAM, CPU) across services using Dominant Resource Fairness principles |
| nginx Designer | Pick nginx parameters grounded in nginx docs + CIS nginx Benchmark + Mozilla SSL |
| PostgreSQL Designer | Pick Postgres parameters grounded in PG docs + CIS PG Benchmark + pgtune logic |
| Redis Designer | Pick Redis parameters grounded in Redis docs + CIS Redis Benchmark |
| Validator | Spin up the stack in Docker, run benchmarks, run CIS checks, return structured report |
| Critic | Read validator report; accept, route targeted revision to specific designer, or abort |

(The IaC Generator that renders Pydantic specs into config files is a deterministic step shared by both architectures.)

## Tech stack

- **Python 3.11+** — agent runtime
- **LangGraph** — multi-agent orchestration as a stateful directed graph
- **Instructor + Pydantic** — constrained decoding of LLM outputs against typed schemas
- **LlamaIndex + Chroma + BM25** — hybrid keyword+vector RAG over PostgreSQL, nginx, Redis, and CIS Benchmark documentation
- **Ollama** (local) / **Anthropic, OpenAI, Google APIs** (cloud) — model serving
- **Docker + docker-compose** — sandboxed multi-service test harness
- **pgbench, wrk, redis-benchmark, pgAudit** — performance and compliance benchmarking
- **Pandas, NumPy, SciPy, Matplotlib** — statistical analysis and figure generation

## Repository structure

| Path | Purpose |
|---|---|
| `src/agents/` | Single-agent baseline and multi-agent system implementations |
| `src/tools/` | Shared tools: RAG query, config generation, validation, finalisation |
| `src/rag/` | RAG corpus indexing and retrieval |
| `src/validator/` | Docker harness, CIS checks, pgbench/wrk/redis-benchmark wrappers |
| `src/llm/` | LLM client abstraction + token-budget enforcement |
| `src/schemas/` | Pydantic models for structured outputs |
| `src/experiment/` | Experiment runner, metrics, analysis scripts |
| `corpus/` | RAG source documents (PostgreSQL, nginx, Redis docs, CIS Benchmarks, tuning guides) |
| `benchmark/scenarios/` | Natural-language scenarios with derived ground truth |
| `results/aggregated/` | Processed metrics across experimental runs |
| `results/figures/` | Plots and visualisations |
| `tests/` | Unit tests |

## Getting started

```bash
# 1. Install Python deps
pip install -e ".[dev]"

# 2. Configure LLM backend
cp .env.example .env
# Edit .env with your API keys or Ollama settings

# 3. Pull a local model (if using Ollama)
ollama pull qwen2.5:7b

# 4. Verify Docker is running
docker info

# 5. Build the RAG index
python -m src.rag.build_index

# 6. Run a smoke test
python -m src.experiment.runner \
    --scenario benchmark/scenarios/smoke_test.yaml \
    --architecture single

# 7. Run the full evaluation matrix (overnight)
python -m src.experiment.runner --full-matrix
```

## Methodology

The project follows Design Science Research. Both architectures are compared on identical natural-language scenarios under strict token-budget parity. The RAG corpus, tool registry, and Docker validator harness are held constant across both architectures so any performance difference is attributable to the agentic architecture itself, not to differences in underlying infrastructure.

Evaluation metrics include configuration correctness (per-service parameters within expected ranges derived from pgtune, LlamaTune's 45-knob set, Mozilla SSL profiles), safety violations caught (CIS Level 1 controls across all three services), self-correction effectiveness, total token cost, and wall-clock latency. Statistical analysis uses paired comparisons across scenarios with 95% confidence intervals. Observed multi-agent failures are categorised against Cemri et al.'s MAST taxonomy.

## Background

This system was built as the empirical artefact of an MSc Applied AI dissertation at WMG, University of Warwick. The work tests claims from recent literature on multi-agent LLM cost-efficiency — particularly Tran & Kiela's argument that single-agent systems match or beat multi-agent under equal token budgets, and Anthropic's published doctrine favouring the simplest viable architecture.

A full report including the literature review, methodology, findings, and discussion will be linked here after submission.

## Reproducibility

Benchmark scenarios with ground-truth derivations are published under `benchmark/`. The RAG corpus is in `corpus/`. Aggregated experimental results and figures are in `results/`. Raw run logs (large, regenerable) are excluded from version control but can be reproduced by re-running the experimental matrix.

## License

To be decided.
