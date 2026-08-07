"""Top-level stack specification.

A StackSpec is the single typed object that flows between the config
generator, the validator, and the finaliser. The docker-compose.yml is
rendered by the validator harness via its Jinja template — StackSpec
only carries per-service configs (render_conf / render_hba).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

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
    """Specification of the services selected for deployment.

    Each service field is optional: ``None`` means *not selected*, and
    the service is then not rendered, deployed, benchmarked or
    CIS-checked (see :func:`src.services.catalog.for_spec`). Not
    selected is not the same as not configured — a scenario whose ground
    truth asserts a service the spec omitted still fails those
    assertions.

    The fields stay fixed rather than becoming a ``dict[str, ...]``
    because a dynamic mapping is only needed once a second service
    palette exists; that is the L3 decision in
    docs/planning/extensibility-plan.md.
    """

    requirements: StackRequirements
    postgres: PostgresConfig | None = None
    nginx: NginxConfig | None = None
    redis: RedisConfig | None = None
    #: Coupled to postgres: required with it, forbidden without it.
    pg_hba: PostgresHbaConfig | None = None

    @model_validator(mode="after")
    def _check_selection(self) -> StackSpec:
        """Reject specs that deploy nothing, or postgres without pg_hba.

        Both were unrepresentable while the fields were required, and
        both would fail later and less legibly — an empty compose file,
        or a postgres container with no host-based authentication.
        """
        if self.postgres is None and self.nginx is None and self.redis is None:
            raise ValueError(
                "a StackSpec must select at least one service "
                "(postgres, nginx, redis)"
            )
        if self.postgres is not None and self.pg_hba is None:
            raise ValueError("pg_hba is required when postgres is selected")
        if self.postgres is None and self.pg_hba is not None:
            raise ValueError("pg_hba is meaningless without postgres")
        return self

    # NOTE: render_compose() was removed — the validator harness renders
    # its own docker-compose.yml via the Jinja template at
    # docker/compose-template.yml (see src/validator/docker_runner.py).
    # Having two compose renderers was a correctness-scoring landmine.
