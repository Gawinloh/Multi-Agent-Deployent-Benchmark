"""Validator harness: physical deployment, benchmarks, and CIS checks.

The harness deploys LLM-generated stacks into Docker containers, runs
smoke tests / benchmarks / CIS checks, and tears down cleanly between
scenarios. No LLM involvement anywhere in this package.

``validate_config`` is re-exported lazily. It reaches the benchmarks and
CIS checkers through :mod:`src.services.catalog`, and the catalog in
turn imports those same modules from this package — so binding it at
import time makes ``import src.validator.benchmarks.pgbench`` (or any
other leaf) execute this file, re-enter the catalog mid-initialisation,
and fail. Deferring the import to first attribute access breaks the
cycle without changing the public surface.
"""

from typing import Any

from src.validator.docker_runner import (
    CommandResult,
    StackRunner,
    StackStartupError,
)

__all__ = ["CommandResult", "StackRunner", "StackStartupError", "validate_config"]


def __getattr__(name: str) -> Any:
    if name == "validate_config":
        from src.validator.runner import validate_config

        return validate_config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
