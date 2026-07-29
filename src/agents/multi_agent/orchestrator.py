"""Orchestrator agent for the multi-agent architecture.

The orchestrator runs a meta-loop: at each iteration it decides whether
to delegate a task to a specialist worker or to finalise the run.  It
never calls tools directly — all tool usage is mediated through workers.

Communication topology: **star** — every worker talks only to the
orchestrator, never to other workers.  The orchestrator maintains a
shared ``_last_spec`` that workers can read/update via their artifacts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.llm.client import BudgetEnforcer, LLMClient, SchemaParseError
from src.llm.token_budget import BudgetExhausted, TokenBudget
from src.schemas.agent import HistoryEntry
from src.schemas.stack import StackSpec
from src.schemas.validator_report import ValidatorReport
from src.tools.registry import ToolRegistry, get_default_registry

from .prompts import render_orchestrator_prompt
from .schemas import OrchestratorAction, OrchestratorStep, WorkerResult, WorkerRole
from .worker import WorkerAgent

logger = structlog.get_logger(__name__)

# ── Max orchestrator iterations (each iteration = one delegation or finalise)
_DEFAULT_MAX_ORCHESTRATOR_ITERATIONS = 8


@dataclass
class MultiAgentRunResult:
    """Everything produced by a multi-agent run."""

    final_spec: StackSpec | None = None
    validator_report: ValidatorReport | None = None
    history: list[HistoryEntry] = field(default_factory=list)
    delegation_log: list[dict[str, Any]] = field(default_factory=list)
    tokens_used: int = 0
    wall_clock_s: float = 0.0
    termination_reason: str = "unknown"


class OrchestratorAgent:
    """Meta-agent that delegates to specialist workers.

    Args:
        llm_client: raw LLM client (shared across all agents).
        budget: shared per-run token budget.
        max_iterations: cap on orchestrator loop iterations.
        max_worker_iterations: cap on each worker's ReAct loop.
        registry: full tool registry (filtered per-worker).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        budget: TokenBudget,
        max_iterations: int = _DEFAULT_MAX_ORCHESTRATOR_ITERATIONS,
        max_worker_iterations: int = 5,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._client = llm_client
        self._budget = budget
        self._max_iterations = max_iterations
        self._max_worker_iterations = max_worker_iterations
        self._registry = registry or get_default_registry()
        self._enforcer = BudgetEnforcer(llm_client, budget)

        # Pre-build workers (they share client, budget, and registry)
        self._workers: dict[WorkerRole, WorkerAgent] = {
            role: WorkerAgent(
                role=role,
                llm_client=llm_client,
                budget=budget,
                max_iterations=max_worker_iterations,
                full_registry=self._registry,
            )
            for role in WorkerRole
        }

    def run(self, request: str) -> MultiAgentRunResult:
        """Execute the orchestrator meta-loop for *request*.

        Returns a :class:`MultiAgentRunResult` regardless of how the
        loop terminates.
        """
        t0 = time.monotonic()
        result = MultiAgentRunResult()
        delegation_history: list[dict[str, Any]] = []

        # Shared state flowing between workers via orchestrator
        _last_spec: dict[str, Any] | None = None
        _last_report: dict[str, Any] | None = None
        # The security worker is advisory: its findings exist only in its
        # returned summary, which was recorded in delegation_history and
        # never passed on. Delegations of the form "apply the security
        # worker's recommendations" therefore reached the config worker
        # without them, and it re-derived the advice through query_rag.
        _last_security_summary: str | None = None
        all_worker_history: list[HistoryEntry] = []

        try:
            for iteration in range(1, self._max_iterations + 1):
                log = logger.bind(orchestrator_iteration=iteration)

                # 1. Ask orchestrator LLM what to do next
                messages = render_orchestrator_prompt(
                    request, delegation_history
                )

                try:
                    step, _usage = self._enforcer.chat(
                        messages, schema=OrchestratorStep
                    )
                except SchemaParseError as exc:
                    log.warning(
                        "orchestrator_parse_error", error=str(exc)[:200]
                    )
                    # Record failure and retry
                    delegation_history.append(
                        {
                            "worker": "orchestrator",
                            "task": "parse_retry",
                            "success": False,
                            "summary": f"LLM parse error: {str(exc)[:200]}",
                            "artifacts": {},
                        }
                    )
                    continue

                log.info(
                    "orchestrator_step",
                    action=step.action.value,  # type: ignore[union-attr]
                    target=getattr(step, "target_worker", None),
                    reasoning=step.reasoning[:100],  # type: ignore[union-attr]
                )

                # 2. Handle finalise
                if step.action == OrchestratorAction.FINALISE:  # type: ignore[union-attr]
                    if _last_spec is not None:
                        # Use the finalise tool through the registry
                        from src.schemas.agent import ToolCall

                        finalise_call = ToolCall(
                            name="finalise",
                            args={
                                "final_spec": _last_spec,
                                "reason": step.finalise_reason  # type: ignore[union-attr]
                                or "Orchestrator decided to finalise",
                            },
                        )
                        observation = self._registry.dispatch(finalise_call)

                        if observation.success:
                            decision = observation.result
                            if isinstance(decision, dict) and decision.get(
                                "accept"
                            ):
                                final_spec_data = decision.get("final_spec")
                                if final_spec_data:
                                    result.final_spec = (
                                        StackSpec.model_validate(
                                            final_spec_data
                                        )
                                    )
                                result.termination_reason = "finalised"
                                break

                    # Finalise without a spec — shouldn't happen but handle it
                    log.warning("orchestrator_finalise_no_spec")
                    delegation_history.append(
                        {
                            "worker": "orchestrator",
                            "task": "finalise",
                            "success": False,
                            "summary": "Tried to finalise but no spec available",
                            "artifacts": {},
                        }
                    )
                    continue

                # 3. Handle delegation
                if step.action == OrchestratorAction.DELEGATE:  # type: ignore[union-attr]
                    target = step.target_worker  # type: ignore[union-attr]
                    if target is None:
                        log.warning("orchestrator_delegate_no_target")
                        delegation_history.append(
                            {
                                "worker": "unknown",
                                "task": step.task_description,  # type: ignore[union-attr]
                                "success": False,
                                "summary": "No target worker specified",
                                "artifacts": {},
                            }
                        )
                        continue

                    worker = self._workers[target]
                    task_desc = step.task_description  # type: ignore[union-attr]

                    # Build context for the worker
                    worker_context: dict[str, Any] = {}
                    if _last_spec is not None:
                        worker_context["last_spec"] = _last_spec
                    if _last_report is not None:
                        worker_context["last_report"] = _last_report

                    # Enrich task description with relevant context
                    if (
                        target == WorkerRole.VALIDATION
                        and _last_spec is not None
                    ):
                        task_desc += (
                            "\n\nThe StackSpec to validate has been "
                            "provided via context (auto-injected)."
                        )
                    if (
                        target == WorkerRole.SECURITY
                        and _last_report is not None
                    ):
                        # Include CIS failures in the task
                        cis_results = _last_report.get("cis_results", [])
                        failures = [
                            c
                            for c in cis_results
                            if isinstance(c, dict) and not c.get("passed")
                        ]
                        if failures:
                            failure_summary = "; ".join(
                                f"{f.get('check_id', '?')}: {f.get('description', '')[:60]}"
                                for f in failures[:10]
                            )
                            task_desc += (
                                f"\n\nCIS failures to address: {failure_summary}"
                            )
                    if (
                        target == WorkerRole.CONFIG
                        and _last_security_summary is not None
                    ):
                        # Carry the advice itself, not just the instruction
                        # to apply it, so the config worker does not have to
                        # rediscover it through the RAG index.
                        task_desc += (
                            "\n\nSecurity worker recommendations to apply: "
                            f"{_last_security_summary}"
                        )

                    log.info(
                        "orchestrator_delegating",
                        target=target.value,
                        task=task_desc[:100],
                    )

                    # 4. Run the worker
                    worker_result: WorkerResult = worker.run(
                        task_desc, context=worker_context
                    )

                    log.info(
                        "worker_returned",
                        worker=target.value,
                        success=worker_result.success,
                        iterations=worker_result.iterations_used,
                        tokens=worker_result.tokens_used,
                    )

                    # 5. Update shared state from worker artifacts
                    if (
                        target == WorkerRole.SECURITY
                        and worker_result.success
                        and worker_result.summary
                    ):
                        _last_security_summary = worker_result.summary
                    if "last_spec" in worker_result.artifacts:
                        _last_spec = worker_result.artifacts["last_spec"]
                    if "validator_report" in worker_result.artifacts:
                        _last_report = worker_result.artifacts[
                            "validator_report"
                        ]
                        try:
                            result.validator_report = (
                                ValidatorReport.model_validate(_last_report)
                            )
                        except Exception:  # noqa: BLE001
                            pass

                    # 6. Record delegation
                    delegation_entry = {
                        "worker": target.value,
                        "task": task_desc[:200],
                        "success": worker_result.success,
                        "summary": worker_result.summary,
                        # Kept so the run JSON can attribute cost per agent
                        # for the H3 analysis — see compute_per_agent_tokens.
                        "tokens_used": worker_result.tokens_used,
                        "iterations_used": worker_result.iterations_used,
                        "artifacts": {
                            k: (
                                "<spec>"
                                if k == "last_spec"
                                else v
                            )
                            for k, v in worker_result.artifacts.items()
                        },
                    }
                    delegation_history.append(delegation_entry)

            else:
                result.termination_reason = "max_iterations"

        except BudgetExhausted:
            result.termination_reason = "budget_exhausted"
            logger.warning("orchestrator_budget_exhausted")
        except Exception as exc:  # noqa: BLE001
            result.termination_reason = f"error: {exc}"
            logger.error("orchestrator_unexpected_error", error=str(exc))

        result.delegation_log = delegation_history
        result.tokens_used = self._budget.summary()["total_used"]
        result.wall_clock_s = time.monotonic() - t0
        return result
