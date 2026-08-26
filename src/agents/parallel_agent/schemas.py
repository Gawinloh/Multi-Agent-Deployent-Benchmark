"""Schemas for the parallel per-service architecture.

The manager makes exactly one structured decision — which catalogue services
the request needs, and the shared requirements every service agent sizes
against. Per-service agents then reuse the single-agent ``AgentStep`` schema,
so the LLM output format is identical to the other two arms and only the
prompt and tool scope differ.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.schemas.stack import StackRequirements


class ManagerDecision(BaseModel):
    """The manager's single decomposition call.

    Selection lives here rather than emerging from whatever the service agents
    happen to produce, because the arm needs one identifiable decision point to
    compare against the other two architectures. ``requirements`` is shared
    verbatim with every service agent, which is what makes the host budget a
    genuinely contended resource rather than each agent inventing its own.
    """

    reasoning: str = Field(
        min_length=1,
        description="Why these services, and why not the ones omitted",
    )
    #: Promoted to the top level and made mandatory. Nested inside
    #: StackRequirements it is ``list[str] | None`` with a None default, and
    #: constrained decoding duly omitted it — the manager reasoned about
    #: services at length and then selected none, which the arm reads as
    #: "deploy nothing". Selection is the one decision this manager exists to
    #: make, so the schema now refuses to let it go unstated.
    selected_services: list[str] = Field(
        min_length=1,
        description=(
            "Catalogue services to deploy; must name at least one "
            "(postgres, nginx, redis, rabbitmq)"
        ),
    )
    requirements: StackRequirements = Field(
        description="Shared requirements block; every service agent sizes against this"
    )
    per_service_notes: dict[str, str] = Field(
        default_factory=dict,
        description="Optional per-service instruction, keyed by service name",
    )


class ServiceFragmentResult(BaseModel):
    """What one per-service agent produced.

    ``fragment`` holds only that service's own config section (plus ``pg_hba``
    for postgres, which StackSpec couples to it). Anything the agent emitted
    for other services is discarded by the service agent before returning, so
    the merge step never has to arbitrate two agents' opinions about a third
    service.
    """

    service: str
    success: bool
    summary: str = ""
    iterations_used: int = 0
    tokens_used: int = 0
    wall_clock_s: float = 0.0
    fragment: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
