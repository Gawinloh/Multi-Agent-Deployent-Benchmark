"""Tests for worker-output routing through the orchestrator.

The pilot showed two places where information the orchestrator held never
reached the worker that needed it: the security worker's advice, and the
validator report. Both made workers rediscover what the system already
knew, which cost tokens and iterations and shows up in H3.

These tests pin the routing, not the wording of any prompt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from src.agents.multi_agent.schemas import OrchestratorStep, WorkerResult, WorkerRole
from src.agents.multi_agent.worker import WorkerAgent
from src.agents.single_agent.agent import AgentStep
from src.llm.client import TokenUsage
from src.llm.token_budget import TokenBudget
from src.schemas.agent import AgentThought, ToolCall


class CapturingClient:
    """Records every prompt it is asked to complete.

    Returns a step calling a tool the worker does not have, so the loop
    takes one iteration and stops without needing Docker or the RAG index.
    """

    backend_name = "fake"

    def __init__(self) -> None:
        self.prompts: list[list[dict[str, str]]] = []

    @property
    def model_name(self) -> str:
        return "fake-model"

    def chat(
        self, messages: list[dict[str, str]], schema: type[BaseModel] | None = None
    ) -> tuple[Any, TokenUsage]:
        self.prompts.append(messages)
        step = AgentStep(
            thought=AgentThought(
                reasoning="inspecting the task", planned_next_action="stop"
            ),
            tool_call=ToolCall(name="no_such_tool", args={}),
        )
        return step, TokenUsage(10, 5)

    def all_text(self) -> str:
        return "\n".join(
            m["content"] for prompt in self.prompts for m in prompt
        )


def _report(error: str | None = None) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "smoke_tests": {},
        "benchmarks": {},
        "cis_results": [],
        "healthchecks": {},
        "error": error,
    }


def _worker(role: WorkerRole, client: CapturingClient) -> WorkerAgent:
    return WorkerAgent(
        role=role,
        llm_client=client,
        budget=TokenBudget(limit=10_000),
        max_iterations=1,
    )


class TestWorkerConsumesValidatorReport:
    def test_inbound_report_reaches_the_worker(self) -> None:
        """Previously last_report was initialised to None and only ever set
        from the worker's own validate_config call, so a report handed in by
        the orchestrator was silently dropped."""
        client = CapturingClient()
        worker = _worker(WorkerRole.CONFIG, client)

        worker.run(
            "fix the failing configuration",
            context={"last_report": _report(error="postgres exited (1)")},
        )

        text = client.all_text()
        assert "Most recent validation result" in text
        assert "postgres exited (1)" in text

    def test_absent_report_changes_nothing(self) -> None:
        client = CapturingClient()
        worker = _worker(WorkerRole.CONFIG, client)

        worker.run("generate a configuration", context={})

        assert "Most recent validation result" not in client.all_text()

    def test_malformed_report_is_ignored_not_fatal(self) -> None:
        """A context report that fails validation must not take the run down."""
        client = CapturingClient()
        worker = _worker(WorkerRole.CONFIG, client)

        result = worker.run(
            "generate a configuration",
            context={"last_report": {"unexpected": "shape"}},
        )

        assert result is not None
        assert "Most recent validation result" not in client.all_text()


class RecordingWorker:
    """Fake worker capturing the task and context the orchestrator sends."""

    def __init__(self, role: WorkerRole, summary: str) -> None:
        self._role = role
        self._summary = summary
        self.tasks: list[str] = []
        self.contexts: list[dict[str, Any]] = []

    def run(self, task: str, context: dict[str, Any] | None = None) -> Any:
        self.tasks.append(task)
        self.contexts.append(context or {})
        return WorkerResult(
            worker=self._role,
            task=task,
            success=True,
            summary=self._summary,
            iterations_used=1,
            tokens_used=100,
        )


class ScriptedOrchestratorClient:
    """Drives the orchestrator through a fixed sequence of decisions."""

    backend_name = "fake"

    def __init__(self, steps: list[OrchestratorStep]) -> None:
        self._steps = list(steps)

    @property
    def model_name(self) -> str:
        return "fake-model"

    def chat(
        self, messages: list[dict[str, str]], schema: type[BaseModel] | None = None
    ) -> tuple[Any, TokenUsage]:
        step = self._steps.pop(0) if self._steps else OrchestratorStep(
            reasoning="done", action="finalise"
        )
        return step, TokenUsage(10, 5)


class TestSecurityAdviceReachesConfigWorker:
    """The security worker's findings live only in its returned summary.
    The orchestrator recorded them in delegation_history and passed them
    nowhere, so a config worker told to "apply the security worker's
    recommendations" received the instruction without the recommendations
    and recovered them through query_rag instead."""

    def test_config_task_carries_the_security_summary(self) -> None:
        from src.agents.multi_agent.orchestrator import OrchestratorAgent

        advice = "rename FLUSHALL and CONFIG; set X-Frame-Options"
        client = ScriptedOrchestratorClient(
            [
                OrchestratorStep(
                    reasoning="get security advice",
                    action="delegate",
                    target_worker=WorkerRole.SECURITY,
                    task_description="review the security posture",
                ),
                OrchestratorStep(
                    reasoning="apply it",
                    action="delegate",
                    target_worker=WorkerRole.CONFIG,
                    task_description="apply the security recommendations",
                ),
                OrchestratorStep(reasoning="done", action="finalise"),
            ]
        )
        orchestrator = OrchestratorAgent(
            llm_client=client, budget=TokenBudget(limit=100_000)
        )
        security = RecordingWorker(WorkerRole.SECURITY, advice)
        config = RecordingWorker(WorkerRole.CONFIG, "generated")
        orchestrator._workers[WorkerRole.SECURITY] = security  # type: ignore[assignment]
        orchestrator._workers[WorkerRole.CONFIG] = config  # type: ignore[assignment]

        orchestrator.run("provision a stack")

        assert config.tasks, "config worker was never delegated to"
        assert advice in config.tasks[0]

    def test_config_task_unchanged_when_no_security_advice_yet(self) -> None:
        from src.agents.multi_agent.orchestrator import OrchestratorAgent

        client = ScriptedOrchestratorClient(
            [
                OrchestratorStep(
                    reasoning="generate first",
                    action="delegate",
                    target_worker=WorkerRole.CONFIG,
                    task_description="generate the configuration",
                ),
                OrchestratorStep(reasoning="done", action="finalise"),
            ]
        )
        orchestrator = OrchestratorAgent(
            llm_client=client, budget=TokenBudget(limit=100_000)
        )
        config = RecordingWorker(WorkerRole.CONFIG, "generated")
        orchestrator._workers[WorkerRole.CONFIG] = config  # type: ignore[assignment]

        orchestrator.run("provision a stack")

        assert config.tasks == ["generate the configuration"]
