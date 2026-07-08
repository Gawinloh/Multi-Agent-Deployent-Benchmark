"""wrk HTTP benchmark for nginx.

Implementation choice (per build-plan note): wrk is NOT installed in the
nginx image. Rather than maintaining a custom Dockerfile.test, wrk runs
as a throwaway SIDECAR container (`docker run --rm`) attached to the
stack's isolated network, targeting the nginx service by its compose DNS
name. This keeps the deployed stack identical to what the agent
specified and needs no image rebuilds.
"""

from __future__ import annotations

import platform
import re
import subprocess

import structlog

from src.schemas.validator_report import BenchmarkResult
from src.validator.docker_runner import StackRunner

logger = structlog.get_logger(__name__)

_SKIP_ARM64 = (
    "skipped: williamyeh/wrk image is amd64-only; "
    "no native ARM64 build available (Apple Silicon limitation)"
)

WRK_IMAGE = "williamyeh/wrk:latest"

_LATENCY_UNITS = {"us": 0.001, "ms": 1.0, "s": 1000.0}


def _latency_to_ms(value: str) -> float | None:
    """'1.63ms' -> 1.63; '634.00us' -> 0.634; '1.05s' -> 1050.0."""
    match = re.fullmatch(r"([\d.]+)(us|ms|s)", value.strip())
    if not match:
        return None
    return float(match.group(1)) * _LATENCY_UNITS[match.group(2)]


def parse_wrk_output(text: str) -> BenchmarkResult:
    """Parse Requests/sec and latency-distribution percentiles."""
    rps_match = re.search(r"Requests/sec:\s*([\d.]+)", text)
    if not rps_match:
        return BenchmarkResult(error_message=f"could not parse wrk output: {text[-300:]}")

    percentiles: dict[str, float | None] = {}
    for pct in ("50", "90", "99"):
        match = re.search(rf"^\s*{pct}%\s+(\S+)", text, re.MULTILINE)
        percentiles[pct] = _latency_to_ms(match.group(1)) if match else None

    return BenchmarkResult(
        throughput=float(rps_match.group(1)),
        latency_p50=percentiles["50"],
        latency_p90=percentiles["90"],
        latency_p99=percentiles["99"],
    )


def _run_sidecar(
    network: str, url: str, duration_s: int, threads: int, connections: int
) -> tuple[str, int]:
    """Run wrk in a one-off container on the stack's network.

    Separated out so unit tests can monkeypatch it.
    """
    command = [
        "docker", "run", "--rm", "--network", network, WRK_IMAGE,
        "-t", str(threads), "-c", str(connections),
        "-d", f"{duration_s}s", "--latency", url,
    ]
    proc = subprocess.run(
        command, capture_output=True, text=True, timeout=duration_s + 120
    )
    return proc.stdout + proc.stderr, proc.returncode


def run_wrk(
    runner: StackRunner,
    duration_s: int = 10,
    threads: int = 2,
    connections: int = 50,
) -> BenchmarkResult:
    """Benchmark nginx from a wrk sidecar on the stack's network.

    Gracefully skips on ARM64 (Apple Silicon) because the
    ``williamyeh/wrk`` image has no native build for that arch.
    """
    if platform.machine() in ("arm64", "aarch64"):
        logger.info("wrk_skipped_arm64")
        return BenchmarkResult(error_message=_SKIP_ARM64)

    try:
        output, returncode = _run_sidecar(
            runner.network_name, "http://nginx:80/", duration_s, threads, connections
        )
    except Exception as exc:  # noqa: BLE001 — benchmark failure must not abort validation
        logger.warning("wrk_sidecar_failed", error=str(exc))
        return BenchmarkResult(error_message=f"wrk sidecar failed: {exc}")
    if returncode != 0:
        logger.warning("wrk_failed", output=output[:300])
        return BenchmarkResult(
            error_message=f"wrk failed (exit {returncode}): {output[:300]}"
        )
    result = parse_wrk_output(output)
    logger.info("wrk_complete", throughput=result.throughput)
    return result
