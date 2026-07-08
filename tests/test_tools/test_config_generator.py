"""Tests for the ``generate_config`` tool.

Covers deterministic rendering, validation errors, LLM-completion mode
(mocked client), and budget enforcement.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from src.llm.client import TokenUsage
from src.llm.token_budget import BudgetExhausted, TokenBudget
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
from src.tools.config_generator import GeneratedFiles, generate_config


def _make_stack_spec() -> StackSpec:
    """Return a known-good StackSpec for testing."""
    return StackSpec(
        requirements=StackRequirements(
            workload_class=WorkloadClass.BALANCED,
            expected_concurrent_users=5,
            expected_data_size_gb=5,
            hardware=HardwareConstraints(ram_gb=8, vcpu=4, disk_gb=100),
            compliance=ComplianceProfile.NONE,
            backup_required=False,
        ),
        postgres=PostgresConfig(
            memory=PostgresMemoryParams(
                shared_buffers="2GB",
                effective_cache_size="6GB",
                work_mem="16MB",
                maintenance_work_mem="512MB",
            ),
            connections=PostgresConnectionParams(
                max_connections=50, superuser_reserved_connections=3
            ),
            wal=PostgresWALParams(
                wal_level="replica", checkpoint_completion_target=0.9, max_wal_size="1GB"
            ),
            security=PostgresSecurityParams(
                ssl=True,
                password_encryption="scram-sha-256",
                log_connections=True,
                log_disconnections=True,
                ssl_min_protocol_version="TLSv1.2",
            ),
            logging=PostgresLoggingParams(
                log_destination="stderr", log_statement="ddl", log_min_duration_statement=1000
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
                prefer_server_ciphers=True,
                session_cache="shared:SSL:10m",
                session_timeout="1d",
                stapling=True,
            ),
        ),
        redis=RedisConfig(
            memory=RedisMemoryParams(
                maxmemory="2GB", maxmemory_policy="allkeys-lru", maxmemory_samples=5
            ),
            persistence=RedisPersistenceParams(
                save=["3600 1", "300 100"], appendonly=True, appendfsync="everysec"
            ),
            security=RedisSecurityParams(
                protected_mode=True,
                requirepass="s3cret-pass-123",
                rename_commands={"FLUSHALL": "", "CONFIG": "CONFIG_a1b2"},
            ),
            networking=RedisNetworkingParams(bind=["0.0.0.0"], port=6379),
        ),
        pg_hba=PostgresHbaConfig(
            rules=[
                PostgresHbaRule(
                    type="local", database="all", user="postgres", auth_method="peer"
                ),
                PostgresHbaRule(
                    type="hostssl",
                    database="all",
                    user="all",
                    address="10.0.0.0/8",
                    auth_method="scram-sha-256",
                ),
            ]
        ),
    )


class TestDeterministicMode:
    def test_render_produces_all_files(self) -> None:
        spec = _make_stack_spec()
        result = generate_config(spec.model_dump(), mode="deterministic")
        assert isinstance(result, GeneratedFiles)
        assert "shared_buffers = 2GB" in result.postgresql_conf
        assert "pg_hba.conf" in result.pg_hba_conf
        assert "worker_processes auto" in result.nginx_conf
        assert "maxmemory 2GB" in result.redis_conf
        assert "services:" in result.compose_yml
        assert result.spec == spec

    def test_invalid_spec_raises(self) -> None:
        with pytest.raises(ValidationError):
            generate_config({"garbage": True}, mode="deterministic")

    def test_rendered_postgres_contains_key_directives(self) -> None:
        spec = _make_stack_spec()
        result = generate_config(spec.model_dump(), mode="deterministic")
        assert "ssl = on" in result.postgresql_conf
        assert "max_connections = 50" in result.postgresql_conf

    def test_rendered_compose_contains_services(self) -> None:
        spec = _make_stack_spec()
        result = generate_config(spec.model_dump(), mode="deterministic")
        assert "postgres:" in result.compose_yml
        assert "nginx:" in result.compose_yml
        assert "redis:" in result.compose_yml


class TestCompleteMode:
    def test_complete_mode_requires_client_and_budget(self) -> None:
        with pytest.raises(ValueError, match="complete mode requires"):
            generate_config({}, mode="complete")

    def test_complete_mode_uses_llm(self) -> None:
        """Mock LLM returns a valid StackSpec; verify it's called and files render."""
        spec = _make_stack_spec()
        mock_client = MagicMock()
        mock_client.chat.return_value = (spec, TokenUsage(input_tokens=100, output_tokens=200))
        budget = TokenBudget(10_000)

        result = generate_config(
            {"requirements": spec.model_dump()["requirements"]},
            mode="complete",
            llm_client=mock_client,
            budget=budget,
        )

        mock_client.chat.assert_called_once()
        assert isinstance(result, GeneratedFiles)
        assert result.spec == spec
        assert "shared_buffers = 2GB" in result.postgresql_conf

    def test_complete_mode_respects_budget(self) -> None:
        """Exhaust the budget; assert BudgetExhausted propagates."""
        mock_client = MagicMock()
        spec = _make_stack_spec()
        # First call tips the budget over
        mock_client.chat.return_value = (spec, TokenUsage(input_tokens=500, output_tokens=600))
        budget = TokenBudget(100)  # tiny budget

        with pytest.raises(BudgetExhausted):
            generate_config(
                {"requirements": spec.model_dump()["requirements"]},
                mode="complete",
                llm_client=mock_client,
                budget=budget,
            )

    def test_complete_mode_debits_budget(self) -> None:
        """Verify token usage is recorded on the budget."""
        spec = _make_stack_spec()
        mock_client = MagicMock()
        mock_client.chat.return_value = (spec, TokenUsage(input_tokens=100, output_tokens=200))
        budget = TokenBudget(10_000)

        generate_config(
            {},
            mode="complete",
            llm_client=mock_client,
            budget=budget,
        )

        summary = budget.summary()
        assert summary["total_used"] == 300
        assert summary["calls"] == 1
