"""Docker test harness: deploy a StackSpec into containers and back out.

:class:`StackRunner` renders the Jinja2 compose template plus the
per-service configs from a :class:`~src.schemas.stack.StackSpec`, writes
them into an isolated workdir, brings the stack up with ``docker
compose``, waits for health, and tears down cleanly (including on
exceptions, via context-manager use).

Implementation choices:
- ``docker compose`` operations go through ``subprocess.run`` (the
  Python docker SDK has no first-class compose support); container
  inspection / exec / logs use the docker SDK.
- The compose project name embeds the run_id so concurrent runs never
  collide; networks and volumes are also suffixed with the run_id.
- The docker SDK import is deferred so unit tests of file rendering run
  without docker installed.
"""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from jinja2 import StrictUndefined, Template

from src.schemas.stack import StackSpec

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "docker" / "compose-template.yml"

#: Compose service names, also used for container lookup by label.
SERVICES = ("postgres", "redis", "nginx")

_COMPOSE_TIMEOUT_S = 300

#: Share of the Docker daemon's memory the three containers may request
#: in total. The remainder covers daemon overhead and the page cache.
_HOST_MEMORY_HEADROOM = 0.75

#: Floors below which a container will not start reliably.
_MIN_CONTAINER_MEMORY_MB = {
    "pg_mem_mb": 512,
    "redis_mem_mb": 256,
    "nginx_mem_mb": 128,
}


@dataclass(frozen=True)
class CommandResult:
    """Result of a command executed inside a container."""

    stdout: str
    stderr: str
    exit_code: int
    duration_s: float


class StackStartupError(RuntimeError):
    """The stack failed to come up healthy. Carries per-service logs."""

    def __init__(self, message: str, logs: dict[str, str] | None = None) -> None:
        self.logs = logs or {}
        super().__init__(message)


class StackRunner:
    """Brings a StackSpec up in Docker and tears it down again.

    Usage::

        with StackRunner(run_id="abc123") as runner:
            runner.up(spec)
            result = runner.exec_in("postgres", ["psql", "-U", "postgres", "-c", "SELECT 1"])
        # down() ran automatically, even on exceptions
    """

    def __init__(
        self,
        run_id: str,
        workdir: Path | None = None,
        nginx_http_port: int = 8080,
        nginx_https_port: int = 8443,
    ) -> None:
        sanitized = re.sub(r"[^a-z0-9_-]", "", run_id.lower())
        if not sanitized:
            raise ValueError(f"run_id {run_id!r} has no usable characters")
        self.run_id = sanitized
        self.project = f"stack-{sanitized}"
        self._owns_workdir = workdir is None
        self.workdir = (
            Path(tempfile.mkdtemp(prefix=f"stack_{sanitized}_"))
            if workdir is None
            else Path(workdir)
        )
        self._nginx_http_port = nginx_http_port
        self._nginx_https_port = nginx_https_port
        self._postgres_password = secrets.token_hex(16)
        self._docker_client: Any = None
        self._log = structlog.get_logger(__name__).bind(run_id=self.run_id)

    @property
    def postgres_password(self) -> str:
        """The generated POSTGRES_PASSWORD for this run (used by benchmarks)."""
        return self._postgres_password

    @property
    def network_name(self) -> str:
        """The isolated bridge network name (used by sidecar containers)."""
        return f"stack_{self.run_id}_net"

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _host_memory_mb(self) -> int | None:
        """Total memory available to the Docker daemon, in MB.

        Returns ``None`` when the daemon cannot be queried, in which case
        no clamping is applied.
        """
        try:
            import docker

            info = docker.from_env().info()
            total = info.get("MemTotal")
            return int(total) // (1024 * 1024) if total else None
        except Exception as exc:  # noqa: BLE001 — clamping is best effort
            self._log.debug("host_memory_probe_failed", error=str(exc)[:120])
            return None

    def _clamp_to_host(self, limits_mb: dict[str, int]) -> dict[str, int]:
        """Scale container memory limits down to fit the Docker host.

        Scenarios describe hypothetical hosts that may be larger than the
        machine running the experiment. Requesting more memory than the
        daemon has causes PostgreSQL to exit during startup, which the
        agent then misreads as a configuration fault and burns its budget
        chasing. Limits are therefore scaled proportionally to fit within
        ``_HOST_MEMORY_HEADROOM`` of the daemon's total, and the clamp is
        logged so the deviation is visible in the run record.
        """
        host_mb = self._host_memory_mb()
        if host_mb is None:
            return limits_mb
        budget = int(host_mb * _HOST_MEMORY_HEADROOM)
        requested = sum(limits_mb.values())
        if requested <= budget:
            return limits_mb
        factor = budget / requested
        clamped = {
            key: max(int(value * factor), _MIN_CONTAINER_MEMORY_MB[key])
            for key, value in limits_mb.items()
        }
        self._log.warning(
            "container_memory_clamped",
            host_mb=host_mb,
            requested_mb=requested,
            budget_mb=budget,
            before=limits_mb,
            after=clamped,
        )
        return clamped

    def _template_context(self, spec: StackSpec) -> dict[str, Any]:
        hw = spec.requirements.hardware
        ram_mb = hw.ram_gb * 1024
        redis_password = spec.redis.security.requirepass
        redis_health_cmd = (
            f"redis-cli -a '{redis_password}' ping | grep -q PONG"
            if redis_password
            else "redis-cli ping | grep -q PONG"
        )
        postgres_command = (
            "postgres -c config_file=/etc/postgresql/postgresql.conf"
            " -c hba_file=/etc/postgresql/pg_hba.conf"
        )
        if spec.postgres.security.ssl:
            postgres_command += (
                " -c ssl_cert_file=/var/lib/postgresql/server.crt"
                " -c ssl_key_file=/var/lib/postgresql/server.key"
            )
        return {
            "run_id": self.run_id,
            "postgres_password": self._postgres_password,
            "postgres_ssl": spec.postgres.security.ssl,
            "postgres_command": postgres_command,
            "redis_health_cmd": redis_health_cmd,
            **self._clamp_to_host(
                {
                    "pg_mem_mb": int(max(ram_mb * 0.5, 512)),
                    "redis_mem_mb": int(max(ram_mb * 0.25, 256)),
                    "nginx_mem_mb": int(max(ram_mb * 0.125, 128)),
                }
            ),
            "pg_cpus": round(max(hw.vcpu * 0.5, 0.5), 2),
            "redis_cpus": round(max(hw.vcpu * 0.25, 0.25), 2),
            "nginx_cpus": round(max(hw.vcpu * 0.25, 0.25), 2),
            "nginx_http_port": self._nginx_http_port,
            "nginx_https_port": self._nginx_https_port,
        }

    def render_files(self, spec: StackSpec) -> dict[str, str]:
        """Render all deployable files as {relative_path: content}."""
        template = Template(
            TEMPLATE_PATH.read_text(encoding="utf-8"), undefined=StrictUndefined
        )
        return {
            "docker-compose.yml": template.render(**self._template_context(spec)),
            "postgresql.conf": spec.postgres.render_conf(),
            "pg_hba.conf": spec.pg_hba.render_hba(),
            "nginx.conf": spec.nginx.render_conf(),
            "redis.conf": spec.redis.render_conf(),
            "html/index.html": "<html><body>stack test harness</body></html>\n",
        }

    def write_files(self, spec: StackSpec) -> Path:
        """Write rendered files (and TLS certs if needed) into the workdir."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in self.render_files(spec).items():
            target = self.workdir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if spec.postgres.security.ssl:
            self._generate_self_signed_certs()
        self._log.info("workdir_written", workdir=str(self.workdir))
        return self.workdir / "docker-compose.yml"

    def _generate_self_signed_certs(self) -> None:
        """Self-signed cert for postgres SSL.

        The key is chmod 0640: postgres accepts a root-owned key with
        mode <= 0640, which is what a bind mount looks like in-container.
        """
        certs_dir = self.workdir / "certs"
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
        key.chmod(0o640)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _compose(
        self, *args: str, timeout: int = _COMPOSE_TIMEOUT_S
    ) -> subprocess.CompletedProcess:
        command = [
            "docker", "compose",
            "-f", str(self.workdir / "docker-compose.yml"),
            "-p", self.project,
            *args,
        ]
        self._log.debug("compose_command", args=args)
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)

    def up(self, spec: StackSpec) -> None:
        """Render + write files, ``docker compose up -d``, wait for health.

        Raises:
            StackStartupError: compose failed, or any container did not
                become healthy within the timeout. Per-service logs are
                attached for diagnosis. The stack is NOT torn down on
                failure so callers can inspect it; call :meth:`down`.
        """
        self.write_files(spec)
        self.start()

    def start(self) -> None:
        """``docker compose up -d`` against already-written files."""
        result = self._compose("up", "-d", "--quiet-pull")
        if result.returncode != 0:
            raise StackStartupError(
                f"docker compose up failed (exit {result.returncode}): {result.stderr}",
                logs=self._collect_logs(),
            )
        health = self.wait_healthy()
        unhealthy = sorted(svc for svc, ok in health.items() if not ok)
        if unhealthy:
            raise StackStartupError(
                f"services failed healthcheck: {', '.join(unhealthy)}",
                logs=self._collect_logs(),
            )
        self._log.info("stack_healthy", services=list(health))

    def wait_healthy(self, timeout_s: int = 60) -> dict[str, bool]:
        """Poll container healthcheck status until all healthy or timeout."""
        deadline = time.monotonic() + timeout_s
        health = dict.fromkeys(SERVICES, False)
        while time.monotonic() < deadline:
            for service in SERVICES:
                container = self._container(service)
                if container is None:
                    health[service] = False
                    continue
                container.reload()
                state = container.attrs.get("State", {})
                status = state.get("Health", {}).get("Status")
                health[service] = status == "healthy"
                if state.get("Status") == "exited":
                    self._log.warning("container_exited", service=service)
            if all(health.values()):
                return health
            time.sleep(1)
        self._log.warning("wait_healthy_timeout", health=health)
        return health

    def exec_in(
        self,
        service: str,
        command: list[str],
        environment: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run a command inside a service's container."""
        container = self._container(service)
        if container is None:
            return CommandResult(
                stdout="", stderr=f"no container for service {service!r}",
                exit_code=127, duration_s=0.0,
            )
        start = time.monotonic()
        exit_code, output = container.exec_run(
            command, demux=True, environment=environment or {}
        )
        duration = time.monotonic() - start
        stdout_b, stderr_b = output if output else (None, None)
        result = CommandResult(
            stdout=(stdout_b or b"").decode(errors="replace"),
            stderr=(stderr_b or b"").decode(errors="replace"),
            exit_code=exit_code if exit_code is not None else -1,
            duration_s=duration,
        )
        self._log.info(
            "exec_in", service=service, command=command[:3],
            exit_code=result.exit_code, duration_s=round(duration, 3),
        )
        return result

    def get_logs(self, service: str) -> str:
        """Container logs for one service ('' if the container is gone)."""
        container = self._container(service)
        if container is None:
            return ""
        return container.logs().decode(errors="replace")

    def down(self) -> None:
        """``docker compose down -v`` and remove the workdir. Idempotent;
        never raises (teardown must not mask the original error)."""
        try:
            if (self.workdir / "docker-compose.yml").exists():
                result = self._compose("down", "-v", "--remove-orphans", "-t", "10")
                if result.returncode != 0:
                    self._log.warning("compose_down_failed", stderr=result.stderr)
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            self._log.warning("compose_down_error", error=str(exc))
        finally:
            if self._owns_workdir:
                shutil.rmtree(self.workdir, ignore_errors=True)
            self._log.info("stack_down")

    def __enter__(self) -> StackRunner:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.down()

    # ------------------------------------------------------------------
    # Docker SDK helpers
    # ------------------------------------------------------------------

    @property
    def _docker(self) -> Any:
        if self._docker_client is None:
            import docker

            self._docker_client = docker.from_env()
        return self._docker_client

    def _container(self, service: str) -> Any | None:
        containers = self._docker.containers.list(
            all=True,
            filters={
                "label": [
                    f"com.docker.compose.project={self.project}",
                    f"com.docker.compose.service={service}",
                ]
            },
        )
        return containers[0] if containers else None

    def _collect_logs(self) -> dict[str, str]:
        logs = {}
        for service in SERVICES:
            try:
                logs[service] = self.get_logs(service)[-4000:]
            except Exception as exc:  # noqa: BLE001 — diagnostics only
                logs[service] = f"<failed to fetch logs: {exc}>"
        return logs
