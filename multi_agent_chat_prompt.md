# New Chat Prompt: Build Multi-Agent Architecture

Paste the below into a new Cowork session with the Dissertation folder mounted.

---

## Context

I'm building an MSc dissertation comparing **single-agent vs multi-agent LLM architectures** for PostgreSQL/nginx/Redis infrastructure provisioning. The single-agent baseline (ReAct loop) is working and validated. Now I need to build the multi-agent comparison arm.

**Model:** qwen2.5:14b via Ollama (local). All agents will use the same model to isolate the architectural variable.

## What exists and works

The codebase is at the repo root. Key files:

### Agent loop
- `src/agents/single_agent/agent.py` — **Reference implementation.** SingleAgent class with ReAct loop (think → act → observe). Key patterns to reuse:
  - `AgentStep` schema: combined `AgentThought` + `ToolCall` in one LLM call
  - Auto-injection: model can't reliably forward specs between tools, so `validate_config` and `finalise` args are auto-injected from cached `_last_spec`
  - Observation stripping: `generate_config` results are replaced with a short "config generated" message to save context tokens
  - `SchemaParseError` recovery: failed JSON parses are recorded as failed observations so the next iteration sees the error
- `src/agents/single_agent/prompts.py` — System prompt + `render_react_prompt()` with history windowing (`_MAX_HISTORY_DETAIL = 4`)

### LLM client
- `src/llm/client.py` — Multi-backend LLM client. For Ollama: schema-constrained decoding with grammar → `format="json"` + Pydantic retry fallback. Two-pass JSON control character escaping in `_extract_json` (fixed last session). `BudgetEnforcer` wraps the client with token budget enforcement.
- `src/llm/token_budget.py` — `TokenBudget` with `record_usage()` and `BudgetExhausted` exception.

### Tools (shared — multi-agent reuses these)
- `src/tools/registry.py` — `ToolRegistry` with `dispatch()`, input validation via Pydantic, JSON-safe serialization. Four default tools:
  1. `query_rag` — search indexed docs (postgres/nginx/redis/CIS). Input: `{question, k, service?}`
  2. `generate_config` — LLM-complete a StackSpec from requirements. Input: `{partial_spec, mode}`. Needs `llm_client` + `budget` as deps.
  3. `validate_config` — Docker deploy → smoke tests → benchmarks → CIS checks → teardown. Input: `{spec}`
  4. `finalise` — accept/reject final spec, terminates loop. Input: `{final_spec, reason}`
- `src/tools/config_generator.py`, `src/tools/validator.py`, `src/tools/finaliser.py`, `src/tools/rag.py` — Tool implementations.

### Schemas
- `src/schemas/stack.py` — `StackSpec` (requirements + postgres + nginx + redis + pg_hba). This is the single typed object flowing through the pipeline.
- `src/schemas/agent.py` — `AgentThought`, `ToolCall`, `ToolObservation`, `HistoryEntry`, `FinalisationDecision`
- `src/schemas/postgres.py`, `nginx.py`, `redis.py` — Service config schemas with Pydantic validation + defaults.

### Experiment harness
- `src/experiment/runner.py` — CLI with `--architecture single` (line 103: `choices=["single"]`). Needs `--architecture multi` added + a `_run_multi()` function.
- `src/experiment/result_logger.py` — `RunResult` dataclass with `architecture` field already supports `"multi"`. `compute_scores()` extracts CIS pass rate and smoke pass rate from `ValidatorReport`.
- `benchmark/scenarios/smoke_test.yaml` — Scenario file used for all runs.

### Validation pipeline
- `src/validator/docker_runner.py` + `docker/compose-template.yml` — Jinja2-rendered docker-compose, full deploy/test/teardown cycle.
- `src/validator/cis_checks/` — postgres, nginx, redis CIS benchmark checks.
- `src/validator/benchmarks/` — pgbench, wrk, redis_bench.

## Single-agent baseline results (14b model)

- **Completion rate:** 80% (4/5 runs)
- **Clean-path tokens:** ~13k tokens, ~200s wall clock
- **Degenerate-path tokens:** up to 68k (retry storms from bad tool args)
- **CIS pass rate:** 50–66%
- **Failure mode:** model gets stuck calling generate_config with invalid args in a loop

## What needs to be built

A **multi-agent architecture** as the comparison arm. My dissertation proposal describes a planner → specialist workers pattern:

1. **Planner/Orchestrator agent** — receives the user request, breaks it into sub-tasks, delegates to specialist workers, aggregates results, decides when to finalise.
2. **Specialist workers** — each focuses on one aspect (e.g., postgres tuning, nginx config, redis config, security/CIS compliance). Each worker has access to the same tool registry but a narrower system prompt focusing on its domain.

### Key design decisions to make together:
- How many workers and what are their specialisms?
- How do workers communicate? (via orchestrator? shared state? message passing?)
- Does each worker get its own token budget, or is it shared?
- How does the orchestrator merge worker outputs into a single StackSpec?
- Do workers use the same `AgentStep` schema or do they need different schemas?

### Implementation plan:
1. Create `src/agents/multi_agent/` package
2. Build orchestrator agent + worker agents
3. Create prompts for each role
4. Add `_run_multi()` to `src/experiment/runner.py` and extend `--architecture` choices
5. Run comparison batch: single vs multi on the same scenario
6. Analyse token efficiency, CIS scores, completion rates, wall clock time

### Constraints:
- Must use the **same tools and validation pipeline** as single-agent (fair comparison)
- Must use the **same model** (qwen2.5:14b via Ollama)
- Token budget is shared across all agents in a run (measures total LLM cost)
- Results must be saved via the same `RunResult` format for apples-to-apples comparison
- Keep it buildable in ~1 week — this is a dissertation, not a production system

## Pending housekeeping
- There's a HEAD.lock file that may block git commits — delete it manually if git complains: `rm -f .git/HEAD.lock`
- The two-pass escaping fix from last session should be committed: `git add -A && git commit -m "fix: two-pass control char escaping in _extract_json"`

Let's start by designing the multi-agent architecture, then build it.
