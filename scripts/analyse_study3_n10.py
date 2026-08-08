"""Study 3 N=10 extension: what the added precision actually bought.

Companion to ``analyse_study3.py``, which is re-run unchanged on the full
160 and remains the source of the headline numbers. This script answers
only the questions the extension was launched to answer, and it answers
them by re-estimating the *same* quantities at both cell sizes rather
than introducing new ones. No new hypotheses: the predictions registered
at 69066b6 stand, and P2 stays reported as failed.

The N=3 subset is identified from git tag ``dataset-study3-n3`` — the run
ids tracked at that tag — not by timestamp or filename order, so the
split is exactly the 48 runs the N=3 result was computed on.

Sections:
  1. Censoring at N=10, per arm.
  2. Within-cell coefficient of variation, N=3 vs N=10, per arm.
  3. Every paired test re-estimated at both N, side by side with both CIs.
     Macro metrics use the paired t from analyse_study2.paired(); micro
     metrics are pooled and so carry a scenario-level cluster bootstrap
     instead, which is the matching interval for a pooled statistic.
  4. Explicit null-exclusion verdict per measure, and whether it changed.
  5. s2_003 diagnostic: is single's token distribution in that cell still
     bimodal at N=10, or did N=3 catch a tail. DIAGNOSTIC ONLY.
  6. Survivorship check: multi completion at 300k, and whether the
     micro/macro divergence seen in Study 2 stays absent.

Usage::

    python scripts/analyse_study3_n10.py
"""

from __future__ import annotations

import csv
import json
import math
import subprocess
import sys
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from scipy.stats import t  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyse_study2 import fbeta, mean, paired  # noqa: E402
from analyse_study3 import CAP_STUDY3, RUNS2, RUNS3, censoring, dist, load  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results/analysis_study3"
TAG_N3 = "dataset-study3-n3"
BOOTSTRAP_DRAWS = 20_000
RNG = np.random.default_rng(20260808)


def n3_run_ids() -> set[str]:
    """Run ids tracked at the N=3 tag — the exact 48 the N=3 result used."""
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", TAG_N3, "results/runs_study3/"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    ).stdout.splitlines()
    return {Path(p).stem for p in listing if p.endswith(".json")}


def cell_cv(rs: list[dict], arm: str) -> dict:
    """Within-cell coefficient of variation of tokens, across the 8 cells."""
    cvs = []
    for sid in sorted({r["scenario_id"] for r in rs}):
        v = [r["tokens_used"] for r in rs
             if r["scenario_id"] == sid and r["architecture"] == arm]
        if len(v) > 1:
            cvs.append(float(np.std(v, ddof=1) / np.mean(v)))
    a = np.array(cvs)
    return dict(n_cells=len(a), median=float(np.median(a)), max=float(a.max()),
                min=float(a.min()), mean=float(a.mean()))


def paired_log_ratio(rs: list[dict]) -> dict:
    logs = []
    for sid in sorted({r["scenario_id"] for r in rs}):
        here = [r for r in rs if r["scenario_id"] == sid]
        a = mean([r["tokens_used"] for r in here if r["architecture"] == "single"])
        b = mean([r["tokens_used"] for r in here if r["architecture"] == "multi"])
        logs.append(math.log(b / a))
    d = np.array(logs)
    se = d.std(ddof=1) / math.sqrt(len(d))
    crit = t.ppf(0.975, len(d) - 1)
    return dict(geometric_ratio=float(np.exp(d.mean())),
                ci_low=float(np.exp(d.mean() - crit * se)),
                ci_high=float(np.exp(d.mean() + crit * se)),
                t=float(d.mean() / se) if se else 0.0, df=len(d) - 1)


def micro_counts(rs: list[dict], arm: str) -> tuple[int, int, int]:
    c = Counter(d["verdict"] for r in rs if r["architecture"] == arm
                for d in r["scores"].get("selection_detail", []))
    return c["true_positive"], c["false_positive"], c["false_negative"]


def micro_from(tp: int, fp: int, fn: int, beta: float = 1.0) -> float:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    return fbeta(p, r, beta)


def micro_bootstrap(rs: list[dict], beta: float = 1.0) -> dict:
    """Cluster bootstrap over scenarios for the pooled micro F-beta delta.

    Micro pools individual service decisions, so it has no per-run value
    to feed a paired t-test. Resampling whole scenarios preserves the
    pairing (each draw takes both arms of a scenario together) and
    respects the clustering of decisions within a scenario.
    """
    sids = sorted({r["scenario_id"] for r in rs})
    by_sid = {s: [r for r in rs if r["scenario_id"] == s] for s in sids}
    obs = (micro_from(*micro_counts(rs, "multi"), beta)
           - micro_from(*micro_counts(rs, "single"), beta))
    draws = np.empty(BOOTSTRAP_DRAWS)
    for i in range(BOOTSTRAP_DRAWS):
        pick = RNG.choice(len(sids), len(sids), replace=True)
        sample = [r for k in pick for r in by_sid[sids[k]]]
        draws[i] = (micro_from(*micro_counts(sample, "multi"), beta)
                    - micro_from(*micro_counts(sample, "single"), beta))
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return dict(delta=float(obs), ci_low=float(lo), ci_high=float(hi),
                excludes_null=bool(lo > 0 or hi < 0))


def bimodality(values: list[float]) -> dict:
    """1-D two-group split diagnostic. Descriptive, not a test."""
    v = np.sort(np.array(values, dtype=float))
    gaps = np.diff(v)
    rng_ = v[-1] - v[0]
    best = None
    for k in range(1, len(v)):
        lo, hi = v[:k], v[k:]
        within = float(((lo - lo.mean()) ** 2).sum() + ((hi - hi.mean()) ** 2).sum())
        total = float(((v - v.mean()) ** 2).sum())
        if best is None or within < best[0]:
            best = (within, k, 1 - within / total if total else 0.0)
    return dict(
        sorted=[float(x) for x in v],
        n=len(v), median=float(np.median(v)),
        largest_gap=float(gaps.max()),
        largest_gap_frac_of_range=float(gaps.max() / rng_) if rng_ else 0.0,
        best_split_low_n=int(best[1]), variance_explained_by_2_groups=float(best[2]),
    )


def write(name: str, rows_: list[dict]) -> None:
    if not rows_:
        return
    with (OUT / name).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_[0]))
        w.writeheader()
        w.writerows(rows_)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    full = load(RUNS3)
    rs2 = load(RUNS2)
    ids3 = n3_run_ids()
    sub = [r for r in full if r["run_id"] in ids3]

    sizes = set(Counter((r["scenario_id"], r["architecture"]) for r in full).values())
    assert len(sizes) == 1, f"unbalanced cells: {sizes}"
    n_per_cell = sizes.pop()
    assert len(sub) == 48, f"N=3 subset resolved to {len(sub)} runs, expected 48"
    per_arm = 8 * n_per_cell

    out: dict = {
        "n_total": len(full), "n_per_cell": n_per_cell,
        "n3_subset": len(sub), "per_arm": per_arm,
        "models": sorted({r["model"] for r in full}),
        "commits": sorted({(r.get("git_commit") or "?")[:7] for r in full}),
    }

    # -- 1. censoring -------------------------------------------------------
    out["censoring_n10"] = censoring(full, CAP_STUDY3)
    out["censoring_n3"] = censoring(sub, CAP_STUDY3)

    # -- 2. within-cell CV --------------------------------------------------
    out["cv"] = {
        arm: {"n3": cell_cv(sub, arm), "n10": cell_cv(full, arm)}
        for arm in ("single", "multi")
    }

    # -- 3. paired tests at both N ------------------------------------------
    macro_keys = ("selection_f1", "selection_precision", "selection_recall", "tokens_used")
    rows = []
    for key in macro_keys:
        a, b = paired(sub, key), paired(full, key)
        rows.append(dict(
            measure=key, kind="paired t (macro, n=8 scenarios)",
            n3_delta=a["mean_delta"], n3_ci_low=a["ci_low"],
            n3_ci_high=a["ci_high"], n3_t=a["t"],
            n10_delta=b["mean_delta"], n10_ci_low=b["ci_low"],
            n10_ci_high=b["ci_high"], n10_t=b["t"],
            n3_excludes_null=bool(a["ci_low"] > 0 or a["ci_high"] < 0),
            n10_excludes_null=bool(b["ci_low"] > 0 or b["ci_high"] < 0),
            n3_ci_width=a["ci_high"] - a["ci_low"], n10_ci_width=b["ci_high"] - b["ci_low"],
        ))
    ra, rb = paired_log_ratio(sub), paired_log_ratio(full)
    rows.append(dict(
        measure="token_ratio_geometric", kind="paired log-ratio (n=8 scenarios)",
        n3_delta=ra["geometric_ratio"], n3_ci_low=ra["ci_low"],
        n3_ci_high=ra["ci_high"], n3_t=ra["t"],
        n10_delta=rb["geometric_ratio"], n10_ci_low=rb["ci_low"],
        n10_ci_high=rb["ci_high"], n10_t=rb["t"],
        n3_excludes_null=bool(ra["ci_low"] > 1 or ra["ci_high"] < 1),
        n10_excludes_null=bool(rb["ci_low"] > 1 or rb["ci_high"] < 1),
        n3_ci_width=ra["ci_high"] - ra["ci_low"], n10_ci_width=rb["ci_high"] - rb["ci_low"],
    ))
    for beta, label in ((1.0, "micro_F1"), (0.5, "micro_F0.5"), (2.0, "micro_F2")):
        a, b = micro_bootstrap(sub, beta), micro_bootstrap(full, beta)
        rows.append(dict(
            measure=label, kind="cluster bootstrap over scenarios (pooled micro)",
            n3_delta=a["delta"], n3_ci_low=a["ci_low"], n3_ci_high=a["ci_high"], n3_t=None,
            n10_delta=b["delta"], n10_ci_low=b["ci_low"], n10_ci_high=b["ci_high"], n10_t=None,
            n3_excludes_null=a["excludes_null"], n10_excludes_null=b["excludes_null"],
            n3_ci_width=a["ci_high"] - a["ci_low"], n10_ci_width=b["ci_high"] - b["ci_low"],
        ))
    out["paired_comparison"] = rows
    write("table7_n3_vs_n10_paired.csv", rows)

    # -- 4. what changed ----------------------------------------------------
    out["null_exclusion_changes"] = [
        dict(measure=r["measure"], n3=r["n3_excludes_null"], n10=r["n10_excludes_null"],
             changed=r["n3_excludes_null"] != r["n10_excludes_null"],
             ci_width_ratio=(r["n10_ci_width"] / r["n3_ci_width"]) if r["n3_ci_width"] else None)
        for r in rows
    ]

    # -- 5. s2_003 diagnostic (NOT a headline figure) -----------------------
    diag = {}
    for arm in ("single", "multi"):
        for label, data in (("n3", sub), ("n10", full)):
            v = [r["tokens_used"] for r in data
                 if r["scenario_id"] == "s2_003_distractor_queue" and r["architecture"] == arm]
            diag[f"{arm}_{label}"] = bimodality(v)
    # Leave-one-scenario-out ratio, reported ONLY as a robustness diagnostic.
    loo = {}
    for sid in sorted({r["scenario_id"] for r in full}):
        rest = [r for r in full if r["scenario_id"] != sid]
        loo[sid] = paired_log_ratio(rest)["geometric_ratio"]
    diag["leave_one_scenario_out_geometric_ratio"] = loo
    diag["NOTE"] = (
        "Diagnostic only. The headline ratio is the all-scenario figure from "
        "analyse_study3.py; no s2_003-excluded ratio is reported as a headline."
    )
    out["s2_003_diagnostic"] = diag

    # -- 6. survivorship ----------------------------------------------------
    surv = {}
    for arm in ("single", "multi"):
        x = [r for r in full if r["architecture"] == arm]
        fin = [r for r in x if r["termination_reason"] == "finalised"]
        contributing = len({r["run_id"] for r in x if r["scores"].get("selection_detail")})
        tp, fp, fn = micro_counts(full, arm)
        surv[arm] = dict(
            n=len(x), finalised=len(fin), completion=len(fin) / len(x),
            runs_contributing_decisions=contributing,
            micro_macro_denominator_gap=len(x) - contributing,
            micro_F1=micro_from(tp, fp, fn, 1.0),
            macro_F1=mean([r["scores"].get("selection_f1", 0) for r in x]),
            termination=dict(Counter(r["termination_reason"] for r in x)),
        )
    study2_multi = [r for r in rs2 if r["architecture"] == "multi"]
    surv["study2_multi_denominator_gap"] = 24 - len(
        {r["run_id"] for r in study2_multi if r["scores"].get("selection_detail")}
    )
    out["survivorship"] = surv

    out["tokens_n10"] = {a: dist([r["tokens_used"] for r in full if r["architecture"] == a])
                         for a in ("single", "multi")}
    out["tokens_n3"] = {a: dist([r["tokens_used"] for r in sub if r["architecture"] == a])
                        for a in ("single", "multi")}

    with (OUT / "summary_n10_extension.json").open("w") as f:
        json.dump(out, f, indent=2, default=str)

    # -- figure: CI width shrinkage ------------------------------------------
    plt.style.use("seaborn-v0_8-whitegrid")
    macro_rows = [r for r in rows if r["kind"].startswith("paired t")]
    fig, axs = plt.subplots(1, len(macro_rows), figsize=(3.1 * len(macro_rows), 4))
    for ax, r in zip(axs, macro_rows, strict=True):
        for i, (lo, hi, d) in enumerate((
            (r["n3_ci_low"], r["n3_ci_high"], r["n3_delta"]),
            (r["n10_ci_low"], r["n10_ci_high"], r["n10_delta"]),
        )):
            ax.plot([i, i], [lo, hi], lw=3, color=["#999999", "#4C72B0"][i])
            ax.plot([i], [d], "o", color="black", ms=5)
        ax.axhline(0, color="#C44E52", ls="--", lw=1)
        ax.set_xticks([0, 1], ["N=3", "N=10"])
        ax.set_title(r["measure"].replace("selection_", ""), fontsize=9)
    fig.suptitle("Paired 95% CIs, multi − single: N=3 vs N=10", fontsize=11)
    fig.tight_layout()
    fig.savefig(OUT / "fig4_ci_width_n3_vs_n10.png", dpi=200)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4.2))
    for arm, colour in (("single", "#4C72B0"), ("multi", "#DD8452")):
        v = [r["tokens_used"] for r in full
             if r["scenario_id"] == "s2_003_distractor_queue" and r["architecture"] == arm]
        ax.scatter(np.full(len(v), 0 if arm == "single" else 1), v,
                   color=colour, s=44, alpha=0.8, label=arm)
    ax.axhline(100_000, color="#C44E52", ls="--", lw=1.2, label="Study 2 cap 100k")
    ax.set_xticks([0, 1], ["single", "multi"])
    ax.set_ylabel("tokens used")
    ax.set_title("s2_003_distractor_queue at N=10 (diagnostic)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "fig5_s2_003_diagnostic.png", dpi=200)
    plt.close(fig)

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
