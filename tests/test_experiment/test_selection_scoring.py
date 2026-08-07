"""Tests for Study 2 service-selection scoring.

The rule these pin down is the one thing that makes Study 2 interpretable:
selection and correctness measure different capabilities, and one wrong
decision must be charged to exactly one of them. A regression here would
not crash anything — it would quietly produce plausible numbers that
answer the wrong question.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from src.experiment.result_logger import (
    RunResult,
    compute_scores,
    score_configuration_correctness,
    score_service_selection,
)

CATALOG = ("postgres", "nginx", "redis", "rabbitmq")


def spec_with(*services: str) -> dict[str, Any]:
    """A final_spec shaped like a real one, selecting exactly *services*."""
    spec: dict[str, Any] = {
        "requirements": {
            "workload_class": "BALANCED",
            "expected_concurrent_users": 10,
            "expected_data_size_gb": 5.0,
            "hardware": {"ram_gb": 8.0, "vcpu": 4, "disk_gb": 100.0},
            "compliance": "NONE",
            "backup_required": False,
            "selected_services": list(services),
        }
    }
    blocks: dict[str, Any] = {
        "postgres": {
            "memory": {"shared_buffers": "2GB"},
            "security": {"ssl": True, "password_encryption": "scram-sha-256"},
        },
        "nginx": {"security": {"server_tokens": False}},
        "redis": {
            "memory": {"maxmemory": "512MB", "maxmemory_policy": "allkeys-lru"},
            "security": {"requirepass": "a-long-enough-password"},
        },
        "rabbitmq": {
            "security": {"default_user": "appuser", "default_pass": "a-long-pass"},
            "networking": {"heartbeat": 60, "max_connections": 500},
        },
    }
    for name in CATALOG:
        spec[name] = blocks[name] if name in services else None
    spec["pg_hba"] = {"rules": []} if "postgres" in services else None
    return spec


STUDY2_GT: dict[str, Any] = {
    "workload_class": "BALANCED",
    "compliance": "NONE",
    "expected_services": ["postgres", "nginx", "redis"],
    "expected_postgres": {"ssl": True, "password_encryption": "scram-sha-256"},
    "expected_nginx": {"server_tokens": False},
    "expected_redis": {
        "maxmemory_policy_acceptable": ["allkeys-lru", "allkeys-lfu"],
        "requirepass_or_acl": True,
    },
}

STUDY1_GT: dict[str, Any] = {
    "workload_class": "BALANCED",
    "compliance": "NONE",
    "expected_postgres": {"ssl": True},
    "expected_redis": {"requirepass_or_acl": True},
}


def detail_for(scored: dict[str, Any], service: str) -> dict[str, Any]:
    matches = [d for d in scored["selection_detail"] if d["service"] == service]
    assert matches, service
    return matches[0]


class TestExactMatch:
    def test_perfect_selection(self) -> None:
        scored = score_service_selection(
            spec_with("postgres", "nginx", "redis"), STUDY2_GT
        )
        assert scored is not None
        assert scored["selection_exact_match"] is True
        assert scored["selection_precision"] == 1.0
        assert scored["selection_recall"] == 1.0
        assert scored["selection_f1"] == 1.0

    def test_order_does_not_matter(self) -> None:
        scored = score_service_selection(
            spec_with("redis", "postgres", "nginx"), STUDY2_GT
        )
        assert scored["selection_exact_match"] is True

    def test_one_extra_breaks_exact_match(self) -> None:
        scored = score_service_selection(
            spec_with("postgres", "nginx", "redis", "rabbitmq"), STUDY2_GT
        )
        assert scored["selection_exact_match"] is False
        assert scored["selection_recall"] == 1.0
        assert scored["selection_precision"] == pytest.approx(3 / 4)


class TestVerdictClasses:
    @pytest.fixture
    def scored(self) -> dict[str, Any]:
        # expects postgres+nginx+redis; deploys postgres+nginx+rabbitmq
        return score_service_selection(
            spec_with("postgres", "nginx", "rabbitmq"), STUDY2_GT
        )

    def test_true_positive(self, scored: dict[str, Any]) -> None:
        entry = detail_for(scored, "postgres")
        assert entry["verdict"] == "true_positive"
        assert entry["expected"] is True and entry["selected"] is True

    def test_false_negative(self, scored: dict[str, Any]) -> None:
        entry = detail_for(scored, "redis")
        assert entry["verdict"] == "false_negative"
        assert entry["expected"] is True and entry["selected"] is False

    def test_false_positive(self, scored: dict[str, Any]) -> None:
        entry = detail_for(scored, "rabbitmq")
        assert entry["verdict"] == "false_positive"
        assert entry["expected"] is False and entry["selected"] is True

    def test_true_negative(self) -> None:
        scored = score_service_selection(
            spec_with("postgres", "nginx", "redis"), STUDY2_GT
        )
        entry = detail_for(scored, "rabbitmq")
        assert entry["verdict"] == "true_negative"
        assert entry["expected"] is False and entry["selected"] is False

    def test_every_catalog_service_is_reported(self, scored: dict[str, Any]) -> None:
        assert [d["service"] for d in scored["selection_detail"]] == list(CATALOG)


class TestMetricArithmetic:
    def test_precision_recall_f1(self) -> None:
        # expects 3, deploys 3, 2 correct -> p = r = 2/3
        scored = score_service_selection(
            spec_with("postgres", "nginx", "rabbitmq"), STUDY2_GT
        )
        assert scored["selection_precision"] == pytest.approx(2 / 3)
        assert scored["selection_recall"] == pytest.approx(2 / 3)
        assert scored["selection_f1"] == pytest.approx(2 / 3)

    def test_over_selection_costs_precision_only(self) -> None:
        scored = score_service_selection(
            spec_with("postgres", "nginx", "redis", "rabbitmq"), STUDY2_GT
        )
        assert scored["selection_recall"] == 1.0
        assert scored["selection_precision"] < 1.0

    def test_under_selection_costs_recall_only(self) -> None:
        scored = score_service_selection(spec_with("postgres", "nginx"), STUDY2_GT)
        assert scored["selection_precision"] == 1.0
        assert scored["selection_recall"] == pytest.approx(2 / 3)

    def test_no_overlap_gives_zero_f1(self) -> None:
        gt = {**STUDY2_GT, "expected_services": ["rabbitmq"]}
        scored = score_service_selection(spec_with("nginx"), gt)
        assert scored["selection_precision"] == 0.0
        assert scored["selection_recall"] == 0.0
        assert scored["selection_f1"] == 0.0

    def test_true_negatives_are_excluded_from_the_metrics(self) -> None:
        """Standard positive-class definitions: correctly declining three of
        four services must not inflate precision or recall."""
        gt = {**STUDY2_GT, "expected_services": ["nginx"]}
        scored = score_service_selection(spec_with("nginx"), gt)
        assert scored["selection_precision"] == 1.0
        assert scored["selection_recall"] == 1.0
        assert (
            sum(d["verdict"] == "true_negative" for d in scored["selection_detail"])
            == 3
        )


class TestSelectionReadFromTheSpec:
    def test_a_claimed_service_left_null_does_not_count_as_selected(self) -> None:
        """requirements.selected_services is the agent's claim; the config
        blocks are what actually deploys. The metric follows deployment."""
        spec = spec_with("postgres", "nginx")
        spec["requirements"]["selected_services"] = ["postgres", "nginx", "redis"]
        scored = score_service_selection(spec, STUDY2_GT)
        assert detail_for(scored, "redis")["verdict"] == "false_negative"

    def test_pg_hba_is_not_treated_as_a_service(self) -> None:
        scored = score_service_selection(
            spec_with("postgres", "nginx", "redis"), STUDY2_GT
        )
        assert "pg_hba" not in [d["service"] for d in scored["selection_detail"]]


class TestNotEmitted:
    def test_study1_scenario_emits_no_selection_keys(self) -> None:
        """The twelve frozen scenarios have no expected_services block."""
        assert score_service_selection(spec_with(*CATALOG), STUDY1_GT) is None

    def test_no_ground_truth_at_all(self) -> None:
        assert score_service_selection(spec_with("nginx"), None) is None

    def test_unfinalised_run_emits_nothing(self) -> None:
        assert score_service_selection(None, STUDY2_GT) is None

    def test_compute_scores_omits_the_keys_for_study1(self) -> None:
        result = RunResult(
            scenario_id="s", scenario_text="t", architecture="single", model="m"
        )
        result.termination_reason = "finalised"
        result.final_spec = spec_with(*CATALOG)
        scores = compute_scores(result, STUDY1_GT)
        assert not [k for k in scores if k.startswith("selection_")]
        assert "correctness" in scores

    def test_compute_scores_emits_the_keys_for_study2(self) -> None:
        result = RunResult(
            scenario_id="s", scenario_text="t", architecture="single", model="m"
        )
        result.termination_reason = "finalised"
        result.final_spec = spec_with("postgres", "nginx", "redis")
        scores = compute_scores(result, STUDY2_GT)
        assert scores["selection_exact_match"] is True
        assert scores["selection_f1"] == 1.0
        assert "correctness" in scores


class TestScenarioIntegrity:
    def test_expected_services_outside_the_catalog_raises(self) -> None:
        """A scenario asserting a service that cannot be deployed would make
        recall unreachable for every run; fail loudly instead."""
        gt = {**STUDY2_GT, "expected_services": ["postgres", "mysql"]}
        with pytest.raises(ValueError, match="non-catalog service"):
            score_service_selection(spec_with("postgres"), gt)


# ---------------------------------------------------------------------------
# The interaction with correctness — the crux of the Study 2 rule
# ---------------------------------------------------------------------------


class TestCorrectnessExcludesUnselectedServices:
    def test_correct_omission_shrinks_the_denominator(self) -> None:
        """A scenario needing no queue asserts nothing about one, so the
        denominator is the same whether or not the agent deploys it."""
        full = score_configuration_correctness(
            spec_with("postgres", "nginx", "redis"), STUDY2_GT
        )
        # 2 postgres + 1 nginx + 2 redis assertions
        assert full["parameters_checked"] == 5
        assert full["correctness"] == 1.0

    def test_missed_service_is_excluded_not_failed(self) -> None:
        """The crux. Omitting redis is a selection false negative; it must
        NOT also appear as failed redis parameters."""
        scored = score_configuration_correctness(
            spec_with("postgres", "nginx"), STUDY2_GT
        )
        redis_details = [
            d for d in scored["parameter_details"] if d["parameter"].startswith("redis")
        ]
        assert [d["status"] for d in redis_details] == ["not_selected"]
        assert all(d["passed"] is None for d in redis_details)
        # denominator shrank by exactly redis's two assertions
        assert scored["parameters_checked"] == 3
        # and the run is not penalised twice: what it did deploy was correct
        assert scored["correctness"] == 1.0

    def test_the_same_omission_is_charged_to_selection(self) -> None:
        spec = spec_with("postgres", "nginx")
        selection = score_service_selection(spec, STUDY2_GT)
        correctness = score_configuration_correctness(spec, STUDY2_GT)
        assert selection["selection_recall"] < 1.0  # charged here
        assert correctness["correctness"] == 1.0  # and not here

    def test_extra_service_is_not_scored_for_parameters(self) -> None:
        """No expected_rabbitmq block exists, so a deployed queue has no
        ground truth; it costs precision and nothing else."""
        spec = spec_with("postgres", "nginx", "redis", "rabbitmq")
        correctness = score_configuration_correctness(spec, STUDY2_GT)
        assert not [
            d
            for d in correctness["parameter_details"]
            if d["parameter"].startswith("rabbitmq")
        ]
        assert correctness["correctness"] == 1.0
        assert score_service_selection(spec, STUDY2_GT)["selection_precision"] < 1.0

    def test_a_selected_service_is_still_scored_normally(self) -> None:
        """Exclusion applies to unselected services only — a deployed one
        that is misconfigured still fails."""
        spec = spec_with("postgres", "nginx", "redis")
        spec["redis"]["security"]["requirepass"] = None
        scored = score_configuration_correctness(spec, STUDY2_GT)
        entry = [
            d
            for d in scored["parameter_details"]
            if d["parameter"] == "redis.requirepass_or_acl"
        ][0]
        assert entry["passed"] is False
        assert scored["correctness"] < 1.0

    def test_study1_still_counts_an_absent_service_as_failures(self) -> None:
        """Study 1 mode is unchanged: no expected_services means no selection
        task, so a missing service is simply a broken spec."""
        spec = spec_with("postgres", "nginx")
        scored = score_configuration_correctness(spec, STUDY1_GT)
        entry = [
            d
            for d in scored["parameter_details"]
            if d["parameter"] == "redis.requirepass_or_acl"
        ][0]
        assert entry["passed"] is False
        assert entry["status"] == "missing"
        assert scored["parameters_checked"] == 2
        assert scored["correctness"] == pytest.approx(0.5)

    def test_exclusion_is_visible_not_silent(self) -> None:
        scored = score_configuration_correctness(
            spec_with("postgres", "nginx"), STUDY2_GT
        )
        entry = [
            d for d in scored["parameter_details"] if d["status"] == "not_selected"
        ][0]
        assert entry["parameter"] == "redis.*"
        assert sorted(entry["expected"]) == [
            "maxmemory_policy_acceptable",
            "requirepass_or_acl",
        ]

    def test_parameters_checked_varies_with_the_outcome(self) -> None:
        """Documented consequence: correctness is not comparable across runs
        of the same Study 2 scenario without its denominator."""
        denominators = {
            services: score_configuration_correctness(
                spec_with(*services), STUDY2_GT
            )["parameters_checked"]
            for services in (
                ("postgres", "nginx", "redis"),
                ("postgres", "nginx"),
                ("nginx",),
            )
        }
        assert len(set(denominators.values())) == 3


@pytest.fixture(scope="module")
def scenarios() -> list[dict[str, Any]]:
    """Every written Study 2 scenario document."""
    import yaml

    directory = Path(__file__).resolve().parents[2] / "benchmark" / "scenarios_study2"
    documents = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.yaml"))
    ]
    assert len(documents) == 8, "expected 8 Study 2 scenarios"
    return documents


class TestAgainstTheRealScenarios:
    """The written YAML, not a hand-built fixture."""

    def test_a_perfect_run_scores_one_on_both_metrics(
        self, scenarios: list[dict[str, Any]]
    ) -> None:
        for doc in scenarios:
            expected = doc["ground_truth"]["expected_services"]
            scored = score_service_selection(spec_with(*expected), doc["ground_truth"])
            assert scored["selection_exact_match"] is True, doc["id"]
            assert scored["selection_f1"] == 1.0, doc["id"]

    def test_deploying_everything_is_penalised_where_it_should_be(
        self, scenarios: list[dict[str, Any]]
    ) -> None:
        """The Study 1 habit — always deploy all four — must score poorly on
        the scenarios that do not want all four, and perfectly on the one
        that does."""
        for doc in scenarios:
            expected = set(doc["ground_truth"]["expected_services"])
            scored = score_service_selection(spec_with(*CATALOG), doc["ground_truth"])
            assert scored["selection_recall"] == 1.0, doc["id"]
            assert scored["selection_exact_match"] is (expected == set(CATALOG)), doc["id"]

    def test_correctness_never_penalises_an_unexpected_service(
        self, scenarios: list[dict[str, Any]]
    ) -> None:
        for doc in scenarios:
            correctness = score_configuration_correctness(
                spec_with(*CATALOG), doc["ground_truth"]
            )
            assert correctness is not None, doc["id"]
            for detail in correctness["parameter_details"]:
                service = detail["parameter"].split(".", 1)[0]
                assert service in doc["ground_truth"]["expected_services"], doc["id"]


class TestScoresAreIndependentlyRecoverable:
    def test_selection_and_correctness_do_not_share_keys(self) -> None:
        spec = spec_with("postgres", "nginx")
        selection = score_service_selection(spec, STUDY2_GT)
        correctness = score_configuration_correctness(spec, STUDY2_GT)
        assert not set(selection) & set(correctness)

    def test_compute_scores_carries_the_denominator(self) -> None:
        """parameters_checked must survive into the run record, since Study 2
        correctness cannot be interpreted without it."""
        result = RunResult(
            scenario_id="s", scenario_text="t", architecture="single", model="m"
        )
        result.termination_reason = "finalised"
        result.final_spec = spec_with("postgres", "nginx")
        scores = compute_scores(result, copy.deepcopy(STUDY2_GT))
        assert "parameters_checked" in scores
        assert "selection_f1" in scores
