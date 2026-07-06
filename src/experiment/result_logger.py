"""Structured result logging for experiment runs.

Every run produces a :class:`RunResult` that is persisted as a JSON file
under ``results/runs/{scenario_id}/{architecture}/{run_id}.json``. This
captures everything needed to reproduce or analyse the run later.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class RunResult:
    """Complete record of one agent run."""

    scenario_id: str
    scenario_text: str
    architecture: str  # "single" | "multi"
    model: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = ""
    finished_at: str = ""
    tokens_used: int = 0
    wall_clock_s: float = 0.0
    max_iterations: int = 25
    termination_reason: str = ""
    final_spec: dict[str, Any] | None = None
    validator_report: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()  # noqa: UP017 — 3.10-compatible


def save(result: RunResult, out_dir: Path) -> Path:
    """Persist a :class:`RunResult` as JSON.

    Directory structure: ``{out_dir}/{scenario_id}/{architecture}/{run_id}.json``

    Returns:
        Path to the written JSON file.
    """
    dest_dir = out_dir / result.scenario_id / result.architecture
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{result.run_id}.json"

    data = asdict(result)
    dest.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logger.info(
        "result_saved",
        path=str(dest),
        scenario=result.scenario_id,
        architecture=result.architecture,
    )
    return dest


def compute_scores(
    result: RunResult, ground_truth: dict[str, Any] | None
) -> dict[str, Any]:
    """Compute evaluation scores against optional ground truth.

    Scores currently computed:
    - ``cis_pass_rate``: fraction of CIS checks passed (from validator report)
    - ``smoke_pass_rate``: fraction of smoke tests passed
    - ``completed``: whether the agent terminated via finalisation

    Additional ground-truth-based scores can be added as the benchmark
    dataset grows.
    """
    scores: dict[str, Any] = {"completed": result.termination_reason == "finalised"}

    report = result.validator_report
    if report:
        cis_results = report.get("cis_results", [])
        if cis_results:
            passed = sum(1 for c in cis_results if c.get("passed"))
            scores["cis_pass_rate"] = passed / len(cis_results)

        smoke = report.get("smoke_tests", {})
        if smoke:
            passed = sum(
                1 for s in smoke.values()
                if s.get("did_start") and s.get("accepts_connections")
            )
            scores["smoke_pass_rate"] = passed / len(smoke)

    return scores
