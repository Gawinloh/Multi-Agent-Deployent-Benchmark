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
        assert "shared_buffers = '2GB'" in result.postgresql_conf
        assert "pg_hba.conf" in result.pg_hba_conf
        assert "worker_processes auto" in result.nginx_conf
        assert "maxmemory 2GB" in result.redis_conf
        assert result.spec == spec

    def test_invalid_spec_raises(self) -> None:
        with pytest.raises(ValidationError):
            generate_config({"garbage": True}, mode="deterministic")

    def test_rendered_postgres_contains_key_directives(self) -> None:
        spec = _make_stack_spec()
        result = generate_config(spec.model_dump(), mode="deterministic")
        assert "ssl = on" in result.postgresql_conf
        assert "max_connections = 50" in result.postgresql_conf

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
        assert "shared_buffers = '2GB'" in result.postgresql_conf

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


class TestServiceSelectionEnforcement:
    """Selection is honoured in code, not by trusting the completion model.

    The completion model regenerates ``requirements`` from scratch and was
    observed dropping ``selected_services`` entirely (qwen2.5:14b, Study 2
    smoke run), so enforcement reads the agent's own call.
    """

    def _example(self, **requirements: object) -> dict:
        import json

        from src.tools.config_generator import _STACKSPEC_EXAMPLE

        payload = json.loads(_STACKSPEC_EXAMPLE)
        payload["requirements"].update(requirements)
        return payload

    def test_unselected_services_are_nulled(self) -> None:
        files = generate_config(
            self._example(selected_services=["postgres", "nginx"]),
            mode="deterministic",
        )
        assert files.spec.redis is None
        assert files.spec.rabbitmq is None
        assert files.redis_conf is None
        assert files.rabbitmq_conf is None
        assert files.postgresql_conf is not None

    def test_pg_hba_follows_postgres_out(self) -> None:
        files = generate_config(
            self._example(selected_services=["nginx"]), mode="deterministic"
        )
        assert files.spec.postgres is None
        assert files.spec.pg_hba is None
        assert files.pg_hba_conf is None

    def test_selection_is_recorded_on_the_spec(self) -> None:
        """The run record must show what the agent asked for, so selection
        can be audited without re-deriving it from the config blocks."""
        files = generate_config(
            self._example(selected_services=["nginx", "redis"]),
            mode="deterministic",
        )
        assert files.spec.requirements.selected_services == ["nginx", "redis"]

    def test_absent_selection_leaves_everything_deployed(self) -> None:
        """Study 1 behaviour: saying nothing about selection changes nothing."""
        files = generate_config(self._example(), mode="deterministic")
        assert files.spec.requirements.selected_services is None
        for name in ("postgres", "nginx", "redis", "rabbitmq"):
            assert getattr(files.spec, name) is not None

    def test_duplicates_are_collapsed(self) -> None:
        files = generate_config(
            self._example(selected_services=["nginx", "nginx"]),
            mode="deterministic",
        )
        assert files.spec.requirements.selected_services == ["nginx"]

    def test_hallucinated_service_is_rejected(self) -> None:
        with pytest.raises(Exception, match="kafka"):
            generate_config(
                self._example(selected_services=["nginx", "kafka"]),
                mode="deterministic",
            )

    def test_empty_selection_is_rejected(self) -> None:
        with pytest.raises(Exception, match="at least one service"):
            generate_config(
                self._example(selected_services=[]), mode="deterministic"
            )

    def test_completion_mode_uses_the_agents_call_not_the_model_output(
        self, monkeypatch
    ) -> None:
        """The regression this whole mechanism exists for: the model drops
        selected_services and emits all four services anyway."""
        import json

        from src.schemas.stack import StackSpec
        from src.tools.config_generator import _STACKSPEC_EXAMPLE

        full = json.loads(_STACKSPEC_EXAMPLE)  # all four, no selected_services
        completed = StackSpec.model_validate(full)

        class FakeEnforcer:
            def __init__(self, *args, **kwargs) -> None: ...

            def chat(self, messages, schema):
                return completed, None

        monkeypatch.setattr("src.llm.client.BudgetEnforcer", FakeEnforcer)

        class FakeBudget:
            def remaining(self) -> int:
                return 1000

        files = generate_config(
            {"requirements": {**full["requirements"], "selected_services": ["nginx"]}},
            mode="complete",
            llm_client=object(),
            budget=FakeBudget(),
        )
        assert files.spec.requirements.selected_services == ["nginx"]
        assert files.spec.postgres is None
        assert files.spec.redis is None
        assert files.spec.rabbitmq is None
        assert files.spec.nginx is not None
