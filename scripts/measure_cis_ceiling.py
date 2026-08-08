"""Measure the actionable security-control ceiling for selectable palettes.

This is a pre-collection diagnostic, not an experiment runner.  It deploys
deliberately maximally hardened specifications through the normal validator
and reports the control-level results needed to establish whether any remaining
actionable controls are genuinely satisfiable through the public schema.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
from src.schemas.rabbitmq import (
    RabbitMQConfig,
    RabbitMQManagementParams,
    RabbitMQNetworkingParams,
    RabbitMQResourceParams,
    RabbitMQSecurityParams,
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
from src.validator.cis_checks import UNREACHABLE_CONTROLS, is_actionable
from src.validator.runner import validate_config

PALETTES: tuple[tuple[str, ...], ...] = (
    ("nginx",),
    ("postgres", "nginx"),
    ("postgres", "rabbitmq"),
    ("postgres", "nginx", "redis"),
    ("postgres", "nginx", "rabbitmq"),
    ("postgres", "nginx", "redis", "rabbitmq"),
)


def _requirements(palette: tuple[str, ...]) -> StackRequirements:
    return StackRequirements(
        workload_class=WorkloadClass.BALANCED,
        expected_concurrent_users=100,
        expected_data_size_gb=10,
        hardware=HardwareConstraints(ram_gb=8, vcpu=4, disk_gb=100),
        compliance=ComplianceProfile.PCI_DSS,
        backup_required=True,
        selected_services=list(palette),
    )


def _postgres() -> tuple[PostgresConfig, PostgresHbaConfig]:
    return (
        PostgresConfig(
            memory=PostgresMemoryParams(
                shared_buffers="2GB",
                effective_cache_size="6GB",
                work_mem="64MB",
                maintenance_work_mem="1GB",
            ),
            connections=PostgresConnectionParams(
                max_connections=500, superuser_reserved_connections=3
            ),
            wal=PostgresWALParams(
                wal_level="logical", checkpoint_completion_target=0.9,
                max_wal_size="4GB",
            ),
            security=PostgresSecurityParams(
                ssl=True,
                password_encryption="scram-sha-256",
                log_connections=True,
                log_disconnections=True,
                ssl_min_protocol_version="TLSv1.3",
            ),
            logging=PostgresLoggingParams(
                log_destination="csvlog", log_statement="all",
                log_min_duration_statement=1,
            ),
            # Docker needs a non-wildcard address that accepts loopback TCP.
            listen_addresses="0.0.0.0",
        ),
        PostgresHbaConfig(
            rules=[
                PostgresHbaRule(
                    type="local", database="all", user="all", auth_method="peer"
                ),
                PostgresHbaRule(
                    type="hostssl", database="all", user="all",
                    address="0.0.0.0/0", auth_method="scram-sha-256",
                ),
            ]
        ),
    )


def _nginx() -> NginxConfig:
    return NginxConfig(
        worker=NginxWorkerParams(worker_processes="auto", worker_connections=100_000),
        http=NginxHttpParams(
            sendfile=True, tcp_nopush=True, tcp_nodelay=True,
            keepalive_timeout=30, keepalive_requests=10_000, gzip=False,
        ),
        security=NginxSecurityParams(
            server_tokens=False, autoindex=False, client_max_body_size="1m"
        ),
        ssl=NginxSSLParams(
            protocols=["TLSv1.3"],
            ciphers="HIGH:!aNULL:!MD5:!3DES",
            prefer_server_ciphers=True,
            session_cache="shared:SSL:10m",
            session_timeout="1d",
            stapling=True,
        ),
    )


def _redis() -> RedisConfig:
    return RedisConfig(
        memory=RedisMemoryParams(
            maxmemory="1GB", maxmemory_policy="allkeys-lfu", maxmemory_samples=64
        ),
        persistence=RedisPersistenceParams(
            save=["900 1", "300 10", "60 10000"], appendonly=True,
            appendfsync="always",
        ),
        security=RedisSecurityParams(
            protected_mode=True,
            requirepass="ceiling-hardened-redis-password",
            rename_commands={
                "FLUSHALL": "",
                "FLUSHDB": "",
                "CONFIG": "",
                "EVAL": "",
                "DEBUG": "",
                "SHUTDOWN": "",
            },
        ),
        networking=RedisNetworkingParams(bind=["127.0.0.1"], port=6379),
    )


def _rabbitmq() -> RabbitMQConfig:
    return RabbitMQConfig(
        resources=RabbitMQResourceParams(
            vm_memory_high_watermark=0.4,
            vm_memory_high_watermark_paging_ratio=0.5,
            disk_free_limit="1GB",
        ),
        networking=RabbitMQNetworkingParams(
            listener_ip="127.0.0.1", listener_port=5672,
            max_connections=500, heartbeat=60,
        ),
        security=RabbitMQSecurityParams(
            default_user="application",
            default_pass="ceiling-hardened-rabbit-password",
            loopback_users=["application"],
            tls_enabled=True,
            tls_port=5671,
            tls_verify_peer=True,
        ),
        management=RabbitMQManagementParams(
            enabled=True, listener_ip="127.0.0.1", port=15672
        ),
    )


def maximum_spec(palette: tuple[str, ...]) -> StackSpec:
    """Return the strongest schema-satisfiable spec for ``palette``."""
    postgres, pg_hba = _postgres()
    return StackSpec(
        requirements=_requirements(palette),
        postgres=postgres if "postgres" in palette else None,
        pg_hba=pg_hba if "postgres" in palette else None,
        nginx=_nginx() if "nginx" in palette else None,
        redis=_redis() if "redis" in palette else None,
        rabbitmq=_rabbitmq() if "rabbitmq" in palette else None,
    )


def _record(palette: tuple[str, ...], budget_seconds: int) -> dict[str, object]:
    report = validate_config(maximum_spec(palette), budget_seconds=budget_seconds)
    controls = [
        {
            "key": f"{check.service} {check.control_id}",
            "passed": check.passed,
            "actionable": is_actionable(check.service, check.control_id),
            "evidence": check.evidence,
        }
        for check in report.cis_results
    ]
    actionable = [control for control in controls if control["actionable"]]
    return {
        "palette": list(palette),
        "error": report.error,
        "smoke": {
            service: result.passed for service, result in report.smoke_tests.items()
        },
        "raw": {
            "passed": sum(control["passed"] for control in controls),
            "total": len(controls),
        },
        "actionable": {
            "passed": sum(control["passed"] for control in actionable),
            "total": len(actionable),
            "rate": (sum(control["passed"] for control in actionable) / len(actionable))
            if actionable
            else None,
        },
        "failed_actionable": [
            control for control in actionable if not control["passed"]
        ],
        "controls": controls,
    }


def _chosen_palettes(values: Iterable[str] | None) -> tuple[tuple[str, ...], ...]:
    if values is None:
        return PALETTES
    wanted = {tuple(value.split(",")) for value in values}
    unknown = wanted - set(PALETTES)
    if unknown:
        choices = "; ".join(",".join(palette) for palette in PALETTES)
        raise SystemExit(f"Unknown palette(s) {sorted(unknown)}; choose from: {choices}")
    return tuple(palette for palette in PALETTES if palette in wanted)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--palette", action="append", metavar="SERVICE[,SERVICE...]",
        help="Measure only this ordered palette; repeatable",
    )
    parser.add_argument("--budget", type=int, default=300)
    parser.add_argument(
        "--output", type=Path,
        help="Write the complete machine-readable measurement record to this path",
    )
    args = parser.parse_args()

    records = [_record(palette, args.budget) for palette in _chosen_palettes(args.palette)]
    payload = {"unreachable_controls": UNREACHABLE_CONTROLS, "measurements": records}
    rendered = json.dumps(payload, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
