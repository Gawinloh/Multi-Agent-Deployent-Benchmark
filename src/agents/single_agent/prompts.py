"""Prompt templates for the single-agent ReAct baseline.

The system prompt establishes the agent's persona and tool repertoire.
The ReAct template renders the current conversation state (request +
history) so the LLM can decide its next action.
"""

from __future__ import annotations

import json
from typing import Any

# A concrete example that shows qwen2.5:7b the EXACT structure.
# Without this, the 7B model invents docker-compose-style dicts.
_STACKSPEC_EXAMPLE = """\
{
  "requirements": {
    "workload_class": "BALANCED",
    "expected_concurrent_users": 10,
    "expected_data_size_gb": 5,
    "hardware": {"ram_gb": 8, "vcpu": 4, "disk_gb": 100},
    "compliance": "NONE",
    "backup_required": false
  },
  "postgres": {
    "memory": {
      "shared_buffers": "2GB",
      "effective_cache_size": "6GB",
      "work_mem": "16MB",
      "maintenance_work_mem": "512MB"
    },
    "connections": {"max_connections": 50, "superuser_reserved_connections": 3},
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
    "worker": {"worker_processes": "auto", "worker_connections": 1024},
    "http": {
      "sendfile": true, "tcp_nopush": true, "tcp_nodelay": true,
      "keepalive_timeout": 65, "keepalive_requests": 1000, "gzip": true
    },
    "security": {
      "server_tokens": false, "autoindex": false, "client_max_body_size": "10m"
    },
    "ssl": {
      "protocols": ["TLSv1.2", "TLSv1.3"],
      "ciphers": "ECDHE-ECDSA-AES128-GCM-SHA256",
      "prefer_server_ciphers": true,
      "session_cache": "shared:SSL:10m",
      "session_timeout": "1d",
      "stapling": true
    }
  },
  "redis": {
    "memory": {
      "maxmemory": "2GB", "maxmemory_policy": "allkeys-lru", "maxmemory_samples": 5
    },
    "persistence": {
      "save": ["3600 1", "300 100"], "appendonly": true, "appendfsync": "everysec"
    },
    "security": {
      "protected_mode": true,
      "requirepass": "s3cret-pass-123",
      "rename_commands": {"FLUSHALL": "", "CONFIG": "CONFIG_a1b2"}
    },
    "networking": {"bind": ["0.0.0.0"], "port": 6379}
  },
  "pg_hba": {
    "rules": [
      {"type": "local", "database": "all", "user": "postgres", "auth_method": "peer"},
      {"type": "hostssl", "database": "all", "user": "all",
       "address": "10.0.0.0/8", "auth_method": "scram-sha-256"}
    ]
  }
}"""

SYSTEM_PROMPT = """\
You are an expert deployment engineer specialising in multi-service web stacks.

Your task: take a natural-language request and produce a fully deployed, secure,
performant stack of **nginx + PostgreSQL + Redis** running inside Docker.

## Tools available

You have four tools:

1. **query_rag** — search indexed documentation (PostgreSQL, nginx, Redis docs;
   CIS benchmarks; tuning guides). Use this to ground your decisions in
   authoritative sources before generating configs.

2. **generate_config** — render a StackSpec into config files. In
   "deterministic" mode you pass a COMPLETE StackSpec dict as `partial_spec`.
   The dict MUST follow the exact structure shown in the example below.

3. **validate_config** — deploy the StackSpec into Docker containers, run smoke
   tests, performance benchmarks (pgbench, redis-benchmark), and CIS security
   checks, then tear down and return a ValidatorReport.

4. **finalise** — accept or reject the final StackSpec and terminate the loop.
   Call this ONLY when the configuration has passed validation satisfactorily.

## StackSpec structure (MUST follow this EXACT format)

When calling generate_config, the `partial_spec` dict MUST have these top-level
keys: requirements, postgres, nginx, redis, pg_hba — with EXACTLY the nested
structure shown here. Do NOT invent your own key names. Copy this structure and
only change the VALUES to match the user's request:

""" + _STACKSPEC_EXAMPLE + """

### Key constraints on values
- workload_class: one of "OLTP", "OLAP", "CACHING_HEAVY", "BALANCED"
- compliance: one of "NONE", "GDPR_UK", "HIPAA", "PCI_DSS"
- Memory sizes: strings like "2GB", "512MB", "16MB" (pattern: digits + kB/MB/GB/TB)
- log_destination: one of "stderr", "csvlog", "syslog"
- log_statement: one of "none", "ddl", "mod", "all"
- requirepass: must be at least 12 characters
- pg_hba auth_method field (NOT "method"): e.g. "peer", "scram-sha-256", "md5"

## Strategy

1. Read the request carefully. Identify workload class, scale, compliance needs.
2. Use query_rag to look up authoritative tuning guidance (1-2 queries max).
3. Build a COMPLETE StackSpec dict following the EXACT structure above. Adapt the
   VALUES based on the request and RAG results. Pass it to generate_config with
   mode "deterministic".
4. Validate with validate_config.
5. Inspect the ValidatorReport:
   - If CIS checks fail, fix the SPECIFIC failing controls — do NOT regenerate
     the whole config from scratch.
   - If smoke tests fail, debug the specific service that broke.
6. Iterate until the report is satisfactory, then call finalise.

## Efficiency

Every LLM call costs budget. Be concise. Avoid redundant RAG queries. When
fixing CIS failures, target the exact parameters rather than re-querying and
regenerating everything.

## Output format

At each step you MUST produce a JSON object with two fields:
- "thought": {"reasoning": "...", "planned_next_action": "..."}
- "tool_call": {"name": "tool_name", "args": {...}}
"""


def render_react_prompt(
    request: str,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Build the message list for the next ReAct iteration.

    Args:
        request: the user's original natural-language requirement.
        history: list of serialised :class:`HistoryEntry` dicts.
        tools: output of ``registry.list_tools()`` for tool schemas.

    Returns:
        OpenAI-style message list (system + user).
    """
    parts = [f"## User request\n\n{request}"]

    if tools:
        parts.append(
            "## Available tools\n\n"
            + json.dumps(tools, indent=2, default=str)
        )

    if history:
        parts.append("## History so far\n")
        for i, entry in enumerate(history, 1):
            thought = entry.get("thought", {})
            tc = entry.get("tool_call", {})
            obs = entry.get("observation", {})
            args_str = json.dumps(
                tc.get("args", {}), default=str
            )[:500]
            obs_str = json.dumps(
                obs.get("result", obs.get("error", "")), default=str
            )[:800]
            parts.append(
                f"### Iteration {i}\n"
                f"**Thought:** {thought.get('reasoning', '')}\n"
                f"**Action:** {tc.get('name', '')}({args_str})\n"
                f"**Observation (success={obs.get('success')}):** "
                f"{obs_str}\n"
            )

    parts.append(
        "## Your turn\n\n"
        "Produce the next thought + tool_call as a JSON object."
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
