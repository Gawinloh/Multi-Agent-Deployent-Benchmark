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

_DOCKER_DIR = Path(__file__).resolve().parents[2] / "docker"

TEMPLATE_PATH = _DOCKER_DIR / "compose-template.yml"

#: Per-service compose fragments, one file per catalog service. Read by
#: src/services/catalog.py, which owns the mapping from service name to
#: fragment; the runner only assembles what the catalog hands it.
FRAGMENT_DIR = _DOCKER_DIR / "services"

#: The full palette in compose order, used as the default when no spec
#: has been rendered yet (container lookup by label, log collection).
#: Duplicated here as a literal rather than derived from the catalog
#: because the catalog imports this module; ``test_catalog.py`` asserts
#: the two agree. Per-spec selection always comes from the catalog.
SERVICES = ("postgres", "redis", "rabbitmq", "nginx")

_COMPOSE_TIMEOUT_S = 300

#: Share of the Docker daemon's memory the deployed containers may
#: request in total. The remainder covers daemon overhead and the page
#: cache.
_HOST_MEMORY_HEADROOM = 0.75


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
        self._services: tuple[str, ...] = SERVICES
        self._docker_client: Any = None
        self._log = structlog.get_logger(__name__).bind(run_id=self.run_id)

    @property
    def postgres_password(self) -> str:
        """The generated POSTGRES_PASSWORD for this run (used by benchmarks)."""
        return self._postgres_password

    @property
    def nginx_http_port(self) -> int:
        """Host port mapped to nginx's :80."""
        return self._nginx_http_port

    @property
    def nginx_https_port(self) -> int:
        """Host port mapped to nginx's :443."""
        return self._nginx_https_port

    @property
    def services(self) -> tuple[str, ...]:
        """Services this run deploys, in compose order.

        The full palette until a spec has been rendered, then exactly
        the services that spec selected. Health polling and log
        collection use this, so an unselected service is never waited
        on and never reported as a missing container.
        """
        return self._services

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

    def _clamp_to_host(
        self, limits_mb: dict[str, int], floors_mb: dict[str, int]
    ) -> dict[str, int]:
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
            key: max(int(value * factor), floors_mb[key])
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

    @staticmethod
    def _render_jinja(source: str, context: dict[str, Any]) -> str:
        return Template(source, undefined=StrictUndefined).render(**context)

    def _template_context(self, spec: StackSpec) -> dict[str, Any]:
        """Build the skeleton's context by assembling per-service blocks.

        The runner no longer knows what a postgres or an nginx is: it
        asks the catalog which services *spec* selects, renders each
        one's fragment with that service's own healthcheck, resource
        limits and (filtered) dependency edges, and concatenates them in
        dependency order.
        """
        from src.services.catalog import compose_order, depends_on_for, for_spec

        selected = for_spec(spec)
        ordered = compose_order(selected)
        hw = spec.requirements.hardware
        ram_mb = hw.ram_gb * 1024

        shares = {definition.name: definition.default_resource_share for definition in ordered}
        memory_mb = self._clamp_to_host(
            {name: share.memory_mb(ram_mb) for name, share in shares.items()},
            {name: share.min_memory_mb for name, share in shares.items()},
        )

        # Services contribute their own extra variables (postgres's
        # password and start command, nginx's host ports).
        context: dict[str, Any] = {"run_id": self.run_id}
        for definition in ordered:
            if definition.template_context is not None:
                context.update(definition.template_context(spec, self))

        blocks = [
            self._render_jinja(
                definition.compose_fragment,
                {
                    **context,
                    "healthcheck": definition.healthcheck(spec),
                    "mem_mb": memory_mb[definition.name],
                    "cpus": definition.default_resource_share.cpus(hw.vcpu),
                    "depends_on": depends_on_for(definition, selected),
                },
            ).strip("\n")
            for definition in ordered
        ]

        volumes = [
            self._render_jinja(definition.compose_volumes, context).strip("\n")
            for definition in ordered
            if definition.compose_volumes
        ]

        context["service_blocks"] = "\n\n".join(blocks)
        # Omitted entirely when nothing needs a volume — an empty
        # `volumes:` key is not valid compose.
        context["volumes_section"] = (
            "\nvolumes:\n" + "\n".join(volumes) + "\n" if volumes else ""
        )
        return context

    def render_files(self, spec: StackSpec) -> dict[str, str]:
        """Render all deployable files as {relative_path: content}.

        Config files come from each selected service's ``render``, so a
        service that is ``None`` in *spec* contributes no files and no
        compose block.
        """
        from src.services.catalog import compose_order, for_spec

        selected = for_spec(spec)
        self._services = tuple(
            definition.name for definition in compose_order(selected)
        )

        template = Template(
            TEMPLATE_PATH.read_text(encoding="utf-8"), undefined=StrictUndefined
        )
        files = {"docker-compose.yml": template.render(**self._template_context(spec))}
        for definition in selected:
            files.update(definition.render(spec))
        files["html/index.html"] = "<html><body>stack test harness</body></html>\n"
        return files

    def write_files(self, spec: StackSpec) -> Path:
        """Write rendered files (and any per-service extras) into the workdir."""
        from src.services.catalog import for_spec

        self.workdir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in self.render_files(spec).items():
            target = self.workdir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        # Anything a service needs beyond its rendered files — today only
        # postgres's TLS certs, and only when ssl is on.
        for definition in for_spec(spec):
            if definition.prepare_workdir is not None:
                definition.prepare_workdir(spec, self.workdir)
        self._log.info("workdir_written", workdir=str(self.workdir))
        return self.workdir / "docker-compose.yml"

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
        health = dict.fromkeys(self._services, False)
        while time.monotonic() < deadline:
            for service in self._services:
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
        for service in self._services:
            try:
                logs[service] = self.get_logs(service)[-4000:]
            except Exception as exc:  # noqa: BLE001 — diagnostics only
                logs[service] = f"<failed to fetch logs: {exc}>"
        return logs
