"""The :class:`ServiceDefinition` record.

One frozen dataclass per deployable service, wrapping the modules that
already implement that service. Nothing here reimplements rendering,
checking or benchmarking: every field is a reference to existing code,
which is what keeps L1 a refactor rather than a rewrite.

Field-by-field, a definition answers the questions the harness used to
answer with an ``if service == "postgres"`` branch:

    what does its config look like     ``config_schema`` / ``config_of``
    what files does it deploy          ``render``
    is it up                           ``smoke_test`` / ``healthcheck``
    is it safe                         ``cis_checker``
    is it fast                         ``benchmark``
    how does it appear in compose      ``compose_fragment`` /
                                       ``compose_volumes`` / ``depends_on``
    what else does compose need        ``template_context`` /
                                       ``prepare_workdir``
    how big is it                      ``default_resource_share``
    where are its docs                 ``corpus_service_tag``
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from src.schemas.stack import StackSpec
    from src.schemas.validator_report import BenchmarkResult, SmokeTestResult
    from src.validator.docker_runner import StackRunner


@dataclass(frozen=True)
class ResourceShare:
    """This service's slice of the host, as fractions plus floors.

    Mirrors the arithmetic the compose renderer has always applied:
    ``max(host_ram_mb * memory_fraction, min_memory_mb)`` and
    ``round(max(vcpu * cpu_fraction, min_cpus), 2)``. Expressed per
    service so the shares are declared next to the service they size
    instead of in one table inside the runner.
    """

    memory_fraction: float
    min_memory_mb: int
    cpu_fraction: float
    min_cpus: float

    def memory_mb(self, host_ram_mb: float) -> int:
        """Memory limit in MB for a host with *host_ram_mb* of RAM."""
        return int(max(host_ram_mb * self.memory_fraction, self.min_memory_mb))

    def cpus(self, vcpu: int) -> float:
        """CPU limit for a host with *vcpu* virtual cores."""
        return round(max(vcpu * self.cpu_fraction, self.min_cpus), 2)


@dataclass(frozen=True)
class ServiceDefinition:
    """Everything the harness needs to know about one deployable service.

    Args:
        name: compose service name; also the container lookup label and
            the key used throughout the validator report.
        config_schema: the Pydantic model carrying this service's
            parameters.
        spec_field: attribute on :class:`~src.schemas.stack.StackSpec`
            holding that model, or ``None`` when the service was not
            selected.
        render: config files this service deploys, as
            ``{relative_path: content}``. Takes the whole spec rather
            than just the config because postgres renders two files, the
            second of which (``pg_hba.conf``) is a sibling field.
        cis_checker: class implementing ``run_all()`` against a running
            stack.
        benchmark: performance probe; returns a
            :class:`BenchmarkResult` and never raises.
        smoke_test: connection-acceptance probe.
        healthcheck: the compose healthcheck test command. A callable of
            the spec, because redis's ``redis-cli ping`` needs the
            requirepass value.
        compose_fragment: Jinja source for this service's block in
            docker-compose.yml.
        compose_volumes: Jinja source for its entries under the
            top-level ``volumes:`` key (``""`` when it needs none).
        template_context: extra Jinja variables this service's fragment
            references, e.g. postgres's generated password.
        prepare_workdir: side effects needed before ``compose up``
            beyond writing rendered files (postgres's TLS certs).
        corpus_service_tag: the RAG corpus ``service`` filter value.
        default_resource_share: sizing weights, see
            :class:`ResourceShare`.
        depends_on: names this service must start after, when they are
            also selected. Edges to unselected services are dropped, not
            treated as errors — a service that is not deployed cannot be
            waited on.
    """

    name: str
    config_schema: type[BaseModel]
    spec_field: str
    render: Callable[[StackSpec], dict[str, str]]
    cis_checker: type
    benchmark: Callable[[StackRunner, StackSpec, int], BenchmarkResult]
    smoke_test: Callable[[StackRunner, StackSpec], SmokeTestResult]
    healthcheck: Callable[[StackSpec], str]
    compose_fragment: str
    corpus_service_tag: str
    default_resource_share: ResourceShare
    compose_volumes: str = ""
    template_context: Callable[[StackSpec, StackRunner], dict[str, Any]] | None = None
    prepare_workdir: Callable[[StackSpec, Path], None] | None = None
    depends_on: tuple[str, ...] = ()

    def config_of(self, spec: StackSpec) -> BaseModel | None:
        """This service's config on *spec*, or ``None`` if not selected."""
        return getattr(spec, self.spec_field, None)

    def is_selected(self, spec: StackSpec) -> bool:
        """Whether *spec* deploys this service."""
        return self.config_of(spec) is not None
