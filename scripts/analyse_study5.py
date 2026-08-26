"""Study 5: a parallel per-service architecture, against Study 4's two arms.

Exploratory third arm, built after Studies 1-4 were complete to answer the
objection Section 6.2 raises and cannot settle: that the dissertation's null
result might be an artefact of having compared a single agent against only a
*star* topology, in which functional workers run in sequence and never address
one another. Study 5 splits by **service** instead of by function and runs the
parts **concurrently**, which is the arrangement the multi-agent literature's
stronger claims rest on.

Predictions were hash-committed before collection (``d982366``), with one
logged amendment for retry parity (``e6136b7``); see
``docs/planning/study5-prereg.md`` and ``study5-amendment-1.md``.

Comparison arms are **Study 4's** single and multi, never Study 3's, because
Study 4 and Study 5 share the rebuilt 31-document index and Study 3 does not.

A confound this script handles explicitly
-----------------------------------------
Study 4 was collected across a night of macOS maintenance sleep; Study 5 ran
with none (elapsed/wall-clock ratios 1.98 and 1.00). ``wall_clock_s`` is
measured with ``time.monotonic()``, which does not advance during sleep, so it
is not inflated by sleep itself — but Study 4's runs were additionally slowed
about 1.55x by dark-wake throttling, measured in the Study 4 findings. A raw
wall-clock comparison therefore **flatters the parallel arm**.

P2's wall-clock half is consequently reported with a sensitivity bound: the
Study 4 arms' wall clock is also deflated by the throttling factor, and the
prediction is treated as supported only if its direction survives that
adjustment. Token and quality comparisons carry no such contamination.

Usage::

    python scripts/analyse_study5.py
"""

from __future__ import annotations

import csv
import glob
import json
import math
import sys
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from scipy.stats import t  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reused verbatim so metric definitions cannot drift between studies.
from analyse_study2 import fbeta, mean, role  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNS4 = ROOT / "results/runs_study4"
RUNS5 = ROOT / "results/runs_study5"
OUT = ROOT / "results/analysis_study5"

SERVICES = ("postgres", "nginx", "redis", "rabbitmq")

#: Dark-wake throttling measured in the Study 4 findings: Study 4's mean
#: recorded wall clock was 1.55x Study 3's while consuming fewer tokens.
#: Used only for the P2 sensitivity bound, never to restate Study 4.
STUDY4_THROTTLE_FACTOR = 1.55

#: Pre-registered thresholds, fixed at d982366. Changing them invalidates the
#: pre-registration, which is why they are constants rather than inline.
P1_IMPROVEMENT_DELTA = 0.05
P3_RECALL_BAND = (0.00, 0.20)
P4_ARBITRATION_RATE = 0.50

#: Services that actually reserve host memory, so "runs with two or more
#: memory-reserving services" — P4's denominator — is unambiguous.
RESERVING = ("postgres", "redis", "rabbitmq")


def load(directory: Path) -> list[dict]:
    return [
        json.loads(Path(p).read_text())
        for p in glob.glob(str(directory / "**/*.json"), recursive=True)
        if Path(p).name != "matrix_progress.jsonl"
    ]


def write(name: str, rows_: list[dict]) -> None:
    if not rows_:
        return
    with (OUT / name).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_[0]))
        w.writeheader()
        w.writerows(rows_)


def _value(run: dict, key: str) -> float:
    if key in ("tokens_used", "wall_clock_s"):
        return float(run.get(key, 0) or 0)
    return float(run["scores"].get(key, 0) or 0)


def paired_across(
    rs_a: list[dict],
    arm_a: str,
    rs_b: list[dict],
    arm_b: str,
    key: str,
    scale_b: float = 1.0,
) -> dict:
    """Paired scenario-level test of arm A minus arm B.

    Cross-dataset, so it cannot reuse ``analyse_study2.paired`` (which assumes
    both arms live in one list). The pairing unit is the scenario, matching
    every other paired test in this dissertation, and n=8 throughout.

    ``scale_b`` divides arm B's per-run values before differencing. It exists
    solely for the P2 wall-clock sensitivity bound and defaults to 1.0.
    """
    sids = sorted({r["scenario_id"] for r in rs_a})
    deltas = []
    for sid in sids:
        a = mean(
            [
                _value(r, key)
                for r in rs_a
                if r["scenario_id"] == sid and r["architecture"] == arm_a
            ]
        )
        b = mean(
            [
                _value(r, key) / scale_b
                for r in rs_b
                if r["scenario_id"] == sid and r["architecture"] == arm_b
            ]
        )
        deltas.append(a - b)
    d = np.array(deltas, dtype=float)
    n = len(d)
    se = d.std(ddof=1) / math.sqrt(n)
    crit = t.ppf(0.975, n - 1)
    return dict(
        metric=key,
        n_scenarios=n,
        mean_delta=float(d.mean()),
        sd=float(d.std(ddof=1)),
        ci_low=float(d.mean() - crit * se),
        ci_high=float(d.mean() + crit * se),
        t=float(d.mean() / se) if se else 0.0,
        df=n - 1,
        excludes_null=bool(
            (d.mean() - crit * se) > 0 or (d.mean() + crit * se) < 0
        ),
    )


def svc_counts(rs: list[dict], arm: str, svc: str) -> tuple[int, int, int, int]:
    c = Counter(
        d["verdict"]
        for r in rs
        if r["architecture"] == arm
        for d in r["scores"].get("selection_detail", [])
        if d["service"] == svc
    )
    return (
        c["true_positive"],
        c["false_positive"],
        c["false_negative"],
        c["true_negative"],
    )


def merge_entry(run: dict) -> dict:
    for entry in run.get("history") or []:
        if entry.get("worker") == "merge" and "arbitration" in entry:
            return entry
    return {}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rs5, rs4 = load(RUNS5), load(RUNS4)

    assert len(rs5) == 80, f"expected 80 Study 5 runs, found {len(rs5)}"
    assert {r["architecture"] for r in rs5} == {"parallel"}
    assert {r["model"] for r in rs5} == {"gpt-4.1-nano-2025-04-14"}

    arms = [("parallel", rs5), ("single", rs4), ("multi", rs4)]

    # --- 1. Headline per arm --------------------------------------------
    headline = []
    for name, rs in arms:
        x = [r for r in rs if r["architecture"] == name]
        headline.append(
            dict(
                arm=name,
                study="Study 5" if name == "parallel" else "Study 4",
                n=len(x),
                finalised=sum(r["termination_reason"] == "finalised" for r in x),
                macro_f1=round(mean([r["scores"].get("selection_f1", 0) for r in x]), 4),
                macro_precision=round(
                    mean([r["scores"].get("selection_precision", 0) for r in x]), 4
                ),
                macro_recall=round(
                    mean([r["scores"].get("selection_recall", 0) for r in x]), 4
                ),
                exact_match=round(
                    mean([r["scores"].get("selection_exact_match", 0) for r in x]), 4
                ),
                correctness=round(
                    mean(
                        [
                            r["scores"]["correctness"]
                            for r in x
                            if "correctness" in r["scores"]
                        ]
                    )
                    or 0,
                    4,
                ),
                cis_actionable=round(
                    mean(
                        [
                            r["scores"]["cis_pass_rate_actionable"]
                            for r in x
                            if "cis_pass_rate_actionable" in r["scores"]
                        ]
                    )
                    or 0,
                    4,
                ),
                mean_tokens=round(mean([r["tokens_used"] for r in x])),
                mean_wall_clock_s=round(mean([r["wall_clock_s"] for r in x]), 1),
            )
        )
    write("table1_headline_by_arm.csv", headline)

    # --- 2. Three-way per-service confusion ------------------------------
    confusion = []
    for svc in SERVICES:
        for name, rs in arms:
            tp, fp, fn, tn = svc_counts(rs, name, svc)
            confusion.append(
                dict(
                    service=svc,
                    arm=name,
                    TP=tp,
                    FP=fp,
                    FN=fn,
                    TN=tn,
                    precision=round(tp / (tp + fp), 4) if (tp + fp) else 0.0,
                    recall=round(tp / (tp + fn), 4) if (tp + fn) else 0.0,
                )
            )
    write("table2_per_service_confusion_threeway.csv", confusion)

    # --- 3. Paired tests, parallel vs each Study 4 arm -------------------
    metrics = (
        "selection_f1",
        "selection_precision",
        "selection_recall",
        "correctness",
        "cis_pass_rate_actionable",
        "tokens_used",
        "wall_clock_s",
    )
    paired_rows = []
    paired_lookup: dict[tuple[str, str], dict] = {}
    for other in ("single", "multi"):
        for key in metrics:
            res = paired_across(rs5, "parallel", rs4, other, key)
            paired_lookup[(other, key)] = res
            paired_rows.append(dict(comparison=f"parallel - {other}", **res))
    # P2 sensitivity: deflate Study 4's wall clock by the throttling factor.
    for other in ("single", "multi"):
        res = paired_across(
            rs5, "parallel", rs4, other, "wall_clock_s", scale_b=STUDY4_THROTTLE_FACTOR
        )
        paired_lookup[(other, "wall_clock_s_adj")] = res
        paired_rows.append(
            dict(comparison=f"parallel - {other} (wall clock, throttle-adjusted)", **res)
        )
    write("table3_paired_vs_study4.csv", paired_rows)

    # --- 4. Arbitration (P4) --------------------------------------------
    arb_rows = []
    multi_service_runs = 0
    arbitrated_runs = 0
    for r in rs5:
        entry = merge_entry(r)
        if not entry:
            continue
        arb = entry.get("arbitration", {})
        reserving = [s for s in RESERVING if s in (r.get("services_deployed") or [])]
        is_multi = len(reserving) >= 2
        if is_multi:
            multi_service_runs += 1
            if arb.get("arbitrated"):
                arbitrated_runs += 1
        arb_rows.append(
            dict(
                run_id=r["run_id"],
                scenario_id=r["scenario_id"],
                role=role(r["scenario_id"]),
                services_deployed=",".join(r.get("services_deployed") or []),
                reserving_services=len(reserving),
                counts_for_p4=is_multi,
                arbitrated=bool(arb.get("arbitrated")),
                requested_fraction_of_ram=arb.get("requested_fraction_of_ram"),
                scale_factor=arb.get("scale_factor"),
                fan_out_wall_clock_s=(entry.get("concurrency") or {}).get(
                    "fan_out_wall_clock_s"
                ),
                sum_agent_wall_clock_s=(entry.get("concurrency") or {}).get(
                    "sum_agent_wall_clock_s"
                ),
                agents=(entry.get("concurrency") or {}).get("agents"),
            )
        )
    write("table4_arbitration.csv", arb_rows)

    arb_rate = arbitrated_runs / multi_service_runs if multi_service_runs else 0.0

    # --- 5. Concurrency realised ----------------------------------------
    speedups = [
        row["sum_agent_wall_clock_s"] / row["fan_out_wall_clock_s"]
        for row in arb_rows
        if row["agents"]
        and row["agents"] > 1
        and row["fan_out_wall_clock_s"]
    ]

    # --- 6. Verdicts -----------------------------------------------------
    f1_vs_single = paired_lookup[("single", "selection_f1")]
    corr_vs_single = paired_lookup[("single", "correctness")]
    p1_improved = (
        f1_vs_single["mean_delta"] >= P1_IMPROVEMENT_DELTA
        and f1_vs_single["excludes_null"]
    ) or (
        corr_vs_single["mean_delta"] >= P1_IMPROVEMENT_DELTA
        and corr_vs_single["excludes_null"]
    )

    tok_vs_multi = paired_lookup[("multi", "tokens_used")]
    wc_vs_multi = paired_lookup[("multi", "wall_clock_s")]
    wc_vs_multi_adj = paired_lookup[("multi", "wall_clock_s_adj")]
    p2_tokens_held = tok_vs_multi["mean_delta"] > 0 and tok_vs_multi["excludes_null"]
    p2_wall_held = (
        wc_vs_multi["mean_delta"] < 0
        and wc_vs_multi["excludes_null"]
        and wc_vs_multi_adj["mean_delta"] < 0
        and wc_vs_multi_adj["excludes_null"]
    )

    tp, _, fn, _ = svc_counts(rs5, "parallel", "rabbitmq")
    rmq_recall = tp / (tp + fn) if (tp + fn) else 0.0
    p3_held = P3_RECALL_BAND[0] <= rmq_recall <= P3_RECALL_BAND[1]

    verdicts = [
        dict(
            prediction="P1",
            statement="parallel does NOT improve correctness over single",
            rule=f"improvement = macro F1 or correctness delta >= +{P1_IMPROVEMENT_DELTA} with CI excluding 0",
            observed=(
                f"macro F1 {f1_vs_single['mean_delta']:+.4f} "
                f"CI [{f1_vs_single['ci_low']:+.4f}, {f1_vs_single['ci_high']:+.4f}]; "
                f"correctness {corr_vs_single['mean_delta']:+.4f} "
                f"CI [{corr_vs_single['ci_low']:+.4f}, {corr_vs_single['ci_high']:+.4f}]"
            ),
            held="HELD" if not p1_improved else "FAILED",
        ),
        dict(
            prediction="P2a",
            statement="parallel uses MORE tokens than the star arm",
            rule="paired delta > 0 with CI excluding 0",
            observed=(
                f"{tok_vs_multi['mean_delta']:+,.0f} tokens "
                f"CI [{tok_vs_multi['ci_low']:+,.0f}, {tok_vs_multi['ci_high']:+,.0f}]"
            ),
            held="HELD" if p2_tokens_held else "FAILED",
        ),
        dict(
            prediction="P2b",
            statement="parallel uses LESS wall-clock than the star arm",
            rule="paired delta < 0 with CI excluding 0, AND surviving throttle adjustment",
            observed=(
                f"raw {wc_vs_multi['mean_delta']:+.1f}s "
                f"CI [{wc_vs_multi['ci_low']:+.1f}, {wc_vs_multi['ci_high']:+.1f}]; "
                f"throttle-adjusted {wc_vs_multi_adj['mean_delta']:+.1f}s "
                f"CI [{wc_vs_multi_adj['ci_low']:+.1f}, {wc_vs_multi_adj['ci_high']:+.1f}]"
            ),
            held="HELD" if p2_wall_held else "FAILED",
        ),
        dict(
            prediction="P3",
            statement=f"RabbitMQ recall resembles the other arms ({P3_RECALL_BAND[0]:.2f}-{P3_RECALL_BAND[1]:.2f})",
            rule="diagnostic, not a hypothesis",
            observed=f"parallel RabbitMQ recall {rmq_recall:.4f} (TP={tp}, FN={fn})",
            held="IN BAND" if p3_held else "OUT OF BAND",
        ),
        dict(
            prediction="P4",
            statement=f"arbitration reduces requests in >{P4_ARBITRATION_RATE:.0%} of multi-reserving runs",
            rule="runs deploying >=2 memory-reserving services",
            observed=(
                f"{arbitrated_runs}/{multi_service_runs} = {arb_rate:.1%}"
                if multi_service_runs
                else "no qualifying runs"
            ),
            held="HELD" if arb_rate > P4_ARBITRATION_RATE else "FAILED",
        ),
    ]
    write("table5_verdicts.csv", verdicts)

    summary = dict(
        registered="docs/planning/study5-prereg.md (sha256 at d982366); amendment 1 at e6136b7",
        exploratory=True,
        headline=headline,
        paired=paired_rows,
        arbitration=dict(
            multi_reserving_runs=multi_service_runs,
            arbitrated_runs=arbitrated_runs,
            rate=round(arb_rate, 4),
        ),
        concurrency=dict(
            runs_with_multiple_agents=len(speedups),
            median_speedup=round(float(np.median(speedups)), 3) if speedups else None,
            min_speedup=round(min(speedups), 3) if speedups else None,
            max_speedup=round(max(speedups), 3) if speedups else None,
        ),
        wall_clock_confound=dict(
            note=(
                "Study 4 ran through macOS maintenance sleep and was additionally "
                "slowed ~1.55x by dark-wake throttling; Study 5 had zero sleep "
                "events. Raw wall-clock comparisons flatter parallel, so P2b "
                "requires the throttle-adjusted result to agree."
            ),
            throttle_factor=STUDY4_THROTTLE_FACTOR,
        ),
        termination=dict(Counter(r["termination_reason"] for r in rs5)),
        verdicts=verdicts,
    )
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    # --- Figures ---------------------------------------------------------
    plt.style.use("seaborn-v0_8-whitegrid")
    colours = {"single": "#4C72B0", "multi": "#DD8452", "parallel": "#55A868"}
    names = ["single", "multi", "parallel"]

    fig, axs = plt.subplots(1, 3, figsize=(13, 4))
    for ax, (key, label) in zip(
        axs,
        (
            ("macro_f1", "macro selection F1"),
            ("mean_tokens", "mean tokens"),
            ("mean_wall_clock_s", "mean wall clock (s)"),
        ),
        strict=True,
    ):
        vals = [next(h[key] for h in headline if h["arm"] == n) for n in names]
        ax.bar(names, vals, color=[colours[n] for n in names])
        ax.set_title(label)
        if key == "mean_wall_clock_s":
            ax.set_xlabel("Study 4 arms inflated ~1.55x by throttling")
    fig.tight_layout()
    fig.savefig(OUT / "fig1_threeway_headline.png", dpi=200)
    plt.close(fig)

    fig, axs = plt.subplots(1, 4, figsize=(14, 3.6), sharey=True)
    for ax, svc in zip(axs, SERVICES, strict=True):
        vals = [
            (lambda c: c[0] / (c[0] + c[2]) if (c[0] + c[2]) else 0.0)(
                svc_counts(rs, n, svc)
            )
            for n, rs in arms
        ]
        ax.bar(names, vals, color=[colours[n] for n in names])
        ax.set_title(svc)
    axs[0].set_ylabel("recall")
    fig.tight_layout()
    fig.savefig(OUT / "fig2_per_service_recall_threeway.png", dpi=200)
    plt.close(fig)

    print(json.dumps(summary, indent=2, default=str))
    print("\n" + "=" * 70)
    for v in verdicts:
        print(f"  {v['prediction']:4} {v['held']:12} {v['statement'][:60]}")
    print("=" * 70)


if __name__ == "__main__":
    main()
