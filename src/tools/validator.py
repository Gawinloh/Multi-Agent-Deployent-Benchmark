"""The ``validate_config`` tool wrapper.

Thin adapter that converts a raw dict into a :class:`StackSpec` and
delegates to :func:`src.validator.runner.validate_config`. The agent
never imports the validator runner directly; it dispatches through the
tool registry which calls this wrapper.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import ValidationError

from src.schemas.stack import StackSpec
from src.schemas.validator_report import ValidatorReport

logger = structlog.get_logger(__name__)


def validate_config_tool(spec: dict[str, Any]) -> ValidatorReport:
    """Validate a StackSpec dict by deploying and testing it.

    Args:
        spec: dict that should validate as a :class:`StackSpec`.
            Also accepts a ``GeneratedFiles``-shaped dict — if the input
            has both ``"spec"`` and ``"postgresql_conf"`` keys, the nested
            ``"spec"`` is extracted automatically.

    Returns:
        :class:`ValidatorReport` — always returns (never raises); errors
        are captured in the report's ``error`` field.
    """
    # The agent sometimes passes the full GeneratedFiles dict (with
    # rendered config strings) instead of just the StackSpec.  Extract
    # the nested spec so validation proceeds normally.
    if "spec" in spec and "postgresql_conf" in spec:
        spec = spec["spec"]  # type: ignore[assignment]

    try:
        stack_spec = StackSpec.model_validate(spec)
    except ValidationError as exc:
        logger.warning("validate_config_tool_bad_spec", error=str(exc)[:200])
        return ValidatorReport(
            timestamp=datetime.now(timezone.utc),  # noqa: UP017 — 3.10-compatible
            error=f"spec validation failed: {exc}",
        )

    try:
        from src.validator.runner import validate_config

        return validate_config(stack_spec)
    except Exception as exc:  # noqa: BLE001 — tool must return, not raise
        logger.error("validate_config_tool_error", error=str(exc))
        return ValidatorReport(
            timestamp=datetime.now(timezone.utc),  # noqa: UP017 — 3.10-compatible
            error=f"validation error: {exc}",
        )
