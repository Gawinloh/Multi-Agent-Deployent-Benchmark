"""Analysis of the architecture-comparison experiment.

Single source of truth for every number in the Findings chapter. Reads the run
JSONs, writes CSV tables and PNG figures into ``results/analysis/``, and prints
a summary. Deterministic and re-runnable: no randomness, no network, no
dependence on anything outside the committed dataset except the batch logs,
which are used only to recover container log tails for the failure taxonomy
(and whose absence is reported rather than silently skipped).

Datasets, never pooled:

  results/runs/       Study 1, the primary dataset. 72 runs, N=3 over 12
                      scenarios x 2 architectures, all at commit 8751e47 on
                      gpt-4.1-nano-2025-04-14.
  results/runs_qwen/  the earlier qwen2.5:14b arm. Reported separately and
                      labelled; the 24-run pilot at cac0ba1 is the comparable
                      subset.
  results/runs_tuning/ pre-instrumentation, no provenance, never reported.

Conventions that the numbers depend on, stated so they can be checked:

  * Correctness is averaged over *finalised* runs only. A run that never
    finalised has no correctness key at all — absent and wrong are different
    outcomes — so including it would require inventing a value.
  * Token and wall-clock means are over *all* runs in a cell, finalised or not,
    because cost is incurred either way.
  * Paired tests operate on the 12 scenario-level cell means, not on the 72
    individual runs, so each scenario contributes once and replicates are
    averaged first.

Usage::

    python scripts/analyse.py
"""

from __future__ import annotations

import json
import math
import re
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # deterministic, headless
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "results" / "runs"
RUNS_QWEN = REPO / "results" / "runs_qwen"
OUT = REPO / "results" / "analysis"
LOGS = sorted((REPO / "results").glob("matrix_nano_*.log"))

STUDY1_COMMIT = "8751e47"
STUDY1_MODEL = "gpt-4.1-nano-2025-04-14"
QWEN_COMMIT = "cac0ba1"
QWEN_MODEL = "qwen2.5:14b"

BUDGET = 100_000
ARMS = ("single", "multi")

# Greyscale-safe: identity is carried by marker shape and hatch as well as by
# lightness, so the figures survive black-and-white printing and CVD.
ARM_STYLE = {
    "single": {"marker": "o", "face": "white", "edge": "black", "hatch": ""},
    "multi": {"marker": "^", "face": "0.45", "edge": "black", "hatch": "///"},
}
AGENT_STYLE = {
    "orchestrator": {"color": "0.15", "hatch": ""},
    "config": {"color": "0.45", "hatch": "///"},
    "security": {"color": "0.70", "hatch": "..."},
    "validation": {"color": "0.88", "hatch": "xx"},
}

# Numbers verified independently by the author. The script asserts against
# these so that a silent change in method or data is caught immediately.
EXPECTED = {
    "paired_correctness_delta": -0.0027,
    "paired_correctness_ci": (-0.0393, 0.0339),
    "paired_correctness_t": -0.162,
    "paired_tokens_delta": -22978,
    "paired_tokens_ci": (-34405, -11551),
    "paired_tokens_t": -4.426,
    "completion_single": (35, 36),
    "completion_multi": (32, 36),
    "fisher_p": 0.357,
    "pooled_correctness_single": (0.9167, 0.0752, 35),
    "pooled_correctness_multi": (0.9124, 0.0708, 32),
    "pooled_cis_single": (0.9022, 0.0570),
    "pooled_cis_multi": (0.9088, 0.0438),
    "pooled_tokens_single": (26119, 18603),
    "pooled_tokens_multi": (49097, 28644),
    "ratio_tokens": 1.88,
    # The author's working note recorded 1.69, which is 79.1 / 46.9 — the ratio
    # of the *rounded* means. The exact means are 79.078669 and 46.948505, so
    # the ratio is 1.6844 and rounds to 1.68. Corrected here after checking
    # both paths; the token ratio is unaffected because 1.8797 rounds to 1.88
    # either way.
    "ratio_wall": 1.6844,
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load(directory: Path) -> list[dict[str, Any]]:
    """Load every run JSON under *directory*, sorted for determinism."""
    runs = []
    for path in sorted(directory.glob("*/*/*.json")):
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        data["_path"] = str(path.relative_to(REPO))
        runs.append(data)
    return runs


def correctness_of(run: dict[str, Any]) -> float | None:
    """Correctness, or None when the run did not finalise.

    A non-finalised run carries no correctness key by design, so this returns
    None rather than 0.0 and every caller filters it out explicitly.
    """
    if run.get("termination_reason") != "finalised":
        return None
    return (run.get("scores") or {}).get("correctness")


def cell_key(run: dict[str, Any]) -> tuple[str, str]:
    return (run["scenario_id"], run["architecture"])


def mean_or_nan(values: list[float]) -> float:
    return st.mean(values) if values else math.nan


def sd_or_nan(values: list[float]) -> float:
    return st.stdev(values) if len(values) > 1 else math.nan


# ---------------------------------------------------------------------------
# Table 1 — per scenario, per arm
# ---------------------------------------------------------------------------


def table_per_cell(runs: list[dict[str, Any]]) -> pd.DataFrame:
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        cells[cell_key(run)].append(run)

    rows = []
    for (scenario, arch), group in sorted(cells.items()):
        corr = [c for c in (correctness_of(r) for r in group) if c is not None]
        scores = [r.get("scores") or {} for r in group]
        act = [s["cis_pass_rate_actionable"] for s in scores if "cis_pass_rate_actionable" in s]
        raw = [s["cis_pass_rate"] for s in scores if "cis_pass_rate" in s]
        rows.append(
            {
                "scenario": scenario,
                "architecture": arch,
                "n": len(group),
                "n_finalised": sum(
                    1 for r in group if r.get("termination_reason") == "finalised"
                ),
                "mean_correctness_given_completion": mean_or_nan(corr),
                "sd_correctness_given_completion": sd_or_nan(corr),
                "n_correctness": len(corr),
                "mean_cis_actionable": mean_or_nan(act),
                "mean_cis_raw": mean_or_nan(raw),
                "n_deployed": len(act),
                "mean_tokens": mean_or_nan([r.get("tokens_used") or 0 for r in group]),
                "mean_wall_clock_s": mean_or_nan([r["wall_clock_s"] for r in group]),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Table 2 — paired differences
# ---------------------------------------------------------------------------


def paired_frame(runs: list[dict[str, Any]]) -> pd.DataFrame:
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        cells[cell_key(run)].append(run)

    rows = []
    for scenario in sorted({s for s, _ in cells}):
        row: dict[str, Any] = {"scenario": scenario}
        for arch in ARMS:
            group = cells[(scenario, arch)]
            corr = [c for c in (correctness_of(r) for r in group) if c is not None]
            row[f"correctness_{arch}"] = mean_or_nan(corr)
            row[f"tokens_{arch}"] = mean_or_nan([r.get("tokens_used") or 0 for r in group])
            row[f"wall_{arch}"] = mean_or_nan([r["wall_clock_s"] for r in group])
        row["d_correctness"] = row["correctness_single"] - row["correctness_multi"]
        row["d_tokens"] = row["tokens_single"] - row["tokens_multi"]
        row["d_wall"] = row["wall_single"] - row["wall_multi"]
        rows.append(row)
    return pd.DataFrame(rows)


def paired_test(diffs: list[float]) -> dict[str, float]:
    """Paired t-test summary from the differences themselves."""
    n = len(diffs)
    mean = st.mean(diffs)
    sd = st.stdev(diffs)
    se = sd / math.sqrt(n)
    tcrit = stats.t.ppf(0.975, n - 1)
    tstat = mean / se
    return {
        "n": n,
        "mean": mean,
        "sd": sd,
        "se": se,
        "t": tstat,
        "df": n - 1,
        "p": 2 * stats.t.sf(abs(tstat), n - 1),
        "ci_low": mean - tcrit * se,
        "ci_high": mean + tcrit * se,
    }


# ---------------------------------------------------------------------------
# Table 3 — failure taxonomy
# ---------------------------------------------------------------------------

#: Container-log signatures mapped to (cause, classification). Agent
#: configuration errors are values the agent chose and could have chosen
#: differently; harness limits are cases where the environment refused input
#: the schema advertised as valid.
FAILURE_SIGNATURES = [
    (
        r'outside the valid range for parameter "([a-z_]+)"',
        "{0} outside postgres's permitted range",
        "agent configuration error",
    ),
    (
        r"No tls-cert-file configured",
        "Redis TLS enabled with no certificate supplied",
        "agent configuration error",
    ),
    (
        r'syntax error in file .*postgresql\.conf.*near token "([A-Za-z]+)"',
        "postgresql.conf syntax error near {0}",
        "harness limit",
    ),
    (
        r"private key file .* has group or world access",
        "postgres SSL key permissions",
        "harness limit",
    ),
    (
        r'requires wal_level "replica" or "logical"',
        "wal_level minimal with max_wal_senders > 0",
        "harness limit",
    ),
]


def log_text() -> str:
    """Concatenated batch logs, used only to recover container log tails."""
    parts = []
    for path in LOGS:
        parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def container_tail(stack_id: str, blob: str) -> str:
    """The container log tails logged alongside a startup failure."""
    for match in re.finditer(r"validation_startup_failed(.{0,12000}?)run_id=", blob, re.S):
        chunk = match.group(1)
        if stack_id in chunk:
            tails = re.findall(r"(postgres_log_tail|redis_log_tail)='([^']*)'", chunk)
            return " | ".join(f"{name}: {body}" for name, body in tails)
    return ""


def classify(text: str) -> tuple[str, str]:
    for pattern, template, kind in FAILURE_SIGNATURES:
        match = re.search(pattern, text)
        if match:
            return template.format(*match.groups()), kind
    return "unclassified", "unclassified"


def table_failures(runs: list[dict[str, Any]]) -> pd.DataFrame:
    blob = log_text()
    rows = []
    for run in sorted(runs, key=lambda r: (r["scenario_id"], r["architecture"])):
        report = run.get("validator_report") or {}
        error = report.get("error")
        reason = run.get("termination_reason")

        if reason != "finalised":
            rows.append(
                {
                    "scenario": run["scenario_id"],
                    "architecture": run["architecture"],
                    "run_id": run["run_id"],
                    "failure_type": "did not finalise",
                    "cause": reason,
                    "classification": "agent did not converge",
                    "verbatim": f"termination_reason={reason}",
                }
            )
        if error:
            stack_ids = set(re.findall(r"stack[_-]([0-9a-f]{12})", error))
            tail = ""
            for sid in sorted(stack_ids):
                tail = container_tail(sid, blob)
                if tail:
                    break
            exited = re.search(r"container \S+-(\w+)-1 exited", error)
            cause, kind = classify(tail or error)
            rows.append(
                {
                    "scenario": run["scenario_id"],
                    "architecture": run["architecture"],
                    "run_id": run["run_id"],
                    "failure_type": "validation_startup_failed",
                    "cause": cause
                    + (f" ({exited.group(1)} exited)" if exited else ""),
                    "classification": kind,
                    "verbatim": (tail or error).strip()[:2000]
                    or "container tail unavailable (batch log missing)",
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Extras: correlation, variance decomposition, overshoot
# ---------------------------------------------------------------------------


def correlations(runs: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for arch in ARMS:
        pairs = [
            (r.get("tokens_used") or 0, correctness_of(r))
            for r in runs
            if r["architecture"] == arch
        ]
        pairs = [(t, c) for t, c in pairs if c is not None]
        tokens = [t for t, _ in pairs]
        corr = [c for _, c in pairs]
        pear = stats.pearsonr(tokens, corr)
        spear = stats.spearmanr(tokens, corr)
        rows.append(
            {
                "architecture": arch,
                "n": len(pairs),
                "pearson_r": pear.statistic,
                "pearson_p": pear.pvalue,
                "spearman_rho": spear.statistic,
                "spearman_p": spear.pvalue,
            }
        )
    return pd.DataFrame(rows)


def variance_decomposition(runs: list[dict[str, Any]]) -> pd.DataFrame:
    """Descriptive partition of correctness variance.

    Sums of squares about the grand mean, attributed to scenario, to
    architecture, and to run-to-run variation within a cell. The design is
    slightly unbalanced (non-finalised runs contribute no correctness), so the
    components are not orthogonal and the remainder is reported explicitly
    rather than folded into one of the named terms.
    """
    obs = [(r["scenario_id"], r["architecture"], correctness_of(r)) for r in runs]
    obs = [(s, a, c) for s, a, c in obs if c is not None]
    grand = st.mean([c for _, _, c in obs])

    ss_total = sum((c - grand) ** 2 for _, _, c in obs)

    by_scenario: dict[str, list[float]] = defaultdict(list)
    by_arch: dict[str, list[float]] = defaultdict(list)
    by_cell: dict[tuple[str, str], list[float]] = defaultdict(list)
    for s, a, c in obs:
        by_scenario[s].append(c)
        by_arch[a].append(c)
        by_cell[(s, a)].append(c)

    ss_scenario = sum(len(v) * (st.mean(v) - grand) ** 2 for v in by_scenario.values())
    ss_arch = sum(len(v) * (st.mean(v) - grand) ** 2 for v in by_arch.values())
    ss_within = sum(
        sum((c - st.mean(v)) ** 2 for c in v) for v in by_cell.values()
    )
    remainder = ss_total - ss_scenario - ss_arch - ss_within

    return pd.DataFrame(
        [
            {"component": "between scenarios", "ss": ss_scenario,
             "pct_of_total": 100 * ss_scenario / ss_total},
            {"component": "between architectures", "ss": ss_arch,
             "pct_of_total": 100 * ss_arch / ss_total},
            {"component": "within cell (run-to-run)", "ss": ss_within,
             "pct_of_total": 100 * ss_within / ss_total},
            {"component": "remainder (interaction + imbalance)", "ss": remainder,
             "pct_of_total": 100 * remainder / ss_total},
            {"component": "total", "ss": ss_total, "pct_of_total": 100.0},
        ]
    )


def overshoot(runs: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for run in sorted(runs, key=lambda r: -(r.get("tokens_used") or 0)):
        used = run.get("tokens_used") or 0
        if used > BUDGET:
            rows.append(
                {
                    "scenario": run["scenario_id"],
                    "architecture": run["architecture"],
                    "tokens_used": used,
                    "overshoot": used - BUDGET,
                    "overshoot_pct": 100 * (used - BUDGET) / BUDGET,
                }
            )
    return pd.DataFrame(rows)


def per_agent_shares(runs: list[dict[str, Any]]) -> pd.DataFrame:
    """Token attribution within the multi arm."""
    totals: Counter[str] = Counter()
    per_run: dict[str, list[float]] = defaultdict(list)
    for run in runs:
        if run["architecture"] != "multi":
            continue
        agents = run.get("per_agent_tokens") or {}
        total = sum(agents.values())
        for name, value in agents.items():
            totals[name] += value
            per_run[name].append(100 * value / total if total else math.nan)
    grand = sum(totals.values())
    rows = []
    for name in ("orchestrator", "config", "security", "validation"):
        rows.append(
            {
                "agent": name,
                "total_tokens": totals.get(name, 0),
                "pct_of_multi_tokens": 100 * totals.get(name, 0) / grand if grand else math.nan,
                "mean_pct_within_run": mean_or_nan(per_run.get(name, [])),
                "n_runs_present": len(per_run.get(name, [])),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def _finish(ax: plt.Axes) -> None:
    """Recessive axes and grid; no chartjunk."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("0.4")
    ax.spines["bottom"].set_color("0.4")
    ax.tick_params(colors="0.25", labelsize=8)
    ax.grid(True, color="0.88", linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)


def fig_cost_accuracy(runs: list[dict[str, Any]], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(5.2, 3.6))
    for arch in ARMS:
        pts = [
            (r.get("tokens_used") or 0, correctness_of(r))
            for r in runs
            if r["architecture"] == arch
        ]
        pts = [(t, c) for t, c in pts if c is not None]
        style = ARM_STYLE[arch]
        ax.scatter(
            [t / 1000 for t, _ in pts],
            [c for _, c in pts],
            marker=style["marker"],
            facecolors=style["face"],
            edgecolors=style["edge"],
            linewidths=0.9,
            s=42,
            label=f"{arch} (n={len(pts)})",
            zorder=3,
        )
    ax.set_xlabel("tokens used (thousands)", fontsize=9)
    ax.set_ylabel("correctness", fontsize=9)
    ax.set_title("Cost against accuracy, one point per finalised run", fontsize=10)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    _finish(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


def fig_paired_correctness(paired: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(5.2, 4.0))
    for _, row in paired.iterrows():
        ax.plot(
            [0, 1],
            [row["correctness_single"], row["correctness_multi"]],
            color="0.35",
            linewidth=1.0,
            zorder=2,
        )
        for x, arch in ((0, "single"), (1, "multi")):
            style = ARM_STYLE[arch]
            ax.scatter(
                [x],
                [row[f"correctness_{arch}"]],
                marker=style["marker"],
                facecolors=style["face"],
                edgecolors=style["edge"],
                linewidths=0.9,
                s=40,
                zorder=3,
            )
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["single", "multi"], fontsize=9)
    ax.set_xlim(-0.35, 1.35)
    ax.set_ylabel("mean correctness given completion", fontsize=9)
    ax.set_title("Paired by scenario (n=12)", fontsize=10)
    _finish(ax)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


def fig_token_distribution(runs: list[dict[str, Any]], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(4.4, 3.6))
    data = [
        [(r.get("tokens_used") or 0) / 1000 for r in runs if r["architecture"] == arch]
        for arch in ARMS
    ]
    bp = ax.boxplot(
        data, tick_labels=list(ARMS), widths=0.5, patch_artist=True, zorder=3,
        medianprops={"color": "black", "linewidth": 1.4},
        whiskerprops={"color": "0.35"}, capprops={"color": "0.35"},
        flierprops={"marker": "o", "markersize": 4, "markerfacecolor": "white",
                    "markeredgecolor": "0.35"},
    )
    for patch, arch in zip(bp["boxes"], ARMS, strict=True):
        patch.set_facecolor(ARM_STYLE[arch]["face"])
        patch.set_edgecolor("black")
        patch.set_hatch(ARM_STYLE[arch]["hatch"])
        patch.set_linewidth(0.9)
    ax.axhline(BUDGET / 1000, color="black", linestyle=(0, (4, 3)), linewidth=1.0, zorder=2)
    ax.annotate("budget 100k", xy=(0.62, BUDGET / 1000), fontsize=7.5,
                color="0.25", va="bottom")
    ax.set_ylabel("tokens used (thousands)", fontsize=9)
    ax.set_title("Token use by architecture", fontsize=10)
    _finish(ax)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


def fig_per_agent(runs: list[dict[str, Any]], shares: pd.DataFrame, path: Path) -> None:
    """Where the multi arm's tokens go, per scenario and pooled."""
    scenarios = sorted({r["scenario_id"] for r in runs if r["architecture"] == "multi"})
    order = ["orchestrator", "config", "security", "validation"]
    per_scen: dict[str, dict[str, float]] = {}
    for scenario in scenarios:
        acc: Counter[str] = Counter()
        n = 0
        for run in runs:
            if run["architecture"] != "multi" or run["scenario_id"] != scenario:
                continue
            n += 1
            for name, value in (run.get("per_agent_tokens") or {}).items():
                acc[name] += value
        per_scen[scenario] = {k: acc.get(k, 0) / max(n, 1) / 1000 for k in order}

    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    labels = [s.replace("scenario_", "").replace("_", " ") for s in scenarios]
    bottoms = [0.0] * len(scenarios)
    for name in order:
        values = [per_scen[s][name] for s in scenarios]
        style = AGENT_STYLE[name]
        pct = shares.loc[shares["agent"] == name, "pct_of_multi_tokens"].iloc[0]
        ax.bar(
            range(len(scenarios)), values, bottom=bottoms, width=0.68,
            color=style["color"], edgecolor="white", linewidth=1.2,
            hatch=style["hatch"], label=f"{name} ({pct:.1f}%)", zorder=3,
        )
        bottoms = [b + v for b, v in zip(bottoms, values, strict=True)]
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=7.5)
    ax.set_ylabel("mean tokens per run (thousands)", fontsize=9)
    ax.set_title("Where the multi-agent token cost goes", fontsize=10)
    ax.legend(frameon=False, fontsize=8, ncol=2)
    _finish(ax)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Verification against independently checked values
# ---------------------------------------------------------------------------


def check(label: str, got: float, want: float, tol: float, failures: list[str]) -> str:
    ok = abs(got - want) <= tol
    if not ok:
        failures.append(f"{label}: computed {got!r}, expected {want!r}")
    verdict = "PASS" if ok else "FAIL"
    return f"  {verdict}  {label:<44} computed={got:>12.4f}  expected={want:>12.4f}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    all_runs = load(RUNS)
    study1 = [
        r for r in all_runs
        if (r.get("git_commit") or "").startswith(STUDY1_COMMIT)
        and r.get("model") == STUDY1_MODEL
    ]
    qwen_all = load(RUNS_QWEN)
    qwen = [
        r for r in qwen_all
        if (r.get("git_commit") or "").startswith(QWEN_COMMIT)
        and r.get("model") == QWEN_MODEL
    ]

    print("=" * 78)
    print("STUDY 1 — gpt-4.1-nano-2025-04-14 @ 8751e47")
    print("=" * 78)
    print(f"runs loaded from results/runs/: {len(all_runs)}   qualifying: {len(study1)}")
    if len(all_runs) != len(study1):
        print(f"  !! {len(all_runs) - len(study1)} run(s) in results/runs/ are NOT Study 1")
    cells = Counter(cell_key(r) for r in study1)
    print(f"cells: {len(cells)}   runs per cell: {dict(Counter(cells.values()))}")
    dirty = Counter(r.get("git_dirty") for r in study1)
    print(f"git_dirty: {dict(dirty)}")
    if dirty.get(True):
        print(f"  !! {dirty[True]} run(s) recorded git_dirty=true")

    # ---- tables -----------------------------------------------------------
    t1 = table_per_cell(study1)
    t1.to_csv(OUT / "table1_per_scenario_per_arm.csv", index=False)

    paired = paired_frame(study1)
    corr_test = paired_test(paired["d_correctness"].tolist())
    tok_test = paired_test(paired["d_tokens"].tolist())
    wall_test = paired_test(paired["d_wall"].tolist())
    summary_rows = []
    tests = (("correctness", corr_test), ("tokens", tok_test), ("wall_clock_s", wall_test))
    for name, res in tests:
        summary_rows.append({"measure": name, **res})
    paired.to_csv(OUT / "table2_paired_differences.csv", index=False)
    pd.DataFrame(summary_rows).to_csv(OUT / "table2_paired_summary.csv", index=False)

    t3 = table_failures(study1)
    t3.to_csv(OUT / "table3_failure_taxonomy.csv", index=False)

    t4 = table_per_cell(qwen)
    t4.insert(0, "dataset", "qwen2.5:14b pilot @ cac0ba1 — NOT pooled with Study 1")
    t4.to_csv(OUT / "table4_qwen_arm.csv", index=False)

    shares = per_agent_shares(study1)
    shares.to_csv(OUT / "table5_per_agent_tokens.csv", index=False)
    corr_df = correlations(study1)
    corr_df.to_csv(OUT / "table6_token_correctness_correlation.csv", index=False)
    var_df = variance_decomposition(study1)
    var_df.to_csv(OUT / "table7_variance_decomposition.csv", index=False)
    over_df = overshoot(study1)
    over_df.to_csv(OUT / "table8_budget_overshoot.csv", index=False)

    # ---- figures ----------------------------------------------------------
    fig_cost_accuracy(study1, OUT / "fig1_cost_accuracy.png")
    fig_paired_correctness(paired, OUT / "fig2_paired_correctness.png")
    fig_token_distribution(study1, OUT / "fig3_token_distribution.png")
    fig_per_agent(study1, shares, OUT / "fig4_per_agent_tokens.png")

    # ---- pooled descriptives ---------------------------------------------
    pooled: dict[str, dict[str, Any]] = {}
    for arch in ARMS:
        sub = [r for r in study1 if r["architecture"] == arch]
        corr = [c for c in (correctness_of(r) for r in sub) if c is not None]
        scores = [r.get("scores") or {} for r in sub]
        act = [s["cis_pass_rate_actionable"] for s in scores if "cis_pass_rate_actionable" in s]
        raw = [s["cis_pass_rate"] for s in scores if "cis_pass_rate" in s]
        tok = [r.get("tokens_used") or 0 for r in sub]
        wall = [r["wall_clock_s"] for r in sub]
        pooled[arch] = {
            "n": len(sub),
            "n_finalised": sum(1 for r in sub if r.get("termination_reason") == "finalised"),
            "corr_mean": mean_or_nan(corr), "corr_sd": sd_or_nan(corr), "corr_n": len(corr),
            "cis_mean": mean_or_nan(act), "cis_sd": sd_or_nan(act),
            "cis_raw_mean": mean_or_nan(raw), "cis_raw_sd": sd_or_nan(raw),
            "tok_mean": mean_or_nan(tok), "tok_sd": sd_or_nan(tok),
            "wall_mean": mean_or_nan(wall), "wall_sd": sd_or_nan(wall),
        }
    pd.DataFrame(pooled).T.to_csv(OUT / "table0_pooled_descriptives.csv")

    ps_, pm_ = pooled["single"], pooled["multi"]
    fisher = stats.fisher_exact(
        [
            [ps_["n_finalised"], ps_["n"] - ps_["n_finalised"]],
            [pm_["n_finalised"], pm_["n"] - pm_["n_finalised"]],
        ]
    )

    # ---- printed summary --------------------------------------------------
    print("\n--- COMPLETION ---")
    for arch in ARMS:
        p = pooled[arch]
        print(f"  {arch:<7} {p['n_finalised']}/{p['n']} finalised ({p['n_finalised']/p['n']:.1%})")
    print(f"  Fisher exact p = {fisher.pvalue:.4f}  (odds ratio {fisher.statistic:.3f})")

    print("\n--- POOLED (correctness over finalised runs only) ---")
    for arch in ARMS:
        p = pooled[arch]
        print(f"  {arch:<7} correctness {p['corr_mean']:.4f}"
              f" (sd {p['corr_sd']:.4f}, n={p['corr_n']})"
              f" | cis_actionable {p['cis_mean']:.4f} (sd {p['cis_sd']:.4f})"
              f" | cis_raw {p['cis_raw_mean']:.4f}")
        print(f"          tokens {p['tok_mean']:,.0f} (sd {p['tok_sd']:,.0f})"
              f" | wall {p['wall_mean']:.1f}s (sd {p['wall_sd']:.1f})")
    ratio_tok = pooled["multi"]["tok_mean"] / pooled["single"]["tok_mean"]
    ratio_wall = pooled["multi"]["wall_mean"] / pooled["single"]["wall_mean"]
    print(f"  multi/single ratio — tokens {ratio_tok:.2f}x, wall clock {ratio_wall:.2f}x")

    print("\n--- PAIRED BY SCENARIO (n=12, single minus multi) ---")
    for name, res in tests:
        print(f"  {name:<14} d={res['mean']:>12,.4f}  95% CI [{res['ci_low']:>12,.4f},"
              f" {res['ci_high']:>12,.4f}]  t({res['df']})={res['t']:.3f}  p={res['p']:.4f}")

    print("\n--- PER-AGENT TOKEN SHARES (multi arm) ---")
    for _, row in shares.iterrows():
        print(f"  {row['agent']:<14} {row['total_tokens']:>10,.0f} tokens"
              f"  {row['pct_of_multi_tokens']:>5.1f}% of multi total"
              f"  (mean {row['mean_pct_within_run']:.1f}% within a run,"
              f" present in {row['n_runs_present']} runs)")
    single_total = sum(r.get("tokens_used") or 0 for r in study1 if r["architecture"] == "single")
    multi_total = sum(r.get("tokens_used") or 0 for r in study1 if r["architecture"] == "multi")
    print(f"  multi total {multi_total:,} vs single total {single_total:,}"
          f"  (overhead {multi_total - single_total:,} tokens)")
    non_config = shares.loc[shares["agent"] != "config", "total_tokens"].sum()
    print(f"  orchestrator + security + validation = {non_config:,} tokens"
          f" ({100*non_config/multi_total:.1f}% of the multi arm)")

    print("\n--- TOKENS vs CORRECTNESS ---")
    for _, row in corr_df.iterrows():
        print(f"  {row['architecture']:<7}"
              f" pearson r={row['pearson_r']:+.3f} (p={row['pearson_p']:.3f})"
              f"  spearman rho={row['spearman_rho']:+.3f}"
              f" (p={row['spearman_p']:.3f})  n={row['n']}")

    print("\n--- VARIANCE DECOMPOSITION (correctness) ---")
    for _, row in var_df.iterrows():
        print(f"  {row['component']:<38} SS={row['ss']:.5f}  {row['pct_of_total']:>6.1f}%")

    print("\n--- BUDGET OVERSHOOT (cap 100,000) ---")
    for arch in ARMS:
        sub = over_df[over_df["architecture"] == arch]
        n_arm = pooled[arch]["n"]
        print(f"  {arch:<7} {len(sub)}/{n_arm} exceeded"
              + (f"  mean +{sub['overshoot'].mean():,.0f}  max +{sub['overshoot'].max():,.0f}"
                 if len(sub) else ""))
    for _, row in over_df.iterrows():
        print(f"      {row['scenario']:<34} {row['architecture']:<7}"
              f" +{row['overshoot']:,} ({row['overshoot_pct']:.1f}%)")

    print("\n--- FAILURE TAXONOMY ---")
    if t3.empty:
        print("  none")
    else:
        for kind, group in t3.groupby("classification"):
            print(f"  {kind}: {len(group)}")
            for _, row in group.iterrows():
                print(f"      {row['scenario']:<34} {row['architecture']:<7} {row['cause']}")

    print("\n--- QWEN ARM (reported separately, never pooled) ---")
    print(f"  runs in results/runs_qwen/: {len(qwen_all)}   qualifying pilot @cac0ba1: {len(qwen)}")
    for arch in ARMS:
        sub = [r for r in qwen if r["architecture"] == arch]
        if not sub:
            continue
        corr = [c for c in (correctness_of(r) for r in sub) if c is not None]
        fin = sum(1 for r in sub if r.get("termination_reason") == "finalised")
        print(f"  {arch:<7} n={len(sub)} finalised={fin}"
              f" correctness {mean_or_nan(corr):.4f} (sd {sd_or_nan(corr):.4f}, n={len(corr)})"
              f" tokens {mean_or_nan([r.get('tokens_used') or 0 for r in sub]):,.0f}")

    # ---- verification -----------------------------------------------------
    print("\n" + "=" * 78)
    print("VERIFICATION against independently checked values")
    print("=" * 78)
    failures: list[str] = []
    exp = EXPECTED
    ct, tt = corr_test, tok_test
    ps, pm = pooled["single"], pooled["multi"]
    checks = [
        ("paired correctness delta", ct["mean"], exp["paired_correctness_delta"], 5e-4),
        ("paired correctness CI low", ct["ci_low"], exp["paired_correctness_ci"][0], 5e-4),
        ("paired correctness CI high", ct["ci_high"], exp["paired_correctness_ci"][1], 5e-4),
        ("paired correctness t", ct["t"], exp["paired_correctness_t"], 5e-3),
        ("paired tokens delta", tt["mean"], exp["paired_tokens_delta"], 1.0),
        ("paired tokens CI low", tt["ci_low"], exp["paired_tokens_ci"][0], 1.0),
        ("paired tokens CI high", tt["ci_high"], exp["paired_tokens_ci"][1], 1.0),
        ("paired tokens t", tt["t"], exp["paired_tokens_t"], 5e-3),
        ("completion single", ps["n_finalised"], exp["completion_single"][0], 0),
        ("completion multi", pm["n_finalised"], exp["completion_multi"][0], 0),
        ("fisher exact p", fisher.pvalue, exp["fisher_p"], 5e-3),
        ("pooled correctness single", ps["corr_mean"], exp["pooled_correctness_single"][0], 5e-5),
        ("pooled correctness single sd", ps["corr_sd"], exp["pooled_correctness_single"][1], 5e-5),
        ("pooled correctness multi", pm["corr_mean"], exp["pooled_correctness_multi"][0], 5e-5),
        ("pooled correctness multi sd", pm["corr_sd"], exp["pooled_correctness_multi"][1], 5e-5),
        ("pooled cis single", ps["cis_mean"], exp["pooled_cis_single"][0], 5e-5),
        ("pooled cis multi", pm["cis_mean"], exp["pooled_cis_multi"][0], 5e-5),
        ("pooled tokens single", ps["tok_mean"], exp["pooled_tokens_single"][0], 1.0),
        ("pooled tokens multi", pm["tok_mean"], exp["pooled_tokens_multi"][0], 1.0),
        ("ratio tokens", ratio_tok, exp["ratio_tokens"], 5e-3),
        ("ratio wall clock", ratio_wall, exp["ratio_wall"], 5e-3),
    ]
    print("\n".join(check(lbl, got, want, tol, failures) for lbl, got, want, tol in checks))
    print()
    if failures:
        print(f"!! {len(failures)} MISMATCH(ES) — do not write from these numbers until resolved:")
        for line in failures:
            print(f"   {line}")
    else:
        print("All checks passed; the script reproduces the verified numbers.")

    n_csv = len(list(OUT.glob("*.csv")))
    n_png = len(list(OUT.glob("*.png")))
    print(f"\nwrote {n_csv} CSV and {n_png} PNG to {OUT.relative_to(REPO)}/")


if __name__ == "__main__":
    main()
