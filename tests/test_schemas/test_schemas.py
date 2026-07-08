"""Unit tests for src.schemas: instantiation, rendering, validation."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from src.schemas.agent import (
    AgentThought,
    FinalisationDecision,
    HistoryEntry,
    ToolCall,
    ToolObservation,
)
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
from src.schemas.validator_report import (
    BenchmarkResult,
    CISCheckResult,
    HealthcheckResult,
    SmokeTestResult,
    ValidatorReport,
)

# ---------------------------------------------------------------------------
# Fixtures: known-good configs
# ---------------------------------------------------------------------------


@pytest.fixture
def postgres_config() -> PostgresConfig:
    return PostgresConfig(
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
    )


@pytest.fixture
def hba_config() -> PostgresHbaConfig:
    return PostgresHbaConfig(
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
    )


@pytest.fixture
def nginx_config() -> NginxConfig:
    return NginxConfig(
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
            ciphers="ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256",
            prefer_server_ciphers=True,
            session_cache="shared:SSL:10m",
            session_timeout="1d",
            stapling=True,
        ),
    )


@pytest.fixture
def redis_config() -> RedisConfig:
    return RedisConfig(
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
        networking=RedisNetworkingParams(bind=["0.0.0.0"], port=6379, tls_port=None),
    )


@pytest.fixture
def stack_spec(
    postgres_config: PostgresConfig,
    nginx_config: NginxConfig,
    redis_config: RedisConfig,
    hba_config: PostgresHbaConfig,
) -> StackSpec:
    return StackSpec(
        requirements=StackRequirements(
            workload_class=WorkloadClass.BALANCED,
            expected_concurrent_users=5,
            expected_data_size_gb=5,
            hardware=HardwareConstraints(ram_gb=8, vcpu=4, disk_gb=100),
            compliance=ComplianceProfile.NONE,
            backup_required=False,
        ),
        postgres=postgres_config,
        nginx=nginx_config,
        redis=redis_config,
        pg_hba=hba_config,
    )


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


class TestPostgresConfig:
    def test_render_conf_contains_directives(self, postgres_config: PostgresConfig) -> None:
        conf = postgres_config.render_conf()
        assert "shared_buffers = 2GB" in conf
        assert "max_connections = 50" in conf
        assert "wal_level = replica" in conf
        assert "ssl = on" in conf
        assert "password_encryption = scram-sha-256" in conf
        assert "log_statement = 'ddl'" in conf
        assert "log_connections = on" in conf

    def test_bad_memory_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostgresMemoryParams(
                shared_buffers="lots",
                effective_cache_size="6GB",
                work_mem="16MB",
                maintenance_work_mem="512MB",
            )

    def test_bad_wal_level_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostgresWALParams(
                wal_level="extreme", checkpoint_completion_target=0.9, max_wal_size="1GB"
            )

    def test_checkpoint_target_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostgresWALParams(
                wal_level="replica", checkpoint_completion_target=1.5, max_wal_size="1GB"
            )

    def test_negative_max_connections_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostgresConnectionParams(
                max_connections=-1, superuser_reserved_connections=3
            )

    def test_md5_password_encryption_allowed_but_renderable(self) -> None:
        # md5 is schema-valid (the CIS checker flags it, not the schema)
        params = PostgresSecurityParams(
            ssl=False,
            password_encryption="md5",
            log_connections=False,
            log_disconnections=False,
            ssl_min_protocol_version="TLSv1.2",
        )
        assert params.password_encryption == "md5"


class TestPostgresHba:
    def test_render_hba(self, hba_config: PostgresHbaConfig) -> None:
        hba = hba_config.render_hba()
        assert "local\tall\tpostgres\tpeer" in hba
        assert "hostssl\tall\tall\t10.0.0.0/8\tscram-sha-256" in hba

    def test_empty_rules_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostgresHbaConfig(rules=[])

    def test_bad_auth_method_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostgresHbaRule(
                type="host",
                database="all",
                user="all",
                address="0.0.0.0/0",
                auth_method="plaintext",
            )


# ---------------------------------------------------------------------------
# nginx
# ---------------------------------------------------------------------------


class TestNginxConfig:
    def test_render_conf_contains_directives(self, nginx_config: NginxConfig) -> None:
        conf = nginx_config.render_conf()
        assert "worker_processes auto;" in conf
        assert "worker_connections 1024;" in conf
        assert "server_tokens off;" in conf
        assert "autoindex off;" in conf
        assert "ssl_protocols TLSv1.2 TLSv1.3;" in conf
        assert "ssl_prefer_server_ciphers on;" in conf
        assert "client_max_body_size 10m;" in conf

    def test_integer_worker_processes(self) -> None:
        params = NginxWorkerParams(worker_processes=4, worker_connections=512)
        assert params.worker_processes == 4

    def test_bad_worker_processes_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NginxWorkerParams(worker_processes="many", worker_connections=512)

    def test_unknown_tls_protocol_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NginxSSLParams(
                protocols=["SSLv3"],
                ciphers="x",
                prefer_server_ciphers=True,
                session_cache="shared:SSL:10m",
                session_timeout="1d",
                stapling=False,
            )

    def test_bad_body_size_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NginxSecurityParams(
                server_tokens=False, autoindex=False, client_max_body_size="huge"
            )


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


class TestRedisConfig:
    def test_render_conf_contains_directives(self, redis_config: RedisConfig) -> None:
        conf = redis_config.render_conf()
        assert "maxmemory 2GB" in conf
        assert "maxmemory-policy allkeys-lru" in conf
        assert "appendonly yes" in conf
        assert "appendfsync everysec" in conf
        assert "protected-mode yes" in conf
        assert "requirepass s3cret-pass-123" in conf
        assert 'rename-command FLUSHALL ""' in conf
        assert 'rename-command CONFIG "CONFIG_a1b2"' in conf
        assert "save 3600 1" in conf

    def test_no_requirepass_omitted_from_render(self, redis_config: RedisConfig) -> None:
        config = redis_config.model_copy(
            update={
                "security": RedisSecurityParams(
                    protected_mode=True, requirepass=None, rename_commands={}
                )
            }
        )
        assert "requirepass" not in config.render_conf()

    def test_bad_policy_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RedisMemoryParams(
                maxmemory="2GB", maxmemory_policy="evict-everything", maxmemory_samples=5
            )

    def test_bad_save_entry_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RedisPersistenceParams(
                save=["whenever"], appendonly=True, appendfsync="everysec"
            )

    def test_short_requirepass_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RedisSecurityParams(
                protected_mode=True, requirepass="short", rename_commands={}
            )

    def test_bad_port_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RedisNetworkingParams(bind=["127.0.0.1"], port=99999)


# ---------------------------------------------------------------------------
# Stack
# ---------------------------------------------------------------------------


class TestStackSpec:
    def test_invalid_requirements_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StackRequirements(
                workload_class="WEBSCALE",  # not a valid WorkloadClass
                expected_concurrent_users=5,
                expected_data_size_gb=5,
                hardware=HardwareConstraints(ram_gb=8, vcpu=4, disk_gb=100),
                compliance=ComplianceProfile.NONE,
                backup_required=False,
            )

    def test_zero_hardware_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HardwareConstraints(ram_gb=0, vcpu=4, disk_gb=100)

    def test_round_trips_through_json(self, stack_spec: StackSpec) -> None:
        restored = StackSpec.model_validate_json(stack_spec.model_dump_json())
        assert restored == stack_spec


# ---------------------------------------------------------------------------
# Validator report
# ---------------------------------------------------------------------------


class TestValidatorReport:
    def _report(self) -> ValidatorReport:
        return ValidatorReport(
            timestamp=datetime(2026, 6, 10, 12, 0),
            smoke_tests={
                "postgres": SmokeTestResult(did_start=True, accepts_connections=True),
                "nginx": SmokeTestResult(
                    did_start=True, accepts_connections=False, error_message="502"
                ),
            },
            benchmarks={
                "postgres": BenchmarkResult(throughput=850.5, latency_p50=4.2),
                "redis": BenchmarkResult(error_message="redis-benchmark not found"),
            },
            cis_results=[
                CISCheckResult(
                    control_id="2.1",
                    name="log_connections enabled",
                    level=1,
                    passed=True,
                    evidence="log_connections = on",
                    service="postgres",
                ),
                CISCheckResult(
                    control_id="4.1",
                    name="server_tokens off",
                    level=1,
                    passed=False,
                    evidence="server_tokens on",
                    service="nginx",
                ),
            ],
            healthchecks={
                "postgres": HealthcheckResult(
                    healthcheck_present=True, healthcheck_condition="service_healthy"
                )
            },
        )

    def test_summary_one_line(self) -> None:
        summary = self._report().summary()
        assert "\n" not in summary
        assert "smoke 1/2" in summary
        assert "CIS 1/2 (50%)" in summary

    def test_cis_pass_rate(self) -> None:
        assert self._report().cis_pass_rate() == 0.5

    def test_cis_pass_rate_none_when_no_checks(self) -> None:
        report = ValidatorReport(timestamp=datetime.now())
        assert report.cis_pass_rate() is None

    def test_error_report_summary(self) -> None:
        report = ValidatorReport(timestamp=datetime.now(), error="docker daemon down")
        assert report.summary() == "validation failed: docker daemon down"

    def test_invalid_cis_level_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CISCheckResult(
                control_id="1.1",
                name="x",
                level=3,
                passed=True,
                evidence="e",
                service="postgres",
            )


# ---------------------------------------------------------------------------
# Agent schemas
# ---------------------------------------------------------------------------


class TestAgentSchemas:
    def test_history_entry(self) -> None:
        entry = HistoryEntry(
            thought=AgentThought(
                reasoning="Need PG tuning guidance", planned_next_action="query_rag"
            ),
            tool_call=ToolCall(name="query_rag", args={"question": "shared_buffers?"}),
            observation=ToolObservation(success=True, result=[{"text": "..."}]),
        )
        assert entry.tool_call.name == "query_rag"

    def test_empty_reasoning_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentThought(reasoning="", planned_next_action="query_rag")

    def test_finalisation_accept_with_spec(self, stack_spec: StackSpec) -> None:
        decision = FinalisationDecision(
            accept=True, final_spec=stack_spec, reason="all checks passed"
        )
        assert decision.final_spec is not None

    def test_finalisation_reject_without_spec(self) -> None:
        decision = FinalisationDecision(accept=False, reason="spec invalid")
        assert decision.final_spec is None
