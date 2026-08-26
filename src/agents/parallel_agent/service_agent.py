"""One per-service specialist agent, run concurrently with its siblings.

Structurally a mini-ReAct loop, like ``multi_agent.worker.WorkerAgent``, but
scoped to a single service rather than to a functional role. It holds
``query_rag`` and ``generate_config`` and returns a *fragment*: its own
service's config section only.

Thread safety
-------------
Instances run inside a ``ThreadPoolExecutor``. Everything mutable is local to
:meth:`run`. The shared objects are the ``TokenBudget`` (thread-safe by
``threading.Lock``), the LLM client (stateless per call), and the tool
registry (read-only dispatch). ``query_rag``'s retriever singleton is built
lazily, so it is warmed once before the pool starts rather than being raced by
several threads at once — see ``manager.ParallelManagerAgent.run``.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from src.agents.single_agent.agent import AgentStep
from src.llm.client import BudgetEnforcer, LLMClient, SchemaParseError
from src.llm.token_budget import BudgetExhausted, TokenBudget
from src.schemas.agent import AgentThought, HistoryEntry, ToolCall, ToolObservation
from src.tools.registry import ToolRegistry, get_default_registry

from .metering import MeteredBudget
from .prompts import render_service_agent_prompt
from .schemas import ServiceFragmentResult

logger = structlog.get_logger(__name__)

#: Tools every service agent holds. Deliberately the same pair the star
#: architecture's config worker holds, so tool access is not a confound.
_SERVICE_AGENT_TOOLS = ("query_rag", "generate_config")

_DEFAULT_MAX_SERVICE_ITERATIONS = 4

#: postgres carries pg_hba with it — StackSpec rejects one without the other,
#: so the postgres agent owns both.
_COUPLED_KEYS = {"postgres": ("postgres", "pg_hba")}


def _build_service_registry(full_registry: ToolRegistry) -> ToolRegistry:
    restricted = ToolRegistry()
    for name in _SERVICE_AGENT_TOOLS:
        tool = full_registry.get(name)
        if tool is not None:
            restricted.register(tool)
    return restricted


class ServiceAgent:
    """ReAct agent responsible for exactly one catalogue service."""

    def __init__(
        self,
        service: str,
        llm_client: LLMClient,
        budget: TokenBudget,
        max_iterations: int = _DEFAULT_MAX_SERVICE_ITERATIONS,
        full_registry: ToolRegistry | None = None,
    ) -> None:
        self._service = service
        self._client = llm_client
        # Every debit still lands on the one shared budget; the meter only
        # records this agent's share, which a before/after read of the shared
        # budget cannot do while siblings are spending concurrently.
        self._budget = MeteredBudget(budget)
        self._max_iterations = max_iterations
        self._full_registry = full_registry or get_default_registry()
        self._registry = _build_service_registry(self._full_registry)
        self._enforcer = BudgetEnforcer(llm_client, self._budget)

    def run(
        self,
        request: str,
        requirements: dict[str, Any],
        note: str = "",
    ) -> ServiceFragmentResult:
        """Run the loop and return this service's fragment.

        Never raises: a failure here must not take down the other agents in the
        pool or lose their work, so every exit path produces a
        :class:`ServiceFragmentResult`.
        """
        t0 = time.monotonic()
        history: list[HistoryEntry] = []
        tool_calls: list[dict[str, Any]] = []
        fragment: dict[str, Any] = {}
        error = ""

        log = logger.bind(service_agent=self._service)

        try:
            for iteration in range(1, self._max_iterations + 1):
                history_dicts = [e.model_dump(mode="json") for e in history]
                messages = render_service_agent_prompt(
                    self._service,
                    request,
                    requirements,
                    note,
                    history_dicts,
                    self._registry.list_tools(),
                )

                try:
                    step, _usage = self._enforcer.chat(messages, schema=AgentStep)
                except SchemaParseError as exc:
                    log.warning("service_agent_parse_error", error=str(exc)[:200])
                    history.append(
                        HistoryEntry(
                            thought=AgentThought(
                                reasoning="LLM produced unparseable JSON",
                                planned_next_action="retry",
                            ),
                            tool_call=ToolCall(name="none", args={}),
                            observation=ToolObservation(
                                success=False,
                                error=f"Schema parse failure: {str(exc)[:300]}",
                            ),
                        )
                    )
                    continue

                thought = step.thought  # type: ignore[union-attr]
                tool_call = step.tool_call  # type: ignore[union-attr]
                log.info(
                    "service_agent_step",
                    iteration=iteration,
                    tool=tool_call.name,
                    reasoning=thought.reasoning[:100],
                )

                deps: dict[str, Any] = {}
                if tool_call.name == "generate_config":
                    deps["llm_client"] = self._client
                    deps["budget"] = self._budget
                    tool_call = self._pin_partial_spec(tool_call, requirements)

                observation = self._registry.dispatch(tool_call, **deps)

                if (
                    tool_call.name == "generate_config"
                    and observation.success
                    and isinstance(observation.result, dict)
                    and "spec" in observation.result
                ):
                    fragment = self._extract_fragment(observation.result["spec"])
                    observation = ToolObservation(
                        success=True,
                        result={
                            "config_generated": True,
                            "service": self._service,
                            "message": (
                                f"{self._service} section generated and kept. "
                                "Other services were discarded."
                            ),
                        },
                    )

                history.append(
                    HistoryEntry(
                        thought=thought,
                        tool_call=tool_call,
                        observation=observation,
                    )
                )
                tool_calls.append(
                    {"name": tool_call.name, "success": observation.success}
                )

                if fragment:
                    log.info("service_agent_complete", iteration=iteration)
                    break

        except BudgetExhausted:
            error = "budget_exhausted"
            log.warning("service_agent_budget_exhausted")
        except Exception as exc:  # noqa: BLE001 — must not kill sibling agents
            error = f"{type(exc).__name__}: {exc}"
            log.error("service_agent_error", error=str(exc)[:200])

        return ServiceFragmentResult(
            service=self._service,
            success=bool(fragment),
            summary=self._summarise(history, bool(fragment)),
            iterations_used=len(history),
            tokens_used=self._budget.own_tokens,
            wall_clock_s=time.monotonic() - t0,
            fragment=fragment,
            tool_calls=tool_calls,
            error=error,
        )

    def _pin_partial_spec(
        self, tool_call: ToolCall, requirements: dict[str, Any]
    ) -> ToolCall:
        """Force the shared requirements block into the agent's partial spec.

        Every agent must size against the *same* host for the contention this
        arm exists to measure to be real. Left to the prompt, agents restate
        the hardware inconsistently. ``selected_services`` is pinned to this
        agent's own service so ``generate_config`` completes one section rather
        than quietly filling in the whole stack.
        """
        args = dict(tool_call.args or {})
        partial = dict(args.get("partial_spec") or {})
        pinned = dict(requirements)
        pinned["selected_services"] = [self._service]
        partial["requirements"] = pinned
        args["partial_spec"] = partial
        args.setdefault("mode", "complete")
        return ToolCall(name="generate_config", args=args)

    def _extract_fragment(self, spec: Any) -> dict[str, Any]:
        """Keep only this agent's own service section from a completed spec.

        Enforced in code rather than trusted to the prompt, matching how
        ``generate_config`` already enforces ``selected_services``. Without
        this, an agent that helpfully filled in all four services would give
        the merge step several conflicting opinions about services it does not
        own.
        """
        if hasattr(spec, "model_dump"):
            spec = spec.model_dump(mode="json")
        if not isinstance(spec, dict):
            return {}
        keys = _COUPLED_KEYS.get(self._service, (self._service,))
        out = {key: spec[key] for key in keys if spec.get(key) is not None}
        # A postgres fragment without pg_hba cannot be merged into a valid
        # StackSpec, so treat it as no fragment at all rather than letting the
        # merge fail later and less legibly.
        if self._service == "postgres" and set(out) != {"postgres", "pg_hba"}:
            return {}
        return out

    def _summarise(self, history: list[HistoryEntry], produced: bool) -> str:
        if not history:
            return f"{self._service}: no actions taken."
        parts = [
            f"{e.tool_call.name} "
            + ("succeeded" if e.observation.success else "failed")
            for e in history
        ]
        outcome = "produced a fragment" if produced else "produced no fragment"
        return f"{self._service}: {', '.join(parts)}; {outcome}."
