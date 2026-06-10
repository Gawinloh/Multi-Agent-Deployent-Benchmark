"""Agent loop schemas.

Typed contracts for the ReAct think-act-observe loop. The LLM is
constrained to emit AgentThought + ToolCall; every loop iteration is
recorded as a HistoryEntry for the run log.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.schemas.stack import StackSpec


class AgentThought(BaseModel):
    """One reasoning step emitted by the LLM."""

    reasoning: str = Field(min_length=1)
    planned_next_action: str = Field(min_length=1)


class ToolCall(BaseModel):
    """A request to invoke one registered tool."""

    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class ToolObservation(BaseModel):
    """What came back from dispatching a ToolCall."""

    success: bool
    result: Any = None
    error: str | None = None


class HistoryEntry(BaseModel):
    """One complete think-act-observe iteration."""

    thought: AgentThought
    tool_call: ToolCall
    observation: ToolObservation


class FinalisationDecision(BaseModel):
    """Outcome of the finalise tool; terminates the agent loop."""

    accept: bool
    final_spec: StackSpec | None = None
    reason: str = Field(min_length=1)
