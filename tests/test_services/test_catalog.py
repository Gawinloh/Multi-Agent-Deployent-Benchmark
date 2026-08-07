"""Tests for src.services.catalog: the registry itself.

Compose assembly is covered separately in test_compose_rendering.py.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

from src.schemas.nginx import NginxConfig
from src.schemas.postgres import PostgresConfig
from src.schemas.redis import RedisConfig
from src.schemas.stack import StackSpec
from src.services import catalog
from src.services.definition import ResourceShare, ServiceDefinition
from src.validator.docker_runner import SERVICES
from tests.test_validator.test_docker_runner import make_spec

ALL_NAMES = ("postgres", "nginx", "redis")


class TestCatalogRoundTrip:
    """Every definition's pieces resolve to something usable."""

    def test_catalog_holds_the_expected_palette(self) -> None:
        assert tuple(catalog.CATALOG) == ALL_NAMES
        assert catalog.names() == ALL_NAMES

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_name_is_the_registry_key(self, name: str) -> None:
        assert catalog.get(name).name == name

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_config_schema_is_a_pydantic_model(self, name: str) -> None:
        definition = catalog.get(name)
        assert issubclass(definition.config_schema, BaseModel)

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_spec_field_exists_and_holds_the_config_schema(self, name: str) -> None:
        definition = catalog.get(name)
        assert definition.spec_field in StackSpec.model_fields
        config = definition.config_of(make_spec())
        assert isinstance(config, definition.config_schema)

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_render_returns_non_empty_files(self, name: str) -> None:
        rendered = catalog.get(name).render(make_spec())
        assert rendered
        for filename, content in rendered.items():
            assert filename.endswith(".conf")
            assert content.strip()

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_cis_checker_takes_a_runner_and_runs_all(self, name: str) -> None:
        checker = catalog.get(name).cis_checker
        assert callable(checker)
        assert hasattr(checker, "run_all")
        assert list(inspect.signature(checker.__init__).parameters)[:2] == [
            "self",
            "runner",
        ]

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_benchmark_and_smoke_test_are_callable(self, name: str) -> None:
        definition = catalog.get(name)
        assert callable(definition.benchmark)
        assert callable(definition.smoke_test)

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_healthcheck_is_a_non_empty_command(self, name: str) -> None:
        assert catalog.get(name).healthcheck(make_spec()).strip()

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_compose_fragment_declares_its_own_service(self, name: str) -> None:
        fragment = catalog.get(name).compose_fragment
        assert fragment.startswith(f"  {name}:\n")

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_corpus_tag_matches_the_rag_alias_table(self, name: str) -> None:
        from src.rag.query import _SERVICE_ALIASES

        tag = catalog.get(name).corpus_service_tag
        assert _SERVICE_ALIASES[tag] == tag

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_resource_share_is_a_positive_fraction(self, name: str) -> None:
        share = catalog.get(name).default_resource_share
        assert 0 < share.memory_fraction <= 1
        assert 0 < share.cpu_fraction <= 1
        assert share.min_memory_mb > 0
        assert share.min_cpus > 0

    def test_depends_on_only_names_catalog_services(self) -> None:
        for definition in catalog.all_services():
            for dependency in definition.depends_on:
                assert dependency in catalog.CATALOG

    def test_get_rejects_an_unknown_service_by_name(self) -> None:
        with pytest.raises(KeyError, match="unknown service 'rabbitmq'"):
            catalog.get("rabbitmq")

    def test_docker_runner_default_palette_matches_the_catalog(self) -> None:
        """SERVICES is a literal (the catalog imports docker_runner), so it
        has to be checked against the catalog rather than derived from it."""
        expected = catalog.compose_order(catalog.all_services())
        assert SERVICES == tuple(definition.name for definition in expected)


class TestResourceShare:
    def test_reproduces_the_pre_catalog_arithmetic(self) -> None:
        # 2GB / 2 vCPU host, the sizes the harness template used to hardcode.
        ram_mb = 2 * 1024
        assert catalog.get("postgres").default_resource_share.memory_mb(ram_mb) == 1024
        assert catalog.get("redis").default_resource_share.memory_mb(ram_mb) == 512
        assert catalog.get("nginx").default_resource_share.memory_mb(ram_mb) == 256
        assert catalog.get("postgres").default_resource_share.cpus(2) == 1.0
        assert catalog.get("redis").default_resource_share.cpus(2) == 0.5
        assert catalog.get("nginx").default_resource_share.cpus(2) == 0.5

    def test_floors_apply_on_a_tiny_host(self) -> None:
        share = ResourceShare(
            memory_fraction=0.5, min_memory_mb=512, cpu_fraction=0.5, min_cpus=0.5
        )
        assert share.memory_mb(100) == 512
        assert share.cpus(0) == 0.5


class TestForSpec:
    def test_returns_all_three_for_a_full_spec(self) -> None:
        selected = catalog.for_spec(make_spec())
        assert [d.name for d in selected] == list(ALL_NAMES)

    @pytest.mark.parametrize("absent", ["nginx", "redis"])
    def test_omits_an_unselected_service(self, absent: str) -> None:
        spec = make_spec().model_copy(update={absent: None})
        selected = catalog.for_spec(spec)
        assert absent not in [d.name for d in selected]
        assert len(selected) == 2

    def test_omits_postgres_and_its_pg_hba_together(self) -> None:
        spec = make_spec().model_copy(update={"postgres": None, "pg_hba": None})
        assert [d.name for d in catalog.for_spec(spec)] == ["nginx", "redis"]

    def test_registry_order_is_stable_regardless_of_selection(self) -> None:
        spec = make_spec().model_copy(update={"nginx": None})
        assert [d.name for d in catalog.for_spec(spec)] == ["postgres", "redis"]

    def test_is_selected_tracks_the_spec_field(self) -> None:
        spec = make_spec().model_copy(update={"redis": None})
        assert catalog.get("postgres").is_selected(spec)
        assert not catalog.get("redis").is_selected(spec)


class TestComposeOrder:
    def test_dependencies_precede_dependents(self) -> None:
        ordered = catalog.compose_order(catalog.all_services())
        names = [d.name for d in ordered]
        assert names == ["postgres", "redis", "nginx"]
        assert names.index("nginx") > names.index("postgres")
        assert names.index("nginx") > names.index("redis")

    def test_drops_edges_to_unselected_services(self) -> None:
        """nginx depends on postgres and redis, but deploying it alone is a
        legitimate selection — the edges go away rather than raising."""
        spec = make_spec().model_copy(
            update={"postgres": None, "pg_hba": None, "redis": None}
        )
        selected = catalog.for_spec(spec)
        assert [d.name for d in catalog.compose_order(selected)] == ["nginx"]
        assert catalog.depends_on_for(catalog.get("nginx"), selected) == []

    def test_keeps_only_the_edges_that_survive(self) -> None:
        spec = make_spec().model_copy(update={"redis": None})
        selected = catalog.for_spec(spec)
        assert catalog.depends_on_for(catalog.get("nginx"), selected) == ["postgres"]

    def test_empty_selection_orders_to_nothing(self) -> None:
        assert catalog.compose_order([]) == []

    def test_raises_on_a_dependency_cycle(self) -> None:
        left = _stub_definition("left", depends_on=("right",))
        right = _stub_definition("right", depends_on=("left",))
        with pytest.raises(ValueError, match="dependency cycle"):
            catalog.compose_order([left, right])


def _stub_definition(name: str, **overrides: object) -> ServiceDefinition:
    """A minimal definition for ordering tests; never rendered or deployed."""
    defaults: dict[str, object] = {
        "name": name,
        "config_schema": PostgresConfig,
        "spec_field": name,
        "render": lambda spec: {},
        "cis_checker": object,
        "benchmark": lambda runner, spec, duration_s: None,
        "smoke_test": lambda runner, spec: None,
        "healthcheck": lambda spec: "true",
        "compose_fragment": f"  {name}:\n",
        "corpus_service_tag": name,
        "default_resource_share": ResourceShare(0.1, 64, 0.1, 0.1),
    }
    return ServiceDefinition(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestConfigSchemaWiring:
    """The schema on each definition is the one StackSpec actually holds."""

    def test_schemas_match_the_stack_spec_fields(self) -> None:
        assert catalog.get("postgres").config_schema is PostgresConfig
        assert catalog.get("nginx").config_schema is NginxConfig
        assert catalog.get("redis").config_schema is RedisConfig
