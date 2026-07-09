"""Schemas for the multi-agent orchestrator loop.

The orchestrator uses :class:`OrchestratorStep` to decide what to do
next (delegate to a worker, request revision, or finalise).  Workers
reuse the single-agent :class:`~src.schemas.agent.AgentThought` /
:class:`~src.schemas.agent.ToolCall` schemas via the same ``AgentStep``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Worker roles
# ---------------------------------------------------------------------------


class WorkerRole(str, Enum):
    """Specialist worker identities."""

    CONFIG = "config"
    SECURITY = "security"
    VALIDATION = "validation"


# ---------------------------------------------------------------------------
# Orchestrator decision schema
# ---------------------------------------------------------------------------


class OrchestratorAction(str, Enum):
    """What the orchestrator can do at each step."""

    DELEGATE = "delegate"
    FINALISE = "finalise"


class OrchestratorStep(BaseModel):
    """One orchestrator reasoning + action step.

    The LLM produces this at each orchestrator iteration to decide
    whether to delegate work to a specialist or finalise the run.
    """

    reasoning: str = Field(
        min_length=1,
        description="Analysis of current state and what needs to happen next",
    )
    action: OrchestratorAction = Field(
        description='Either "delegate" to send a task to a worker, or "finalise" to end the run',
    )
    target_worker: WorkerRole | None = Field(
        default=None,
        description="Which worker to delegate to (required when action is delegate)",
    )
    task_description: str = Field(
        default="",
        description="Natural-language instruction for the target worker",
    )
    finalise_reason: str = Field(
        default="",
        description="Rationale for finalising (required when action is finalise)",
    )


# ---------------------------------------------------------------------------
# Worker result (returned to orchestrator)
# ---------------------------------------------------------------------------


class WorkerResult(BaseModel):
    """Summary of a worker's execution, returned to the orchestrator."""

    worker: WorkerRole
    task: str = Field(description="The task the worker was given")
    success: bool
    summary: str = Field(description="Natural-language summary of what happened")
    iterations_used: int = 0
    tokens_used: int = 0
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Condensed log of tool calls made",
    )
    artifacts: dict[str, Any] = Field(
        default_factory=dict,
        description="Key outputs: generated spec, validation report, etc.",
    )
