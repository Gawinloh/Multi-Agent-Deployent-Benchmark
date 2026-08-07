"""Integrity of the Study 2 scenario set.

These check the *written* YAML, not the generator's in-memory objects, so
a hand-edited file is caught. The generator runs the same assertions
before writing (:func:`validate_documents`); duplicating them here means
a stale committed file fails the suite rather than only failing a
regeneration nobody runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from benchmark.derive_scenarios import MAX_DEPLOYABLE_RAM_GB
from benchmark.derive_scenarios_study2 import (
    CATALOG_ORDER,
    SCENARIOS,
    build_document,
    validate_documents,
)
from src.experiment.result_logger import GROUND_TRUTH_PARAMETER_MAP, _lookup
from src.schemas.stack import StackRequirements
from src.services.catalog import names

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "benchmark" / "scenarios_study2"


@pytest.fixture(scope="module")
def documents() -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(SCENARIO_DIR.glob("*.yaml"))
    ]


class TestSetShape:
    def test_eight_scenarios(self, documents: list[dict[str, Any]]) -> None:
        assert len(documents) == 8

    def test_ids_are_unique_and_match_filenames(self) -> None:
        stems = sorted(p.stem for p in SCENARIO_DIR.glob("*.yaml"))
        ids = sorted(s2.base.id for s2 in SCENARIOS)
        assert stems == ids

    def test_study1_scenarios_are_untouched(self) -> None:
        """Study 2 lives in its own directory; the frozen twelve must not
        have gained an expected_services block."""
        study1 = Path(__file__).resolve().parents[2] / "benchmark" / "scenarios"
        for path in study1.glob("*.yaml"):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert "expected_services" not in doc.get("ground_truth", {}), path.name

    def test_coverage_spread_is_as_designed(self) -> None:
        coverage = [s2.coverage for s2 in SCENARIOS]
        assert sum(c.startswith("distractor") for c in coverage) == 2
        assert sum(c.startswith("inferred") for c in coverage) == 2
        assert sum(c == "minimal" for c in coverage) == 1
        assert sum(c.startswith("straightforward") for c in coverage) == 3

    def test_every_service_is_both_selected_and_declined_somewhere(self) -> None:
        """A service that appears in every scenario, or none, teaches the
        metric nothing."""
        for name in CATALOG_ORDER:
            selected = [name in s2.expected_services for s2 in SCENARIOS]
            assert any(selected), f"{name} is never expected"
            assert not all(selected), f"{name} is expected in every scenario"

    def test_workload_classes_span_the_enum(self) -> None:
        classes = {s2.base.workload_class for s2 in SCENARIOS}
        assert classes == {"OLTP", "OLAP", "CACHING_HEAVY", "BALANCED"}

    def test_selection_sets_are_not_all_the_same(self) -> None:
        distinct = {tuple(s2.expected_services) for s2 in SCENARIOS}
        assert len(distinct) >= 5


class TestGroundTruthIntegrity:
    def test_every_key_maps_to_a_spec_path(
        self, documents: list[dict[str, Any]]
    ) -> None:
        """An unmapped key is silently dropped from the denominator."""
        for doc in documents:
            for block, expectations in doc["ground_truth"].items():
                if not block.startswith("expected_") or not isinstance(
                    expectations, dict
                ):
                    continue
                assert block in GROUND_TRUTH_PARAMETER_MAP, f"{doc['id']}: {block}"
                for key in expectations:
                    param, _ = _lookup(block, key)
                    assert param is not None, f"{doc['id']}: {block}.{key} unmapped"

    def test_expected_services_names_only_catalog_members(
        self, documents: list[dict[str, Any]]
    ) -> None:
        catalog = set(names())
        for doc in documents:
            services = doc["ground_truth"]["expected_services"]
            assert services, doc["id"]
            assert set(services) <= catalog, doc["id"]

    def test_expected_services_is_in_catalog_order(
        self, documents: list[dict[str, Any]]
    ) -> None:
        for doc in documents:
            services = doc["ground_truth"]["expected_services"]
            assert services == [n for n in CATALOG_ORDER if n in services], doc["id"]

    def test_no_parameter_block_for_an_unselected_service(
        self, documents: list[dict[str, Any]]
    ) -> None:
        """The mechanism behind the Study 2 rule: no ground truth exists for
        a service the scenario does not want."""
        for doc in documents:
            expected = set(doc["ground_truth"]["expected_services"])
            blocks = {
                block.removeprefix("expected_")
                for block, value in doc["ground_truth"].items()
                if block.startswith("expected_") and isinstance(value, dict)
            }
            assert blocks == expected, doc["id"]

    def test_hardware_within_the_deployable_envelope(
        self, documents: list[dict[str, Any]]
    ) -> None:
        for doc in documents:
            facts = doc["derivation"]["stated_facts"]
            assert 0 < facts["ram_gb"] <= MAX_DEPLOYABLE_RAM_GB, doc["id"]
            assert facts["vcpu"] > 0, doc["id"]

    def test_requirements_are_constructible(
        self, documents: list[dict[str, Any]]
    ) -> None:
        """Stated facts must round-trip into a valid StackRequirements, or
        the agent cannot express the scenario at all."""
        for doc in documents:
            facts = doc["derivation"]["stated_facts"]
            requirements = StackRequirements(
                workload_class=doc["ground_truth"]["workload_class"],
                expected_concurrent_users=facts["concurrent_users"],
                expected_data_size_gb=facts["data_size_gb"],
                hardware={
                    "ram_gb": facts["ram_gb"],
                    "vcpu": facts["vcpu"],
                    "disk_gb": 100,
                },
                compliance=doc["ground_truth"]["compliance"],
                backup_required=facts["backup_required"],
                selected_services=doc["ground_truth"]["expected_services"],
            )
            assert requirements.selected_services == (
                doc["ground_truth"]["expected_services"]
            )


class TestDerivationDiscipline:
    def test_expected_services_is_derived_not_hand_written(
        self, documents: list[dict[str, Any]]
    ) -> None:
        """The written list must equal what the selection facts imply."""
        by_id = {s2.base.id: s2 for s2 in SCENARIOS}
        for doc in documents:
            assert (
                doc["ground_truth"]["expected_services"]
                == by_id[doc["id"]].expected_services
            ), doc["id"]

    def test_every_service_carries_evidence(
        self, documents: list[dict[str, Any]]
    ) -> None:
        for doc in documents:
            evidence = doc["derivation"]["selection_evidence"]
            assert set(evidence) == set(CATALOG_ORDER), doc["id"]
            for name, text in evidence.items():
                assert len(text) >= 40, f"{doc['id']}: {name} evidence too thin"

    def test_evidence_verb_matches_the_decision(
        self, documents: list[dict[str, Any]]
    ) -> None:
        for doc in documents:
            expected = set(doc["ground_truth"]["expected_services"])
            for name, text in doc["derivation"]["selection_evidence"].items():
                verb = "INCLUDE" if name in expected else "EXCLUDE"
                assert text.startswith(verb), f"{doc['id']}: {name}"

    def test_requests_never_name_a_config_parameter(
        self, documents: list[dict[str, Any]]
    ) -> None:
        """The request is a user's words, not a spec. Naming a parameter
        would turn the task into transcription."""
        forbidden = (
            "shared_buffers", "effective_cache_size", "work_mem", "maxmemory",
            "max_connections", "maxmemory_policy", "wal_level", "server_tokens",
            "requirepass", "vm_memory_high_watermark", "disk_free_limit",
            "heartbeat", "default_user", "listen_addresses", "appendonly",
            "ssl_protocols", "protected_mode", "selected_services",
        )
        for doc in documents:
            lowered = doc["request"].lower()
            for token in forbidden:
                assert token not in lowered, f"{doc['id']} names {token}"

    def test_requests_never_name_the_service_images(
        self, documents: list[dict[str, Any]]
    ) -> None:
        """A request that says "postgres" hands the agent the answer. The
        distractors deliberately name a *category* (caching, message
        broker), never a product."""
        for doc in documents:
            lowered = doc["request"].lower()
            for product in ("postgres", "postgresql", "nginx", "redis", "rabbitmq"):
                assert product not in lowered, f"{doc['id']} names {product}"

    def test_distractor_requests_mention_the_category_they_do_not_need(
        self, documents: list[dict[str, Any]]
    ) -> None:
        by_id = {doc["id"]: doc for doc in documents}
        cache = by_id["s2_002_distractor_cache"]
        assert "caching" in cache["request"].lower()
        assert "redis" not in cache["ground_truth"]["expected_services"]

        queue = by_id["s2_003_distractor_queue"]
        assert "message broker" in queue["request"].lower()
        assert "rabbitmq" not in queue["ground_truth"]["expected_services"]

    def test_inferred_requests_never_name_the_category(
        self, documents: list[dict[str, Any]]
    ) -> None:
        """The point of the inference cases: the need is described, never
        labelled."""
        by_id = {doc["id"]: doc for doc in documents}
        queue = by_id["s2_004_inferred_queue"]
        assert "rabbitmq" in queue["ground_truth"]["expected_services"]
        for word in ("queue", "broker", "message bus"):
            assert word not in queue["request"].lower()

        cache = by_id["s2_005_inferred_cache"]
        assert "redis" in cache["ground_truth"]["expected_services"]
        for word in ("cache", "caching", "memoi"):
            assert word not in cache["request"].lower()


class TestGeneratorGuards:
    """The generator must refuse to emit an unscoreable scenario."""

    def _document(self, **overrides: Any) -> dict[str, Any]:
        doc = build_document(SCENARIOS[1])  # postgres + nginx
        for key, value in overrides.items():
            doc["ground_truth"][key] = value
        return doc

    def test_rejects_a_non_catalog_service(self) -> None:
        doc = self._document(expected_services=["postgres", "mysql"])
        with pytest.raises(ValueError, match="non-catalog"):
            validate_documents([doc])

    def test_rejects_an_empty_selection(self) -> None:
        with pytest.raises(ValueError, match="expected_services is empty"):
            validate_documents([self._document(expected_services=[])])

    def test_rejects_a_block_for_an_unselected_service(self) -> None:
        doc = build_document(SCENARIOS[1])
        doc["ground_truth"]["expected_redis"] = {"requirepass_or_acl": True}
        with pytest.raises(ValueError, match="does not expect redis"):
            validate_documents([doc])

    def test_rejects_an_unmapped_ground_truth_key(self) -> None:
        doc = build_document(SCENARIOS[1])
        doc["ground_truth"]["expected_postgres"]["invented_knob"] = 1
        with pytest.raises(ValueError, match="does not map"):
            validate_documents([doc])

    def test_rejects_evidence_contradicting_the_selection(self) -> None:
        doc = build_document(SCENARIOS[1])
        doc["derivation"]["selection_evidence"]["redis"] = (
            "INCLUDE: this contradicts expected_services and must be caught."
        )
        with pytest.raises(ValueError, match="must start with EXCLUDE"):
            validate_documents([doc])

    def test_rejects_a_host_beyond_the_deployable_envelope(self) -> None:
        doc = build_document(SCENARIOS[1])
        doc["derivation"]["stated_facts"]["ram_gb"] = MAX_DEPLOYABLE_RAM_GB + 1
        with pytest.raises(ValueError, match="exceeds MAX_DEPLOYABLE_RAM_GB"):
            validate_documents([doc])

    def test_the_committed_files_pass_their_own_validation(
        self, documents: list[dict[str, Any]]
    ) -> None:
        validate_documents(documents)

    def test_regeneration_is_a_no_op(self, documents: list[dict[str, Any]]) -> None:
        """The committed YAML is what the generator produces today."""
        regenerated = [build_document(s2) for s2 in SCENARIOS]
        assert regenerated == documents
