"""Manager agent for the parallel per-service architecture.

Topology::

                         ┌─ postgres agent ─┐
    request → manager ───├─ nginx agent ────┼──→ merge/arbitrate → validate
                         └─ redis agent ────┘
                           (+ rabbitmq when selected)

The contrast with the star architecture is the shape of the decomposition, not
the amount of it. The star splits by *function* (config, security, validation)
and runs the parts in sequence, each seeing the previous one's output. This
splits by *service* and runs the parts concurrently, each seeing none of the
others. That is the arrangement the multi-agent literature's stronger claims
rest on, and the one Section 6.2 notes this dissertation never tested.

Budget parity is preserved exactly as in the star arm: one ``TokenBudget``
instance is shared by the manager and every service agent. ``TokenBudget``
guards its counters with a lock, so concurrent ``record_usage`` calls are safe.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.llm.client import BudgetEnforcer, LLMClient, SchemaParseError
from src.llm.token_budget import BudgetExhausted, TokenBudget
from src.schemas.agent import HistoryEntry
from src.schemas.stack import StackSpec
from src.schemas.validator_report import ValidatorReport
from src.tools.registry import ToolRegistry, get_default_registry

from .arbitration import ArbitrationLog, arbitrate
from .metering import MeteredBudget
from .prompts import render_manager_prompt
from .schemas import ManagerDecision, ServiceFragmentResult
from .service_agent import ServiceAgent

logger = structlog.get_logger(__name__)

#: Cap on concurrent service agents. Four is the whole catalogue, so this
#: never truncates the fan-out; it exists to bound thread creation.
_MAX_WORKERS = 4


def _catalogue() -> frozenset[str]:
    from src.services.catalog import names

    return frozenset(names())


#: Hallucinated service names are dropped rather than allowed to reach
#: StackSpec, which would reject the whole selection and score the run as
#: deliberately deploying nothing.
_CATALOGUE = _catalogue()


@dataclass
class ParallelRunResult:
    """Everything produced by a parallel run.

    Mirrors ``MultiAgentRunResult`` so the runner and the analysis scripts can
    treat the three arms uniformly.
    """

    final_spec: StackSpec | None = None
    validator_report: ValidatorReport | None = None
    history: list[HistoryEntry] = field(default_factory=list)
    delegation_log: list[dict[str, Any]] = field(default_factory=list)
    arbitration: dict[str, Any] = field(default_factory=dict)
    tokens_used: int = 0
    wall_clock_s: float = 0.0
    concurrent_wall_clock_s: float = 0.0
    sum_agent_wall_clock_s: float = 0.0
    termination_reason: str = "unknown"


class ParallelManagerAgent:
    """Decompose once, fan out concurrently, merge, arbitrate, validate."""

    def __init__(
        self,
        llm_client: LLMClient,
        budget: TokenBudget,
        max_iterations: int = 25,
        max_service_iterations: int = 4,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._client = llm_client
        self._shared_budget = budget
        # Metered for the same reason the service agents are: so the per-agent
        # split adds up to the run total exactly, leaving no residual for
        # compute_per_agent_tokens to attribute to a non-existent orchestrator.
        self._budget = MeteredBudget(budget)
        # Accepted for interface parity with the other two arms. The parallel
        # topology has no meta-loop to bound: the manager decides once. It is
        # recorded on the run so the three arms' records stay comparable.
        self._max_iterations = max_iterations
        self._max_service_iterations = max_service_iterations
        self._registry = registry or get_default_registry()
        self._enforcer = BudgetEnforcer(llm_client, self._budget)

    def run(self, request: str) -> ParallelRunResult:
        t0 = time.monotonic()
        result = ParallelRunResult()
        delegation_log: list[dict[str, Any]] = []

        try:
            decision = self._decide(request, delegation_log)
            if decision is None:
                result.termination_reason = "manager_decision_failed"
                return self._finish(result, delegation_log, t0)

            # The top-level selection is authoritative; the nested copy inside
            # requirements is kept in step so generate_config and the selection
            # metric see the same decision.
            selected = [
                name for name in decision.selected_services if name in _CATALOGUE
            ]
            if not selected:
                result.termination_reason = "manager_selected_nothing"
                return self._finish(result, delegation_log, t0)
            requirements = decision.requirements.model_dump(mode="json")
            requirements["selected_services"] = selected

            logger.info("manager_decomposed", services=selected)

            # Warm the retrieval singleton on this thread before fanning out.
            # query_rag builds its HybridRetriever lazily; several threads
            # racing that construction would each load the encoder.
            self._warm_retriever()

            fragments, fan_out_s = self._fan_out(
                request, selected, requirements, decision, delegation_log
            )
            result.concurrent_wall_clock_s = fan_out_s
            result.sum_agent_wall_clock_s = sum(
                entry.get("wall_clock_s", 0.0)
                for entry in delegation_log
                if entry.get("worker") in selected
            )

            if not fragments:
                result.termination_reason = "no_fragments_produced"
                return self._finish(result, delegation_log, t0)

            spec = self._merge(
                fragments, requirements, result, delegation_log,
                concurrency={
                    "fan_out_wall_clock_s": round(fan_out_s, 3),
                    "sum_agent_wall_clock_s": round(
                        result.sum_agent_wall_clock_s, 3
                    ),
                    "agents": len(selected),
                },
            )
            if spec is None:
                result.termination_reason = "merge_failed"
                return self._finish(result, delegation_log, t0)

            result.final_spec = spec
            self._validate(spec, result, delegation_log)
            result.termination_reason = "finalised"

        except BudgetExhausted:
            result.termination_reason = "budget_exhausted"
            logger.warning("manager_budget_exhausted")
        except Exception as exc:  # noqa: BLE001 — must not crash the runner
            result.termination_reason = f"error: {exc}"
            logger.error("manager_unexpected_error", error=str(exc))

        return self._finish(result, delegation_log, t0)

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _decide(
        self, request: str, delegation_log: list[dict[str, Any]]
    ) -> ManagerDecision | None:
        """The single decomposition call. This is the selection decision."""
        try:
            decision, _usage = self._enforcer.chat(
                render_manager_prompt(request), schema=ManagerDecision
            )
        except SchemaParseError as exc:
            logger.warning("manager_parse_error", error=str(exc)[:200])
            delegation_log.append(
                {
                    "worker": "manager",
                    "task": "decompose",
                    "success": False,
                    "summary": f"LLM parse error: {str(exc)[:200]}",
                    "tokens_used": self._budget.own_tokens,
                }
            )
            return None

        delegation_log.append(
            {
                "worker": "manager",
                "task": "decompose",
                "success": True,
                "summary": decision.reasoning[:500],
                "tokens_used": self._budget.own_tokens,
                "selected_services": list(decision.selected_services),
            }
        )
        return decision  # type: ignore[return-value]

    def _warm_retriever(self) -> None:
        """Build the RAG retriever singleton once, on the calling thread."""
        try:
            from src.tools.rag import _get_retriever

            _get_retriever()
        except Exception as exc:  # noqa: BLE001 — warming is best-effort
            logger.warning("retriever_warm_failed", error=str(exc)[:200])

    def _fan_out(
        self,
        request: str,
        selected: list[str],
        requirements: dict[str, Any],
        decision: ManagerDecision,
        delegation_log: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], float]:
        """Run one agent per selected service concurrently.

        Returns the fragments and the wall-clock time the fan-out took, which
        is the number P2 is about: it should be close to the slowest agent
        rather than to the sum of them.
        """
        fragments: dict[str, dict[str, Any]] = {}
        fan_out_start = time.monotonic()

        with ThreadPoolExecutor(
            max_workers=min(_MAX_WORKERS, len(selected)),
            thread_name_prefix="svc",
        ) as pool:
            futures = {
                pool.submit(
                    ServiceAgent(
                        service=service,
                        llm_client=self._client,
                        budget=self._budget,
                        max_iterations=self._max_service_iterations,
                        full_registry=self._registry,
                    ).run,
                    request,
                    requirements,
                    decision.per_service_notes.get(service, ""),
                ): service
                for service in selected
            }
            for future in as_completed(futures):
                service = futures[future]
                try:
                    outcome: ServiceFragmentResult = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "service_agent_crashed", service=service, error=str(exc)[:200]
                    )
                    delegation_log.append(
                        {
                            "worker": service,
                            "task": f"configure {service}",
                            "success": False,
                            "summary": f"agent crashed: {str(exc)[:200]}",
                            "tokens_used": 0,
                        }
                    )
                    continue

                if outcome.fragment:
                    fragments.update(outcome.fragment)
                delegation_log.append(
                    {
                        "worker": service,
                        "task": f"configure {service}",
                        "success": outcome.success,
                        "summary": outcome.summary,
                        "tokens_used": outcome.tokens_used,
                        "iterations_used": outcome.iterations_used,
                        "wall_clock_s": round(outcome.wall_clock_s, 3),
                        "tool_calls": outcome.tool_calls,
                        "error": outcome.error,
                    }
                )

        return fragments, time.monotonic() - fan_out_start

    def _merge(
        self,
        fragments: dict[str, dict[str, Any]],
        requirements: dict[str, Any],
        result: ParallelRunResult,
        delegation_log: list[dict[str, Any]],
        concurrency: dict[str, Any] | None = None,
    ) -> StackSpec | None:
        """Combine fragments into one StackSpec, arbitrating shared memory.

        ``selected_services`` is rewritten to the services that actually
        produced a fragment. An agent that failed did not deploy its service,
        and the selection metric reads deployment from the spec, so claiming it
        here would score a service that does not exist in the stack.
        """
        hardware = requirements.get("hardware") or {}
        ram_gb = float(hardware.get("ram_gb") or 0) or 1.0

        service_keys = [key for key in fragments if key != "pg_hba"]
        arbitrated, log = arbitrate(
            {key: fragments[key] for key in service_keys}, ram_gb
        )
        result.arbitration = log.to_dict()

        merged: dict[str, Any] = dict(arbitrated)
        if "pg_hba" in fragments:
            merged["pg_hba"] = fragments["pg_hba"]

        effective = dict(requirements)
        effective["selected_services"] = sorted(service_keys)
        merged["requirements"] = effective

        delegation_log.append(
            {
                "worker": "merge",
                "task": "combine fragments and arbitrate shared memory",
                "success": True,
                "summary": self._merge_summary(log, service_keys),
                "tokens_used": 0,
                "arbitration": result.arbitration,
                "concurrency": concurrency or {},
            }
        )

        try:
            return StackSpec.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            logger.error("merge_validation_failed", error=str(exc)[:300])
            delegation_log.append(
                {
                    "worker": "merge",
                    "task": "validate merged StackSpec",
                    "success": False,
                    "summary": f"merged spec rejected: {str(exc)[:300]}",
                    "tokens_used": 0,
                }
            )
            return None

    @staticmethod
    def _merge_summary(log: ArbitrationLog, services: list[str]) -> str:
        head = f"Merged {len(services)} fragments ({', '.join(sorted(services))})."
        if not log.arbitrated:
            return (
                f"{head} Reservations totalled "
                f"{log.requested_bytes / 1024**3:.2f}GB against a "
                f"{log.ceiling_bytes / 1024**3:.2f}GB ceiling; no arbitration."
            )
        return (
            f"{head} Overcommit: {log.requested_bytes / 1024**3:.2f}GB requested "
            f"against a {log.ceiling_bytes / 1024**3:.2f}GB ceiling; scaled all "
            f"reservations by {log.scale_factor:.3f}."
        )

    def _validate(
        self,
        spec: StackSpec,
        result: ParallelRunResult,
        delegation_log: list[dict[str, Any]],
    ) -> None:
        """Deploy and test the merged spec, as the other two arms do."""
        from src.schemas.agent import ToolCall

        observation = self._registry.dispatch(
            ToolCall(name="validate_config", args={"spec": spec.model_dump(mode="json")})
        )
        entry: dict[str, Any] = {
            "worker": "validation",
            "task": "deploy and test the merged spec",
            "success": observation.success,
            "tokens_used": 0,
        }
        if observation.success:
            try:
                result.validator_report = ValidatorReport.model_validate(
                    observation.result
                )
                entry["summary"] = result.validator_report.summary()
            except Exception as exc:  # noqa: BLE001
                entry["summary"] = f"report unparseable: {str(exc)[:200]}"
        else:
            entry["summary"] = f"validation failed: {(observation.error or '')[:200]}"
        delegation_log.append(entry)

    def _finish(
        self,
        result: ParallelRunResult,
        delegation_log: list[dict[str, Any]],
        t0: float,
    ) -> ParallelRunResult:
        result.delegation_log = delegation_log
        result.tokens_used = self._budget.summary()["total_used"]
        result.wall_clock_s = time.monotonic() - t0
        return result
