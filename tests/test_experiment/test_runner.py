"""Tests for the experiment runner and result logger.

Agent is fully mocked — no LLM, no Docker. Tests verify scenario
loading, result file persistence, and multi-run execution.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.experiment.result_logger import RunResult, compute_scores, save
from src.experiment.runner import load_scenario, main

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scenario_path(tmp_path: Path) -> Path:
    """Write a minimal scenario YAML and return its path."""
    data = {
        "id": "test_scenario_001",
        "description": "Unit test scenario",
        "request": "Deploy a small dev web stack with nginx, postgres, and redis.",
        "ground_truth": {
            "workload_class": "BALANCED",
            "expected_cis_minimum_pass_rate": 0.75,
        },
    }
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


@pytest.fixture
def mock_agent_result() -> MagicMock:
    """A mock AgentRunResult with plausible fields."""
    result = MagicMock()
    result.tokens_used = 5000
    result.wall_clock_s = 12.3
    result.termination_reason = "finalised"
    result.history = []
    result.final_spec = None
    result.validator_report = None
    return result


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------


class TestScenarioLoading:
    def test_load_valid_scenario(self, scenario_path: Path) -> None:
        scenario = load_scenario(scenario_path)
        assert scenario["id"] == "test_scenario_001"
        assert "Deploy" in scenario["request"]

    def test_load_missing_key_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(yaml.dump({"description": "no id"}), encoding="utf-8")
        with pytest.raises(ValueError, match="missing required key"):
            load_scenario(bad)

    def test_load_non_mapping_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just a list", encoding="utf-8")
        with pytest.raises(ValueError, match="not a YAML mapping"):
            load_scenario(bad)


# ---------------------------------------------------------------------------
# Result logger
# ---------------------------------------------------------------------------


class TestResultLogger:
    def test_save_creates_json(self, tmp_path: Path) -> None:
        result = RunResult(
            scenario_id="sc01",
            scenario_text="deploy stuff",
            architecture="single",
            model="test-model",
            tokens_used=1234,
            termination_reason="finalised",
        )
        path = save(result, tmp_path)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["scenario_id"] == "sc01"
        assert data["tokens_used"] == 1234

    def test_save_directory_structure(self, tmp_path: Path) -> None:
        result = RunResult(
            scenario_id="sc02",
            scenario_text="x",
            architecture="single",
            model="m",
            run_id="abc123",
        )
        path = save(result, tmp_path)
        assert path == tmp_path / "sc02" / "single" / "abc123.json"

    def test_compute_scores_completed(self) -> None:
        result = RunResult(
            scenario_id="s",
            scenario_text="t",
            architecture="single",
            model="m",
            termination_reason="finalised",
        )
        scores = compute_scores(result, None)
        assert scores["completed"] is True

    def test_compute_scores_not_completed(self) -> None:
        result = RunResult(
            scenario_id="s",
            scenario_text="t",
            architecture="single",
            model="m",
            termination_reason="budget_exhausted",
        )
        scores = compute_scores(result, None)
        assert scores["completed"] is False

    def test_compute_scores_cis_pass_rate(self) -> None:
        result = RunResult(
            scenario_id="s",
            scenario_text="t",
            architecture="single",
            model="m",
            termination_reason="finalised",
            validator_report={
                "cis_results": [
                    {"passed": True},
                    {"passed": True},
                    {"passed": False},
                    {"passed": True},
                ],
                "smoke_tests": {},
            },
        )
        scores = compute_scores(result, None)
        assert scores["cis_pass_rate"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Runner CLI (agent fully mocked)
# ---------------------------------------------------------------------------


class TestRunnerCLI:
    @patch("src.experiment.runner._run_single")
    def test_runner_writes_result(
        self, mock_run: MagicMock, scenario_path: Path, tmp_path: Path
    ) -> None:
        mock_run.return_value = RunResult(
            scenario_id="test_scenario_001",
            scenario_text="Deploy a small dev web stack with nginx, postgres, and redis.",
            architecture="single",
            model="test",
            tokens_used=1000,
            termination_reason="finalised",
            scores={"completed": True},
        )

        main([
            "--scenario", str(scenario_path),
            "--runs", "1",
            "--output", str(tmp_path),
        ])

        result_files = list(tmp_path.rglob("*.json"))
        assert len(result_files) == 1
        data = json.loads(result_files[0].read_text())
        assert data["scenario_id"] == "test_scenario_001"

    @patch("src.experiment.runner._run_single")
    def test_runner_multiple_runs(
        self, mock_run: MagicMock, scenario_path: Path, tmp_path: Path
    ) -> None:
        call_count = 0

        def make_result(*args: Any, **kwargs: Any) -> RunResult:
            nonlocal call_count
            call_count += 1
            return RunResult(
                scenario_id="test_scenario_001",
                scenario_text="x",
                architecture="single",
                model="test",
                run_id=f"run_{call_count}",
                termination_reason="finalised",
                scores={"completed": True},
            )

        mock_run.side_effect = make_result

        main([
            "--scenario", str(scenario_path),
            "--runs", "3",
            "--output", str(tmp_path),
        ])

        result_files = list(tmp_path.rglob("*.json"))
        assert len(result_files) == 3
