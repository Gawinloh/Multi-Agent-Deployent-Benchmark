"""Batch runner for the architecture x scenario experiment matrix.

Runs every (scenario, architecture) combination, resumable and crash
tolerant, so an overnight batch survives a hung model call or a laptop
sleeping. Progress is appended to a JSONL log as each run finishes, and
existing results are counted first so an interrupted batch can be
restarted without repeating completed work.

Pilot (one run per cell, to shake out scenarios no agent has seen)::

    python scripts/run_matrix.py --runs 1

Full matrix::

    python scripts/run_matrix.py --runs 3

Useful flags::

    --architectures single          only one architecture
    --scenarios smoke_test scenario_007_oltp_pci
    --timeout 3600                  per-run wall-clock cap in seconds
    --dry-run                       print the plan and exit
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = REPO_ROOT / "benchmark" / "scenarios"
DEFAULT_OUTPUT = REPO_ROOT / "results" / "runs"

ARCHITECTURES = ("single", "multi")


def discover_scenarios(
    names: list[str] | None, scenario_dir: Path = SCENARIO_DIR
) -> list[Path]:
    """Return scenario YAML paths, optionally filtered by file stem."""
    paths = sorted(scenario_dir.glob("*.yaml"))
    if names:
        wanted = set(names)
        paths = [p for p in paths if p.stem in wanted]
        missing = wanted - {p.stem for p in paths}
        if missing:
            raise SystemExit(f"Unknown scenario(s): {', '.join(sorted(missing))}")
    if not paths:
        raise SystemExit(f"No scenario YAML found in {scenario_dir}")
    return paths


def scenario_id(path: Path) -> str:
    """Read the scenario id without importing the project's dependencies."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("id:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    raise SystemExit(f"{path} has no id field")


def existing_run_count(output: Path, sid: str, architecture: str) -> int:
    """How many usable result JSONs already exist for this cell.

    Only runs carrying provenance (a recorded ``git_commit``) are
    counted. Runs produced before the instrumentation landed are tuning
    artefacts with no commit, no per-agent tokens and a placeholder model
    name; counting them would silently skip a cell that still needs real
    data.
    """
    cell = output / sid / architecture
    if not cell.is_dir():
        return 0
    usable = 0
    for path in cell.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("git_commit"):
            usable += 1
    return usable


def log_progress(record: dict[str, object], progress_log: Path) -> None:
    """Append progress alongside the selected result output, never Study 1."""
    progress_log.parent.mkdir(parents=True, exist_ok=True)
    with progress_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def run_cell(
    path: Path,
    architecture: str,
    runs: int,
    args: argparse.Namespace,
) -> tuple[bool, float, str]:
    """Invoke the experiment runner for one cell.

    Returns ``(ok, elapsed_seconds, note)``. A non-zero exit or a timeout
    is reported rather than raised, so the batch continues.
    """
    command = [
        sys.executable,
        "-m",
        "src.experiment.runner",
        "--scenario",
        str(path),
        "--architecture",
        architecture,
        "--runs",
        str(runs),
        "--budget",
        str(args.budget),
        "--max-iterations",
        str(args.max_iterations),
        "--output",
        str(args.output),
    ]
    if args.model:
        command += ["--model", args.model]

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            timeout=args.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, time.monotonic() - started, f"timeout after {args.timeout}s"
    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        return False, elapsed, f"exit code {completed.returncode}"
    return True, elapsed, ""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1, help="Runs per cell (default: 1)")
    parser.add_argument("--scenarios", nargs="*", help="Scenario file stems to include")
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=SCENARIO_DIR,
        help="Directory containing scenario YAML files (default: benchmark/scenarios)",
    )
    parser.add_argument(
        "--architectures", nargs="*", default=list(ARCHITECTURES), choices=ARCHITECTURES
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--budget", type=int, default=100_000)
    parser.add_argument("--max-iterations", type=int, default=25)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Per-cell wall-clock cap in seconds (default: 3600)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip cells that already have at least --runs results",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    args.scenarios_dir = args.scenarios_dir.resolve()
    args.output = args.output.resolve()
    progress_log = args.output / "matrix_progress.jsonl"
    paths = discover_scenarios(args.scenarios, args.scenarios_dir)
    cells = [(p, a) for p in paths for a in args.architectures]

    plan: list[tuple[Path, str, int]] = []
    for path, architecture in cells:
        sid = scenario_id(path)
        have = existing_run_count(args.output, sid, architecture)
        needed = max(0, args.runs - have) if args.resume else args.runs
        plan.append((path, architecture, needed))

    total_runs = sum(n for _, _, n in plan)
    print(f"Matrix: {len(paths)} scenarios x {len(args.architectures)} architectures")
    print(f"Cells: {len(cells)}  |  runs to execute: {total_runs}")
    if args.resume:
        skipped = sum(1 for _, _, n in plan if n == 0)
        print(f"Resuming: {skipped} cell(s) already complete")
    print()

    if args.dry_run:
        for path, architecture, needed in plan:
            print(f"  {path.stem:<40} {architecture:<7} runs={needed}")
        return

    batch_started = time.monotonic()
    failures: list[str] = []
    done = 0

    for path, architecture, needed in plan:
        if needed == 0:
            continue
        label = f"{path.stem} / {architecture}"
        print(f"[{done}/{total_runs}] {label} ... ", end="", flush=True)

        ok, elapsed, note = run_cell(path, architecture, needed, args)
        done += needed

        status = "ok" if ok else f"FAILED ({note})"
        print(f"{status} in {elapsed / 60:.1f} min")
        if not ok:
            failures.append(f"{label}: {note}")

        log_progress(
            {
                "at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
                "scenario": path.stem,
                "architecture": architecture,
                "runs": needed,
                "ok": ok,
                "elapsed_s": round(elapsed, 1),
                "note": note,
            },
            progress_log,
        )

        remaining = total_runs - done
        if remaining > 0 and done:
            rate = (time.monotonic() - batch_started) / done
            print(f"      ~{rate * remaining / 60:.0f} min remaining")

    total_min = (time.monotonic() - batch_started) / 60
    print(f"\n{'=' * 60}")
    print(f"Batch complete in {total_min:.1f} min ({total_min / 60:.1f} h)")
    if failures:
        print(f"{len(failures)} cell(s) failed:")
        for failure in failures:
            print(f"  {failure}")
        print("\nRe-run with --resume to fill the gaps.")
    else:
        print("All cells succeeded.")
    print(f"Progress log: {progress_log}")


if __name__ == "__main__":
    main()
