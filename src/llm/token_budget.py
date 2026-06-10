"""Per-task token budget tracking.

A single :class:`TokenBudget` instance is shared by every LLM call made
during one task. This enforces token-budget parity between the
single-agent and multi-agent architectures: both get exactly the same
number of tokens to spend, regardless of how many LLM calls they make.
"""

from __future__ import annotations

import threading

import structlog

logger = structlog.get_logger(__name__)


class BudgetExhausted(Exception):
    """Raised when recorded token usage exceeds the budget limit."""

    def __init__(self, limit: int, used: int) -> None:
        self.limit = limit
        self.used = used
        super().__init__(f"Token budget exhausted: used {used} of {limit} tokens")


class TokenBudget:
    """Thread-safe accumulator of token usage against a fixed limit.

    Tokens consumed across all LLM calls in a single task accumulate
    here. Once the total (input + output) exceeds ``limit``,
    :meth:`record_usage` raises :class:`BudgetExhausted`.
    """

    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError(f"Budget limit must be positive, got {limit}")
        self._limit = limit
        self._input_tokens = 0
        self._output_tokens = 0
        self._calls = 0
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record usage from one LLM call.

        Raises:
            BudgetExhausted: if cumulative usage exceeds the limit.
                The usage is still recorded so ``summary()`` reflects
                the overspend.
        """
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("Token counts must be non-negative")
        with self._lock:
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._calls += 1
            used = self._input_tokens + self._output_tokens
            remaining = self._limit - used
            logger.info(
                "token_usage_recorded",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_used=used,
                remaining=max(remaining, 0),
                calls=self._calls,
            )
            if remaining < 0:
                raise BudgetExhausted(limit=self._limit, used=used)

    def remaining(self) -> int:
        """Tokens left before the budget is exhausted (never negative)."""
        with self._lock:
            return max(self._limit - (self._input_tokens + self._output_tokens), 0)

    def is_exhausted(self) -> bool:
        """True once cumulative usage has reached or exceeded the limit."""
        with self._lock:
            return (self._input_tokens + self._output_tokens) >= self._limit

    def summary(self) -> dict:
        """Snapshot of budget state for run logs."""
        with self._lock:
            used = self._input_tokens + self._output_tokens
            return {
                "limit": self._limit,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "total_used": used,
                "remaining": max(self._limit - used, 0),
                "calls": self._calls,
                "exhausted": used >= self._limit,
            }
