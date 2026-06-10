"""Validator report schemas.

The structured result the ``validate_config`` tool returns to the agent.
CIS results here are the ground truth for the dissertation's H2
(safety) hypothesis.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SmokeTestResult(BaseModel):
    """Did one service start and accept connections?"""

    did_start: bool
    accepts_connections: bool
    error_message: str | None = None

    @property
    def passed(self) -> bool:
        return self.did_start and self.accepts_connections


class BenchmarkResult(BaseModel):
    """Performance numbers for one service (pgbench / wrk / redis-benchmark)."""

    throughput: float | None = Field(default=None, ge=0, description="tps / req/s / ops/s")
    latency_p50: float | None = Field(default=None, ge=0, description="ms")
    latency_p90: float | None = Field(default=None, ge=0, description="ms")
    latency_p99: float | None = Field(default=None, ge=0, description="ms")
    error_message: str | None = None


class CISCheckResult(BaseModel):
    """Outcome of one CIS Benchmark control."""

    control_id: str = Field(min_length=1, description='e.g. "2.1"')
    name: str = Field(min_length=1)
    level: Literal[1, 2]
    passed: bool
    evidence: str = Field(description="raw query output or config snippet")
    service: str = Field(min_length=1)


class HealthcheckResult(BaseModel):
    """Compose-file healthcheck wiring for one service."""

    healthcheck_present: bool
    healthcheck_condition: str | None = Field(
        default=None, description='should be "service_healthy"'
    )


class ValidatorReport(BaseModel):
    """Everything the validator learned about one deployed StackSpec."""

    timestamp: datetime
    smoke_tests: dict[str, SmokeTestResult] = Field(default_factory=dict)
    benchmarks: dict[str, BenchmarkResult] = Field(default_factory=dict)
    cis_results: list[CISCheckResult] = Field(default_factory=list)
    healthchecks: dict[str, HealthcheckResult] = Field(default_factory=dict)
    error: str | None = Field(
        default=None, description="top-level failure before any checks ran"
    )

    def cis_pass_rate(self) -> float | None:
        """Fraction of CIS checks passed; None if no checks ran."""
        if not self.cis_results:
            return None
        return sum(check.passed for check in self.cis_results) / len(self.cis_results)

    def summary(self) -> str:
        """One-line text summary for agent observations and logs."""
        if self.error:
            return f"validation failed: {self.error}"
        smoke_passed = sum(r.passed for r in self.smoke_tests.values())
        parts = [f"smoke {smoke_passed}/{len(self.smoke_tests)}"]
        rate = self.cis_pass_rate()
        if rate is not None:
            cis_passed = sum(c.passed for c in self.cis_results)
            parts.append(f"CIS {cis_passed}/{len(self.cis_results)} ({rate:.0%})")
        benched = [
            f"{svc}={r.throughput:g}"
            for svc, r in self.benchmarks.items()
            if r.throughput is not None
        ]
        if benched:
            parts.append("throughput " + ", ".join(benched))
        return "; ".join(parts)

    def model_dump_summary(self) -> dict[str, Any]:
        """Compact dict for result logs."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "summary": self.summary(),
            "cis_pass_rate": self.cis_pass_rate(),
            "error": self.error,
        }
