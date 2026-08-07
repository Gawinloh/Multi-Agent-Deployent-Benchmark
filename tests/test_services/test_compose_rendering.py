"""Compose assembly from per-service fragments.

The three-service path must render exactly what the hand-written
template rendered before the catalog existed — Study 1's dataset is
frozen against that stack. Everything else here is about what happens
when a service is *not* selected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.schemas.stack import StackSpec
from src.services import catalog
from src.tools.config_generator import _STACKSPEC_EXAMPLE, generate_config
from src.validator.docker_runner import StackRunner
from tests.test_validator.test_docker_runner import make_spec

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "rendered-golden"

#: Pinned so the golden comparison is not defeated by a random password
#: or a run-specific id. See the fixture README.
FIXTURE_RUN_ID = "fixture0001"
FIXTURE_PASSWORD = "fixture-postgres-password"


@pytest.fixture
def pinned_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A StackRunner whose rendering is fully deterministic.

    Host-memory clamping is disabled: it queries the Docker daemon, so
    leaving it on would make these assertions depend on how much RAM the
    machine running the tests happens to have.
    """

    def build(**kwargs: object) -> StackRunner:
        monkeypatch.setattr(StackRunner, "_host_memory_mb", lambda self: None)
        runner = StackRunner(run_id=FIXTURE_RUN_ID, workdir=tmp_path, **kwargs)
        monkeypatch.setattr(runner, "_postgres_password", FIXTURE_PASSWORD)
        return runner

    return build


# ---------------------------------------------------------------------------
# The regression that matters: nothing moved for the three-service stack
# ---------------------------------------------------------------------------


class TestGoldenThreeServiceRender:
    @pytest.mark.parametrize(
        ("variant", "spec_kwargs"),
        [
            ("nossl", {}),
            ("ssl", {"ssl": True}),
            ("nopass", {"requirepass": None}),
        ],
    )
    def test_every_deployed_file_is_byte_identical(
        self, pinned_runner, variant: str, spec_kwargs: dict
    ) -> None:
        rendered = pinned_runner().render_files(make_spec(**spec_kwargs))

        expected_files = sorted(
            str(path.relative_to(GOLDEN / variant))
            for path in (GOLDEN / variant).rglob("*")
            if path.is_file()
        )
        assert sorted(rendered) == expected_files

        for relative, content in rendered.items():
            golden = (GOLDEN / variant / relative).read_text(encoding="utf-8")
            assert content == golden, f"{variant}/{relative} changed"

    def test_generate_config_output_is_byte_identical(self) -> None:
        files = generate_config(json.loads(_STACKSPEC_EXAMPLE), mode="deterministic")
        for attribute, filename in (
            ("postgresql_conf", "postgresql.conf"),
            ("pg_hba_conf", "pg_hba.conf"),
            ("nginx_conf", "nginx.conf"),
            ("redis_conf", "redis.conf"),
        ):
            golden = (GOLDEN / "generate_config" / filename).read_text(encoding="utf-8")
            assert getattr(files, attribute) == golden, f"{filename} changed"

    def test_generate_config_spec_round_trips_unchanged(self) -> None:
        files = generate_config(json.loads(_STACKSPEC_EXAMPLE), mode="deterministic")
        golden = json.loads((GOLDEN / "generate_config" / "spec.json").read_text())
        assert files.spec.model_dump(mode="json") == golden


# ---------------------------------------------------------------------------
# Subset deployment
# ---------------------------------------------------------------------------


class TestRedisNotSelected:
    @pytest.fixture
    def compose(self, pinned_runner) -> str:
        spec = make_spec().model_copy(update={"redis": None})
        return pinned_runner().render_files(spec)["docker-compose.yml"]

    def test_no_redis_service_block(self, compose: str) -> None:
        parsed = yaml.safe_load(compose)
        assert set(parsed["services"]) == {"postgres", "nginx"}

    def test_the_word_redis_appears_nowhere_in_the_body(self, compose: str) -> None:
        """Not just absent from the parsed services: an unselected service
        must leave no mount, no healthcheck and no depends_on behind.

        Comment lines are excluded. The template's header is a fixed
        preamble describing the harness — it names all three services
        whatever the spec selects, and is kept verbatim so the rendered
        file stays byte-identical to the pre-catalog output.
        """
        body = "\n".join(
            line for line in compose.splitlines() if not line.lstrip().startswith("#")
        )
        assert "redis" not in body

    def test_remaining_two_still_deploy(self, compose: str) -> None:
        parsed = yaml.safe_load(compose)
        assert parsed["services"]["postgres"]["image"] == "postgres:16"
        assert parsed["services"]["nginx"]["image"] == "nginx:1.27"
        for service in ("postgres", "nginx"):
            assert "healthcheck" in parsed["services"][service]
            assert parsed["services"][service]["deploy"]["resources"]["limits"]

    def test_nginx_keeps_only_the_surviving_dependency(self, compose: str) -> None:
        depends = yaml.safe_load(compose)["services"]["nginx"]["depends_on"]
        assert set(depends) == {"postgres"}
        assert depends["postgres"]["condition"] == "service_healthy"

    def test_no_redis_conf_is_rendered(self, pinned_runner) -> None:
        spec = make_spec().model_copy(update={"redis": None})
        rendered = pinned_runner().render_files(spec)
        assert "redis.conf" not in rendered
        assert "postgresql.conf" in rendered
        assert "nginx.conf" in rendered

    def test_generate_config_leaves_redis_conf_none(self) -> None:
        spec = make_spec().model_copy(update={"redis": None})
        files = generate_config(spec.model_dump(mode="json"), mode="deterministic")
        assert files.redis_conf is None
        assert files.postgresql_conf is not None
        assert files.nginx_conf is not None

    def test_runner_only_waits_on_deployed_services(self, pinned_runner) -> None:
        runner = pinned_runner()
        spec = make_spec().model_copy(update={"redis": None})
        runner.render_files(spec)
        assert runner.services == ("postgres", "nginx")


class TestPostgresNotSelected:
    @pytest.fixture
    def compose(self, pinned_runner) -> str:
        spec = make_spec().model_copy(update={"postgres": None, "pg_hba": None})
        return pinned_runner().render_files(spec)["docker-compose.yml"]

    def test_no_postgres_service_block(self, compose: str) -> None:
        assert set(yaml.safe_load(compose)["services"]) == {"redis", "nginx"}

    def test_the_volumes_section_disappears_with_its_only_user(
        self, compose: str
    ) -> None:
        """postgres owns the only named volume; an empty `volumes:` key is
        not valid compose, so the whole section has to go."""
        parsed = yaml.safe_load(compose)
        assert "volumes" not in parsed
        assert "pgdata" not in compose

    def test_the_network_survives(self, compose: str) -> None:
        parsed = yaml.safe_load(compose)
        assert parsed["networks"][f"net_{FIXTURE_RUN_ID}"]["driver"] == "bridge"
        for service in parsed["services"].values():
            assert service["networks"] == [f"net_{FIXTURE_RUN_ID}"]

    def test_nginx_still_waits_on_redis(self, compose: str) -> None:
        assert set(yaml.safe_load(compose)["services"]["nginx"]["depends_on"]) == {
            "redis"
        }


class TestSingleServiceStack:
    def test_nginx_alone_renders_valid_compose(self, pinned_runner) -> None:
        spec = make_spec().model_copy(
            update={"postgres": None, "pg_hba": None, "redis": None}
        )
        parsed = yaml.safe_load(pinned_runner().render_files(spec)["docker-compose.yml"])
        assert set(parsed["services"]) == {"nginx"}
        assert "depends_on" not in parsed["services"]["nginx"]
        assert "volumes" not in parsed

    def test_postgres_alone_keeps_its_volume(self, pinned_runner) -> None:
        spec = make_spec().model_copy(update={"nginx": None, "redis": None})
        compose = pinned_runner().render_files(spec)["docker-compose.yml"]
        parsed = yaml.safe_load(compose)
        assert set(parsed["services"]) == {"postgres"}
        assert f"pgdata_{FIXTURE_RUN_ID}" in parsed["volumes"]


class TestStackSpecSelection:
    def test_all_three_services_still_validate(self) -> None:
        spec = StackSpec.model_validate(json.loads(_STACKSPEC_EXAMPLE))
        assert spec.postgres is not None
        assert spec.nginx is not None
        assert spec.redis is not None

    def test_a_spec_selecting_nothing_is_rejected(self) -> None:
        payload = json.loads(_STACKSPEC_EXAMPLE)
        for field in ("postgres", "nginx", "redis", "pg_hba"):
            payload.pop(field)
        with pytest.raises(ValueError, match="at least one service"):
            StackSpec.model_validate(payload)

    def test_postgres_without_pg_hba_is_rejected(self) -> None:
        payload = json.loads(_STACKSPEC_EXAMPLE)
        payload.pop("pg_hba")
        with pytest.raises(ValueError, match="pg_hba is required"):
            StackSpec.model_validate(payload)

    def test_pg_hba_without_postgres_is_rejected(self) -> None:
        payload = json.loads(_STACKSPEC_EXAMPLE)
        payload.pop("postgres")
        with pytest.raises(ValueError, match="pg_hba is meaningless"):
            StackSpec.model_validate(payload)

    def test_omitted_services_default_to_none(self) -> None:
        payload = json.loads(_STACKSPEC_EXAMPLE)
        payload.pop("redis")
        spec = StackSpec.model_validate(payload)
        assert spec.redis is None
        assert [d.name for d in catalog.for_spec(spec)] == ["postgres", "nginx"]
