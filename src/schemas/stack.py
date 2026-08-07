"""Top-level stack specification.

A StackSpec is the single typed object that flows between the config
generator, the validator, and the finaliser. The docker-compose.yml is
rendered by the validator harness via its Jinja template — StackSpec
only carries per-service configs (render_conf / render_hba).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

from src.schemas.nginx import NginxConfig
from src.schemas.postgres import PostgresConfig, PostgresHbaConfig
from src.schemas.rabbitmq import RabbitMQConfig
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
    #: Which catalog services to deploy. ``None`` means "not stated",
    #: which is how every Study 1 run behaved and leaves service
    #: selection to the config generator's defaults. A list is binding:
    #: :func:`src.tools.config_generator.generate_config` nulls out every
    #: service not named, so the agent's decision is enforced in code
    #: rather than left to a second model's discretion.
    selected_services: list[str] | None = None

    @field_validator("selected_services")
    @classmethod
    def _check_catalog_members(cls, value: list[str] | None) -> list[str] | None:
        """Reject service names the catalog cannot deploy.

        A hallucinated name ("mysql", "kafka") fails validation here
        instead of silently selecting nothing, which would otherwise look
        like a deliberate omission in the selection metric.

        The import is deferred because ``src.services.catalog`` reaches
        the docker harness, which imports this module.
        """
        if value is None:
            return value
        from src.services.catalog import names

        catalog = names()
        unknown = [name for name in value if name not in catalog]
        if unknown:
            raise ValueError(
                f"unknown service(s) {unknown}; catalog is {sorted(catalog)}"
            )
        if not value:
            raise ValueError("selected_services must name at least one service")
        return list(dict.fromkeys(value))  # de-duplicate, keep order


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
    rabbitmq: RabbitMQConfig | None = None
    #: Coupled to postgres: required with it, forbidden without it.
    pg_hba: PostgresHbaConfig | None = None

    @model_validator(mode="after")
    def _check_selection(self) -> StackSpec:
        """Reject specs that deploy nothing, or postgres without pg_hba.

        Both were unrepresentable while the fields were required, and
        both would fail later and less legibly — an empty compose file,
        or a postgres container with no host-based authentication.
        """
        if (
            self.postgres is None
            and self.nginx is None
            and self.redis is None
            and self.rabbitmq is None
        ):
            raise ValueError(
                "a StackSpec must select at least one service "
                "(postgres, nginx, redis, rabbitmq)"
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
