"""The ``generate_config`` tool: render or LLM-complete a StackSpec.

Two modes:

- **deterministic**: caller passes a fully-specified StackSpec dict; tool
  validates and renders the four config files + compose YAML.
- **complete**: caller passes a partial spec; the LLM fills missing fields
  via constrained decoding against the StackSpec schema, then renders.

In *complete* mode every LLM call debits the supplied
:class:`~src.llm.token_budget.TokenBudget` via a
:class:`~src.llm.client.BudgetEnforcer`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

from src.schemas.stack import StackSpec

if TYPE_CHECKING:
    from src.llm.client import LLMClient
    from src.llm.token_budget import TokenBudget

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GeneratedFiles:
    """The rendered configuration artefacts produced by :func:`generate_config`.

    Does NOT include docker-compose.yml — the validator harness renders
    its own via the Jinja template (single source of truth).
    """

    postgresql_conf: str
    pg_hba_conf: str
    nginx_conf: str
    redis_conf: str
    spec: StackSpec


_COMPLETION_SYSTEM = (
    "You are an expert infrastructure engineer. Given partial deployment "
    "requirements, propose sensible values for all fields. Your response "
    "must be a JSON object that validates against the provided schema. "
    "Authoritative sources: pgtune for PostgreSQL memory, Mozilla SSL "
    "intermediate profile for TLS, CIS benchmarks for security defaults."
)


def _render(spec: StackSpec) -> GeneratedFiles:
    """Deterministic render from a fully-validated StackSpec."""
    return GeneratedFiles(
        postgresql_conf=spec.postgres.render_conf(),
        pg_hba_conf=spec.pg_hba.render_hba(),
        nginx_conf=spec.nginx.render_conf(),
        redis_conf=spec.redis.render_conf(),
        spec=spec,
    )


def generate_config(
    partial_spec: dict[str, Any],
    mode: Literal["deterministic", "complete"] = "deterministic",
    llm_client: LLMClient | None = None,
    budget: TokenBudget | None = None,
) -> GeneratedFiles:
    """Render config files from a (possibly partial) StackSpec dict.

    Args:
        partial_spec: dict that is either a full StackSpec or a partial
            skeleton when *mode* is ``"complete"``.
        mode: ``"deterministic"`` validates and renders directly;
            ``"complete"`` calls the LLM to fill missing fields first.
        llm_client: required when *mode* is ``"complete"``.
        budget: :class:`TokenBudget` to debit; required when *mode* is
            ``"complete"``.

    Returns:
        :class:`GeneratedFiles` containing the four config file bodies
        and the completed :class:`StackSpec`.

    Raises:
        ValidationError: if *partial_spec* cannot be parsed in
            deterministic mode.
        ValueError: if *mode* is ``"complete"`` but *llm_client* or
            *budget* is missing.
    """
    if mode == "deterministic":
        spec = StackSpec.model_validate(partial_spec)
        logger.info("generate_config_deterministic")
        return _render(spec)

    # --- complete mode ---
    if llm_client is None or budget is None:
        raise ValueError("complete mode requires both llm_client and budget")

    from src.llm.client import BudgetEnforcer

    enforcer = BudgetEnforcer(llm_client, budget)

    import json

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _COMPLETION_SYSTEM},
        {
            "role": "user",
            "content": (
                "Given these partial requirements:\n"
                f"{json.dumps(partial_spec, indent=2)}\n\n"
                "Produce a complete StackSpec JSON that fills in all "
                "missing fields with sensible, production-quality defaults."
            ),
        },
    ]

    spec, _usage = enforcer.chat(messages, schema=StackSpec)
    logger.info("generate_config_complete", tokens_remaining=budget.remaining())
    return _render(spec)  # type: ignore[arg-type]
