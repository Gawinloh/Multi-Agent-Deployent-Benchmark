"""Unit tests for src.llm.token_budget."""

import threading

import pytest

from src.llm.token_budget import BudgetExhausted, TokenBudget


class TestAccumulation:
    def test_usage_accumulates(self) -> None:
        budget = TokenBudget(1000)
        budget.record_usage(100, 50)
        budget.record_usage(200, 100)
        assert budget.remaining() == 550

    def test_remaining_starts_at_limit(self) -> None:
        assert TokenBudget(500).remaining() == 500

    def test_not_exhausted_below_limit(self) -> None:
        budget = TokenBudget(100)
        budget.record_usage(40, 40)
        assert not budget.is_exhausted()


class TestExhaustion:
    def test_exceeding_limit_raises(self) -> None:
        budget = TokenBudget(100)
        with pytest.raises(BudgetExhausted) as exc_info:
            budget.record_usage(50, 60)
        assert exc_info.value.limit == 100
        assert exc_info.value.used == 110

    def test_exhausted_after_raise(self) -> None:
        budget = TokenBudget(100)
        with pytest.raises(BudgetExhausted):
            budget.record_usage(50, 60)
        assert budget.is_exhausted()
        assert budget.remaining() == 0

    def test_exactly_at_limit_is_exhausted_but_does_not_raise(self) -> None:
        budget = TokenBudget(100)
        budget.record_usage(50, 50)  # exactly 100: no overspend, no raise
        assert budget.is_exhausted()
        assert budget.remaining() == 0

    def test_plan_verification_case(self) -> None:
        # From the build plan: TokenBudget(100).record_usage(50, 60)
        # then is_exhausted() returns True.
        budget = TokenBudget(100)
        with pytest.raises(BudgetExhausted):
            budget.record_usage(50, 60)
        assert budget.is_exhausted() is True


class TestSummary:
    def test_summary_fields(self) -> None:
        budget = TokenBudget(1000)
        budget.record_usage(100, 50)
        budget.record_usage(10, 5)
        assert budget.summary() == {
            "limit": 1000,
            "input_tokens": 110,
            "output_tokens": 55,
            "total_used": 165,
            "remaining": 835,
            "calls": 2,
            "exhausted": False,
        }

    def test_summary_after_overspend(self) -> None:
        budget = TokenBudget(100)
        with pytest.raises(BudgetExhausted):
            budget.record_usage(80, 80)
        summary = budget.summary()
        assert summary["total_used"] == 160
        assert summary["remaining"] == 0
        assert summary["exhausted"] is True


class TestValidation:
    def test_zero_limit_rejected(self) -> None:
        with pytest.raises(ValueError):
            TokenBudget(0)

    def test_negative_limit_rejected(self) -> None:
        with pytest.raises(ValueError):
            TokenBudget(-10)

    def test_negative_usage_rejected(self) -> None:
        with pytest.raises(ValueError):
            TokenBudget(100).record_usage(-1, 0)


class TestThreadSafety:
    def test_concurrent_recording_is_consistent(self) -> None:
        budget = TokenBudget(1_000_000)
        n_threads, n_iterations = 8, 100

        def worker() -> None:
            for _ in range(n_iterations):
                budget.record_usage(1, 1)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        summary = budget.summary()
        assert summary["total_used"] == n_threads * n_iterations * 2
        assert summary["calls"] == n_threads * n_iterations
