"""LLM client abstraction and token budget enforcement.

All LLM calls in the project go through :class:`src.llm.client.LLMClient`
so that backends can be swapped without touching agent code, and token
consumption can be enforced per task for architecture-comparison parity.
"""

from src.llm.client import (
    BudgetEnforcer,
    LLMClient,
    TokenUsage,
    get_client,
)
from src.llm.token_budget import BudgetExhausted, TokenBudget

__all__ = [
    "BudgetEnforcer",
    "BudgetExhausted",
    "LLMClient",
    "TokenBudget",
    "TokenUsage",
    "get_client",
]
