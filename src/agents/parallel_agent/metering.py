"""Per-agent token metering that survives concurrency.

The star architecture attributes a worker's cost by reading the shared budget
before and after it runs. That works because its workers run strictly in
sequence, so the window between the two reads contains that worker's calls and
nothing else.

Under a concurrent fan-out the same technique is wrong: three agents running at
once each observe a delta containing all three agents' usage. In the first
Study 5 smoke run this inflated the three service agents to 56,463 tokens
against a true run total of 29,992, and ``compute_per_agent_tokens`` reported
the orchestrator's residual share as **-27,648**.

:class:`MeteredBudget` fixes the attribution without changing the parity
protocol. Every mutating call is forwarded to the one shared
:class:`~src.llm.token_budget.TokenBudget` — same limit, same exhaustion
semantics, same total — while a private counter records only what passed
through this proxy. Parity is preserved exactly; only the bookkeeping improves.
"""

from __future__ import annotations

import threading

from src.llm.token_budget import TokenBudget


class MeteredBudget:
    """A pass-through view of a shared :class:`TokenBudget`.

    Duck-typed against ``TokenBudget`` so it can be handed to
    :class:`~src.llm.client.BudgetEnforcer` and to ``generate_config``'s
    ``budget`` argument interchangeably. It owns no budget of its own: the
    limit, the running total and the ``BudgetExhausted`` behaviour all come
    from the wrapped instance.
    """

    def __init__(self, shared: TokenBudget) -> None:
        self._shared = shared
        self._own_input = 0
        self._own_output = 0
        self._own_calls = 0
        self._lock = threading.Lock()

    # -- the part that meters -------------------------------------------

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Count locally, then debit the shared budget.

        Counting happens first so that the call which trips the limit is still
        attributed to the agent that made it — ``record_usage`` raises after
        recording, and this agent's share should include the call that
        exhausted the run.
        """
        with self._lock:
            self._own_input += input_tokens
            self._own_output += output_tokens
            self._own_calls += 1
        self._shared.record_usage(input_tokens, output_tokens)

    @property
    def own_tokens(self) -> int:
        """Tokens charged through this proxy only."""
        with self._lock:
            return self._own_input + self._own_output

    @property
    def own_calls(self) -> int:
        with self._lock:
            return self._own_calls

    # -- everything else defers to the shared budget ---------------------

    @property
    def limit(self) -> int:
        return self._shared.limit

    def remaining(self) -> int:
        return self._shared.remaining()

    def is_exhausted(self) -> bool:
        return self._shared.is_exhausted()

    def summary(self) -> dict:
        return self._shared.summary()
