"""Specialist worker agent — a mini-ReAct loop with restricted tools.

Each worker is parameterised by role (config | security | validation),
which determines its system prompt and which tools it can access.
Workers are invoked by the orchestrator, run a short ReAct loop, and
return a :class:`~src.agents.multi_agent.schemas.WorkerResult`.

The worker reuses the same ``AgentStep`` schema as the single-agent
baseline so the LLM output format is identical — the only difference
is a narrower prompt and tool set.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from pydantic import ValidationError

from src.agents.single_agent.agent import AgentStep
from src.llm.client import BudgetEnforcer, LLMClient, SchemaParseError
from src.llm.token_budget import BudgetExhausted, TokenBudget
from src.schemas.agent import AgentThought, HistoryEntry, ToolCall, ToolObservation
from src.schemas.validator_report import ValidatorReport
from src.tools.registry import (
    Tool,
    ToolRegistry,
    get_default_registry,
)

from .prompts import render_worker_prompt
from .schemas import WorkerResult, WorkerRole

logger = structlog.get_logger(__name__)

# ── Tool subsets per worker role ──────────────────────────────────────────

_WORKER_TOOLS: dict[str, list[str]] = {
    "config": ["query_rag", "generate_config"],
    "security": ["query_rag"],
    "validation": ["validate_config"],
}

# ── Max iterations per worker (keep short to conserve budget) ─────────────

_DEFAULT_MAX_WORKER_ITERATIONS = 5


def _build_worker_registry(
    role: str,
    full_registry: ToolRegistry,
) -> ToolRegistry:
    """Create a restricted registry containing only the tools for *role*."""
    allowed = _WORKER_TOOLS.get(role, [])
    restricted = ToolRegistry()
    for name in allowed:
        tool = full_registry.get(name)
        if tool is not None:
            restricted.register(tool)
    return restricted


#: How much of each retrieved chunk to carry into a worker summary, and how
#: many chunks. The summary is forwarded to other workers via the
#: orchestrator, so it lands in their prompts and is charged to the token
#: budget that H3 measures. These bounds keep a multi-query worker's summary
#: to roughly a thousand characters while still carrying something specific
#: enough to act on.
_RAG_EXCERPT_CHARS = 240
_RAG_MAX_CHUNKS = 2


def _summarise_rag(entry: HistoryEntry) -> str:
    """Summarise a query_rag observation by what it *retrieved*.

    This previously reported ``entry.thought.reasoning`` — the worker's
    stated intention before searching — and discarded the returned chunks
    entirely. Because query_rag is the security worker's only tool, its
    whole summary was a series of intentions, so it could never communicate
    a finding: the orchestrator forwarded the summary to the config worker,
    which received no recommendations and re-derived them through its own
    RAG queries. The retrieved text is the payload; the question is kept as
    context for what the excerpts answer.
    """
    chunks = entry.observation.result
    question = str(entry.tool_call.args.get("question", "")).strip()
    context = f' for "{question[:80]}"' if question else ""

    if not isinstance(chunks, list) or not chunks:
        return f"RAG lookup{context} returned no results."

    excerpts: list[str] = []
    for chunk in chunks[:_RAG_MAX_CHUNKS]:
        if not isinstance(chunk, dict):
            continue
        # Collapse whitespace so multi-line corpus chunks stay on one line
        # and the character budget buys content rather than newlines.
        text = " ".join(str(chunk.get("text", "")).split())[:_RAG_EXCERPT_CHARS]
        if not text:
            continue
        source = str(chunk.get("source") or "unknown source")
        excerpts.append(f"[{source}] {text}")

    if not excerpts:
        return f"RAG lookup{context} returned results with no usable text."
    return f"Retrieved guidance{context}: " + " | ".join(excerpts)


class WorkerAgent:
    """Mini-ReAct agent scoped to a single specialist role.

    Args:
        role: one of config | security | validation.
        llm_client: raw LLM client (shared with orchestrator).
        budget: shared per-run token budget.
        max_iterations: cap on worker loop iterations.
        full_registry: the full tool registry; will be filtered by role.
    """

    def __init__(
        self,
        role: WorkerRole,
        llm_client: LLMClient,
        budget: TokenBudget,
        max_iterations: int = _DEFAULT_MAX_WORKER_ITERATIONS,
        full_registry: ToolRegistry | None = None,
    ) -> None:
        self._role = role
        self._client = llm_client
        self._budget = budget
        self._max_iterations = max_iterations
        self._full_registry = full_registry or get_default_registry()
        self._registry = _build_worker_registry(role.value, self._full_registry)
        self._enforcer = BudgetEnforcer(llm_client, budget)

    def run(
        self,
        task: str,
        context: dict[str, Any] | None = None,
    ) -> WorkerResult:
        """Execute the worker's mini-ReAct loop for *task*.

        Args:
            task: natural-language instruction from the orchestrator.
            context: optional dict with shared state (e.g. last_spec,
                     validation_report) injected by the orchestrator.

        Returns:
            A :class:`WorkerResult` summarising what happened.
        """
        t0 = time.monotonic()
        history: list[HistoryEntry] = []
        tool_call_log: list[dict[str, Any]] = []
        artifacts: dict[str, Any] = {}
        tokens_start = self._budget.summary()["total_used"]

        # Shared state passed from orchestrator
        _last_spec: dict[str, Any] | None = (
            context.get("last_spec") if context else None
        )

        # The orchestrator serialises the most recent validator report into
        # context, but this previously started as None and was only ever
        # assigned from the worker's *own* validate_config call, so an
        # inbound report was discarded. A worker asked to fix validation
        # failures could not see what had failed and had to rediscover it.
        last_report: ValidatorReport | None = None
        if context and context.get("last_report") is not None:
            try:
                last_report = ValidatorReport.model_validate(
                    context["last_report"]
                )
            except ValidationError:
                last_report = None

        # Surfaced through the task rather than the prompt template, so the
        # worker's instructions are unchanged and only the information it
        # was already meant to receive is added.
        if last_report is not None:
            task = (
                f"{task}\n\nMost recent validation result: "
                f"{last_report.summary()}"
            )

        try:
            for iteration in range(1, self._max_iterations + 1):
                log = logger.bind(
                    worker=self._role.value, iteration=iteration
                )

                # 1. Build prompt
                history_dicts = [e.model_dump(mode="json") for e in history]
                tools_desc = self._registry.list_tools()
                messages = render_worker_prompt(
                    self._role.value, task, history_dicts, tools_desc,
                )

                # 2. Structured LLM call
                try:
                    step, _usage = self._enforcer.chat(
                        messages, schema=AgentStep
                    )
                except SchemaParseError as exc:
                    log.warning(
                        "worker_step_parse_error",
                        error=str(exc)[:200],
                    )
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
                    "worker_step",
                    tool=tool_call.name,
                    reasoning=thought.reasoning[:100],
                )

                # 3. Dispatch tool (with same auto-inject logic as single-agent)
                deps: dict[str, Any] = {}
                if tool_call.name == "generate_config":
                    deps["llm_client"] = self._client
                    deps["budget"] = self._budget

                if tool_call.name == "validate_config" and _last_spec is not None:
                    tool_call = ToolCall(
                        name="validate_config",
                        args={"spec": _last_spec},
                    )
                    log.info("worker_auto_injected_spec_for_validation")

                observation = self._registry.dispatch(tool_call, **deps)

                # Cache generated spec (same pattern as single-agent)
                if (
                    tool_call.name == "generate_config"
                    and observation.success
                    and isinstance(observation.result, dict)
                    and "spec" in observation.result
                ):
                    _last_spec = observation.result["spec"]
                    artifacts["last_spec"] = _last_spec
                    observation = ToolObservation(
                        success=True,
                        result={
                            "config_generated": True,
                            "message": (
                                "Configuration generated and cached. "
                                "Report back to orchestrator."
                            ),
                        },
                    )

                # Track validator report
                if tool_call.name == "validate_config" and observation.success:
                    try:
                        last_report = ValidatorReport.model_validate(
                            observation.result
                        )
                        artifacts["validator_report"] = observation.result
                    except Exception:  # noqa: BLE001
                        pass

                # 4. Record history
                entry = HistoryEntry(
                    thought=thought,
                    tool_call=tool_call,
                    observation=observation,
                )
                history.append(entry)
                tool_call_log.append(
                    {"name": tool_call.name, "success": observation.success}
                )

                # 5. Workers terminate when they've done their job:
                #    - config worker: after successful generate_config
                #    - validation worker: after successful validate_config
                #    - security worker: after successful query_rag (advisory)
                if observation.success and tool_call.name in (
                    "generate_config",
                    "validate_config",
                ):
                    log.info("worker_task_complete", tool=tool_call.name)
                    break

                # Security worker: stop after 2 successful RAG queries
                # (it's advisory — one lookup + one follow-up is enough)
                if self._role == WorkerRole.SECURITY:
                    rag_successes = sum(
                        1
                        for e in history
                        if e.tool_call.name == "query_rag"
                        and e.observation.success
                    )
                    if rag_successes >= 2:
                        log.info("security_worker_advisory_complete")
                        break

        except BudgetExhausted:
            logger.warning(
                "worker_budget_exhausted", worker=self._role.value
            )

        tokens_end = self._budget.summary()["total_used"]

        # Build natural-language summary from final history
        summary = self._build_summary(history)

        return WorkerResult(
            worker=self._role,
            task=task,
            success=any(e.observation.success for e in history),
            summary=summary,
            iterations_used=len(history),
            tokens_used=tokens_end - tokens_start,
            tool_calls=tool_call_log,
            artifacts=artifacts,
        )

    @staticmethod
    def _build_summary(history: list[HistoryEntry]) -> str:
        """Produce a concise summary from the worker's history."""
        if not history:
            return "No actions taken."

        parts: list[str] = []
        for entry in history:
            tc = entry.tool_call
            obs = entry.observation
            if obs.success:
                if tc.name == "generate_config":
                    parts.append("Generated a StackSpec configuration.")
                elif tc.name == "validate_config":
                    # Extract key metrics from report
                    report = obs.result if isinstance(obs.result, dict) else {}
                    smoke = report.get("smoke_tests", {})
                    cis = report.get("cis_results", [])
                    smoke_ok = sum(
                        1
                        for s in smoke.values()
                        if isinstance(s, dict)
                        and s.get("did_start")
                        and s.get("accepts_connections")
                    ) if isinstance(smoke, dict) else 0
                    cis_pass = sum(
                        1 for c in cis if isinstance(c, dict) and c.get("passed")
                    )
                    parts.append(
                        f"Validation: {smoke_ok}/{len(smoke)} smoke tests passed, "
                        f"{cis_pass}/{len(cis)} CIS checks passed."
                    )
                elif tc.name == "query_rag":
                    parts.append(_summarise_rag(entry))
                else:
                    parts.append(f"{tc.name} succeeded.")
            else:
                err = obs.error or "unknown error"
                parts.append(f"{tc.name} failed: {err[:100]}")

        return " ".join(parts)
