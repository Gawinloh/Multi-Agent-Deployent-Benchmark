"""Reproduce the PostgreSQL startup failure and print the container's own log.

The pilot showed PostgreSQL exiting during startup on compliance scenarios
while non-compliance scenarios deployed fine. compose only reports
"dependency failed to start", which does not say why. This script deploys
a minimal spec twice, once with ``postgres.security.ssl`` off and once on,
and prints each container's log so the actual error is visible.

Usage::

    python scripts/diagnose_ssl_startup.py
"""

from __future__ import annotations

import sys

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
from src.validator.docker_runner import StackRunner, StackStartupError


def build_spec(*, ssl: bool) -> StackSpec:
    """Minimal 4 GB spec, identical apart from the SSL flag."""
    return StackSpec(
        requirements=StackRequirements(
            workload_class=WorkloadClass.BALANCED,
            expected_concurrent_users=10,
            expected_data_size_gb=5,
            hardware=HardwareConstraints(ram_gb=4, vcpu=2, disk_gb=50),
            compliance=ComplianceProfile.NONE,
            backup_required=False,
        ),
        postgres=PostgresConfig(
            memory=PostgresMemoryParams(
                shared_buffers="1GB",
                effective_cache_size="2GB",
                work_mem="16MB",
                maintenance_work_mem="256MB",
            ),
            connections=PostgresConnectionParams(
                max_connections=50, superuser_reserved_connections=3
            ),
            wal=PostgresWALParams(
                wal_level="replica",
                checkpoint_completion_target=0.9,
                max_wal_size="1GB",
            ),
            security=PostgresSecurityParams(
                ssl=ssl,
                password_encryption="scram-sha-256",
                log_connections=True,
                log_disconnections=True,
            ),
            logging=PostgresLoggingParams(
                log_destination="stderr",
                log_statement="ddl",
                log_min_duration_statement=250,
            ),
        ),
        nginx=NginxConfig(
            worker=NginxWorkerParams(worker_processes="auto", worker_connections=1024),
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
                ciphers="ECDHE-ECDSA-AES128-GCM-SHA256",
                prefer_server_ciphers=False,
                session_cache="shared:SSL:10m",
                session_timeout="1d",
                stapling=False,
            ),
        ),
        redis=RedisConfig(
            memory=RedisMemoryParams(
                maxmemory="512MB", maxmemory_policy="allkeys-lru", maxmemory_samples=5
            ),
            persistence=RedisPersistenceParams(
                save=["3600 1"], appendonly=True, appendfsync="everysec"
            ),
            security=RedisSecurityParams(
                protected_mode=True, requirepass="diagnostic-password"
            ),
            networking=RedisNetworkingParams(bind=["0.0.0.0"], port=6379),
        ),
        pg_hba=PostgresHbaConfig(
            rules=[
                PostgresHbaRule(
                    type="host",
                    database="all",
                    user="all",
                    address="0.0.0.0/0",
                    auth_method="scram-sha-256",
                )
            ]
        ),
    )


def attempt(*, ssl: bool) -> bool:
    """Deploy once and report. Returns True if the stack came up."""
    label = "ssl=True" if ssl else "ssl=False"
    print(f"\n{'=' * 70}\n  {label}\n{'=' * 70}")
    runner = StackRunner()
    try:
        runner.up(build_spec(ssl=ssl))
        print(f"  STACK CAME UP OK ({label})")
        return True
    except StackStartupError as exc:
        print(f"  STARTUP FAILED ({label}): {str(exc)[:160]}")
        for service, text in (exc.logs or {}).items():
            body = (text or "").strip()
            if not body:
                continue
            print(f"\n  ---- {service} container log (last 25 lines) ----")
            for line in body.splitlines()[-25:]:
                print(f"    {line}")
        return False
    finally:
        try:
            runner.down()
        except Exception as exc:  # noqa: BLE001 — teardown is best effort
            print(f"  (teardown warning: {exc})")


if __name__ == "__main__":
    without = attempt(ssl=False)
    with_ssl = attempt(ssl=True)
    print(f"\n{'=' * 70}")
    print(f"  ssl=False deployed: {without}")
    print(f"  ssl=True  deployed: {with_ssl}")
    if without and not with_ssl:
        print("\n  CONFIRMED: enabling postgres SSL is what breaks startup.")
        print("  The postgres log above states the actual reason.")
    elif without and with_ssl:
        print("\n  Both deployed. SSL is not the cause; the trigger is elsewhere.")
    else:
        print("\n  Even the non-SSL stack failed. The problem is more general.")
    sys.exit(0)
