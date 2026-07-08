"""Top-level stack specification.

A StackSpec is the single typed object that flows between the config
generator, the validator, and the finaliser. The docker-compose.yml is
rendered by the validator harness via its Jinja template — StackSpec
only carries per-service configs (render_conf / render_hba).
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

    # NOTE: render_compose() was removed — the validator harness renders
    # its own docker-compose.yml via the Jinja template at
    # docker/compose-template.yml (see src/validator/docker_runner.py).
    # Having two compose renderers was a correctness-scoring landmine.
