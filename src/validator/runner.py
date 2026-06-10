"""The ``validate_config`` tool: deploy, test, score, tear down.

This is the function the agent calls to learn whether its generated
configuration actually works. It is self-contained, returns a
deterministic :class:`~src.schemas.validator_report.ValidatorReport`,
and never leaves Docker state behind (teardown is in a ``finally``).

Pipeline: up → smoke tests → benchmarks (sequential, to avoid
interference) → CIS checks → healthcheck parsing → down. Total
wall-clock is bounded by ``budget_seconds``: phases that would start
after the deadline are skipped and reported as such.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog
import yaml

from src.schemas.stack import StackSpec
from src.schemas.validator_report import (
    BenchmarkResult,
    HealthcheckResult,
    SmokeTestResult,
    ValidatorReport,
)
from src.validator.benchmarks.pgbench import run_pgbench
from src.validator.benchmarks.redis_bench import run_redis_benchmark
from src.validator.benchmarks.wrk import run_wrk
from src.validator.cis_checks.nginx import NginxCISChecker
from src.validator.cis_checks.postgres import PostgresCISChecker
from src.validator.cis_checks.redis import RedisCISChecker
from src.validator.docker_runner import SERVICES, StackRunner, StackStartupError

logger = structlog.get_logger(__name__)

_SKIPPED = "skipped: validation time budget exhausted"


def _now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017 — 3.10-compatible


def _run_smoke_tests(
    runner: StackRunner, spec: StackSpec
) -> dict[str, SmokeTestResult]:
    """Probe each running service for connection acceptance."""
    results: dict[str, SmokeTestResult] = {}

    pg = runner.exec_in(
        "postgres",
        ["psql", "-h", "127.0.0.1", "-U", "postgres", "-tAc", "SELECT 1"],
        environment={"PGPASSWORD": runner.postgres_password},
    )
    pg_ok = pg.exit_code == 0 and "1" in pg.stdout
    results["postgres"] = SmokeTestResult(
        did_start=True,
        accepts_connections=pg_ok,
        error_message=None if pg_ok else (pg.stderr or pg.stdout)[:300],
    )

    ng = runner.exec_in("nginx", ["curl", "-fsS", "http://localhost/"])
    results["nginx"] = SmokeTestResult(
        did_start=True,
        accepts_connections=ng.exit_code == 0,
        error_message=None if ng.exit_code == 0 else (ng.stderr or ng.stdout)[:300],
    )

    password = spec.redis.security.requirepass
    ping = ["redis-cli", *(["-a", password] if password else []), "ping"]
    rd = runner.exec_in("redis", ping)
    rd_ok = rd.exit_code == 0 and "PONG" in rd.stdout
    results["redis"] = SmokeTestResult(
        did_start=True,
        accepts_connections=rd_ok,
        error_message=None if rd_ok else (rd.stderr or rd.stdout)[:300],
    )
    return results


def _parse_healthchecks(compose_path: Path) -> dict[str, HealthcheckResult]:
    """Read healthcheck blocks and depends_on conditions from the rendered
    compose file (verifies the wiring the validator relies on)."""
    try:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("healthcheck_parse_failed", error=str(exc))
        return {}
    services = compose.get("services", {})

    # which condition does each service's healthcheck gate, if any?
    conditions: dict[str, str] = {}
    for definition in services.values():
        depends = definition.get("depends_on", {})
        if isinstance(depends, dict):
            for dependency, clause in depends.items():
                if isinstance(clause, dict) and "condition" in clause:
                    conditions[dependency] = clause["condition"]

    return {
        name: HealthcheckResult(
            healthcheck_present="healthcheck" in definition,
            healthcheck_condition=conditions.get(name),
        )
        for name, definition in services.items()
    }


def _startup_failure_report(
    runner: StackRunner, exc: StackStartupError
) -> ValidatorReport:
    health = runner.wait_healthy(timeout_s=1)
    smoke = {
        service: SmokeTestResult(
            did_start=health.get(service, False),
            accepts_connections=False,
            error_message=(exc.logs.get(service) or str(exc))[-300:],
        )
        for service in SERVICES
    }
    return ValidatorReport(
        timestamp=_now(), smoke_tests=smoke, error=f"startup failed: {exc}"
    )


def validate_config(spec: StackSpec, budget_seconds: int = 120) -> ValidatorReport:
    """Deploy ``spec``, run all checks, tear down, return the report."""
    run_id = uuid.uuid4().hex[:12]
    log = logger.bind(run_id=run_id)
    deadline = time.monotonic() + budget_seconds

    def time_left() -> bool:
        return time.monotonic() < deadline

    runner = StackRunner(run_id=run_id)
    try:
        try:
            runner.up(spec)
        except StackStartupError as exc:
            log.warning("validation_startup_failed", error=str(exc))
            return _startup_failure_report(runner, exc)

        smoke = _run_smoke_tests(runner, spec)
        log.info("smoke_tests_done", passed=sum(r.passed for r in smoke.values()))

        # benchmarks run sequentially to avoid interference
        benchmarks: dict[str, BenchmarkResult] = {}
        bench_duration = max(min(10, budget_seconds // 12), 3)
        for service, bench in (
            ("postgres", lambda: run_pgbench(runner, duration_s=bench_duration)),
            ("nginx", lambda: run_wrk(runner, duration_s=bench_duration)),
            (
                "redis",
                lambda: run_redis_benchmark(
                    runner,
                    duration_s=bench_duration,
                    password=spec.redis.security.requirepass,
                ),
            ),
        ):
            benchmarks[service] = (
                bench() if time_left() else BenchmarkResult(error_message=_SKIPPED)
            )

        cis_results = []
        for checker_cls in (PostgresCISChecker, NginxCISChecker, RedisCISChecker):
            if time_left():
                cis_results.extend(checker_cls(runner).run_all())

        healthchecks = _parse_healthchecks(runner.workdir / "docker-compose.yml")

        error = None
        if not time_left():
            error = f"time budget of {budget_seconds}s exceeded; some phases skipped"
            log.warning("validation_timeout", budget_seconds=budget_seconds)

        report = ValidatorReport(
            timestamp=_now(),
            smoke_tests=smoke,
            benchmarks=benchmarks,
            cis_results=cis_results,
            healthchecks=healthchecks,
            error=error,
        )
        log.info("validation_complete", summary=report.summary())
        return report
    except Exception as exc:  # noqa: BLE001 — agent tool must return, not raise
        log.error("validation_unexpected_error", error=str(exc))
        return ValidatorReport(timestamp=_now(), error=f"unexpected error: {exc}")
    finally:
        runner.down()
