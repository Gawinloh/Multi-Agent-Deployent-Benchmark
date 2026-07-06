"""The ``finalise`` tool: terminates the agent loop.

The agent calls this when it believes the configuration is ready. The
tool validates the final spec dict into a :class:`StackSpec` and returns
a :class:`FinalisationDecision`. An invalid spec is not fatal — the
agent receives ``accept=False`` and can try again.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import ValidationError

from src.schemas.agent import FinalisationDecision
from src.schemas.stack import StackSpec

logger = structlog.get_logger(__name__)


def finalise(final_spec: dict[str, Any], reason: str) -> FinalisationDecision:
    """Attempt to finalise the agent loop.

    Args:
        final_spec: dict that should validate as a :class:`StackSpec`.
        reason: human-readable rationale for accepting/rejecting.

    Returns:
        :class:`FinalisationDecision` — ``accept=True`` if the spec is
        valid, ``accept=False`` otherwise.
    """
    try:
        spec = StackSpec.model_validate(final_spec)
    except ValidationError as exc:
        logger.warning("finalise_rejected", error=str(exc)[:200])
        return FinalisationDecision(
            accept=False,
            final_spec=None,
            reason=f"spec validation failed: {exc}",
        )

    logger.info("finalise_accepted", reason=reason[:120])
    return FinalisationDecision(accept=True, final_spec=spec, reason=reason)
