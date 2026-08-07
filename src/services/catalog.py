"""The service registry: names to the code that already implements them.

``CATALOG`` holds one :class:`~src.services.definition.ServiceDefinition`
per deployable service. Every adapter below is a few lines wrapping an
existing module — the schemas, CIS checkers and benchmark runners are
untouched.

Two orderings matter and they are deliberately different:

* **Registry order** (postgres, nginx, redis) is the order
  :func:`for_spec` returns, and therefore the order of benchmarks, smoke
  tests and CIS results in the validator report. It matches the existing
  hardcoded call order and the ground-truth block order in
  ``src.experiment.result_logger``.
* **Compose order** (:func:`compose_order`) is a topological sort over
  ``depends_on``, so a service is always declared after the services it
  waits on. For the full palette this yields postgres, redis, nginx —
  the order the hand-written template had.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import structlog

from src.schemas.nginx import NginxConfig
from src.schemas.postgres import PostgresConfig
from src.schemas.redis import RedisConfig
from src.schemas.validator_report import BenchmarkResult, SmokeTestResult
from src.services.definition import ResourceShare, ServiceDefinition
from src.validator.benchmarks.pgbench import run_pgbench
from src.validator.benchmarks.redis_bench import run_redis_benchmark
from src.validator.benchmarks.wrk import run_wrk
from src.validator.cis_checks.nginx import NginxCISChecker
from src.validator.cis_checks.postgres import PostgresCISChecker
from src.validator.cis_checks.redis import RedisCISChecker
from src.validator.docker_runner import FRAGMENT_DIR, StackRunner, StackStartupError

if TYPE_CHECKING:
    from pathlib import Path

    from src.schemas.stack import StackSpec

logger = structlog.get_logger(__name__)


def _fragment(name: str) -> str:
    """Read a service's compose fragment from ``docker/services/``."""
    return (FRAGMENT_DIR / f"{name}.yml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# postgres
# ---------------------------------------------------------------------------


def _postgres_render(spec: StackSpec) -> dict[str, str]:
    """postgresql.conf plus its pg_hba.conf.

    pg_hba is a sibling field on StackSpec rather than a member of
    PostgresConfig, but it is meaningless without postgres and is
    deployed by it, so postgres renders both files.
    """
    assert spec.postgres is not None and spec.pg_hba is not None
    return {
        "postgresql.conf": spec.postgres.render_conf(),
        "pg_hba.conf": spec.pg_hba.render_hba(),
    }


def _postgres_smoke(runner: StackRunner, spec: StackSpec) -> SmokeTestResult:
    result = runner.exec_in(
        "postgres",
        ["psql", "-h", "127.0.0.1", "-U", "postgres", "-tAc", "SELECT 1"],
        environment={"PGPASSWORD": runner.postgres_password},
    )
    ok = result.exit_code == 0 and "1" in result.stdout
    return SmokeTestResult(
        did_start=True,
        accepts_connections=ok,
        error_message=None if ok else (result.stderr or result.stdout)[:300],
    )


def _postgres_benchmark(
    runner: StackRunner, spec: StackSpec, duration_s: int
) -> BenchmarkResult:
    return run_pgbench(runner, duration_s=duration_s)


def _postgres_template_context(
    spec: StackSpec, runner: StackRunner
) -> dict[str, Any]:
    assert spec.postgres is not None
    command = (
        "postgres -c config_file=/etc/postgresql/postgresql.conf"
        " -c hba_file=/etc/postgresql/pg_hba.conf"
    )
    if spec.postgres.security.ssl:
        command += (
            " -c ssl_cert_file=/var/lib/postgresql/server.crt"
            " -c ssl_key_file=/var/lib/postgresql/server.key"
        )
    return {
        "postgres_password": runner.postgres_password,
        "postgres_ssl": spec.postgres.security.ssl,
        "postgres_command": command,
    }


def _postgres_prepare_workdir(spec: StackSpec, workdir: Path) -> None:
    """Self-signed cert for postgres SSL. No-op when ssl is off.

    Left world-readable on the host on purpose. These are throwaway
    two-day self-signed certs in a temp directory, and the container
    needs to be able to read them through the bind mount before its
    entrypoint copies them into place with the strict ownership and
    mode PostgreSQL requires (see docker/services/postgres.yml).

    An earlier version chmod 0640 here and mounted the key directly to
    its final path, on the assumption it would appear root-owned
    in-container. It does not, and the server refused to start with
    "private key file has group or world access", which surfaced only
    as a compose dependency failure.
    """
    assert spec.postgres is not None
    if not spec.postgres.security.ssl:
        return
    certs_dir = workdir / "certs"
    certs_dir.mkdir(exist_ok=True)
    key, crt = certs_dir / "server.key", certs_dir / "server.crt"
    result = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(crt),
            "-days", "2", "-subj", "/CN=localhost",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise StackStartupError(f"openssl cert generation failed: {result.stderr}")
    key.chmod(0o644)


POSTGRES = ServiceDefinition(
    name="postgres",
    config_schema=PostgresConfig,
    spec_field="postgres",
    render=_postgres_render,
    cis_checker=PostgresCISChecker,
    benchmark=_postgres_benchmark,
    smoke_test=_postgres_smoke,
    healthcheck=lambda spec: "pg_isready -U postgres",
    compose_fragment=_fragment("postgres"),
    compose_volumes="  pgdata_{{ run_id }}:",
    template_context=_postgres_template_context,
    prepare_workdir=_postgres_prepare_workdir,
    corpus_service_tag="postgres",
    default_resource_share=ResourceShare(
        memory_fraction=0.5, min_memory_mb=512, cpu_fraction=0.5, min_cpus=0.5
    ),
)


# ---------------------------------------------------------------------------
# nginx
# ---------------------------------------------------------------------------


def _nginx_render(spec: StackSpec) -> dict[str, str]:
    assert spec.nginx is not None
    return {"nginx.conf": spec.nginx.render_conf()}


def _nginx_smoke(runner: StackRunner, spec: StackSpec) -> SmokeTestResult:
    result = runner.exec_in("nginx", ["curl", "-fsS", "http://localhost/"])
    ok = result.exit_code == 0
    return SmokeTestResult(
        did_start=True,
        accepts_connections=ok,
        error_message=None if ok else (result.stderr or result.stdout)[:300],
    )


def _nginx_benchmark(
    runner: StackRunner, spec: StackSpec, duration_s: int
) -> BenchmarkResult:
    return run_wrk(runner, duration_s=duration_s)


def _nginx_template_context(spec: StackSpec, runner: StackRunner) -> dict[str, Any]:
    return {
        "nginx_http_port": runner.nginx_http_port,
        "nginx_https_port": runner.nginx_https_port,
    }


NGINX = ServiceDefinition(
    name="nginx",
    config_schema=NginxConfig,
    spec_field="nginx",
    render=_nginx_render,
    cis_checker=NginxCISChecker,
    benchmark=_nginx_benchmark,
    smoke_test=_nginx_smoke,
    healthcheck=lambda spec: "curl -f http://localhost/ || exit 1",
    compose_fragment=_fragment("nginx"),
    template_context=_nginx_template_context,
    corpus_service_tag="nginx",
    default_resource_share=ResourceShare(
        memory_fraction=0.125, min_memory_mb=128, cpu_fraction=0.25, min_cpus=0.25
    ),
    depends_on=("postgres", "redis"),
)


# ---------------------------------------------------------------------------
# redis
# ---------------------------------------------------------------------------


def _redis_render(spec: StackSpec) -> dict[str, str]:
    assert spec.redis is not None
    return {"redis.conf": spec.redis.render_conf()}


def _redis_smoke(runner: StackRunner, spec: StackSpec) -> SmokeTestResult:
    assert spec.redis is not None
    password = spec.redis.security.requirepass
    command = ["redis-cli", *(["-a", password] if password else []), "ping"]
    result = runner.exec_in("redis", command)
    ok = result.exit_code == 0 and "PONG" in result.stdout
    return SmokeTestResult(
        did_start=True,
        accepts_connections=ok,
        error_message=None if ok else (result.stderr or result.stdout)[:300],
    )


def _redis_benchmark(
    runner: StackRunner, spec: StackSpec, duration_s: int
) -> BenchmarkResult:
    assert spec.redis is not None
    return run_redis_benchmark(
        runner,
        duration_s=duration_s,
        password=spec.redis.security.requirepass,
    )


def _redis_healthcheck(spec: StackSpec) -> str:
    assert spec.redis is not None
    password = spec.redis.security.requirepass
    if password:
        return f"redis-cli -a '{password}' ping | grep -q PONG"
    return "redis-cli ping | grep -q PONG"


REDIS = ServiceDefinition(
    name="redis",
    config_schema=RedisConfig,
    spec_field="redis",
    render=_redis_render,
    cis_checker=RedisCISChecker,
    benchmark=_redis_benchmark,
    smoke_test=_redis_smoke,
    healthcheck=_redis_healthcheck,
    compose_fragment=_fragment("redis"),
    corpus_service_tag="redis",
    default_resource_share=ResourceShare(
        memory_fraction=0.25, min_memory_mb=256, cpu_fraction=0.25, min_cpus=0.25
    ),
)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

#: Name to definition. Insertion order is the iteration order of
#: :func:`for_spec` — see the module docstring.
CATALOG: dict[str, ServiceDefinition] = {
    definition.name: definition for definition in (POSTGRES, NGINX, REDIS)
}


def get(name: str) -> ServiceDefinition:
    """Look up one service definition by name.

    Raises:
        KeyError: if *name* is not a catalog service. The message lists
            the valid names, because this is the error an agent's
            hallucinated service name will surface as.
    """
    try:
        return CATALOG[name]
    except KeyError:
        raise KeyError(
            f"unknown service {name!r}; catalog has {sorted(CATALOG)}"
        ) from None


def all_services() -> list[ServiceDefinition]:
    """Every definition in the catalog, in registry order."""
    return list(CATALOG.values())


def names() -> tuple[str, ...]:
    """Every catalog service name, in registry order."""
    return tuple(CATALOG)


def for_spec(spec: StackSpec) -> list[ServiceDefinition]:
    """The definitions *spec* actually selects, in registry order.

    A service whose StackSpec field is ``None`` was not selected and is
    omitted — it is not deployed, benchmarked, smoke-tested or
    CIS-checked. Note that this says nothing about scoring: a scenario
    whose ground truth asserts a service the spec omitted still fails
    those assertions (see
    :func:`src.experiment.result_logger.score_configuration_correctness`).
    """
    return [definition for definition in CATALOG.values() if definition.is_selected(spec)]


def compose_order(definitions: list[ServiceDefinition]) -> list[ServiceDefinition]:
    """Sort *definitions* so each follows the ones it depends on.

    Kahn's algorithm, with registry order as the tie-break so the output
    is deterministic. ``depends_on`` entries naming services that are not
    in *definitions* are ignored: an unselected service cannot be waited
    on, and dropping the edge is what lets a subset of the palette
    deploy at all.

    Raises:
        ValueError: if the selected services contain a dependency cycle.
    """
    selected = {definition.name for definition in definitions}
    pending = list(definitions)
    ordered: list[ServiceDefinition] = []
    placed: set[str] = set()

    while pending:
        ready = [
            definition
            for definition in pending
            if all(
                dependency in placed
                for dependency in definition.depends_on
                if dependency in selected
            )
        ]
        if not ready:
            raise ValueError(
                "dependency cycle among services: "
                f"{sorted(definition.name for definition in pending)}"
            )
        ordered.extend(ready)
        placed.update(definition.name for definition in ready)
        pending = [definition for definition in pending if definition.name not in placed]

    return ordered


def depends_on_for(
    definition: ServiceDefinition, selected: list[ServiceDefinition]
) -> list[str]:
    """*definition*'s compose dependency edges, restricted to *selected*."""
    present = {other.name for other in selected}
    return [name for name in definition.depends_on if name in present]
