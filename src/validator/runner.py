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
from src.services.catalog import for_spec
from src.validator.docker_runner import StackRunner, StackStartupError

logger = structlog.get_logger(__name__)

_SKIPPED = "skipped: validation time budget exhausted"


def _now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017 — 3.10-compatible


def _run_smoke_tests(
    runner: StackRunner, spec: StackSpec
) -> dict[str, SmokeTestResult]:
    """Probe each *selected* service for connection acceptance.

    Services the spec did not select are absent from the result, so they
    neither pass nor fail: ``smoke_pass_rate`` is a fraction of what was
    actually deployed.
    """
    return {
        definition.name: definition.smoke_test(runner, spec)
        for definition in for_spec(spec)
    }


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
        for service in runner.services
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
            # exc.logs carries each container's own stderr. Without it the
            # warning shows only compose's "dependency failed to start",
            # which says nothing about *why* the service died, and the agent
            # is left to guess at the cause.
            log.warning(
                "validation_startup_failed",
                error=str(exc),
                **{
                    f"{svc}_log_tail": "\n".join(
                        text.strip().splitlines()[-15:]
                    )
                    for svc, text in (exc.logs or {}).items()
                    if text and text.strip()
                },
            )
            return _startup_failure_report(runner, exc)

        smoke = _run_smoke_tests(runner, spec)
        log.info("smoke_tests_done", passed=sum(r.passed for r in smoke.values()))

        # benchmarks run sequentially to avoid interference
        selected = for_spec(spec)
        benchmarks: dict[str, BenchmarkResult] = {}
        bench_duration = max(min(10, budget_seconds // 12), 3)
        for definition in selected:
            benchmarks[definition.name] = (
                definition.benchmark(runner, spec, bench_duration)
                if time_left()
                else BenchmarkResult(error_message=_SKIPPED)
            )

        cis_results = []
        for definition in selected:
            if time_left():
                cis_results.extend(definition.cis_checker(runner).run_all())

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
