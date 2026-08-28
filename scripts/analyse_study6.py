"""Study 6: does the architectural null survive a more capable model?

Study 6 repeats the Study 4 matrix with exactly one variable changed —
``gpt-4.1-nano-2025-04-14`` to ``gpt-4.1-mini-2025-04-14``, the same dated
snapshot in the same family. Studies 4 and 6 therefore differ only in model
capability, which is what licenses reading any difference between them as a
capability effect.

Predictions were hash-committed before collection (``1615783``); see
``docs/planning/study6-prereg.md``.

The quantity of interest
------------------------
Not "is multi better than single at mini", but whether the *architectural
difference itself* changes with capability — the *architecture x capability
interaction*. Section 6.5 sets out two opposed predictions:

* decomposition helps weaker models **more** (specialisation substitutes for
  capability), which predicts a **negative** interaction; or
* decomposition helps weaker models **less** (coordination demands capability),
  which predicts a **positive** interaction.

P3 committed in advance to the second. The interaction is reported two ways:

1. **Difference-in-differences, paired by scenario** (n=8). For each scenario,
   ``(multi - single at mini) - (multi - single at nano)``, then a paired t.
   This matches how every other comparison in the dissertation is paired and is
   the interpretable form.
2. **A factorial F-test** in the same shape as the scenario x architecture
   interaction reported in Section 5.2. Scenario is retained as a factor
   because Section 5.2 found it accounts for 59.5% of correctness variance;
   dropping it would push that variance into the error term and make the
   interaction look more significant than it is.

Cell sizes may be unequal — Study 6's multi arm lost runs to per-cell timeouts
— so the factorial decomposition uses an unweighted-means analysis with the
harmonic mean cell size, which is the standard treatment for mild imbalance and
does not silently reweight scenarios.

Wall clock carries no verdict: Study 4 ran through machine sleep with about
1.55x dark-wake throttling, so a Study 4 to Study 6 wall-clock difference mixes
capability with that artefact. Declared in the pre-registration, reported
descriptively only.

Usage::

    python scripts/analyse_study6.py
"""

from __future__ import annotations

import csv
import glob
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from scipy.stats import f as f_dist  # noqa: E402
from scipy.stats import t as t_dist  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyse_study2 import mean  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNS4 = ROOT / "results/runs_study4"
RUNS6 = ROOT / "results/runs_study6"
OUT = ROOT / "results/analysis_study6"

SERVICES = ("postgres", "nginx", "redis", "rabbitmq")
ARMS = ("single", "multi")
LEVELS = (("nano", "gpt-4.1-nano-2025-04-14"), ("mini", "gpt-4.1-mini-2025-04-14"))

#: Pre-registered thresholds, fixed at 1615783.
P1_CORRECTNESS_RISE = 0.02
P1_F1_RISE = 0.05
P2_REAL_DIFFERENCE = 0.05
P4_RATIO_CEILING = 1.45
STUDY4_RATIO = 1.596


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


def value(run: dict, key: str) -> float | None:
    if key in ("tokens_used", "wall_clock_s"):
        return float(run.get(key, 0) or 0)
    v = run["scores"].get(key)
    return None if v is None else float(v)


def cell(rs: list[dict], sid: str, arm: str, key: str) -> list[float]:
    out = [
        value(r, key)
        for r in rs
        if r["scenario_id"] == sid and r["architecture"] == arm
    ]
    return [v for v in out if v is not None]


def paired_diff_in_diff(rs4: list[dict], rs6: list[dict], key: str) -> dict:
    """(multi - single at mini) - (multi - single at nano), paired by scenario.

    The interaction in its interpretable form. Positive means the multi-agent
    arm gained more than the single arm from the capability increase, which is
    what P3 committed to.
    """
    sids = sorted({r["scenario_id"] for r in rs4})
    per_scenario = []
    for sid in sids:
        d_nano = mean(cell(rs4, sid, "multi", key)) - mean(cell(rs4, sid, "single", key))
        d_mini = mean(cell(rs6, sid, "multi", key)) - mean(cell(rs6, sid, "single", key))
        per_scenario.append(
            dict(scenario_id=sid, delta_nano=d_nano, delta_mini=d_mini,
                 interaction=d_mini - d_nano)
        )
    d = np.array([p["interaction"] for p in per_scenario], dtype=float)
    n = len(d)
    se = d.std(ddof=1) / math.sqrt(n)
    crit = t_dist.ppf(0.975, n - 1)
    return dict(
        metric=key,
        n_scenarios=n,
        interaction=float(d.mean()),
        sd=float(d.std(ddof=1)),
        ci_low=float(d.mean() - crit * se),
        ci_high=float(d.mean() + crit * se),
        t=float(d.mean() / se) if se else 0.0,
        df=n - 1,
        excludes_null=bool((d.mean() - crit * se) > 0 or (d.mean() + crit * se) < 0),
        per_scenario=per_scenario,
    )


def factorial_interaction(rs4: list[dict], rs6: list[dict], key: str) -> dict:
    """Architecture x capability interaction F-test, scenario retained.

    Unweighted-means analysis: cell means carry equal weight and the harmonic
    mean cell size scales the sums of squares. Standard for mild imbalance, and
    it keeps every scenario contributing equally rather than letting a cell
    that lost runs to a timeout also lose influence.
    """
    sids = sorted({r["scenario_id"] for r in rs4})
    cells: dict[tuple[str, str, str], list[float]] = {}
    for cap, rs in (("nano", rs4), ("mini", rs6)):
        for sid in sids:
            for arm in ARMS:
                vals = cell(rs, sid, arm, key)
                if vals:
                    cells[(sid, arm, cap)] = vals

    sizes = [len(v) for v in cells.values()]
    n_tilde = len(sizes) / sum(1.0 / s for s in sizes)  # harmonic mean
    means = {k: float(np.mean(v)) for k, v in cells.items()}
    grand = float(np.mean(list(means.values())))

    def marginal(idx: tuple[int, ...], want: tuple[str, ...]) -> float:
        sel = [m for k, m in means.items() if tuple(k[i] for i in idx) == want]
        return float(np.mean(sel)) if sel else grand

    n_arm, n_cap, n_scen = len(ARMS), 2, len(sids)

    ss_arch = n_tilde * n_cap * n_scen * sum(
        (marginal((1,), (a,)) - grand) ** 2 for a in ARMS
    )
    ss_cap = n_tilde * n_arm * n_scen * sum(
        (marginal((2,), (c,)) - grand) ** 2 for c in ("nano", "mini")
    )
    ss_inter = n_tilde * n_scen * sum(
        (
            marginal((1, 2), (a, c))
            - marginal((1,), (a,))
            - marginal((2,), (c,))
            + grand
        )
        ** 2
        for a in ARMS
        for c in ("nano", "mini")
    )
    ss_within = sum(
        sum((v - means[k]) ** 2 for v in vals) for k, vals in cells.items()
    )
    df_within = sum(sizes) - len(cells)
    ms_within = ss_within / df_within if df_within else float("nan")

    df_inter = (n_arm - 1) * (n_cap - 1)
    f_stat = (ss_inter / df_inter) / ms_within if ms_within else float("nan")
    p = float(1 - f_dist.cdf(f_stat, df_inter, df_within)) if ms_within else float("nan")

    ss_total = ss_arch + ss_cap + ss_inter + ss_within
    return dict(
        metric=key,
        harmonic_cell_n=round(n_tilde, 3),
        cells=len(cells),
        ss_architecture=ss_arch,
        ss_capability=ss_cap,
        ss_interaction=ss_inter,
        ss_within=ss_within,
        pct_architecture=100 * ss_arch / ss_total,
        pct_capability=100 * ss_cap / ss_total,
        pct_interaction=100 * ss_inter / ss_total,
        pct_within=100 * ss_within / ss_total,
        df_interaction=df_inter,
        df_within=df_within,
        F=f_stat,
        p=p,
    )


def paired_arm(rs: list[dict], key: str) -> dict:
    """multi - single within one study, paired by scenario (n=8)."""
    sids = sorted({r["scenario_id"] for r in rs})
    d = np.array(
        [
            mean(cell(rs, s, "multi", key)) - mean(cell(rs, s, "single", key))
            for s in sids
        ],
        dtype=float,
    )
    n = len(d)
    se = d.std(ddof=1) / math.sqrt(n)
    crit = t_dist.ppf(0.975, n - 1)
    return dict(
        metric=key,
        mean_delta=float(d.mean()),
        ci_low=float(d.mean() - crit * se),
        ci_high=float(d.mean() + crit * se),
        t=float(d.mean() / se) if se else 0.0,
        df=n - 1,
        excludes_null=bool((d.mean() - crit * se) > 0 or (d.mean() + crit * se) < 0),
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


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rs4, rs6 = load(RUNS4), load(RUNS6)

    assert {r["model"] for r in rs6} == {"gpt-4.1-mini-2025-04-14"}, sorted(
        {r["model"] for r in rs6}
    )
    assert {r["model"] for r in rs4} == {"gpt-4.1-nano-2025-04-14"}
    assert {r["max_iterations"] for r in rs6} == {25}

    cell_sizes = Counter((r["scenario_id"], r["architecture"]) for r in rs6)
    incomplete = {f"{k[0]}/{k[1]}": v for k, v in cell_sizes.items() if v != 10}

    # --- 1. Headline: both arms at both capability levels ----------------
    headline = []
    for cap, rs in (("nano (Study 4)", rs4), ("mini (Study 6)", rs6)):
        for arm in ARMS:
            x = [r for r in rs if r["architecture"] == arm]
            headline.append(
                dict(
                    capability=cap,
                    arm=arm,
                    n=len(x),
                    finalised=sum(r["termination_reason"] == "finalised" for r in x),
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
                    macro_f1=round(
                        mean([r["scores"].get("selection_f1", 0) for r in x]), 4
                    ),
                    macro_precision=round(
                        mean([r["scores"].get("selection_precision", 0) for r in x]), 4
                    ),
                    macro_recall=round(
                        mean([r["scores"].get("selection_recall", 0) for r in x]), 4
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
    write("table1_headline_by_capability_and_arm.csv", headline)

    def head(cap: str, arm: str, key: str) -> float:
        return next(h[key] for h in headline if h["capability"] == cap and h["arm"] == arm)

    # --- 2. The interaction ----------------------------------------------
    did = {k: paired_diff_in_diff(rs4, rs6, k) for k in
           ("correctness", "selection_f1", "tokens_used")}
    fac = {k: factorial_interaction(rs4, rs6, k) for k in
           ("correctness", "selection_f1")}
    write(
        "table2_interaction_did.csv",
        [{k: v for k, v in d.items() if k != "per_scenario"} for d in did.values()],
    )
    write("table3_interaction_anova.csv", list(fac.values()))
    write(
        "table4_interaction_by_scenario.csv",
        [
            dict(metric=m, **p)
            for m, d in did.items()
            for p in d["per_scenario"]
        ],
    )

    # --- 2b. Finalised-only sensitivity ----------------------------------
    # Macro metrics score a run that produced no spec as 0, which is the
    # convention Studies 2-4 use. When one arm's completion rate moves sharply
    # that convention conflates two different failures: selecting badly, and
    # not finishing. Study 2 and Study 3 both carried a finalised-only
    # sensitivity for exactly this reason, so it is repeated here rather than
    # invented — without it, "multi got worse" cannot be separated into "multi
    # fails more often" and "multi selects worse when it does finish".
    sensitivity = []
    for cap, rs in (("nano (Study 4)", rs4), ("mini (Study 6)", rs6)):
        for arm in ARMS:
            x = [r for r in rs if r["architecture"] == arm]
            fin = [r for r in x if r["termination_reason"] == "finalised"]
            sensitivity.append(
                dict(
                    capability=cap,
                    arm=arm,
                    n_all=len(x),
                    n_finalised=len(fin),
                    completion=round(len(fin) / len(x), 4) if x else 0.0,
                    macro_f1_all=round(
                        mean([r["scores"].get("selection_f1", 0) for r in x]), 4
                    ),
                    macro_f1_finalised_only=round(
                        mean([r["scores"].get("selection_f1", 0) for r in fin]) or 0, 4
                    ),
                    correctness_finalised_only=round(
                        mean(
                            [
                                r["scores"]["correctness"]
                                for r in fin
                                if "correctness" in r["scores"]
                            ]
                        )
                        or 0,
                        4,
                    ),
                )
            )
    write("table2b_finalised_only_sensitivity.csv", sensitivity)

    # --- 3. Within-study paired tests ------------------------------------
    within = []
    for label, rs in (("nano (Study 4)", rs4), ("mini (Study 6)", rs6)):
        for key in ("correctness", "selection_f1", "tokens_used"):
            within.append(dict(capability=label, **paired_arm(rs, key)))
    write("table5_within_study_paired.csv", within)

    # --- 4. Per-service confusion ----------------------------------------
    confusion = []
    for svc in SERVICES:
        for cap, rs in (("nano", rs4), ("mini", rs6)):
            for arm in ARMS:
                tp, fp, fn, tn = svc_counts(rs, arm, svc)
                confusion.append(
                    dict(
                        service=svc,
                        capability=cap,
                        arm=arm,
                        TP=tp, FP=fp, FN=fn, TN=tn,
                        precision=round(tp / (tp + fp), 4) if (tp + fp) else 0.0,
                        recall=round(tp / (tp + fn), 4) if (tp + fn) else 0.0,
                    )
                )
    write("table6_per_service_confusion.csv", confusion)

    # --- 5. Failures (P5) -------------------------------------------------
    failures = []
    for cap, rs in (("nano", rs4), ("mini", rs6)):
        for arm in ARMS:
            x = [r for r in rs if r["architecture"] == arm]
            reasons = Counter(r["termination_reason"] for r in x)
            failures.append(
                dict(
                    capability=cap,
                    arm=arm,
                    n=len(x),
                    finalised=reasons.get("finalised", 0),
                    max_iterations=reasons.get("max_iterations", 0),
                    budget_exhausted=reasons.get("budget_exhausted", 0),
                    other=len(x) - sum(
                        reasons.get(k, 0)
                        for k in ("finalised", "max_iterations", "budget_exhausted")
                    ),
                    runs_over_100k_tokens=sum(1 for r in x if r["tokens_used"] >= 100_000),
                )
            )
    write("table7_failures.csv", failures)

    # --- 6. Verdicts ------------------------------------------------------
    ratio_nano = head("nano (Study 4)", "multi", "mean_tokens") / head(
        "nano (Study 4)", "single", "mean_tokens"
    )
    ratio_mini = head("mini (Study 6)", "multi", "mean_tokens") / head(
        "mini (Study 6)", "single", "mean_tokens"
    )

    p1_corr = all(
        head("mini (Study 6)", a, "correctness") - head("nano (Study 4)", a, "correctness")
        >= P1_CORRECTNESS_RISE
        for a in ARMS
    )
    p1_f1 = all(
        head("mini (Study 6)", a, "macro_f1") - head("nano (Study 4)", a, "macro_f1")
        >= P1_F1_RISE
        for a in ARMS
    )

    mini_f1 = paired_arm(rs6, "selection_f1")
    mini_corr = paired_arm(rs6, "correctness")
    p2_real = (
        abs(mini_f1["mean_delta"]) >= P2_REAL_DIFFERENCE and mini_f1["excludes_null"]
    ) or (
        abs(mini_corr["mean_delta"]) >= P2_REAL_DIFFERENCE and mini_corr["excludes_null"]
    )

    p3_positive = did["correctness"]["interaction"] > 0
    nonfinal_6 = sum(1 for r in rs6 if r["termination_reason"] != "finalised")

    verdicts = [
        dict(
            prediction="P1",
            statement="both arms improve in absolute terms on the larger model",
            rule=f"correctness +{P1_CORRECTNESS_RISE} in both arms, or macro F1 +{P1_F1_RISE} in both",
            observed="; ".join(
                f"{a}: correctness {head('nano (Study 4)', a, 'correctness'):.4f}->"
                f"{head('mini (Study 6)', a, 'correctness'):.4f}, "
                f"F1 {head('nano (Study 4)', a, 'macro_f1'):.4f}->"
                f"{head('mini (Study 6)', a, 'macro_f1'):.4f}"
                for a in ARMS
            ),
            held="HELD" if (p1_corr or p1_f1) else "FAILED",
        ),
        dict(
            prediction="P2",
            statement="the architectural difference remains null at the higher capability",
            rule=f"a real difference = |paired delta| >= {P2_REAL_DIFFERENCE} with CI excluding 0",
            observed=(
                f"mini macro F1 {mini_f1['mean_delta']:+.4f} "
                f"CI [{mini_f1['ci_low']:+.4f}, {mini_f1['ci_high']:+.4f}]; "
                f"correctness {mini_corr['mean_delta']:+.4f} "
                f"CI [{mini_corr['ci_low']:+.4f}, {mini_corr['ci_high']:+.4f}]"
            ),
            held="HELD (null survives)" if not p2_real else "FAILED (real difference)",
        ),
        dict(
            prediction="P3",
            statement="multi gains MORE than single (coordination demands capability)",
            rule="architecture x capability interaction on correctness is positive",
            observed=(
                f"interaction {did['correctness']['interaction']:+.4f} "
                f"CI [{did['correctness']['ci_low']:+.4f}, {did['correctness']['ci_high']:+.4f}], "
                f"F({fac['correctness']['df_interaction']},{fac['correctness']['df_within']})="
                f"{fac['correctness']['F']:.2f}, p={fac['correctness']['p']:.3f}"
            ),
            held="HELD" if p3_positive else "FAILED (direction reversed)",
        ),
        dict(
            prediction="P4",
            statement="the cost ratio narrows",
            rule=f"multi/single token ratio falls below {P4_RATIO_CEILING} (Study 4: {STUDY4_RATIO})",
            observed=f"nano {ratio_nano:.3f}x -> mini {ratio_mini:.3f}x",
            held="HELD" if ratio_mini < P4_RATIO_CEILING else "FAILED",
        ),
        dict(
            prediction="P5",
            statement="capability-dependent failures fall to zero",
            rule="zero non-finalised terminations across all Study 6 runs",
            observed=(
                f"{nonfinal_6} non-finalised of {len(rs6)} "
                f"(Study 4: {sum(1 for r in rs4 if r['termination_reason'] != 'finalised')} of {len(rs4)})"
            ),
            held="HELD" if nonfinal_6 == 0 else "FAILED",
        ),
    ]
    write("table8_verdicts.csv", verdicts)

    summary = dict(
        registered="docs/planning/study6-prereg.md (sha256 at 1615783)",
        manipulated_variable="model capability: gpt-4.1-nano -> gpt-4.1-mini (same 2025-04-14 snapshot)",
        n_study6=len(rs6),
        incomplete_cells=incomplete or "none",
        headline=headline,
        interaction_did={k: {kk: vv for kk, vv in v.items() if kk != "per_scenario"}
                         for k, v in did.items()},
        interaction_anova=fac,
        within_study_paired=within,
        cost_ratio=dict(nano=round(ratio_nano, 3), mini=round(ratio_mini, 3)),
        failures=failures,
        wall_clock_caveat=(
            "Study 4 ran through macOS maintenance sleep with ~1.55x dark-wake "
            "throttling; wall-clock comparisons across studies are confounded and "
            "carry no verdict. Declared in the pre-registration."
        ),
        verdicts=verdicts,
    )
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    # --- Figures ---------------------------------------------------------
    plt.style.use("seaborn-v0_8-whitegrid")
    colours = {"single": "#4C72B0", "multi": "#DD8452"}
    caps = ["nano (Study 4)", "mini (Study 6)"]

    fig, axs = plt.subplots(1, 3, figsize=(13, 4))
    for ax, (key, label) in zip(
        axs,
        (("correctness", "correctness"), ("macro_f1", "macro selection F1"),
         ("mean_tokens", "mean tokens")),
        strict=True,
    ):
        x = np.arange(2)
        for i, arm in enumerate(ARMS):
            ax.plot(x, [head(c, arm, key) for c in caps], marker="o",
                    color=colours[arm], label=arm, lw=2)
        ax.set_xticks(x, ["nano", "mini"])
        ax.set_title(label)
        ax.set_xlabel("model capability")
    axs[0].legend()
    fig.suptitle("Architecture x capability: non-parallel lines are the interaction")
    fig.tight_layout()
    fig.savefig(OUT / "fig1_interaction.png", dpi=200)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(["nano", "mini"], [ratio_nano, ratio_mini], color="#4C72B0")
    ax.axhline(P4_RATIO_CEILING, ls="--", color="black",
               label=f"P4 threshold {P4_RATIO_CEILING}")
    ax.set_ylabel("multi / single token ratio")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "fig2_cost_ratio.png", dpi=200)
    plt.close(fig)

    print(json.dumps(summary, indent=2, default=str))
    print("\n" + "=" * 72)
    for v in verdicts:
        print(f"  {v['prediction']:4} {v['held'][:24]:26} {v['statement'][:44]}")
    print("=" * 72)


if __name__ == "__main__":
    main()
