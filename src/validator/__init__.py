"""Validator harness: physical deployment, benchmarks, and CIS checks.

The harness deploys LLM-generated stacks into Docker containers, runs
smoke tests / benchmarks / CIS checks, and tears down cleanly between
scenarios. No LLM involvement anywhere in this package.
"""

from src.validator.docker_runner import (
    CommandResult,
    StackRunner,
    StackStartupError,
)

__all__ = ["CommandResult", "StackRunner", "StackStartupError"]
