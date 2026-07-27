"""Tests for the generated benchmark scenarios.

Guards the three conditions that make the correctness metric meaningful:
every scenario loads, every ground-truth assertion is scoreable, and
every derived range is internally coherent.
"""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Any

import pytest

from benchmark.derive_scenarios import (
    MAX_DEPLOYABLE_RAM_GB,
    SCENARIOS,
    build_document,
    format_size,
)
from src.experiment.result_logger import (
    GROUND_TRUTH_PARAMETER_MAP,
    parse_size,
    score_configuration_correctness,
)
from src.experiment.runner import load_scenario

SCENARIO_PATHS = sorted(glob.glob("benchmark/scenarios/*.yaml"))


def _all_ground_truth_keys(gt: dict[str, Any]) -> list[tuple[str, str, Any]]:
    return [
        (block, key, value)
        for block in GROUND_TRUTH_PARAMETER_MAP
        for key, value in gt.get(block, {}).items()
    ]


class TestGeneratedFiles:
    def test_scenario_files_exist(self) -> None:
        assert len(SCENARIO_PATHS) == len(SCENARIOS)

    @pytest.mark.parametrize("path", SCENARIO_PATHS)
    def test_scenario_loads(self, path: str) -> None:
        scenario = load_scenario(Path(path))
        assert scenario["id"]
        assert scenario["request"].strip()
        assert scenario["ground_truth"]

    def test_ids_are_unique(self) -> None:
        ids = [load_scenario(Path(p))["id"] for p in SCENARIO_PATHS]
        assert len(ids) == len(set(ids))

    def test_generated_files_match_the_generator(self) -> None:
        # The YAML files are build artefacts; drift means someone edited
        # them by hand and the derivation doc no longer describes them.
        on_disk = {load_scenario(Path(p))["id"]: load_scenario(Path(p)) for p in SCENARIO_PATHS}
        for scenario in SCENARIOS:
            expected = build_document(scenario)
            assert on_disk[scenario.id]["ground_truth"] == expected["ground_truth"], (
                f"{scenario.id} on disk differs from derive_scenarios.py output; "
                "re-run python benchmark/derive_scenarios.py"
            )


class TestGroundTruthIsScoreable:
    @pytest.mark.parametrize("path", SCENARIO_PATHS)
    def test_every_key_maps_to_a_spec_path(self, path: str) -> None:
        gt = load_scenario(Path(path))["ground_truth"]
        for block, key, _ in _all_ground_truth_keys(gt):
            params = GROUND_TRUTH_PARAMETER_MAP[block]
            base = key.removesuffix("_acceptable").removesuffix("_range")
            assert key in params or base in params, (
                f"{block}.{key} has no entry in GROUND_TRUTH_PARAMETER_MAP, so it "
                "would be silently excluded from the correctness denominator"
            )

    @pytest.mark.parametrize("path", SCENARIO_PATHS)
    def test_ranges_are_coherent(self, path: str) -> None:
        gt = load_scenario(Path(path))["ground_truth"]
        for block, key, value in _all_ground_truth_keys(gt):
            if not key.endswith("_range"):
                continue
            assert isinstance(value, list) and len(value) == 2, f"{block}.{key}"
            low, high = value
            if isinstance(low, str):
                low, high = parse_size(low), parse_size(high)
            assert low <= high, f"{block}.{key} is inverted: {value}"

    @pytest.mark.parametrize("path", SCENARIO_PATHS)
    def test_acceptable_sets_are_non_empty(self, path: str) -> None:
        gt = load_scenario(Path(path))["ground_truth"]
        for block, key, value in _all_ground_truth_keys(gt):
            if key.endswith("_acceptable"):
                assert isinstance(value, list) and value, f"{block}.{key}"

    @pytest.mark.parametrize("path", SCENARIO_PATHS)
    def test_scoring_a_spec_reports_no_unmapped_parameters(self, path: str) -> None:
        gt = load_scenario(Path(path))["ground_truth"]
        # An empty spec still exercises resolution of every mapped path;
        # everything should come back "missing", never "unmapped".
        scored = score_configuration_correctness({}, gt)
        assert scored is not None
        statuses = {d["status"] for d in scored["parameter_details"]}
        assert statuses == {"missing"}, statuses
        assert scored["correctness"] == 0.0
        assert scored["parameters_checked"] == len(_all_ground_truth_keys(gt))


class TestCoverage:
    def test_all_workload_classes_appear_at_least_twice(self) -> None:
        counts: dict[str, int] = {}
        for s in SCENARIOS:
            counts[s.workload_class] = counts.get(s.workload_class, 0) + 1
        assert set(counts) == {"OLTP", "OLAP", "BALANCED", "CACHING_HEAVY"}
        assert all(v >= 2 for v in counts.values()), counts

    def test_all_compliance_profiles_appear_at_least_twice(self) -> None:
        counts: dict[str, int] = {}
        for s in SCENARIOS:
            counts[s.compliance] = counts.get(s.compliance, 0) + 1
        assert set(counts) == {"NONE", "GDPR_UK", "HIPAA", "PCI_DSS"}
        assert all(v >= 2 for v in counts.values()), counts

    def test_hardware_envelope_varies(self) -> None:
        rams = [s.ram_gb for s in SCENARIOS]
        assert max(rams) / min(rams) >= 4, "hardware must still vary meaningfully"

    def test_no_scenario_exceeds_the_test_host(self) -> None:
        # Scenarios are deployed to a Docker VM with 8 GB. A larger claimed
        # host makes the agent size shared_buffers beyond what PostgreSQL
        # can allocate, so the container exits during startup and the run
        # measures a harness limit rather than agent behaviour.
        for scenario in SCENARIOS:
            assert scenario.ram_gb <= MAX_DEPLOYABLE_RAM_GB, (
                f"{scenario.id} claims {scenario.ram_gb} GB, above the "
                f"{MAX_DEPLOYABLE_RAM_GB} GB the test host can deploy"
            )

    def test_upper_shared_buffers_bound_fits_the_test_host(self) -> None:
        from benchmark.derive_scenarios import derive_ground_truth

        for scenario in SCENARIOS:
            upper = derive_ground_truth(scenario)["expected_postgres"][
                "shared_buffers_range"
            ][1]
            assert parse_size(upper) <= 3 * 1024**3, (
                f"{scenario.id} permits shared_buffers up to {upper}, which "
                "risks a PostgreSQL startup failure on the test host"
            )

    def test_requests_do_not_leak_expected_values(self) -> None:
        # The prompt must state the situation, not the answer, or the task
        # degenerates into transcription.
        banned = ("shared_buffers", "max_connections", "maxmemory", "scram-sha-256")
        for s in SCENARIOS:
            lowered = s.request.lower()
            for token in banned:
                assert token not in lowered, f"{s.id} leaks {token} into the prompt"


class TestFormatSize:
    @pytest.mark.parametrize(
        ("num_bytes", "expected"),
        [
            (2 * 1024**3, "2GB"),
            (512 * 1024**2, "512MB"),
            (int(1.5 * 1024**3), "1.5GB"),
            (1024**2, "1MB"),
        ],
    )
    def test_renders_expected_string(self, num_bytes: int, expected: str) -> None:
        assert format_size(num_bytes) == expected

    def test_output_round_trips_through_parse_size(self) -> None:
        for num_bytes in (1024**2, 2 * 1024**3, int(1.2 * 1024**3), 44 * 1024**3):
            assert parse_size(format_size(num_bytes)) == pytest.approx(
                num_bytes, rel=0.01
            )
