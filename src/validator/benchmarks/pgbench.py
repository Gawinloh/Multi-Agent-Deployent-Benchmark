"""pgbench benchmark for PostgreSQL.

pgbench ships in the postgres:16 image, so it runs via exec inside the
postgres container. Connection goes over TCP (127.0.0.1) with
PGPASSWORD so it works regardless of the spec's unix-socket hba rules.

Note on latency fields: pgbench reports average and stddev, not
percentiles. The average is recorded as ``latency_p50`` (a documented
approximation — for TPC-B-like workloads mean and median are close);
p90/p99 are left None.
"""

from __future__ import annotations

import re

import structlog

from src.schemas.validator_report import BenchmarkResult
from src.validator.docker_runner import StackRunner

logger = structlog.get_logger(__name__)


def parse_pgbench_output(text: str) -> BenchmarkResult:
    """Parse `tps = ...` and `latency average = ... ms` from pgbench output."""
    tps_match = re.search(r"tps\s*=\s*([\d.]+)", text)
    latency_match = re.search(r"latency average\s*=\s*([\d.]+)\s*ms", text)
    if not tps_match:
        return BenchmarkResult(
            error_message=f"could not parse pgbench output: {text[-300:]}"
        )
    return BenchmarkResult(
        throughput=float(tps_match.group(1)),
        latency_p50=float(latency_match.group(1)) if latency_match else None,
    )


def run_pgbench(
    runner: StackRunner, duration_s: int = 10, clients: int = 4
) -> BenchmarkResult:
    """Initialise and run pgbench inside the postgres container."""
    env = {"PGPASSWORD": runner.postgres_password}
    base = ["pgbench", "-h", "127.0.0.1", "-U", "postgres"]

    init = runner.exec_in("postgres", [*base, "-i", "-s", "1", "postgres"], environment=env)
    if init.exit_code != 0:
        logger.warning("pgbench_init_failed", stderr=init.stderr[:300])
        return BenchmarkResult(
            error_message=f"pgbench -i failed (exit {init.exit_code}): {init.stderr[:300]}"
        )

    run = runner.exec_in(
        "postgres",
        [*base, "-c", str(clients), "-T", str(duration_s), "postgres"],
        environment=env,
    )
    if run.exit_code != 0:
        logger.warning("pgbench_run_failed", stderr=run.stderr[:300])
        return BenchmarkResult(
            error_message=f"pgbench failed (exit {run.exit_code}): {run.stderr[:300]}"
        )
    result = parse_pgbench_output(run.stdout)
    logger.info("pgbench_complete", throughput=result.throughput)
    return result
