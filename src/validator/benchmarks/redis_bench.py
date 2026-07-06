"""redis-benchmark for Redis.

redis-benchmark ships in the redis:7 image, so it runs via exec inside
the redis container. It is request-count based, not duration based; the
``duration_s`` parameter is kept for signature parity with the other
benchmarks and maps to a request count heuristic (1000 requests/second
of nominal duration).
"""

from __future__ import annotations

import re

import structlog

from src.schemas.validator_report import BenchmarkResult
from src.validator.docker_runner import StackRunner

logger = structlog.get_logger(__name__)

_LINE = re.compile(
    r"^(SET|GET):\s*([\d.]+)\s*requests per second(?:,\s*p50=([\d.]+)\s*msec)?",
    re.MULTILINE,
)


def parse_redis_benchmark_output(text: str) -> BenchmarkResult:
    """Parse `-q` output lines for SET and GET; aggregate by mean.

    Handles both the modern format ("SET: 85470.09 requests per second,
    p50=0.295 msec") and the older one without the p50 suffix.

    redis-benchmark prints live progress using carriage returns, so in real
    output the final SET/GET summary lines are not newline-anchored. Normalise
    carriage returns to newlines first so the line anchor matches.
    """
    text = text.replace("\r", "\n")
    throughputs: list[float] = []
    p50s: list[float] = []
    for match in _LINE.finditer(text):
        throughputs.append(float(match.group(2)))
        if match.group(3):
            p50s.append(float(match.group(3)))
    if not throughputs:
        return BenchmarkResult(
            error_message=f"could not parse redis-benchmark output: {text[-300:]}"
        )
    return BenchmarkResult(
        throughput=sum(throughputs) / len(throughputs),
        latency_p50=(sum(p50s) / len(p50s)) if p50s else None,
    )


def run_redis_benchmark(
    runner: StackRunner,
    duration_s: int = 10,
    password: str | None = None,
) -> BenchmarkResult:
    """Run SET/GET benchmark inside the redis container."""
    requests = max(duration_s * 1000, 1000)
    command = ["redis-benchmark", "-n", str(requests), "-c", "10", "-t", "set,get", "-q"]
    if password:
        command = ["redis-benchmark", "-a", password, *command[1:]]
    result = runner.exec_in("redis", command)
    if result.exit_code != 0:
        logger.warning("redis_benchmark_failed", stderr=result.stderr[:300])
        return BenchmarkResult(
            error_message=(
                f"redis-benchmark failed (exit {result.exit_code}): "
                f"{(result.stderr or result.stdout)[:300]}"
            )
        )
    parsed = parse_redis_benchmark_output(result.stdout)
    logger.info("redis_benchmark_complete", throughput=parsed.throughput)
    return parsed
