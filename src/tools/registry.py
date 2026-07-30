"""Tool registry: uniform dispatch interface for the agent.

Every tool the agent can call is registered here with a name, a
description, a Pydantic input schema, and a callable. The registry
validates inputs, dispatches calls, and wraps results in a
:class:`~src.schemas.agent.ToolObservation`.

The four default tools are registered at module import time so
``from src.tools.registry import default_registry`` gives a ready-to-use
registry.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

from src.schemas.agent import ToolCall, ToolObservation

logger = structlog.get_logger(__name__)


def _to_json_safe(obj: Any) -> Any:
    """Recursively convert Pydantic models and dataclasses to dicts."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            k: _to_json_safe(v)
            for k, v in dataclasses.asdict(obj).items()
        }
    if isinstance(obj, list):
        return [_to_json_safe(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Pydantic input schemas for each tool (kept inline; small enough)
# ---------------------------------------------------------------------------


class QueryRagInput(BaseModel):
    """Input schema for the ``query_rag`` tool."""

    question: str = Field(min_length=1, description="natural-language query")
    k: int = Field(default=5, ge=1, le=20, description="max results")
    service: str | None = Field(
        default=None, description="optional filter: postgres | nginx | redis"
    )

    @field_validator("service", mode="before")
    @classmethod
    def _coerce_service(cls, value: Any) -> str | None:
        """Drop a non-string filter instead of rejecting the whole call.

        Models pass values like a list of services or a bare null here. A
        ValidationError cost the worker a whole iteration and taught it
        nothing, so the filter degrades to None and the search runs
        unfiltered — the same tolerance the corpus alias table applies to
        unrecognised service names.
        """
        if value is None or isinstance(value, str):
            return value
        logger.warning("query_rag_service_coerced", received=repr(value)[:80])
        return None


class GenerateConfigInput(BaseModel):
    """Input schema for the ``generate_config`` tool."""

    partial_spec: dict[str, Any] = Field(description="full or partial StackSpec dict")
    #: Defaults to "complete" because that is the call every agent prompt
    #: documents: pass a partial requirements skeleton and have the LLM fill
    #: the service sections in. The previous default of "deterministic" ran
    #: StackSpec.model_validate on that skeleton and rejected it for missing
    #: postgres/nginx/redis/pg_hba — so a model that omitted the argument was
    #: refused the input shape the tool exists to accept.
    mode: str = Field(
        default="complete",
        description=(
            '"complete" (default) fills missing fields from a partial spec '
            'via the LLM; "deterministic" validates and renders an already '
            "complete StackSpec without an LLM call"
        ),
    )


class ValidateConfigInput(BaseModel):
    """Input schema for the ``validate_config`` tool."""

    spec: dict[str, Any] = Field(description="StackSpec dict to deploy and test")


class FinaliseInput(BaseModel):
    """Input schema for the ``finalise`` tool."""

    final_spec: dict[str, Any] = Field(description="StackSpec dict to accept")
    reason: str = Field(min_length=1, description="rationale for finalising")


# ---------------------------------------------------------------------------
# Tool and ToolRegistry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """Descriptor for one registered tool."""

    name: str
    description: str
    input_schema: type[BaseModel]
    fn: Callable[..., Any]


@dataclass
class ToolRegistry:
    """Registry of agent-callable tools with dispatch and listing."""

    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        """Register a tool (overwrites if name already exists)."""
        self._tools[tool.name] = tool
        logger.debug("tool_registered", name=tool.name)

    def get(self, name: str) -> Tool | None:
        """Retrieve a tool by name, or ``None``."""
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        """Return OpenAI/Anthropic-style tool descriptors."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema.model_json_schema(),
            }
            for t in self._tools.values()
        ]

    def dispatch(
        self, call: ToolCall, **deps: Any
    ) -> ToolObservation:
        """Validate input, invoke the tool, wrap the result.

        Extra *deps* (e.g. ``llm_client``, ``budget``) are forwarded to
        the callable if the tool's input schema fields don't cover them.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolObservation(success=False, error=f"unknown tool: {call.name!r}")

        try:
            validated = tool.input_schema.model_validate(call.args)
        except ValidationError as exc:
            return ToolObservation(success=False, error=f"input validation: {exc}")

        try:
            kwargs = validated.model_dump()
            # Merge dependency overrides that the tool accepts
            kwargs.update(deps)
            result = tool.fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 — agent tool must not raise
            logger.error("tool_dispatch_error", tool=call.name, error=str(exc))
            return ToolObservation(success=False, error=str(exc))

        # Serialise to JSON-safe dicts for agent history
        result = _to_json_safe(result)

        return ToolObservation(success=True, result=result)


# ---------------------------------------------------------------------------
# Default registry with all four tools
# ---------------------------------------------------------------------------


def _build_default_registry() -> ToolRegistry:
    """Create and populate the default registry.

    Imports are deferred so the module loads fast and tests can
    import the registry class without pulling heavy dependencies.
    """
    from src.tools.config_generator import generate_config
    from src.tools.finaliser import finalise
    from src.tools.rag import query_rag
    from src.tools.validator import validate_config_tool

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="query_rag",
            description=(
                "Search the indexed documentation corpus. Returns top-k "
                "chunks with citations for a natural-language question."
            ),
            input_schema=QueryRagInput,
            fn=query_rag,
        )
    )
    reg.register(
        Tool(
            name="generate_config",
            description=(
                "Render or LLM-complete a StackSpec into config files. "
                "Mode 'complete' (the default) uses the LLM to fill missing "
                "fields, so a partial spec containing only 'requirements' is "
                "accepted. Mode 'deterministic' skips the LLM and requires an "
                "already complete StackSpec with postgres, nginx, redis and "
                "pg_hba present."
            ),
            input_schema=GenerateConfigInput,
            fn=generate_config,
        )
    )
    reg.register(
        Tool(
            name="validate_config",
            description=(
                "Deploy a StackSpec into Docker containers, run smoke "
                "tests, benchmarks, and CIS security checks, then tear "
                "down and return a ValidatorReport."
            ),
            input_schema=ValidateConfigInput,
            fn=validate_config_tool,
        )
    )
    reg.register(
        Tool(
            name="finalise",
            description=(
                "Accept or reject the final StackSpec and terminate the "
                "agent loop. Returns a FinalisationDecision."
            ),
            input_schema=FinaliseInput,
            fn=finalise,
        )
    )
    return reg


def get_default_registry() -> ToolRegistry:
    """Return a freshly-built default registry.

    Called once per agent run so tool state doesn't leak between runs.
    """
    return _build_default_registry()
