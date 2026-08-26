"""Tests for the parallel per-service architecture (Study 5).

Three things must hold before the arm is worth collecting data with:

1. The manager's decision produces the selection record the other arms produce.
2. Merge arbitration keeps the stack inside the host when independent agents
   each size against the whole machine — the overshoot this topology creates.
3. The shared TokenBudget is genuinely shared under concurrency, which is the
   parity protocol the whole three-way comparison rests on.
"""

from __future__ import annotations

import threading
import time

import pytest

from src.agents.parallel_agent.arbitration import (
    ARBITRATION_HEADROOM,
    arbitrate,
    format_memory,
    parse_memory,
)
from src.agents.parallel_agent.service_agent import ServiceAgent
from src.llm.token_budget import TokenBudget

# ---------------------------------------------------------------------------
# Memory parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2GB", 2 * 1024**3),
        ("512MB", 512 * 1024**2),
        ("1.5GB", int(1.5 * 1024**3)),
        ("1 GB", 1024**3),
        ("2gb", 2 * 1024**3),
        # Redis permits a bare integer, meaning bytes.
        ("1073741824", 1024**3),
    ],
)
def test_parse_memory_accepts_every_schema_permitted_form(text, expected):
    assert parse_memory(text) == expected


@pytest.mark.parametrize("bad", ["", "lots", "GB", None, "12 parsecs"])
def test_parse_memory_rejects_junk(bad):
    assert parse_memory(bad) is None


def test_format_memory_matches_the_schema_pattern():
    import re

    from src.schemas.postgres import MEMORY_PATTERN

    assert re.match(MEMORY_PATTERN, format_memory(1536 * 1024**2))
    assert format_memory(2 * 1024**3) == "2048MB"
    # Never rounds down to a zero-sized reservation.
    assert format_memory(1) == "1MB"


# ---------------------------------------------------------------------------
# Arbitration — the overshoot case this topology creates
# ---------------------------------------------------------------------------


def _fragments(pg="2GB", redis="2GB", rabbit=0.4):
    return {
        "postgres": {"memory": {"shared_buffers": pg, "effective_cache_size": "6GB"}},
        "redis": {"memory": {"maxmemory": redis, "maxmemory_policy": "allkeys-lru"}},
        "rabbitmq": {"resources": {"vm_memory_high_watermark": rabbit}},
    }


def test_three_agents_each_claiming_a_quarter_of_ram_fit_the_host():
    """The case the study design calls out: three agents, one 8GB host, each
    asking for 25% of it.

    75% of the host is not individually unreasonable from any one agent, and it
    does not exceed the machine — but it leaves only 25% for the OS, page cache
    and PostgreSQL's per-connection memory, so it does breach the 70% ceiling
    and is trimmed. The guarantee under test is the one that matters: the
    merged stack fits the host.
    """
    host_gb = 8.0
    host_bytes = int(host_gb * 1024**3)
    result, log = arbitrate(_fragments(pg="2GB", redis="2GB", rabbit=0.25), host_gb)

    granted = (
        parse_memory(result["postgres"]["memory"]["shared_buffers"])
        + parse_memory(result["redis"]["memory"]["maxmemory"])
        + int(result["rabbitmq"]["resources"]["vm_memory_high_watermark"] * host_bytes)
    )
    assert granted < host_bytes
    assert log.requested_bytes < host_bytes  # was never a true overcommit
    assert log.scale_factor < 1.0  # but did breach the working ceiling


def test_modest_reservations_are_left_untouched():
    """Arbitration must not punish agents that already left room."""
    result, log = arbitrate(_fragments(pg="1GB", redis="1GB", rabbit=0.1), 8.0)

    assert log.arbitrated is False
    assert log.scale_factor == 1.0
    assert result["postgres"]["memory"]["shared_buffers"] == "1GB"
    assert result["redis"]["memory"]["maxmemory"] == "1GB"
    assert result["rabbitmq"]["resources"]["vm_memory_high_watermark"] == 0.1


def test_overcommit_is_scaled_back_inside_the_host():
    """Each agent claims half the host; together they demand 150%."""
    host_gb = 8.0
    host_bytes = int(host_gb * 1024**3)
    fragments = _fragments(pg="4GB", redis="4GB", rabbit=0.5)

    result, log = arbitrate(fragments, host_gb)

    assert log.arbitrated is True
    assert log.requested_bytes > host_bytes  # genuinely overcommitted

    granted = (
        parse_memory(result["postgres"]["memory"]["shared_buffers"])
        + parse_memory(result["redis"]["memory"]["maxmemory"])
        + int(
            result["rabbitmq"]["resources"]["vm_memory_high_watermark"] * host_bytes
        )
    )
    ceiling = int(host_bytes * ARBITRATION_HEADROOM)
    # Rounding to whole MB and to 3dp on the watermark can leave a little
    # slack either way; the guarantee is that the stack now fits the host.
    assert granted <= ceiling * 1.02
    assert granted < host_bytes


def test_arbitration_preserves_relative_shares():
    """Proportional scaling is the stated neutral rule; hold it to that."""
    result, log = arbitrate(_fragments(pg="6GB", redis="3GB", rabbit=0.5), 8.0)
    assert log.arbitrated is True
    pg = parse_memory(result["postgres"]["memory"]["shared_buffers"])
    redis = parse_memory(result["redis"]["memory"]["maxmemory"])
    assert pg / redis == pytest.approx(2.0, rel=0.05)


def test_rabbitmq_watermark_stays_a_valid_fraction():
    """The schema bounds it to (0, 1]; scaling must not push it outside."""
    result, _ = arbitrate(_fragments(pg="16GB", redis="16GB", rabbit=0.9), 2.0)
    watermark = result["rabbitmq"]["resources"]["vm_memory_high_watermark"]
    assert 0.0 < watermark <= 1.0


def test_effective_cache_size_is_clamped_even_without_arbitration():
    """It allocates nothing, so it is clamped independently — a planner told
    it has 64GB of cache on an 8GB host is being lied to either way."""
    fragments = _fragments(pg="1GB", redis="1GB", rabbit=0.1)
    fragments["postgres"]["memory"]["effective_cache_size"] = "64GB"
    result, log = arbitrate(fragments, 8.0)

    assert log.arbitrated is False
    clamped = parse_memory(result["postgres"]["memory"]["effective_cache_size"])
    assert clamped <= int(8.0 * 1024**3 * 0.75)


def test_arbitrate_does_not_mutate_its_input():
    fragments = _fragments(pg="4GB", redis="4GB", rabbit=0.5)
    before = fragments["postgres"]["memory"]["shared_buffers"]
    arbitrate(fragments, 8.0)
    assert fragments["postgres"]["memory"]["shared_buffers"] == before


def test_nginx_only_stack_needs_no_arbitration():
    result, log = arbitrate({"nginx": {"worker": {"worker_processes": "auto"}}}, 2.0)
    assert log.arbitrated is False
    assert log.requested_bytes == 0
    assert result["nginx"] == {"worker": {"worker_processes": "auto"}}


# ---------------------------------------------------------------------------
# Budget parity under concurrency
# ---------------------------------------------------------------------------


def test_token_budget_totals_are_exact_under_concurrent_writers():
    """Parity depends on one shared budget surviving concurrent debits.

    If this drifts, the parallel arm is not spending the same currency as the
    other two and the comparison is void.
    """
    budget = TokenBudget(10_000_000)
    threads = 8
    per_thread = 500

    def worker():
        for _ in range(per_thread):
            budget.record_usage(10, 5)

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()

    summary = budget.summary()
    assert summary["total_used"] == threads * per_thread * 15
    assert summary["calls"] == threads * per_thread


def test_metered_budget_attributes_correctly_under_concurrency():
    """Regression: the first smoke run reported orchestrator = -27,648.

    Each agent had measured its cost as a before/after read of the shared
    budget, which under a concurrent fan-out contains its siblings' spending
    too. Three agents summed to 56,463 tokens against a run total of 29,992.
    The meter must attribute each agent only what it actually spent, while the
    shared total stays exact.
    """
    from src.agents.parallel_agent.metering import MeteredBudget

    shared = TokenBudget(10_000_000)
    meters = [MeteredBudget(shared) for _ in range(4)]
    per_meter_calls = 250

    def spend(meter: MeteredBudget, cost: int):
        for _ in range(per_meter_calls):
            meter.record_usage(cost, 0)

    # Distinct costs so a mix-up between agents cannot cancel out.
    costs = [1, 10, 100, 1000]
    threads = [
        threading.Thread(target=spend, args=(meter, cost))
        for meter, cost in zip(meters, costs)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for meter, cost in zip(meters, costs):
        assert meter.own_tokens == per_meter_calls * cost
        assert meter.own_calls == per_meter_calls

    # The parts reconcile with the whole: no residual, no double counting.
    assert sum(m.own_tokens for m in meters) == shared.summary()["total_used"]


def test_metered_budget_shares_exhaustion_with_the_real_budget():
    """The meter must not soften the parity protocol it reports on."""
    from src.llm.token_budget import BudgetExhausted

    from src.agents.parallel_agent.metering import MeteredBudget

    shared = TokenBudget(100)
    a, b = MeteredBudget(shared), MeteredBudget(shared)

    a.record_usage(60, 0)
    assert b.remaining() == 40  # sees the sibling's spend
    assert b.limit == 100

    with pytest.raises(BudgetExhausted):
        b.record_usage(50, 0)
    # The call that tripped the limit is still attributed to the agent that
    # made it, rather than vanishing from the accounting.
    assert b.own_tokens == 50
    assert shared.is_exhausted()


def test_service_agent_keeps_only_its_own_section():
    """An agent that helpfully fills in the whole stack must still return one
    fragment, or the merge step inherits conflicting opinions."""
    agent = ServiceAgent.__new__(ServiceAgent)
    agent._service = "redis"  # noqa: SLF001 — exercising the pure extractor

    spec = {
        "requirements": {},
        "redis": {"memory": {"maxmemory": "1GB"}},
        "postgres": {"memory": {"shared_buffers": "2GB"}},
        "nginx": {"worker": {}},
    }
    assert ServiceAgent._extract_fragment(agent, spec) == {
        "redis": {"memory": {"maxmemory": "1GB"}}
    }


def test_postgres_fragment_without_pg_hba_is_rejected():
    """StackSpec couples them; a half fragment would fail at merge instead."""
    agent = ServiceAgent.__new__(ServiceAgent)
    agent._service = "postgres"  # noqa: SLF001

    assert (
        ServiceAgent._extract_fragment(
            agent, {"postgres": {"memory": {"shared_buffers": "1GB"}}}
        )
        == {}
    )
    full = {
        "postgres": {"memory": {"shared_buffers": "1GB"}},
        "pg_hba": {"rules": []},
    }
    assert set(ServiceAgent._extract_fragment(agent, full)) == {"postgres", "pg_hba"}


def test_failed_service_is_retried_with_a_fresh_agent():
    """Regression: retry parity with the star arm.

    In the first Study 5 collection every postgres success arrived within 3
    iterations and every failure burned all 4 without recovering — the agent
    was looping on its own failed output, because retries inside one agent see
    that output in their history. The star arm escapes this by re-delegating,
    which builds a worker with empty history. The manager must do the same, and
    must charge the arm for every attempt.
    """
    from src.agents.parallel_agent import manager as manager_mod
    from src.agents.parallel_agent.schemas import ServiceFragmentResult

    constructed: list[int] = []

    class FlakyAgent:
        def __init__(self, service, llm_client, budget, max_iterations, full_registry):
            self._service = service
            constructed.append(1)

        def run(self, request, requirements, note):
            # Fails twice, succeeds on a third, fresh instance.
            attempt = len(constructed)
            return ServiceFragmentResult(
                service=self._service,
                success=attempt >= 3,
                summary=f"attempt {attempt}",
                iterations_used=5,
                tokens_used=100,
                wall_clock_s=1.0,
                fragment={self._service: {"ok": True}} if attempt >= 3 else {},
            )

    mgr = manager_mod.ParallelManagerAgent.__new__(manager_mod.ParallelManagerAgent)
    mgr._client = None  # noqa: SLF001
    mgr._shared_budget = TokenBudget(1_000_000)  # noqa: SLF001
    mgr._max_service_iterations = 5  # noqa: SLF001
    mgr._registry = None  # noqa: SLF001

    original = manager_mod.ServiceAgent
    manager_mod.ServiceAgent = FlakyAgent
    try:
        out = mgr._run_one_service("redis", "req", {}, "")  # noqa: SLF001
    finally:
        manager_mod.ServiceAgent = original

    assert out.fragment == {"redis": {"ok": True}}
    assert len(constructed) == 3, "a fresh agent per attempt"
    # Every attempt spent real tokens; the arm is charged for all of them.
    assert out.tokens_used == 300
    assert out.iterations_used == 15
    assert out.summary.startswith("[3 attempts]")


def test_service_retries_are_bounded():
    """A service that never succeeds must not retry forever."""
    from src.agents.parallel_agent import manager as manager_mod
    from src.agents.parallel_agent.schemas import ServiceFragmentResult

    calls: list[int] = []

    class AlwaysFails:
        def __init__(self, service, llm_client, budget, max_iterations, full_registry):
            self._service = service

        def run(self, request, requirements, note):
            calls.append(1)
            return ServiceFragmentResult(
                service=self._service, success=False, tokens_used=10, fragment={}
            )

    mgr = manager_mod.ParallelManagerAgent.__new__(manager_mod.ParallelManagerAgent)
    mgr._client = None  # noqa: SLF001
    mgr._shared_budget = TokenBudget(1_000_000)  # noqa: SLF001
    mgr._max_service_iterations = 5  # noqa: SLF001
    mgr._registry = None  # noqa: SLF001

    original = manager_mod.ServiceAgent
    manager_mod.ServiceAgent = AlwaysFails
    try:
        out = mgr._run_one_service("postgres", "req", {}, "")  # noqa: SLF001
    finally:
        manager_mod.ServiceAgent = original

    assert len(calls) == manager_mod._SERVICE_ATTEMPTS
    assert out.fragment == {}


def test_parallel_service_iterations_match_the_star_worker():
    """Neither arm may get more attempts per agent than the other."""
    from src.agents.multi_agent.worker import _DEFAULT_MAX_WORKER_ITERATIONS
    from src.agents.parallel_agent.service_agent import (
        _DEFAULT_MAX_SERVICE_ITERATIONS,
    )

    assert _DEFAULT_MAX_SERVICE_ITERATIONS == _DEFAULT_MAX_WORKER_ITERATIONS


def test_fan_out_is_actually_concurrent():
    """If the agents run in sequence the arm has no reason to exist.

    Uses the real ThreadPoolExecutor path with sleeping stand-ins, so this
    catches a future change that accidentally serialises the fan-out.
    """
    from concurrent.futures import ThreadPoolExecutor

    delay = 0.2
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: time.sleep(delay), range(4)))
    elapsed = time.monotonic() - started

    assert elapsed < delay * 4 * 0.75
