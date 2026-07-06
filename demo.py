#!/usr/bin/env python3
"""Demo: the single-agent ReAct loop, end to end.

Runs the REAL SingleAgent (src/agents/single_agent/agent.py) through one
request. To keep the demo instant and deterministic, two parts are stood in:

  * the LLM is SCRIPTED — a stand-in for the local model, so the run is fast
    and repeatable, and
  * the Docker validator is SIMULATED — the real container deployment is the
    next build milestone (Step 12).

Everything else is the real system: the real ReAct loop, the real tool
registry and dispatch, real token-budget accounting, and real config-file
rendering from the Pydantic schemas. The simulated validator actually reads
the submitted config and reports a CIS security failure when Postgres auth is
weak — so you can watch the agent fix it and re-validate.

Run:  python demo.py
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import structlog

# Quieten library logging so the demo trace is clean.
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

from src.agents.single_agent.agent import AgentStep, SingleAgent  # noqa: E402
from src.llm.client import LLMClient, TokenUsage  # noqa: E402
from src.llm.token_budget import TokenBudget  # noqa: E402
from src.schemas.agent import AgentThought, ToolCall  # noqa: E402
from src.schemas.stack import StackSpec  # noqa: E402
from src.tools.finaliser import finalise  # noqa: E402
from src.tools.registry import (  # noqa: E402
    FinaliseInput,
    GenerateConfigInput,
    QueryRagInput,
    Tool,
    ToolRegistry,
    ValidateConfigInput,
)

# --------------------------------------------------------------------------
# A valid StackSpec for a small dev stack. We make two variants: one with weak
# Postgres auth (md5) and one corrected (scram-sha-256), so the agent's fix
# genuinely changes the validator's verdict.
# --------------------------------------------------------------------------

SPEC: dict[str, Any] = {
    "requirements": {
        "workload_class": "BALANCED",
        "expected_concurrent_users": 5,
        "expected_data_size_gb": 5,
        "hardware": {"ram_gb": 8, "vcpu": 4, "disk_gb": 100},
        "compliance": "NONE",
        "backup_required": False,
    },
    "postgres": {
        "memory": {
            "shared_buffers": "2GB",
            "effective_cache_size": "6GB",
            "work_mem": "16MB",
            "maintenance_work_mem": "512MB",
        },
        "connections": {"max_connections": 50, "superuser_reserved_connections": 3},
        "wal": {
            "wal_level": "replica",
            "checkpoint_completion_target": 0.9,
            "max_wal_size": "1GB",
        },
        "security": {
            "ssl": True,
            "password_encryption": "md5",  # <-- weak; the agent will fix this
            "log_connections": True,
            "log_disconnections": True,
            "ssl_min_protocol_version": "TLSv1.2",
        },
        "logging": {
            "log_destination": "stderr",
            "log_statement": "ddl",
            "log_min_duration_statement": 1000,
        },
    },
    "nginx": {
        "worker": {"worker_processes": "auto", "worker_connections": 1024},
        "http": {
            "sendfile": True,
            "tcp_nopush": True,
            "tcp_nodelay": True,
            "keepalive_timeout": 65,
            "keepalive_requests": 1000,
            "gzip": True,
        },
        "security": {
            "server_tokens": False,
            "autoindex": False,
            "client_max_body_size": "10m",
        },
        "ssl": {
            "protocols": ["TLSv1.2", "TLSv1.3"],
            "ciphers": "ECDHE-ECDSA-AES128-GCM-SHA256",
            "prefer_server_ciphers": True,
            "session_cache": "shared:SSL:10m",
            "session_timeout": "1d",
            "stapling": True,
        },
    },
    "redis": {
        "memory": {
            "maxmemory": "2GB",
            "maxmemory_policy": "allkeys-lru",
            "maxmemory_samples": 5,
        },
        "persistence": {
            "save": ["3600 1", "300 100"],
            "appendonly": True,
            "appendfsync": "everysec",
        },
        "security": {
            "protected_mode": True,
            "requirepass": "s3cret-pass-123",
            "rename_commands": {"FLUSHALL": "", "CONFIG": "CONFIG_a1b2"},
        },
        "networking": {"bind": ["127.0.0.1"], "port": 6379},
    },
    "pg_hba": {
        "rules": [
            {"type": "local", "database": "all", "user": "postgres", "auth_method": "peer"},
            {
                "type": "hostssl",
                "database": "all",
                "user": "all",
                "address": "10.0.0.0/8",
                "auth_method": "scram-sha-256",
            },
        ]
    },
}

SPEC_BAD = copy.deepcopy(SPEC)
SPEC_GOOD = copy.deepcopy(SPEC)
SPEC_GOOD["postgres"]["security"]["password_encryption"] = "scram-sha-256"


def _step(reasoning: str, action: str, tool: str, args: dict[str, Any]) -> AgentStep:
    return AgentStep(
        thought=AgentThought(reasoning=reasoning, planned_next_action=action),
        tool_call=ToolCall(name=tool, args=args),
    )


# The scripted "model" decisions: think -> act, six iterations including a fix.
SCRIPT = [
    _step(
        "Small dev stack, 8GB RAM, no compliance. First I'll look up sizing "
        "guidance and the CIS authentication baseline.",
        "search the documentation",
        "query_rag",
        {"question": "shared_buffers sizing and CIS auth baseline for a small Postgres stack"},
    ),
    _step(
        "Got pgtune sizing and the CIS auth rules. I'll build the full stack "
        "spec and render the config files.",
        "generate the config files",
        "generate_config",
        {"partial_spec": SPEC_BAD, "mode": "deterministic"},
    ),
    _step(
        "Configs rendered. Deploying to validate: smoke tests, benchmarks, and "
        "CIS security checks.",
        "validate the deployed stack",
        "validate_config",
        {"spec": SPEC_BAD},
    ),
    _step(
        "Validator flagged CIS 1.2 FAIL: password_encryption is md5. I'll switch "
        "Postgres to scram-sha-256 and regenerate.",
        "fix the auth setting and regenerate",
        "generate_config",
        {"partial_spec": SPEC_GOOD, "mode": "deterministic"},
    ),
    _step(
        "Re-validating the corrected configuration.",
        "re-validate",
        "validate_config",
        {"spec": SPEC_GOOD},
    ),
    _step(
        "All CIS checks pass and smoke tests are green. Accepting the configuration.",
        "finalise the run",
        "finalise",
        {"final_spec": SPEC_GOOD, "reason": "all CIS checks pass; smoke green"},
    ),
]


class ScriptedModel(LLMClient):
    """Stands in for the local LLM: returns the pre-scripted decisions."""

    backend_name = "scripted-demo"

    def __init__(self, steps: list[AgentStep], tokens_per_call: int = 220) -> None:
        self._steps = steps
        self._i = 0
        self._tpc = tokens_per_call

    def chat(self, messages: list[dict[str, str]], schema: type | None = None):
        step = self._steps[min(self._i, len(self._steps) - 1)]
        self._i += 1
        return step, TokenUsage(input_tokens=self._tpc, output_tokens=self._tpc // 3)


def _demo_registry() -> ToolRegistry:
    """Real config rendering + a simulated validator that reads the spec."""
    reg = ToolRegistry()

    def query_rag(**_kw: Any) -> list[dict[str, Any]]:
        return [
            {"text": "shared_buffers ~= 25% of RAM (pgtune).", "source": "pgtune",
             "url": "", "service": "postgres", "score": 0.91},
            {"text": "CIS 1.2: password_encryption must be scram-sha-256.",
             "source": "CIS PostgreSQL Benchmark", "url": "", "service": "postgres", "score": 0.88},
        ]

    def generate_config(partial_spec: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        # REAL rendering from the Pydantic schemas.
        spec = StackSpec.model_validate(partial_spec)
        return {
            "postgresql_conf": spec.postgres.render_conf(),
            "pg_hba_conf": spec.pg_hba.render_hba(),
            "nginx_conf": spec.nginx.render_conf(),
            "redis_conf": spec.redis.render_conf(),
            "lines": sum(len(spec.postgres.render_conf().splitlines()) for _ in [0]),
        }

    def validate_config(spec: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        # SIMULATED validator that genuinely inspects the submitted spec.
        enc = spec["postgres"]["security"]["password_encryption"]
        auth_ok = enc == "scram-sha-256"
        cis = [
            {"control_id": "1.2", "name": "password_encryption scram-sha-256",
             "passed": auth_ok, "service": "postgres"},
            {"control_id": "3.2", "name": "ssl on", "passed": True, "service": "postgres"},
            {"control_id": "2.5.1", "name": "server_tokens off", "passed": True, "service": "nginx"},
            {"control_id": "1.1", "name": "protected-mode on", "passed": True, "service": "redis"},
            {"control_id": "3.1", "name": "dangerous commands renamed", "passed": True, "service": "redis"},
        ]
        return {
            "smoke_tests": {s: {"did_start": True, "accepts_connections": True}
                            for s in ("postgres", "nginx", "redis")},
            "benchmarks": {"postgres": {"throughput": 1480.0}, "nginx": {"throughput": 9120.0},
                           "redis": {"throughput": 88000.0}},
            "cis_results": cis,
            "error": None,
        }

    reg.register(Tool("query_rag", "search docs", QueryRagInput, query_rag))
    reg.register(Tool("generate_config", "render configs", GenerateConfigInput, generate_config))
    reg.register(Tool("validate_config", "deploy & test", ValidateConfigInput, validate_config))
    reg.register(Tool("finalise", "accept & stop", FinaliseInput,
                      lambda **kw: finalise(kw["final_spec"], kw["reason"]).model_dump(mode="json")))
    return reg


def _summarise(tool: str, obs: Any) -> str:
    """One-line human summary of a tool observation for the trace."""
    if not obs.success:
        return f"ERROR: {obs.error}"
    r = obs.result
    if tool == "query_rag":
        return f"{len(r)} doc chunks retrieved (top source: {r[0]['source']})"
    if tool == "generate_config":
        pg_lines = len(r["postgresql_conf"].splitlines())
        return f"rendered 4 config files (postgresql.conf = {pg_lines} lines)"
    if tool == "validate_config":
        passed = sum(1 for c in r["cis_results"] if c["passed"])
        total = len(r["cis_results"])
        fails = [c["control_id"] for c in r["cis_results"] if not c["passed"]]
        smoke = sum(1 for s in r["smoke_tests"].values() if s["accepts_connections"])
        tail = f"  FAILING: {', '.join(fails)}" if fails else "  all green"
        return f"smoke {smoke}/3 | CIS {passed}/{total}{tail}"
    if tool == "finalise":
        return f"accept={r['accept']} — {r['reason']}"
    return str(r)[:80]


def main() -> None:
    bar = "=" * 70
    print(f"\n{bar}\n  SINGLE-AGENT BASELINE — live ReAct loop demo\n{bar}")
    print("  Real: ReAct loop, tool dispatch, token budget, config rendering.")
    print("  Simulated for the demo: the LLM (scripted) and Docker (next step).\n")

    request = (
        "I need a small dev web stack for an internal tool. ~5 developers, "
        "about 5GB of Postgres data, on a single 8GB / 4-vCPU host. No compliance, "
        "but use sane security defaults."
    )
    print(f"  USER REQUEST:\n    {request}\n{bar}")

    agent = SingleAgent(
        ScriptedModel(SCRIPT),
        TokenBudget(100_000),
        max_iterations=25,
        registry=_demo_registry(),
    )
    result = agent.run(request)

    for i, e in enumerate(result.history, 1):
        args = dict(e.tool_call.args)
        args_short = {k: ("<spec>" if k in ("partial_spec", "final_spec", "spec") else v)
                      for k, v in args.items()}
        print(f"\n  STEP {i}  think -> {e.tool_call.name}")
        print(f"    thought : {e.thought.reasoning}")
        print(f"    action  : {e.tool_call.name}({args_short})")
        print(f"    result  : {_summarise(e.tool_call.name, e.observation)}")

    print(f"\n{bar}\n  OUTCOME")
    print(f"    termination : {result.termination_reason}")
    print(f"    iterations  : {len(result.history)}")
    print(f"    tokens used : {result.tokens_used:,} / 100,000 budget")
    print(f"    final spec  : {'set' if result.final_spec else 'none'}")

    if result.final_spec:
        print(f"\n{bar}\n  SAMPLE OF THE GENERATED postgresql.conf (real output):\n{bar}")
        for line in result.final_spec.postgres.render_conf().splitlines()[:14]:
            print(f"    {line}")
    print(f"{bar}\n")


if __name__ == "__main__":
    main()
