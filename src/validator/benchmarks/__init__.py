"""Per-service benchmark runners (pgbench, wrk, redis-benchmark).

Each runner executes a benchmark against the deployed stack and parses
the tool's text output into a
:class:`~src.schemas.validator_report.BenchmarkResult`. Failures are
reported via ``error_message``, never raised, so a broken benchmark
cannot abort the validation run.
"""

from src.validator.benchmarks.pgbench import run_pgbench
from src.validator.benchmarks.redis_bench import run_redis_benchmark
from src.validator.benchmarks.wrk import run_wrk

__all__ = ["run_pgbench", "run_redis_benchmark", "run_wrk"]
