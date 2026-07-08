"""The ``generate_config`` tool: render or LLM-complete a StackSpec.

Two modes:

- **deterministic**: caller passes a fully-specified StackSpec dict; tool
  validates and renders the four config files + compose YAML.
- **complete**: caller passes a partial spec; the LLM fills missing fields
  via constrained decoding against the StackSpec schema, then renders.

In *complete* mode every LLM call debits the supplied
:class:`~src.llm.token_budget.TokenBudget` via a
:class:`~src.llm.client.BudgetEnforcer`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

from src.schemas.stack import StackSpec

if TYPE_CHECKING:
    from src.llm.client import LLMClient
    from src.llm.token_budget import TokenBudget

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GeneratedFiles:
    """The rendered configuration artefacts produced by :func:`generate_config`.

    Does NOT include docker-compose.yml — the validator harness renders
    its own via the Jinja template (single source of truth).
    """

    postgresql_conf: str
    pg_hba_conf: str
    nginx_conf: str
    redis_conf: str
    spec: StackSpec


_STACKSPEC_EXAMPLE = """\
{
  "requirements": {
    "workload_class": "BALANCED",
    "expected_concurrent_users": 50,
    "expected_data_size_gb": 10,
    "hardware": {"ram_gb": 8, "vcpu": 4, "disk_gb": 100},
    "compliance": "NONE",
    "backup_required": false
  },
  "postgres": {
    "memory": {
      "shared_buffers": "2GB",
      "effective_cache_size": "6GB",
      "work_mem": "32MB",
      "maintenance_work_mem": "512MB"
    },
    "connections": {
      "max_connections": 200,
      "superuser_reserved_connections": 3
    },
    "wal": {
      "wal_level": "replica",
      "checkpoint_completion_target": 0.9,
      "max_wal_size": "1GB"
    },
    "security": {
      "ssl": true,
      "password_encryption": "scram-sha-256",
      "log_connections": true,
      "log_disconnections": true,
      "ssl_min_protocol_version": "TLSv1.2"
    },
    "logging": {
      "log_destination": "stderr",
      "log_statement": "ddl",
      "log_min_duration_statement": 1000
    }
  },
  "nginx": {
    "worker": {
      "worker_processes": "auto",
      "worker_connections": 1024
    },
    "http": {
      "sendfile": true,
      "tcp_nopush": true,
      "tcp_nodelay": true,
      "keepalive_timeout": 65,
      "keepalive_requests": 100,
      "gzip": true
    },
    "security": {
      "server_tokens": false,
      "autoindex": false,
      "client_max_body_size": "10m"
    },
    "ssl": {
      "protocols": ["TLSv1.2", "TLSv1.3"],
      "ciphers": "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256",
      "prefer_server_ciphers": true,
      "session_cache": "shared:SSL:10m",
      "session_timeout": "1d",
      "stapling": true
    }
  },
  "redis": {
    "memory": {
      "maxmemory": "512MB",
      "maxmemory_policy": "allkeys-lru",
      "maxmemory_samples": 5
    },
    "persistence": {
      "save": ["3600 1", "300 100"],
      "appendonly": true,
      "appendfsync": "everysec"
    },
    "security": {
      "protected_mode": true,
      "requirepass": "change-me-strong-password",
      "rename_commands": {"FLUSHALL": "", "CONFIG": "CONFIG_a1b2c3"}
    },
    "networking": {
      "bind": ["127.0.0.1"],
      "port": 6379,
      "tls_port": null
    }
  },
  "pg_hba": {
    "rules": [
      {"type": "local", "database": "all", "user": "all", "address": null, "auth_method": "peer"},
      {"type": "host", "database": "all", "user": "all", "address": "127.0.0.1/32", "auth_method": "scram-sha-256"},
      {"type": "hostssl", "database": "all", "user": "all", "address": "0.0.0.0/0", "auth_method": "scram-sha-256"}
    ]
  }
}"""

_COMPLETION_SYSTEM = (
    "You are an expert infrastructure engineer. Given partial deployment "
    "requirements, produce a COMPLETE StackSpec JSON.\n\n"
    "You MUST follow EXACTLY the structure and field names shown in the "
    "example below. Do NOT invent new field names or flatten the nesting.\n\n"
    "## EXAMPLE (copy this structure exactly, adjust values)\n\n"
    + _STACKSPEC_EXAMPLE + "\n\n"
    "## Rules\n"
    "- Copy the EXACT field names from the example above\n"
    "- Keep the EXACT nesting: postgres.memory.shared_buffers, NOT postgres.shared_buffers\n"
    "- workload_class MUST be one of: OLTP, OLAP, CACHING_HEAVY, BALANCED "
    "(NOT 'WEB_API', NOT 'WEB/API', NOT 'web' — use BALANCED for web workloads)\n"
    "- compliance MUST be one of: NONE, GDPR_UK, HIPAA, PCI_DSS\n"
    "- hardware must have exactly: ram_gb (number), vcpu (integer), disk_gb (number)\n"
    "- Memory sizes are strings: '2GB', '512MB', '16kB'\n"
    "- ssl_min_protocol_version MUST be 'TLSv1.2' or 'TLSv1.3' (NEVER 'TLSv1.0' or 'TLSv1.1')\n"
    "- log_destination is a single string: 'stderr', 'csvlog', or 'syslog'\n"
    "- ssl.protocols is a list: ['TLSv1.2', 'TLSv1.3']\n"
    "- redis.networking.bind is a list: ['127.0.0.1']\n"
    "- pg_hba.rules is a list of objects with: type, database, user, address, auth_method\n"
    "- save entries look like: '3600 1' (seconds changes)\n"
    "- rename_commands is a dict: {'FLUSHALL': '', 'CONFIG': 'CONFIG_xxx'}\n"
    "- keepalive_timeout is an integer (no 's' suffix)\n"
    "- Respond with ONLY the JSON object, no prose\n"
)


def _render(spec: StackSpec) -> GeneratedFiles:
    """Deterministic render from a fully-validated StackSpec."""
    return GeneratedFiles(
        postgresql_conf=spec.postgres.render_conf(),
        pg_hba_conf=spec.pg_hba.render_hba(),
        nginx_conf=spec.nginx.render_conf(),
        redis_conf=spec.redis.render_conf(),
        spec=spec,
    )


def generate_config(
    partial_spec: dict[str, Any],
    mode: Literal["deterministic", "complete"] = "deterministic",
    llm_client: LLMClient | None = None,
    budget: TokenBudget | None = None,
) -> GeneratedFiles:
    """Render config files from a (possibly partial) StackSpec dict.

    Args:
        partial_spec: dict that is either a full StackSpec or a partial
            skeleton when *mode* is ``"complete"``.
        mode: ``"deterministic"`` validates and renders directly;
            ``"complete"`` calls the LLM to fill missing fields first.
        llm_client: required when *mode* is ``"complete"``.
        budget: :class:`TokenBudget` to debit; required when *mode* is
            ``"complete"``.

    Returns:
        :class:`GeneratedFiles` containing the four config file bodies
        and the completed :class:`StackSpec`.

    Raises:
        ValidationError: if *partial_spec* cannot be parsed in
            deterministic mode.
        ValueError: if *mode* is ``"complete"`` but *llm_client* or
            *budget* is missing.
    """
    if mode == "deterministic":
        spec = StackSpec.model_validate(partial_spec)
        logger.info("generate_config_deterministic")
        return _render(spec)

    # --- complete mode ---
    if llm_client is None or budget is None:
        raise ValueError("complete mode requires both llm_client and budget")

    from src.llm.client import BudgetEnforcer

    enforcer = BudgetEnforcer(llm_client, budget)

    import json

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _COMPLETION_SYSTEM},
        {
            "role": "user",
            "content": (
                "Given these partial requirements:\n"
                f"{json.dumps(partial_spec, indent=2)}\n\n"
                "Produce a complete StackSpec JSON that fills in all "
                "missing fields with sensible, production-quality defaults."
            ),
        },
    ]

    spec, _usage = enforcer.chat(messages, schema=StackSpec)
    logger.info("generate_config_complete", tokens_remaining=budget.remaining())
    return _render(spec)  # type: ignore[arg-type]
