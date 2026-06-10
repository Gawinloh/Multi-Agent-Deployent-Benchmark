"""Tests for src.validator.runner and the benchmark parsers.

Parser and pipeline tests are pure unit tests (mocked runner/benchmarks/
checkers). The full-stack tests require Docker and are marked
@pytest.mark.integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import src.validator.runner as runner_module
from src.schemas.validator_report import BenchmarkResult, CISCheckResult
from src.validator.benchmarks.pgbench import parse_pgbench_output
from src.validator.benchmarks.redis_bench import parse_redis_benchmark_output
from src.validator.benchmarks.wrk import _latency_to_ms, parse_wrk_output
from src.validator.docker_runner import CommandResult, StackStartupError
from src.validator.runner import validate_config

# ---------------------------------------------------------------------------
# Benchmark output parsers
# ---------------------------------------------------------------------------

PGBENCH_OUTPUT = """\
pgbench (16.4)
transaction type: <builtin: TPC-B (sort of)>
scaling factor: 1
number of clients: 4
duration: 10 s
number of transactions actually processed: 8523
latency average = 4.693 ms
latency stddev = 2.104 ms
initial connection time = 12.3 ms
tps = 851.234567 (without initial connection time)
"""

WRK_OUTPUT = """\
Running 10s test @ http://nginx:80/
  2 threads and 50 connections
  Thread Stats   Avg      Stdev     Max   +/- Stdev
    Latency     1.91ms    1.15ms  29.38ms   87.39%
    Req/Sec    13.42k     1.62k   16.28k    69.00%
  Latency Distribution
     50%    1.63ms
     75%    2.27ms
     90%    3.32ms
     99%    6.13ms
  267536 requests in 10.02s, 216.71MB read
Requests/sec:  26695.59
Transfer/sec:     21.63MB
"""

REDIS_OUTPUT_MODERN = """\
SET: 85470.09 requests per second, p50=0.295 msec
GET: 90909.09 requests per second, p50=0.279 msec
"""

REDIS_OUTPUT_LEGACY = """\
SET: 85470.09 requests per second
GET: 90909.09 requests per second
"""


class TestPgbenchParser:
    def test_parses_tps_and_latency(self) -> None:
        result = parse_pgbench_output(PGBENCH_OUTPUT)
        assert result.error_message is None
        assert result.throughput == pytest.approx(851.234567)
        assert result.latency_p50 == pytest.approx(4.693)

    def test_garbage_reports_error(self) -> None:
        result = parse_pgbench_output("connection refused")
        assert result.error_message is not None
        assert result.throughput is None


class TestWrkParser:
    def test_parses_rps_and_percentiles(self) -> None:
        result = parse_wrk_output(WRK_OUTPUT)
        assert result.error_message is None
        assert result.throughput == pytest.approx(26695.59)
        assert result.latency_p50 == pytest.approx(1.63)
        assert result.latency_p90 == pytest.approx(3.32)
        assert result.latency_p99 == pytest.approx(6.13)

    def test_latency_unit_conversion(self) -> None:
        assert _latency_to_ms("634.00us") == pytest.approx(0.634)
        assert _latency_to_ms("1.63ms") == pytest.approx(1.63)
        assert _latency_to_ms("1.05s") == pytest.approx(1050.0)
        assert _latency_to_ms("nonsense") is None

    def test_garbage_reports_error(self) -> None:
        assert parse_wrk_output("unable to connect").error_message is not None


class TestRedisBenchmarkParser:
    def test_modern_format(self) -> None:
        result = parse_redis_benchmark_output(REDIS_OUTPUT_MODERN)
        assert result.error_message is None
        assert result.throughput == pytest.approx((85470.09 + 90909.09) / 2)
        assert result.latency_p50 == pytest.approx((0.295 + 0.279) / 2)

    def test_legacy_format_without_p50(self) -> None:
        result = parse_redis_benchmark_output(REDIS_OUTPUT_LEGACY)
        assert result.error_message is None
        assert result.throughput is not None
        assert result.latency_p50 is None

    def test_garbage_reports_error(self) -> None:
        assert parse_redis_benchmark_output("NOAUTH").error_message is not None


# ---------------------------------------------------------------------------
# validate_config pipeline (mocked)
# ---------------------------------------------------------------------------

FAKE_COMPOSE = """\
services:
  postgres:
    healthcheck: {test: ["CMD-SHELL", "pg_isready"]}
  redis:
    healthcheck: {test: ["CMD-SHELL", "redis-cli ping"]}
  nginx:
    healthcheck: {test: ["CMD-SHELL", "curl -f http://localhost/"]}
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_healthy}
"""


class FakeStackRunner:
    """In-memory StackRunner double for pipeline tests."""

    instances: list[FakeStackRunner] = []
    fail_up: Exception | None = None

    def __init__(self, run_id: str, workdir: Path | None = None) -> None:
        self.run_id = run_id
        self.workdir = Path(self._tmpdir) / run_id
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.down_called = False
        self.postgres_password = "pw"
        self.network_name = f"stack_{run_id}_net"
        FakeStackRunner.instances.append(self)

    _tmpdir = "."

    def up(self, spec: object) -> None:
        if FakeStackRunner.fail_up is not None:
            raise FakeStackRunner.fail_up
        (self.workdir / "docker-compose.yml").write_text(FAKE_COMPOSE)

    def wait_healthy(self, timeout_s: int = 60) -> dict[str, bool]:
        return {"postgres": False, "redis": False, "nginx": False}

    def exec_in(
        self, service: str, command: list[str], environment: dict | None = None
    ) -> CommandResult:
        joined = " ".join(command)
        if "SELECT 1" in joined:
            return CommandResult("1", "", 0, 0.01)
        if "curl" in joined:
            return CommandResult("<html>", "", 0, 0.01)
        if "ping" in joined:
            return CommandResult("PONG", "", 0, 0.01)
        return CommandResult("", "unexpected", 1, 0.01)

    def down(self) -> None:
        self.down_called = True


@pytest.fixture
def mocked_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Patch StackRunner, benchmarks, and CIS checkers in the runner module."""
    FakeStackRunner.instances = []
    FakeStackRunner.fail_up = None
    FakeStackRunner._tmpdir = str(tmp_path)
    monkeypatch.setattr(runner_module, "StackRunner", FakeStackRunner)

    ok_bench = BenchmarkResult(throughput=100.0, latency_p50=1.0)
    monkeypatch.setattr(runner_module, "run_pgbench", lambda *a, **k: ok_bench)
    monkeypatch.setattr(runner_module, "run_wrk", lambda *a, **k: ok_bench)
    monkeypatch.setattr(runner_module, "run_redis_benchmark", lambda *a, **k: ok_bench)

    def fake_checker(service: str):
        class _Checker:
            def __init__(self, runner: object) -> None: ...

            def run_all(self) -> list[CISCheckResult]:
                return [
                    CISCheckResult(
                        control_id="1.1", name="check", level=1,
                        passed=True, evidence="ok", service=service,
                    )
                ]

        return _Checker

    monkeypatch.setattr(runner_module, "PostgresCISChecker", fake_checker("postgres"))
    monkeypatch.setattr(runner_module, "NginxCISChecker", fake_checker("nginx"))
    monkeypatch.setattr(runner_module, "RedisCISChecker", fake_checker("redis"))
    return FakeStackRunner


@pytest.fixture
def good_spec():
    from tests.test_validator.test_docker_runner import make_spec

    return make_spec()


class TestValidateConfigPipeline:
    def test_happy_path_report(self, mocked_pipeline, good_spec) -> None:
        report = validate_config(good_spec, budget_seconds=120)

        assert report.error is None
        assert all(r.passed for r in report.smoke_tests.values())
        assert set(report.benchmarks) == {"postgres", "nginx", "redis"}
        assert all(b.throughput == 100.0 for b in report.benchmarks.values())
        assert len(report.cis_results) == 3
        assert report.cis_pass_rate() == 1.0
        assert report.healthchecks["postgres"].healthcheck_condition == "service_healthy"
        assert report.healthchecks["nginx"].healthcheck_present
        assert mocked_pipeline.instances[0].down_called

    def test_startup_failure_returns_smoke_failures(
        self, mocked_pipeline, good_spec
    ) -> None:
        mocked_pipeline.fail_up = StackStartupError(
            "services failed healthcheck: postgres", logs={"postgres": "FATAL: bad"}
        )
        report = validate_config(good_spec, budget_seconds=120)

        assert report.error is not None and "startup failed" in report.error
        assert not report.smoke_tests["postgres"].passed
        assert "FATAL" in report.smoke_tests["postgres"].error_message
        assert mocked_pipeline.instances[0].down_called  # teardown guaranteed

    def test_teardown_on_unexpected_exception(
        self, mocked_pipeline, good_spec
    ) -> None:
        mocked_pipeline.fail_up = RuntimeError("docker daemon exploded")
        report = validate_config(good_spec, budget_seconds=120)

        assert report.error is not None and "unexpected error" in report.error
        assert mocked_pipeline.instances[0].down_called

    def test_zero_budget_skips_benchmarks_and_reports_timeout(
        self, mocked_pipeline, good_spec
    ) -> None:
        report = validate_config(good_spec, budget_seconds=0)

        assert report.error is not None and "budget" in report.error
        assert all(
            b.error_message and "skipped" in b.error_message
            for b in report.benchmarks.values()
        )
        assert report.cis_results == []
        assert mocked_pipeline.instances[0].down_called


# ---------------------------------------------------------------------------
# Integration — real Docker
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIntegration:
    def test_known_good_config_passes_smoke(self) -> None:
        from tests.test_validator.test_docker_runner import make_spec

        report = validate_config(make_spec(), budget_seconds=180)
        assert report.error is None or "budget" in report.error
        assert all(r.passed for r in report.smoke_tests.values()), report.summary()
        assert report.cis_results  # checkers ran

    def test_known_bad_pg_hba_fails_smoke(self) -> None:
        from src.schemas.postgres import PostgresHbaConfig, PostgresHbaRule
        from tests.test_validator.test_docker_runner import make_spec

        spec = make_spec()
        # a 'local' rule with an address field is a pg_hba syntax error:
        # postgres refuses to start
        broken = spec.model_copy(
            update={
                "pg_hba": PostgresHbaConfig(
                    rules=[
                        PostgresHbaRule(
                            type="local", database="all", user="all",
                            address="10.0.0.0/8", auth_method="trust",
                        )
                    ]
                )
            }
        )
        report = validate_config(broken, budget_seconds=180)
        assert report.error is not None and "startup failed" in report.error
        assert not report.smoke_tests["postgres"].passed
