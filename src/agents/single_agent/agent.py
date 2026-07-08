"""Single-agent ReAct loop.

One LLM drives a think → act → observe loop. At each iteration:

1. Build a prompt from the request + full history.
2. Structured LLM call returns an ``AgentStep`` (thought + tool_call).
3. The tool call is dispatched through the registry.
4. The observation is appended to history.
5. If the tool was ``finalise`` (and accepted), terminate.

Design note on structured output: :meth:`LLMClient.chat` takes a
*single* Pydantic schema. We define :class:`AgentStep` that combines
:class:`AgentThought` and :class:`ToolCall` into one model. This avoids
two sequential LLM calls per iteration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, Field

from src.llm.client import BudgetEnforcer, LLMClient, SchemaParseError
from src.llm.token_budget import BudgetExhausted, TokenBudget
from src.schemas.agent import AgentThought, HistoryEntry, ToolCall, ToolObservation
from src.schemas.stack import StackSpec
from src.schemas.validator_report import ValidatorReport
from src.tools.registry import ToolRegistry, get_default_registry

from .prompts import render_react_prompt

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Combined schema for one LLM step (thought + action in a single call)
# ---------------------------------------------------------------------------


class AgentStep(BaseModel):
    """Combined output the LLM must produce at each ReAct iteration.

    Using a single schema avoids two sequential LLM calls per loop
    iteration while still getting structured, validated output.
    """

    thought: AgentThought
    tool_call: ToolCall = Field(description="Which tool to invoke and with what args")


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------


@dataclass
class AgentRunResult:
    """Everything produced by a single agent run."""

    final_spec: StackSpec | None = None
    validator_report: ValidatorReport | None = None
    history: list[HistoryEntry] = field(default_factory=list)
    tokens_used: int = 0
    wall_clock_s: float = 0.0
    termination_reason: str = "unknown"


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class SingleAgent:
    """ReAct agent backed by one LLM and the standard tool registry.

    Args:
        llm_client: raw LLM client (budget enforcement is layered on).
        budget: per-task token budget.
        max_iterations: safety cap on loop iterations.
        registry: tool registry; defaults to the four standard tools.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        budget: TokenBudget,
        max_iterations: int = 25,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._client = llm_client
        self._budget = budget
        self._max_iterations = max_iterations
        self._registry = registry or get_default_registry()
        self._enforcer = BudgetEnforcer(llm_client, budget)

    def run(self, request: str) -> AgentRunResult:
        """Execute the ReAct loop for *request*.

        Returns an :class:`AgentRunResult` regardless of how the loop
        terminates (finalisation, budget exhaustion, max iterations,
        or unexpected error).
        """
        t0 = time.monotonic()
        history: list[HistoryEntry] = []
        result = AgentRunResult()
        last_report: ValidatorReport | None = None
        # Cache the last successfully generated spec so validate_config
        # can use it even when the agent constructs a broken spec dict.
        _last_spec: dict[str, Any] | None = None

        try:
            for iteration in range(1, self._max_iterations + 1):
                log = logger.bind(iteration=iteration)

                # 1. Build prompt
                history_dicts = [e.model_dump(mode="json") for e in history]
                tools_desc = self._registry.list_tools()
                messages = render_react_prompt(request, history_dicts, tools_desc)

                # 2. Structured LLM call
                try:
                    step, _usage = self._enforcer.chat(messages, schema=AgentStep)
                except SchemaParseError as exc:
                    log.warning(
                        "agent_step_parse_error",
                        iteration=iteration,
                        error=str(exc)[:200],
                    )
                    # Record as a failed observation so the next iteration
                    # sees the error and can try again.
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
                    "agent_step",
                    tool=tool_call.name,
                    reasoning=thought.reasoning[:100],
                    tokens_used=self._budget.summary()["total_used"],
                )

                # 3. Dispatch tool
                deps: dict[str, Any] = {}
                if tool_call.name == "generate_config":
                    deps["llm_client"] = self._client
                    deps["budget"] = self._budget

                # Small models often fail to forward the generated spec to
                # validate_config / finalise, constructing a broken dict
                # instead.  Auto-inject the last successfully generated spec.
                if tool_call.name == "validate_config" and _last_spec is not None:
                    tool_call = ToolCall(
                        name="validate_config",
                        args={"spec": _last_spec},
                    )
                    log.info("auto_injected_spec_for_validation")

                if tool_call.name == "finalise" and _last_spec is not None:
                    tool_call = ToolCall(
                        name="finalise",
                        args={
                            "final_spec": _last_spec,
                            "reason": tool_call.args.get(
                                "reason", "auto-finalised with last generated spec"
                            ),
                        },
                    )
                    log.info("auto_injected_spec_for_finalise")

                observation = self._registry.dispatch(tool_call, **deps)

                # Strip rendered config strings from generate_config
                # observations — the agent only needs the spec dict, and
                # the rendered text wastes context tokens for small models.
                if (
                    tool_call.name == "generate_config"
                    and observation.success
                    and isinstance(observation.result, dict)
                    and "spec" in observation.result
                ):
                    _last_spec = observation.result["spec"]
                    observation = ToolObservation(
                        success=True,
                        result={
                            "config_generated": True,
                            "spec": _last_spec,
                        },
                    )

                # Track latest validator report
                if tool_call.name == "validate_config" and observation.success:
                    try:
                        last_report = ValidatorReport.model_validate(observation.result)
                    except Exception:  # noqa: BLE001 — best-effort
                        pass

                # 4. Record history
                entry = HistoryEntry(
                    thought=thought,
                    tool_call=tool_call,
                    observation=observation,
                )
                history.append(entry)

                # 5. Check for finalisation
                if tool_call.name == "finalise" and observation.success:
                    decision = observation.result
                    if isinstance(decision, dict) and decision.get("accept"):
                        final_spec_data = decision.get("final_spec")
                        if final_spec_data:
                            result.final_spec = StackSpec.model_validate(final_spec_data)
                        result.termination_reason = "finalised"
                        break
            else:
                result.termination_reason = "max_iterations"

        except BudgetExhausted:
            result.termination_reason = "budget_exhausted"
            logger.warning("agent_budget_exhausted")
        except Exception as exc:  # noqa: BLE001 — must capture partial run
            result.termination_reason = f"error: {exc}"
            logger.error("agent_unexpected_error", error=str(exc))

        result.history = history
        result.validator_report = last_report
        result.tokens_used = self._budget.summary()["total_used"]
        result.wall_clock_s = time.monotonic() - t0
        return result
