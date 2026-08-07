"""rabbitmq-perf-test throughput benchmark for RabbitMQ.

Same sidecar strategy as wrk: perf-test is a JVM application and is not
present in the rabbitmq image, so it runs as a throwaway container
(``docker run --rm``) on the stack's isolated network and reaches the
broker by its compose DNS name. The deployed stack stays exactly what
the agent specified.

Unlike wrk there is no ARM64 carve-out — ``pivotalrabbitmq/perf-test``
publishes a native linux/arm64 image, verified running on Apple Silicon
at ~48k msg/s. The skip path below is kept for the case where no image
is available for the host architecture at all.
"""

from __future__ import annotations

import platform
import re
import subprocess
from urllib.parse import quote

import structlog

from src.schemas.validator_report import BenchmarkResult
from src.validator.docker_runner import StackRunner

logger = structlog.get_logger(__name__)

PERF_TEST_IMAGE = "pivotalrabbitmq/perf-test:latest"

#: Architectures the perf-test image publishes. amd64 and arm64 are both
#: native, so on any machine this project runs on the benchmark is real.
_SUPPORTED_ARCHITECTURES = ("x86_64", "amd64", "arm64", "aarch64")

_SKIP_UNSUPPORTED_ARCH = (
    "skipped: no pivotalrabbitmq/perf-test image for this host architecture"
)

#: perf-test reports latency in microseconds; BenchmarkResult is in ms.
_US_TO_MS = 0.001


def parse_perf_test_output(text: str) -> BenchmarkResult:
    """Parse perf-test's closing summary lines.

    The run ends with::

        id: test-..., sending rate avg: 48299 msg/s
        id: test-..., receiving rate avg: 48239 msg/s
        id: test-..., consumer latency min/median/75th/95th/99th/max \
4806/71105/144776/342223/377734/418935 µs

    Throughput is taken from the *receiving* rate: messages a consumer
    actually got are the ones the broker durably handled, whereas the
    sending rate counts publishes that may still be buffered.
    """
    received = re.search(r"receiving rate avg:\s*([\d.]+)\s*msg/s", text)
    if received is None:
        return BenchmarkResult(
            error_message=f"could not parse perf-test output: {text[-300:]}"
        )

    result = BenchmarkResult(throughput=float(received.group(1)))

    # min/median/75th/95th/99th/max — perf-test reports no 90th
    # percentile, so latency_p90 is deliberately left unset rather than
    # filled with the 95th, which would misreport the metric.
    latency = re.search(
        r"consumer latency min/median/75th/95th/99th/max\s+"
        r"(\d+)/(\d+)/(\d+)/(\d+)/(\d+)/(\d+)",
        text,
    )
    if latency is not None:
        result = result.model_copy(
            update={
                "latency_p50": int(latency.group(2)) * _US_TO_MS,
                "latency_p99": int(latency.group(5)) * _US_TO_MS,
            }
        )
    return result


def _run_sidecar(network: str, uri: str, duration_s: int) -> tuple[str, int]:
    """Run perf-test in a one-off container. Patched out by unit tests."""
    command = [
        "docker", "run", "--rm", "--network", network, PERF_TEST_IMAGE,
        "--uri", uri,
        "-x", "1", "-y", "1",
        "-z", str(duration_s),
        "-f", "persistent",
    ]
    proc = subprocess.run(
        command, capture_output=True, text=True, timeout=duration_s + 180
    )
    return proc.stdout + proc.stderr, proc.returncode


def run_rabbitmq_perf_test(
    runner: StackRunner,
    duration_s: int = 10,
    username: str = "guest",
    password: str = "guest",
    port: int = 5672,
) -> BenchmarkResult:
    """Benchmark the broker from a perf-test sidecar on the stack network.

    Credentials come from the spec's ``default_user`` / ``default_pass``,
    since a specification that renamed the default account has no
    guest/guest to fall back on.

    A specification that restricts its own application user to loopback
    connections (``loopback_users``) will fail here with ACCESS_REFUSED,
    and that is reported as a benchmark error rather than smoothed over:
    the broker genuinely is unreachable from another container.
    """
    if platform.machine() not in _SUPPORTED_ARCHITECTURES:
        logger.info("perf_test_skipped_arch", arch=platform.machine())
        return BenchmarkResult(error_message=_SKIP_UNSUPPORTED_ARCH)

    uri = f"amqp://{quote(username, safe='')}:{quote(password, safe='')}@rabbitmq:{port}"
    try:
        output, returncode = _run_sidecar(runner.network_name, uri, duration_s)
    except Exception as exc:  # noqa: BLE001 — benchmark failure must not abort validation
        logger.warning("perf_test_sidecar_failed", error=str(exc))
        return BenchmarkResult(error_message=f"perf-test sidecar failed: {exc}")

    if returncode != 0:
        logger.warning("perf_test_failed", output=output[:300])
        return BenchmarkResult(
            error_message=f"perf-test failed (exit {returncode}): {output[:300]}"
        )

    result = parse_perf_test_output(output)
    logger.info("perf_test_complete", throughput=result.throughput)
    return result
