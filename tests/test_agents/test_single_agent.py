"""Tests for the single-agent ReAct loop.

All tests use a mocked LLM client that returns scripted AgentStep
sequences and a mocked tool registry. No real LLM calls, no Docker.
"""

from __future__ import annotations

from typing import Any

from src.agents.single_agent.agent import AgentStep, SingleAgent
from src.llm.client import LLMClient, TokenUsage
from src.llm.token_budget import TokenBudget
from src.schemas.agent import AgentThought, ToolCall
from src.tools.registry import (
    FinaliseInput,
    GenerateConfigInput,
    QueryRagInput,
    Tool,
    ToolRegistry,
    ValidateConfigInput,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _good_spec_dict() -> dict[str, Any]:
    """Minimal valid StackSpec dict."""
    return {
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
                "password_encryption": "scram-sha-256",
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
            "networking": {"bind": ["0.0.0.0"], "port": 6379},
        },
        "pg_hba": {
            "rules": [
                {
                    "type": "local",
                    "database": "all",
                    "user": "postgres",
                    "auth_method": "peer",
                },
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


def _make_step(tool_name: str, args: dict[str, Any] | None = None) -> AgentStep:
    return AgentStep(
        thought=AgentThought(
            reasoning=f"I will call {tool_name}",
            planned_next_action=f"call {tool_name}",
        ),
        tool_call=ToolCall(name=tool_name, args=args or {}),
    )


class ScriptedLLMClient(LLMClient):
    """LLM client that returns pre-scripted AgentStep responses."""

    backend_name = "scripted"

    def __init__(
        self,
        steps: list[AgentStep],
        tokens_per_call: int = 50,
    ) -> None:
        self._steps = list(steps)
        self._call_idx = 0
        self._tokens_per_call = tokens_per_call

    def chat(
        self,
        messages: list[dict[str, str]],
        schema: type | None = None,
    ) -> tuple[Any, TokenUsage]:
        if self._call_idx >= len(self._steps):
            # Fall back to a finalise call if script is exhausted
            step = _make_step("finalise", {"final_spec": _good_spec_dict(), "reason": "done"})
        else:
            step = self._steps[self._call_idx]
        self._call_idx += 1
        usage = TokenUsage(
            input_tokens=self._tokens_per_call,
            output_tokens=self._tokens_per_call,
        )
        return step, usage


def _stub_registry() -> ToolRegistry:
    """Build a registry with stub tool functions for unit testing."""
    reg = ToolRegistry()

    def stub_query_rag(**_kw: Any) -> list[dict[str, Any]]:
        return [
            {
                "text": "shared_buffers 25% RAM",
                "source": "pgtune",
                "url": "",
                "service": "postgres",
                "score": 0.9,
            }
        ]

    def stub_generate_config(**kw: Any) -> dict[str, Any]:
        return {
            "postgresql_conf": "...",
            "nginx_conf": "...",
            "redis_conf": "...",
            "pg_hba_conf": "...",
            "compose_yml": "...",
            "spec": _good_spec_dict(),
        }

    def stub_validate_config(**_kw: Any) -> dict[str, Any]:
        return {
            "timestamp": "2026-06-28T00:00:00Z",
            "smoke_tests": {},
            "benchmarks": {},
            "cis_results": [],
            "healthchecks": {},
            "error": None,
        }

    def stub_finalise(**kw: Any) -> dict[str, Any]:
        from src.tools.finaliser import finalise
        return finalise(
            final_spec=kw.get("final_spec", {}),
            reason=kw.get("reason", "done"),
        ).model_dump(mode="json")

    reg.register(Tool(
        name="query_rag", description="d",
        input_schema=QueryRagInput, fn=stub_query_rag,
    ))
    reg.register(Tool(
        name="generate_config", description="d",
        input_schema=GenerateConfigInput, fn=stub_generate_config,
    ))
    reg.register(Tool(
        name="validate_config", description="d",
        input_schema=ValidateConfigInput, fn=stub_validate_config,
    ))
    reg.register(Tool(
        name="finalise", description="d",
        input_schema=FinaliseInput, fn=stub_finalise,
    ))
    return reg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSingleAgent:
    def test_simple_run_completes(self) -> None:
        """LLM scripts: query_rag → generate_config → validate_config → finalise."""
        steps = [
            _make_step("query_rag", {"question": "shared_buffers tuning"}),
            _make_step(
                "generate_config",
                {"partial_spec": _good_spec_dict(), "mode": "deterministic"},
            ),
            _make_step("validate_config", {"spec": _good_spec_dict()}),
            _make_step(
                "finalise",
                {"final_spec": _good_spec_dict(), "reason": "validation passed"},
            ),
        ]
        client = ScriptedLLMClient(steps)
        budget = TokenBudget(100_000)
        agent = SingleAgent(client, budget, registry=_stub_registry())

        result = agent.run("deploy a small dev stack")

        assert result.termination_reason == "finalised"
        assert result.final_spec is not None
        assert len(result.history) == 4
        assert result.tokens_used > 0
        assert result.wall_clock_s > 0

    def test_budget_exhausted(self) -> None:
        """Tiny budget; LLM loops query_rag forever."""
        steps = [
            _make_step("query_rag", {"question": f"q{i}"})
            for i in range(50)
        ]
        client = ScriptedLLMClient(steps, tokens_per_call=100)
        budget = TokenBudget(350)  # allows ~1-2 calls then exhausts
        agent = SingleAgent(client, budget, max_iterations=50, registry=_stub_registry())

        result = agent.run("deploy something")

        assert result.termination_reason == "budget_exhausted"
        assert result.final_spec is None

    def test_max_iterations_respected(self) -> None:
        """LLM never finalises; assert iterations are capped."""
        steps = [
            _make_step("query_rag", {"question": f"q{i}"})
            for i in range(100)
        ]
        client = ScriptedLLMClient(steps, tokens_per_call=10)
        budget = TokenBudget(1_000_000)
        agent = SingleAgent(client, budget, max_iterations=3, registry=_stub_registry())

        result = agent.run("deploy something")

        assert result.termination_reason == "max_iterations"
        assert len(result.history) == 3

    def test_tool_error_handled(self) -> None:
        """Tool dispatch raises; agent logs error in observation and continues."""
        steps = [
            _make_step("query_rag", {"question": "something"}),
            _make_step(
                "finalise",
                {"final_spec": _good_spec_dict(), "reason": "done"},
            ),
        ]

        def boom_rag(**_kw: Any) -> None:
            raise RuntimeError("rag exploded")

        reg = _stub_registry()
        # Replace query_rag with one that explodes
        reg.register(Tool(
            name="query_rag", description="d",
            input_schema=QueryRagInput, fn=boom_rag,
        ))

        client = ScriptedLLMClient(steps)
        budget = TokenBudget(100_000)
        agent = SingleAgent(client, budget, registry=reg)

        result = agent.run("deploy something")

        # Should have recovered and finalised on the second step
        assert result.termination_reason == "finalised"
        assert len(result.history) == 2
        # First observation should be a failure
        assert not result.history[0].observation.success
        assert "rag exploded" in result.history[0].observation.error

    def test_result_contains_history(self) -> None:
        steps = [
            _make_step(
                "finalise",
                {"final_spec": _good_spec_dict(), "reason": "immediate"},
            ),
        ]
        client = ScriptedLLMClient(steps)
        budget = TokenBudget(100_000)
        agent = SingleAgent(client, budget, registry=_stub_registry())

        result = agent.run("deploy")

        assert len(result.history) == 1
        assert result.history[0].tool_call.name == "finalise"
