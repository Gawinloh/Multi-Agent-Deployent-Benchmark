"""Top-level stack specification.

A StackSpec is the single typed object that flows between the config
generator, the validator, and the finaliser. ``render_compose()``
produces the docker-compose.yml the validator harness deploys.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from src.schemas.nginx import NginxConfig
from src.schemas.postgres import PostgresConfig, PostgresHbaConfig
from src.schemas.redis import RedisConfig


class WorkloadClass(str, Enum):  # noqa: UP042 — keep 3.10 compat for CI sandboxes
    """Dominant workload shape; drives tuning trade-offs."""

    OLTP = "OLTP"
    OLAP = "OLAP"
    CACHING_HEAVY = "CACHING_HEAVY"
    BALANCED = "BALANCED"


class ComplianceProfile(str, Enum):  # noqa: UP042 — keep 3.10 compat for CI sandboxes
    """Regulatory profile; tightens security requirements."""

    NONE = "NONE"
    GDPR_UK = "GDPR_UK"
    HIPAA = "HIPAA"
    PCI_DSS = "PCI_DSS"


class HardwareConstraints(BaseModel):
    """Host resources the stack must fit inside."""

    ram_gb: float = Field(gt=0)
    vcpu: int = Field(gt=0)
    disk_gb: float = Field(gt=0)


class StackRequirements(BaseModel):
    """Structured form of the user's natural-language request."""

    workload_class: WorkloadClass
    expected_concurrent_users: int = Field(gt=0)
    expected_data_size_gb: float = Field(gt=0)
    hardware: HardwareConstraints
    compliance: ComplianceProfile
    backup_required: bool


class StackSpec(BaseModel):
    """Complete specification of the three-service stack."""

    requirements: StackRequirements
    postgres: PostgresConfig
    nginx: NginxConfig
    redis: RedisConfig
    pg_hba: PostgresHbaConfig

    def render_compose(self) -> str:
        """Render a docker-compose.yml deploying the three services.

        Resource limits are split across services in rough proportion to
        their workload roles (PG is the heaviest consumer). Healthchecks
        follow the patterns required by the validator harness: pg_isready,
        curl, redis-cli ping.
        """
        hw = self.requirements.hardware
        pg_mem = max(hw.ram_gb * 0.5, 0.5)
        redis_mem = max(hw.ram_gb * 0.25, 0.25)
        nginx_mem = max(hw.ram_gb * 0.125, 0.125)
        pg_cpu = max(hw.vcpu * 0.5, 0.5)
        redis_cpu = max(hw.vcpu * 0.25, 0.25)
        nginx_cpu = max(hw.vcpu * 0.25, 0.25)

        redis_ping = "redis-cli ping"
        if self.redis.security.requirepass is not None:
            redis_ping = f"redis-cli -a {self.redis.security.requirepass} ping"

        lines = [
            "# docker-compose.yml - generated, do not edit by hand",
            "services:",
            "  postgres:",
            "    image: postgres:16",
            "    environment:",
            "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?required}",
            "    volumes:",
            "      - ./postgresql.conf:/etc/postgresql/postgresql.conf:ro",
            "      - ./pg_hba.conf:/etc/postgresql/pg_hba.conf:ro",
            "      - pgdata:/var/lib/postgresql/data",
            "    command: >",
            "      postgres -c config_file=/etc/postgresql/postgresql.conf",
            "      -c hba_file=/etc/postgresql/pg_hba.conf",
            "    healthcheck:",
            '      test: ["CMD-SHELL", "pg_isready -U postgres"]',
            "      interval: 5s",
            "      timeout: 5s",
            "      retries: 10",
            "    deploy:",
            "      resources:",
            "        limits:",
            f"          memory: {pg_mem:g}G",
            f"          cpus: '{pg_cpu:g}'",
            "    networks:",
            "      - stack_net",
            "",
            "  redis:",
            "    image: redis:7",
            "    volumes:",
            "      - ./redis.conf:/usr/local/etc/redis/redis.conf:ro",
            "    command: redis-server /usr/local/etc/redis/redis.conf",
            "    healthcheck:",
            f'      test: ["CMD-SHELL", "{redis_ping}"]',
            "      interval: 5s",
            "      timeout: 5s",
            "      retries: 10",
            "    deploy:",
            "      resources:",
            "        limits:",
            f"          memory: {redis_mem:g}G",
            f"          cpus: '{redis_cpu:g}'",
            "    networks:",
            "      - stack_net",
            "",
            "  nginx:",
            "    image: nginx:1.27",
            "    volumes:",
            "      - ./nginx.conf:/etc/nginx/nginx.conf:ro",
            "      - ./html:/usr/share/nginx/html:ro",
            "    ports:",
            '      - "80:80"',
            '      - "443:443"',
            "    healthcheck:",
            '      test: ["CMD-SHELL", "curl -f http://localhost/ || exit 1"]',
            "      interval: 5s",
            "      timeout: 5s",
            "      retries: 10",
            "    depends_on:",
            "      postgres:",
            "        condition: service_healthy",
            "      redis:",
            "        condition: service_healthy",
            "    deploy:",
            "      resources:",
            "        limits:",
            f"          memory: {nginx_mem:g}G",
            f"          cpus: '{nginx_cpu:g}'",
            "    networks:",
            "      - stack_net",
            "",
            "volumes:",
            "  pgdata:",
            "",
            "networks:",
            "  stack_net:",
            "    driver: bridge",
            "",
        ]
        return "\n".join(lines)
