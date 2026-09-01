# Multi-Agent Deployment Benchmark

Do multi-agent LLM systems actually beat a single agent at infrastructure provisioning, once you give both the same token budget?

This repository holds the artefact and the data for an MSc Applied AI dissertation at WMG, University of Warwick. The short answer is no, and the more useful finding is about how easily the measurement itself goes wrong.

## What it does

You describe a web stack in a paragraph of English. The system retrieves the relevant tuning rules and security baselines, chooses configuration parameters, renders a Docker Compose stack for nginx, PostgreSQL, Redis and RabbitMQ, starts it in containers, scores it against CIS Level 1 controls and either accepts the result or revises it.

It is built three ways:

| Architecture | Shape |
| --- | --- |
| `single` | One ReAct loop with the full tool set |
| `multi` | Star topology: an orchestrator delegating to config, security and validation workers |
| `parallel` | One agent per service, fanned out, then merged |

All three share the same retrieval corpus, tool registry, schemas and containerised validator. They differ only in control loop and prompts, so any difference is attributable to architecture rather than to infrastructure.

Budget parity is enforced mechanically. Every model call in every architecture draws from one shared token counter, not from a per-agent allowance.

## What was found

Across 680 runs in six studies, no architecture established an improvement in output quality. The null survived a rebuilt retrieval index, a second topology and a more capable model.

Cost did not survive. The star architecture used 1.60 times as many tokens as the single agent on `gpt-4.1-nano` and 6.75 times as many on `gpt-4.1-mini`. The parallel architecture showed no established overhead.

All 36 failed runs were resource exhaustion. None matched any category in the MAST coordination-failure taxonomy, which is what you would expect from a star topology where workers never address one another.

The measurement finding is the transferable one. A binding token cap censored the cost of the more expensive arm, survivor-only averages manufactured an apparent quality advantage and a nominally identical iteration cap meant different things in the two architectures. Pre-specifying the analysis did not prevent this, because the bias entered through the instrument rather than through the analysis.

## Layout

| Path | Contents |
| --- | --- |
| `src/agents/` | The three architectures |
| `src/tools/`, `src/schemas/` | Shared tool registry and Pydantic output schemas |
| `src/rag/` | Hybrid BM25 and dense retrieval, fused by reciprocal rank |
| `src/validator/` | Docker harness and CIS Level 1 checkers |
| `src/llm/` | Model clients and the shared token budget |
| `benchmark/` | Both scenario sets and the scripts deriving their ground truth |
| `corpus/` | The 31 retrieval documents |
| `results/` | Run records as JSON, one file per run |
| `scripts/` | Batch runner and the deterministic analysis scripts |
| `tests/` | 486 test functions across 22 modules |

Ground truth is derived from published rules by script rather than authored by hand, so it can be regenerated and audited.

## Reproducing the analysis

Each collection is tagged, and every run record carries the commit of the working tree at the moment the run started.

| Study | Tag | Model | Runs |
| --- | --- | --- | ---: |
| 1 | `dataset-nano-n3` | `gpt-4.1-nano-2025-04-14` | 72 |
| 2 | `dataset-study2-n3` | `gpt-4.1-nano-2025-04-14` | 48 |
| 3 | `dataset-study3-n10` | `gpt-4.1-nano-2025-04-14` | 160 |
| 4 | `dataset-study4-n10` | `gpt-4.1-nano-2025-04-14` | 160 |
| 5 | `dataset-study5-n10` | `gpt-4.1-nano-2025-04-14` | 80 |
| 6 | `dataset-study6-n10` | `gpt-4.1-mini-2025-04-14` | 160 |

```bash
pip install -e ".[dev]"
git checkout dataset-study6-n10
python scripts/analyse_study6.py
```

The analysis is deterministic. Running it against a tagged collection reproduces the reported tables and figures exactly.

`results/` also contains collections that are not reported: a tuning set, a discarded first attempt at Study 5 and a small local-model set. They are kept for transparency and are excluded by the analysis scripts, which filter on model.

## Collecting new runs

This needs Docker and an OpenAI API key, and will not reproduce the stored records exactly, since model inference is not deterministic.

```bash
cp .env.example .env        # add your key
python -m src.experiment.runner \
    --scenario benchmark/scenarios/scenario_002_oltp_small.yaml \
    --architecture single

python scripts/run_matrix.py --runs 10 --budget 100000
```

## Status

Dissertation submitted September 2026. The report will be linked here once marks are released.

Licence to be decided.
