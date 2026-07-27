"""Tests for src.validator.docker_runner.

Unit tests (file rendering) run anywhere. Integration tests require a
running Docker daemon and are marked @pytest.mark.integration:

    pytest tests/test_validator/test_docker_runner.py -m integration
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from src.schemas.nginx import (
    NginxConfig,
    NginxHttpParams,
    NginxSecurityParams,
    NginxSSLParams,
    NginxWorkerParams,
)
from src.schemas.postgres import (
    PostgresConfig,
    PostgresConnectionParams,
    PostgresHbaConfig,
    PostgresHbaRule,
    PostgresLoggingParams,
    PostgresMemoryParams,
    PostgresSecurityParams,
    PostgresWALParams,
)
from src.schemas.redis import (
    RedisConfig,
    RedisMemoryParams,
    RedisNetworkingParams,
    RedisPersistenceParams,
    RedisSecurityParams,
)
from src.schemas.stack import (
    ComplianceProfile,
    HardwareConstraints,
    StackRequirements,
    StackSpec,
    WorkloadClass,
)
from src.validator.docker_runner import (
    SERVICES,
    CommandResult,
    StackRunner,
    StackStartupError,
)


def make_spec(
    *,
    ssl: bool = False,
    redis_maxmemory: str = "256MB",
    requirepass: str | None = "harness-test-pass",
) -> StackSpec:
    """A known-good StackSpec sized for the test harness.

    ssl defaults to False so the harness doesn't need certs; pg_hba uses
    'local trust' so exec'd psql works over the unix socket.
    """
    return StackSpec(
        requirements=StackRequirements(
            workload_class=WorkloadClass.BALANCED,
            expected_concurrent_users=5,
            expected_data_size_gb=1,
            hardware=HardwareConstraints(ram_gb=2, vcpu=2, disk_gb=10),
            compliance=ComplianceProfile.NONE,
            backup_required=False,
        ),
        postgres=PostgresConfig(
            memory=PostgresMemoryParams(
                shared_buffers="128MB",
                effective_cache_size="512MB",
                work_mem="4MB",
                maintenance_work_mem="64MB",
            ),
            connections=PostgresConnectionParams(
                max_connections=20, superuser_reserved_connections=3
            ),
            wal=PostgresWALParams(
                wal_level="replica",
                checkpoint_completion_target=0.9,
                max_wal_size="512MB",
            ),
            security=PostgresSecurityParams(
                ssl=ssl,
                password_encryption="scram-sha-256",
                log_connections=True,
                log_disconnections=True,
                ssl_min_protocol_version="TLSv1.2",
            ),
            logging=PostgresLoggingParams(
                log_destination="stderr",
                log_statement="ddl",
                log_min_duration_statement=1000,
            ),
        ),
        nginx=NginxConfig(
            worker=NginxWorkerParams(worker_processes="auto", worker_connections=512),
            http=NginxHttpParams(
                sendfile=True,
                tcp_nopush=True,
                tcp_nodelay=True,
                keepalive_timeout=65,
                keepalive_requests=1000,
                gzip=True,
            ),
            security=NginxSecurityParams(
                server_tokens=False, autoindex=False, client_max_body_size="10m"
            ),
            ssl=NginxSSLParams(
                protocols=["TLSv1.2", "TLSv1.3"],
                ciphers="HIGH:!aNULL:!MD5",
                prefer_server_ciphers=True,
                session_cache="shared:SSL:10m",
                session_timeout="1d",
                stapling=False,
            ),
        ),
        redis=RedisConfig(
            memory=RedisMemoryParams(
                maxmemory=redis_maxmemory,
                maxmemory_policy="allkeys-lru",
                maxmemory_samples=5,
            ),
            persistence=RedisPersistenceParams(
                save=["3600 1"], appendonly=False, appendfsync="everysec"
            ),
            security=RedisSecurityParams(
                protected_mode=True, requirepass=requirepass, rename_commands={}
            ),
            networking=RedisNetworkingParams(bind=["0.0.0.0"], port=6379),
        ),
        pg_hba=PostgresHbaConfig(
            rules=[
                PostgresHbaRule(
                    type="local", database="all", user="all", auth_method="trust"
                ),
                PostgresHbaRule(
                    type="host",
                    database="all",
                    user="all",
                    address="0.0.0.0/0",
                    auth_method="scram-sha-256",
                ),
            ]
        ),
    )


def fresh_run_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Unit tests — no Docker needed
# ---------------------------------------------------------------------------


class TestRenderFiles:
    def test_all_files_rendered(self, tmp_path: Path) -> None:
        runner = StackRunner(run_id=fresh_run_id(), workdir=tmp_path)
        files = runner.render_files(make_spec())
        assert set(files) == {
            "docker-compose.yml",
            "postgresql.conf",
            "pg_hba.conf",
            "nginx.conf",
            "redis.conf",
            "html/index.html",
        }
        assert all(content.strip() for content in files.values())

    def test_compose_is_valid_yaml_with_isolation(self, tmp_path: Path) -> None:
        run_id = fresh_run_id()
        runner = StackRunner(run_id=run_id, workdir=tmp_path)
        compose = yaml.safe_load(runner.render_files(make_spec())["docker-compose.yml"])

        assert set(compose["services"]) == set(SERVICES)
        assert f"net_{run_id}" in compose["networks"]
        assert f"pgdata_{run_id}" in compose["volumes"]
        # resource limits present on every service
        for service in SERVICES:
            limits = compose["services"][service]["deploy"]["resources"]["limits"]
            assert limits["memory"].endswith("M")
            assert float(limits["cpus"]) > 0
        # nginx waits on healthy dependencies
        depends = compose["services"]["nginx"]["depends_on"]
        assert depends["postgres"]["condition"] == "service_healthy"
        assert depends["redis"]["condition"] == "service_healthy"

    def test_redis_password_used_in_healthcheck(self, tmp_path: Path) -> None:
        runner = StackRunner(run_id=fresh_run_id(), workdir=tmp_path)
        compose = yaml.safe_load(
            runner.render_files(make_spec(requirepass="harness-test-pass"))[
                "docker-compose.yml"
            ]
        )
        health_cmd = compose["services"]["redis"]["healthcheck"]["test"][1]
        assert "-a 'harness-test-pass'" in health_cmd

    def test_ssl_mounts_only_when_enabled(self, tmp_path: Path) -> None:
        runner = StackRunner(run_id=fresh_run_id(), workdir=tmp_path)
        plain = runner.render_files(make_spec(ssl=False))["docker-compose.yml"]
        assert "server.crt" not in plain
        with_ssl = runner.render_files(make_spec(ssl=True))["docker-compose.yml"]
        assert "./certs/server.crt" in with_ssl
        assert "ssl_key_file" in with_ssl

    def test_ssl_key_is_staged_and_installed_not_mounted_into_place(
        self, tmp_path: Path
    ) -> None:
        # Regression: mounting the key straight to its final path made
        # PostgreSQL exit with "private key file has group or world access",
        # which reached the agent only as a compose dependency failure and
        # cost it an entire token budget to not diagnose.
        runner = StackRunner(run_id=fresh_run_id(), workdir=tmp_path)
        doc = yaml.safe_load(
            runner.render_files(make_spec(ssl=True))["docker-compose.yml"]
        )
        postgres = doc["services"]["postgres"]

        cert_mounts = [v for v in postgres["volumes"] if "server." in v]
        assert cert_mounts, "SSL spec must mount certs"
        assert all("/tmp/certs/" in m for m in cert_mounts), (
            f"certs must be staged under /tmp, got {cert_mounts}"
        )
        assert not any("/var/lib/postgresql/server." in m for m in cert_mounts)

        entrypoint = "\n".join(postgres["entrypoint"])
        assert "install -o postgres -g postgres -m 0600" in entrypoint
        assert "/var/lib/postgresql/server.key" in entrypoint
        assert "exec /usr/local/bin/docker-entrypoint.sh postgres" in entrypoint

    def test_no_entrypoint_override_without_ssl(self, tmp_path: Path) -> None:
        runner = StackRunner(run_id=fresh_run_id(), workdir=tmp_path)
        doc = yaml.safe_load(
            runner.render_files(make_spec(ssl=False))["docker-compose.yml"]
        )
        postgres = doc["services"]["postgres"]
        assert "entrypoint" not in postgres
        assert postgres["command"].startswith("postgres -c")

    def test_generated_key_is_readable_through_the_bind_mount(
        self, tmp_path: Path
    ) -> None:
        # The container copies the key as root before dropping privileges,
        # so it must be readable on the host side of the mount.
        runner = StackRunner(run_id=fresh_run_id(), workdir=tmp_path)
        runner.write_files(make_spec(ssl=True))
        key = tmp_path / "certs" / "server.key"
        assert key.exists()
        assert key.stat().st_mode & 0o004, "key must be readable through the mount"

    def test_write_files_creates_workdir_layout(self, tmp_path: Path) -> None:
        runner = StackRunner(run_id=fresh_run_id(), workdir=tmp_path / "wd")
        compose_path = runner.write_files(make_spec())
        assert compose_path.exists()
        assert (tmp_path / "wd" / "postgresql.conf").exists()
        assert (tmp_path / "wd" / "html" / "index.html").exists()

    def test_bad_run_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            StackRunner(run_id="!!!")


# ---------------------------------------------------------------------------
# Integration tests — require Docker
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIntegration:
    def test_up_down_clean(self) -> None:
        runner = StackRunner(run_id=fresh_run_id())
        try:
            runner.up(make_spec())
            health = runner.wait_healthy(timeout_s=10)
            assert all(health.values()), health
        finally:
            runner.down()
        # nothing remains
        import docker

        leftovers = docker.from_env().containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={runner.project}"},
        )
        assert leftovers == []

    def test_invalid_config_reports_failure(self) -> None:
        runner = StackRunner(run_id=fresh_run_id())
        try:
            runner.write_files(make_spec())
            # inject a syntax error into postgresql.conf
            conf = runner.workdir / "postgresql.conf"
            conf.write_text(conf.read_text() + "\nthis is not a valid directive\n")
            with pytest.raises(StackStartupError) as exc_info:
                runner.start()
            assert "postgres" in str(exc_info.value) or exc_info.value.logs
        finally:
            runner.down()

    def test_exec_in_returns_output(self) -> None:
        with StackRunner(run_id=fresh_run_id()) as runner:
            runner.up(make_spec())
            result = runner.exec_in(
                "postgres", ["psql", "-U", "postgres", "-c", "SELECT 1"]
            )
            assert isinstance(result, CommandResult)
            assert result.exit_code == 0
            assert "1 row" in result.stdout

    def test_resource_limits_enforced(self) -> None:
        with StackRunner(run_id=fresh_run_id()) as runner:
            runner.up(make_spec(redis_maxmemory="256MB"))
            result = runner.exec_in(
                "redis",
                [
                    "redis-cli",
                    "-a", "harness-test-pass",
                    "CONFIG", "GET", "maxmemory",
                ],
            )
            assert result.exit_code == 0
            # redis reports bytes: 256MB = 256 * 1024 * 1024
            assert str(256 * 1024 * 1024) in result.stdout
