"""Tests for the tool registry and the finaliser / validator wrappers.

All tests use the registry's dispatch interface with mocked backing
functions where needed. No real LLM, no Docker.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from src.schemas.agent import ToolCall
from src.tools.registry import (
    QueryRagInput,
    Tool,
    ToolRegistry,
    get_default_registry,
)

# ---------------------------------------------------------------------------
# Helpers: a known-good StackSpec dict (reused from test_config_generator)
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


# ---------------------------------------------------------------------------
# ToolRegistry basics
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        reg = ToolRegistry()
        tool = Tool(
            name="echo",
            description="Echo the input",
            input_schema=QueryRagInput,
            fn=lambda **kw: kw,
        )
        reg.register(tool)
        assert reg.get("echo") is tool

    def test_get_unknown_returns_none(self) -> None:
        reg = ToolRegistry()
        assert reg.get("nonexistent") is None

    def test_dispatch_unknown_tool(self) -> None:
        reg = ToolRegistry()
        obs = reg.dispatch(ToolCall(name="nonexistent", args={}))
        assert not obs.success
        assert "unknown tool" in obs.error

    def test_dispatch_invalid_input(self) -> None:
        reg = ToolRegistry()
        reg.register(
            Tool(
                name="echo",
                description="d",
                input_schema=QueryRagInput,
                fn=lambda **kw: kw,
            )
        )
        obs = reg.dispatch(ToolCall(name="echo", args={"question": ""}))
        assert not obs.success
        assert "input validation" in obs.error

    def test_dispatch_catches_callable_exception(self) -> None:
        def boom(**_kw: Any) -> None:
            raise RuntimeError("kaboom")

        reg = ToolRegistry()
        reg.register(
            Tool(name="boom", description="d", input_schema=QueryRagInput, fn=boom)
        )
        obs = reg.dispatch(ToolCall(name="boom", args={"question": "hi"}))
        assert not obs.success
        assert "kaboom" in obs.error


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


class TestDefaultRegistry:
    def test_list_tools_returns_four(self) -> None:
        reg = get_default_registry()
        tools = reg.list_tools()
        names = {t["name"] for t in tools}
        assert names == {"query_rag", "generate_config", "validate_config", "finalise"}

    def test_dispatch_finalise_valid_spec(self) -> None:
        reg = get_default_registry()
        obs = reg.dispatch(
            ToolCall(
                name="finalise",
                args={"final_spec": _good_spec_dict(), "reason": "looks good"},
            )
        )
        assert obs.success
        assert obs.result["accept"] is True

    def test_dispatch_finalise_bad_spec(self) -> None:
        reg = get_default_registry()
        obs = reg.dispatch(
            ToolCall(
                name="finalise",
                args={"final_spec": {"bad": True}, "reason": "try it"},
            )
        )
        assert obs.success  # tool didn't raise, it returned a decision
        assert obs.result["accept"] is False

    @patch("src.tools.rag.query_rag")
    def test_dispatch_query_rag(self, mock_rag: Any) -> None:
        mock_rag.return_value = [
            {"text": "chunk", "source": "s", "url": "u",
             "service": "postgres", "score": 0.9}
        ]
        reg = get_default_registry()
        obs = reg.dispatch(
            ToolCall(name="query_rag", args={"question": "shared_buffers tuning"})
        )
        assert obs.success
        assert len(obs.result) == 1

    def test_dispatch_generate_config_deterministic(self) -> None:
        reg = get_default_registry()
        obs = reg.dispatch(
            ToolCall(
                name="generate_config",
                args={"partial_spec": _good_spec_dict(), "mode": "deterministic"},
            )
        )
        assert obs.success
        # GeneratedFiles dataclass is serialised to a JSON-safe dict
        assert isinstance(obs.result, dict)
        assert "postgresql_conf" in obs.result
        assert "shared_buffers" in obs.result["postgresql_conf"]
