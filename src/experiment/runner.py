"""Experiment runner CLI.

Loads a scenario YAML, instantiates the agent, runs it N times, saves
structured :class:`RunResult` files, and prints a summary.

Usage::

    python -m src.experiment.runner \\
        --scenario benchmark/scenarios/smoke_test.yaml \\
        --architecture single \\
        --runs 1 \\
        --budget 100000

    python -m src.experiment.runner \\
        --scenario benchmark/scenarios/smoke_test.yaml \\
        --architecture multi \\
        --runs 5 \\
        --budget 100000
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import structlog
import yaml
from dotenv import load_dotenv

from src.experiment.result_logger import (
    RunResult,
    capture_git_provenance,
    compute_per_agent_tokens,
    compute_scores,
    save,
)
from src.services.catalog import for_spec

logger = structlog.get_logger(__name__)

# Backend selection lives in .env (LLM_BACKEND, OLLAMA_MODEL, API keys).
# Without this the client falls back to its hardcoded default model, which
# silently ran an entire matrix on qwen2.5:7b instead of the 14b the study
# specifies. Loaded at import so every entry point into the runner —
# including scripts/run_matrix.py, which invokes it as a subprocess — sees
# the same configuration. Real environment variables still win.
load_dotenv()


def load_scenario(path: Path) -> dict[str, Any]:
    """Parse a scenario YAML file.

    Expected top-level keys: ``id``, ``description``, ``request``,
    and optionally ``ground_truth``.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Scenario file {path} is not a YAML mapping")
    for key in ("id", "request"):
        if key not in data:
            raise ValueError(f"Scenario file {path} missing required key: {key}")
    return data


def _run_single(
    scenario: dict[str, Any],
    model: str,
    budget_limit: int,
    max_iterations: int,
) -> RunResult:
    """Execute one agent run and return a :class:`RunResult`."""
    from src.agents.single_agent.agent import SingleAgent
    from src.llm.client import get_client
    from src.llm.token_budget import TokenBudget

    run_result = RunResult(
        scenario_id=scenario["id"],
        scenario_text=scenario["request"],
        architecture="single",
        # Provisional: replaced below with the identifier the client
        # actually resolves, since "default" says nothing about which
        # model produced the run.
        model=model,
        max_iterations=max_iterations,
    )
    run_result.started_at = RunResult.now_iso()
    run_result.git_commit, run_result.git_dirty = capture_git_provenance()

    try:
        model_kwargs: dict[str, Any] = {}
        if model and model != "default":
            model_kwargs["model"] = model
        client = get_client(**model_kwargs)
        run_result.model = client.model_name
        budget = TokenBudget(budget_limit)
        agent = SingleAgent(client, budget, max_iterations=max_iterations)

        agent_result = agent.run(scenario["request"])

        run_result.tokens_used = agent_result.tokens_used
        run_result.wall_clock_s = agent_result.wall_clock_s
        run_result.termination_reason = agent_result.termination_reason
        run_result.history = [e.model_dump(mode="json") for e in agent_result.history]

        if agent_result.final_spec:
            run_result.final_spec = agent_result.final_spec.model_dump(mode="json")
            run_result.services_deployed = [
                definition.name for definition in for_spec(agent_result.final_spec)
            ]
        if agent_result.validator_report:
            run_result.validator_report = agent_result.validator_report.model_dump(mode="json")

    except Exception as exc:  # noqa: BLE001 — must not crash the CLI
        run_result.termination_reason = f"runner_error: {exc}"
        logger.error("run_failed", error=str(exc))

    run_result.finished_at = RunResult.now_iso()
    run_result.scores = compute_scores(run_result, scenario.get("ground_truth"))
    return run_result


def _run_multi(
    scenario: dict[str, Any],
    model: str,
    budget_limit: int,
    max_iterations: int,
) -> RunResult:
    """Execute one multi-agent run and return a :class:`RunResult`."""
    from src.agents.multi_agent.orchestrator import OrchestratorAgent
    from src.llm.client import get_client
    from src.llm.token_budget import TokenBudget

    run_result = RunResult(
        scenario_id=scenario["id"],
        scenario_text=scenario["request"],
        architecture="multi",
        # Provisional — see _run_single.
        model=model,
        max_iterations=max_iterations,
    )
    run_result.started_at = RunResult.now_iso()
    run_result.git_commit, run_result.git_dirty = capture_git_provenance()

    try:
        model_kwargs: dict[str, Any] = {}
        if model and model != "default":
            model_kwargs["model"] = model
        client = get_client(**model_kwargs)
        run_result.model = client.model_name
        budget = TokenBudget(budget_limit)
        agent = OrchestratorAgent(
            client,
            budget,
            max_iterations=max_iterations,
            max_worker_iterations=5,
        )

        agent_result = agent.run(scenario["request"])

        run_result.tokens_used = agent_result.tokens_used
        run_result.wall_clock_s = agent_result.wall_clock_s
        run_result.termination_reason = agent_result.termination_reason
        run_result.history = agent_result.delegation_log
        run_result.per_agent_tokens = compute_per_agent_tokens(
            agent_result.delegation_log, agent_result.tokens_used
        )

        if agent_result.final_spec:
            run_result.final_spec = agent_result.final_spec.model_dump(mode="json")
            run_result.services_deployed = [
                definition.name for definition in for_spec(agent_result.final_spec)
            ]
        if agent_result.validator_report:
            run_result.validator_report = agent_result.validator_report.model_dump(mode="json")

    except Exception as exc:  # noqa: BLE001 — must not crash the CLI
        run_result.termination_reason = f"runner_error: {exc}"
        logger.error("run_failed", error=str(exc))

    run_result.finished_at = RunResult.now_iso()
    run_result.scores = compute_scores(run_result, scenario.get("ground_truth"))
    return run_result


def _run_parallel(
    scenario: dict[str, Any],
    model: str,
    budget_limit: int,
    max_iterations: int,
) -> RunResult:
    """Execute one parallel per-service run and return a :class:`RunResult`.

    Study 5's exploratory third arm. Structurally identical to
    :func:`_run_multi` — same shared ``TokenBudget``, same delegation log
    shape — so the three arms produce comparable records. The two extra
    fields it records, ``arbitration`` and the concurrency timings, are the
    ones the star topology has no analogue for.
    """
    from src.agents.parallel_agent.manager import ParallelManagerAgent
    from src.llm.client import get_client
    from src.llm.token_budget import TokenBudget

    run_result = RunResult(
        scenario_id=scenario["id"],
        scenario_text=scenario["request"],
        architecture="parallel",
        # Provisional — see _run_single.
        model=model,
        max_iterations=max_iterations,
    )
    run_result.started_at = RunResult.now_iso()
    run_result.git_commit, run_result.git_dirty = capture_git_provenance()

    try:
        model_kwargs: dict[str, Any] = {}
        if model and model != "default":
            model_kwargs["model"] = model
        client = get_client(**model_kwargs)
        run_result.model = client.model_name
        budget = TokenBudget(budget_limit)
        agent = ParallelManagerAgent(
            client,
            budget,
            max_iterations=max_iterations,
            max_service_iterations=5,
        )

        agent_result = agent.run(scenario["request"])

        run_result.tokens_used = agent_result.tokens_used
        run_result.wall_clock_s = agent_result.wall_clock_s
        run_result.termination_reason = agent_result.termination_reason
        run_result.history = agent_result.delegation_log
        run_result.per_agent_tokens = compute_per_agent_tokens(
            agent_result.delegation_log, agent_result.tokens_used
        )
        # Study-5-specific evidence — how often the merge step had to referee,
        # and whether the fan-out was genuinely concurrent — rides in the
        # delegation log's "merge" entry rather than in a new RunResult field,
        # so single and multi records keep their exact existing shape.

        if agent_result.final_spec:
            run_result.final_spec = agent_result.final_spec.model_dump(mode="json")
            run_result.services_deployed = [
                definition.name for definition in for_spec(agent_result.final_spec)
            ]
        if agent_result.validator_report:
            run_result.validator_report = agent_result.validator_report.model_dump(mode="json")

    except Exception as exc:  # noqa: BLE001 — must not crash the CLI
        run_result.termination_reason = f"runner_error: {exc}"
        logger.error("run_failed", error=str(exc))

    run_result.finished_at = RunResult.now_iso()
    run_result.scores = compute_scores(run_result, scenario.get("ground_truth"))
    return run_result


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Run agent architectures against a scenario."
    )
    parser.add_argument(
        "--scenario", type=Path, required=True, help="Path to scenario YAML"
    )
    parser.add_argument(
        "--architecture",
        default="single",
        choices=["single", "multi", "parallel"],
        help=(
            "Agent architecture: single (ReAct baseline), multi (star: "
            "orchestrator + functional workers), or parallel (manager + "
            "concurrent per-service agents + merge)"
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override default model (reads LLM_BACKEND/OLLAMA_MODEL otherwise)",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=100_000,
        help="Token budget per task (default: 100000)",
    )
    parser.add_argument(
        "--runs", type=int, default=1, help="Number of runs (default: 1)"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=25,
        help="Max agent loop iterations (default: 25)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/runs"),
        help="Output directory (default: results/runs)",
    )
    args = parser.parse_args(argv)

    scenario = load_scenario(args.scenario)
    model = args.model or "default"
    logger.info(
        "experiment_start",
        scenario=scenario["id"],
        runs=args.runs,
        budget=args.budget,
    )

    run_fn = {
        "single": _run_single,
        "multi": _run_multi,
        "parallel": _run_parallel,
    }[args.architecture]

    results: list[RunResult] = []
    for i in range(1, args.runs + 1):
        log = logger.bind(run_number=i)
        log.info("run_start")

        run_result = run_fn(
            scenario,
            model=model,
            budget_limit=args.budget,
            max_iterations=args.max_iterations,
        )
        path = save(run_result, args.output)
        results.append(run_result)
        log.info("run_complete", path=str(path), reason=run_result.termination_reason)

    # Summary
    print(f"\n{'='*60}")
    print(f"Scenario: {scenario['id']}  |  Runs: {len(results)}")
    print(f"{'='*60}")
    for r in results:
        # parameter_details is several hundred lines per run and lives in
        # the JSON; printing it here would bury the summary during a batch.
        summary_scores = {
            k: (round(v, 3) if isinstance(v, float) else v)
            for k, v in r.scores.items()
            if not isinstance(v, (list, dict))
        }
        print(
            f"  {r.run_id}  "
            f"reason={r.termination_reason:<20s}  "
            f"tokens={r.tokens_used:>7d}  "
            f"time={r.wall_clock_s:.1f}s  "
            f"scores={summary_scores}"
        )
    print()


if __name__ == "__main__":
    main()
