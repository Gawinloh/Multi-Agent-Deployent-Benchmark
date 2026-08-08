"""Analysis of Study 3 — the budget-sensitivity replication of Study 2.

PRE-REGISTRATION. This docstring was committed BEFORE any Study 3 run was
launched, so the analysis below is a test of stated predictions rather
than a search through the data for a story. Where a prediction fails, the
failure is the finding and is reported as such. (``docs/`` is gitignored
in this repository — it lives in a separate private repo — so the script
docstring is the version-controlled record.)

Registered 2026-08-08 at source commit 98dca8b, whose tracked ``src/``,
``benchmark/`` and ``docker/`` were verified byte-identical to the state
Study 2 ran at.

Design: identical matrix to Study 2 — the 8 Study 2 scenarios x 2
architectures x N=3 = 48 runs, gpt-4.1-nano-2025-04-14, max_iterations
25. The single manipulated variable is the per-run token cap, raised from
100,000 to 300,000.

Motivation
----------
Study 2's headline token ratio of 1.86x is right-censored: 3 of 24 multi
runs terminated ``budget_exhausted`` at the 100k cap, 5 were >= 90k, and
single's largest run reached 89,997. Both arms were pressing the ceiling,
so 1.86x is a lower bound on the cost of decomposition, not an estimate
of it.

Pre-registered predictions (verbatim)
-------------------------------------
  P1. Multi completes 24/24 at the raised cap. The Study 2 reliability
      gap was a budget artefact.
  P2. The uncensored token ratio EXCEEDS 1.86x, because the three
      truncated runs were cut short of their true consumption.
  P3. Selection quality remains indistinguishable between arms. Among
      Study 2's completed runs the paired macro F1 delta was +0.0006
      (t(4)=0.05). If this moves substantially, STOP and report it rather
      than explaining it away.
  P4 (falsifier). If multi still exhausts the raised budget, the config
      worker is looping rather than merely slow, which is an
      architectural finding about unbounded delegation, not a tuning
      problem.

Analysis plan, fixed in advance
-------------------------------
1. CENSORING CHECK FIRST. Runs per arm at the 300k cap. Zero is what
   licenses calling the estimate uncensored; any non-zero count means the
   ratio remains a bound and must be labelled one.
2. Token distributions per arm (min, median, max, mean, sd); uncensored
   ratio with a paired-by-scenario CI (n=8); comparison against Study 2's
   censored 1.86x and Study 1's 1.88x, each labelled bound or estimate.
3. Completion per arm; Fisher exact against Study 2's 24/24 and 21/24.
4. The full Study 2 selection analysis repeated at the new budget: macro
   and micro F1, micro F-beta at beta in {0.5, 1, 2} with the crossover,
   paired tests (n=8) for F1 / precision / recall / tokens, and the
   completed-runs-only sensitivity version.
5. Verdict table for P1-P4: prediction, observed result, held or failed.
6. Figures: token distribution per arm with both caps marked; token ratio
   across all three studies; F-beta curves for Study 2 vs Study 3.

Frozen datasets
---------------
Study 1 (``results/runs``, tag ``dataset-nano-n3``) and Study 2
(``results/runs_study2``, tag ``dataset-study2-n3``) are frozen. Study 3
writes only to ``results/runs_study3``. ``--resume`` is not used:
``existing_run_count()`` counts any run carrying a ``git_commit``
regardless of model or budget, so a physically separate output directory
is the safeguard against cross-dataset contamination.

N=10 EXTENSION — pre-commitment, recorded before the additional runs
--------------------------------------------------------------------
Committed before launching the extension from N=3 to N=10 per cell.

Motivation is precision, not a new question. Within-cell coefficient of
variation at N=3 was median 0.44, max 0.88, so cell means were poorly
estimated and the standard error of every paired-by-scenario test was
inflated. Study 3's token ratio (1.54x, paired CI [0.93, 2.81],
t(7)=2.05) lost significance relative to Study 2 while the point estimate
stayed in the same region.

Terms, fixed in advance:

- **N is FIXED at 10 per cell.** Collection runs to completion regardless
  of interim results. No optional stopping.
- **No new hypotheses.** The predictions registered above at 69066b6
  stand unchanged. P2 already failed and stays reported as failed. The
  extension re-estimates the same quantities with more data; it does not
  introduce a new test.
- **This extends Study 3.** It does not supersede Study 2 or Study 1,
  both of which remain reported exactly as collected. Tag
  ``dataset-study3-n3`` continues to point at the 48-run state so the
  N=3 result stays citable; ``dataset-study3-n10`` marks the 160-run
  state.

The first 48 runs are retained and pooled, not discarded: they were
collected under identical source (98dca8b), model
(gpt-4.1-nano-2025-04-14), budget (300,000) and max_iterations (25).
``--resume`` tops each cell up from 3 to 10.

Note on provenance: ``RunResult`` does not persist the token budget, so
the 300,000 cap is verifiable only from the batch log
(``budget=300000`` on all 16 cell launches) and from the absence of any
run above the cap. Recorded here as a known gap.

Usage::

    python scripts/analyse_study3.py
"""

from __future__ import annotations

# Analysis implementation follows collection; see the plan above.
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
from scipy.stats import fisher_exact, t  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reuse Study 2's helpers verbatim so metric definitions cannot drift
# between the two analyses.
from analyse_study2 import fbeta, mean, paired, role  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNS3 = ROOT / "results/runs_study3"
RUNS2 = ROOT / "results/runs_study2"
OUT = ROOT / "results/analysis_study3"
SERVICES = ("postgres", "nginx", "redis", "rabbitmq")

CAP_STUDY2 = 100_000
CAP_STUDY3 = 300_000
STUDY1_RATIO = 1.88  # censored: Study 1 ran at the same 100k cap


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


def dist(values: list[float]) -> dict:
    a = np.array(values, dtype=float)
    return dict(
        n=len(a),
        min=float(a.min()),
        median=float(np.median(a)),
        max=float(a.max()),
        mean=float(a.mean()),
        sd=float(a.std(ddof=1)),
    )


def censoring(rs: list[dict], cap: int) -> dict:
    """Runs that hit the cap, by both available signals.

    tokens_used can land slightly above the cap: the budget is checked
    after a call's usage is recorded, so the call that trips it is
    counted in full. Study 2's largest multi run was 101,125 against a
    100,000 cap.
    """
    out = {}
    for arm in ("single", "multi"):
        x = [r for r in rs if r["architecture"] == arm]
        exhausted = [r for r in x if r["termination_reason"] == "budget_exhausted"]
        at_cap = [r for r in x if r["tokens_used"] >= cap]
        near = [r for r in x if r["tokens_used"] >= 0.9 * cap]
        out[arm] = dict(
            n=len(x),
            budget_exhausted=len(exhausted),
            tokens_at_or_above_cap=len(at_cap),
            tokens_within_10pct_of_cap=len(near),
            max_tokens=max(r["tokens_used"] for r in x),
            headroom_tokens=cap - max(r["tokens_used"] for r in x),
        )
    out["censored"] = any(
        out[a]["budget_exhausted"] or out[a]["tokens_at_or_above_cap"]
        for a in ("single", "multi")
    )
    return out


def paired_log_ratio(rs: list[dict]) -> dict:
    """Paired-by-scenario CI on the multi/single token ratio.

    A ratio is CI'd on the log scale and exponentiated back, which keeps
    the interval multiplicative and strictly positive. The point estimate
    is therefore a geometric mean of per-scenario ratios and will differ
    slightly from the aggregate mean/mean figure; both are reported.
    """
    logs = []
    for sid in sorted({r["scenario_id"] for r in rs}):
        here = [r for r in rs if r["scenario_id"] == sid]
        a = mean([r["tokens_used"] for r in here if r["architecture"] == "single"])
        b = mean([r["tokens_used"] for r in here if r["architecture"] == "multi"])
        logs.append(math.log(b / a))
    d = np.array(logs)
    n = len(d)
    se = d.std(ddof=1) / math.sqrt(n)
    crit = t.ppf(0.975, n - 1)
    return dict(
        n_scenarios=n,
        geometric_ratio=float(np.exp(d.mean())),
        ci_low=float(np.exp(d.mean() - crit * se)),
        ci_high=float(np.exp(d.mean() + crit * se)),
        t=float(d.mean() / se) if se else 0.0,
        df=n - 1,
    )


def micro(rs: list[dict], arm: str) -> dict:
    x = [r for r in rs if r["architecture"] == arm]
    c = Counter(
        d["verdict"] for r in x for d in r["scores"].get("selection_detail", [])
    )
    tp, fp, fn = c["true_positive"], c["false_positive"], c["false_negative"]
    p = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    return dict(
        TP=tp, FP=fp, FN=fn, TN=c["true_negative"],
        precision=p, recall=rec,
        F1=fbeta(p, rec, 1), F05=fbeta(p, rec, 0.5), F2=fbeta(p, rec, 2),
        zero_decision_runs=len(x)
        - len({r["run_id"] for r in x if r["scores"].get("selection_detail")}),
    )


def macro(rs: list[dict], arm: str, finalised_only: bool = False) -> dict:
    x = [r for r in rs if r["architecture"] == arm]
    if finalised_only:
        x = [r for r in x if r["termination_reason"] == "finalised"]
    keys = (
        "selection_exact_match",
        "selection_precision",
        "selection_recall",
        "selection_f1",
    )
    return dict({k: mean([r["scores"].get(k, 0) for r in x]) for k in keys}, n=len(x))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rs3, rs2 = load(RUNS3), load(RUNS2)
    # N-agnostic: the N=10 extension tops every cell up from 3 to 10, so
    # the only invariant worth asserting is that the design stays balanced.
    # Nothing else in this script depends on the cell size.
    cell_sizes = set(Counter((r["scenario_id"], r["architecture"]) for r in rs3).values())
    assert len(cell_sizes) == 1, f"unbalanced cells: {cell_sizes}"
    n_per_cell = cell_sizes.pop()
    assert len(rs3) == 16 * n_per_cell, f"expected {16 * n_per_cell} runs, found {len(rs3)}"
    assert {r["model"] for r in rs3} == {"gpt-4.1-nano-2025-04-14"}

    summary: dict = {
        "n": len(rs3),
        "n_per_cell": n_per_cell,
        "cap_study2": CAP_STUDY2,
        "cap_study3": CAP_STUDY3,
        "commits": sorted({(r.get("git_commit") or "?")[:7] for r in rs3}),
        "max_iterations": sorted({r.get("max_iterations") for r in rs3}),
        "warning": (
            "All runs record git_dirty=true; untracked AGENTS.md was present. "
            "No tracked src/, benchmark/ or docker/ differed from 98dca8b."
        ),
    }

    # -- 1. CENSORING CHECK, FIRST ------------------------------------------
    summary["censoring_study3"] = censoring(rs3, CAP_STUDY3)
    summary["censoring_study2"] = censoring(rs2, CAP_STUDY2)

    # -- 2. token distributions and the ratio -------------------------------
    tok = {}
    for arm in ("single", "multi"):
        tok[arm] = dist([r["tokens_used"] for r in rs3 if r["architecture"] == arm])
    summary["tokens_study3"] = tok
    ratio3 = tok["multi"]["mean"] / tok["single"]["mean"]
    t2 = {
        a: dist([r["tokens_used"] for r in rs2 if r["architecture"] == a])
        for a in ("single", "multi")
    }
    summary["tokens_study2"] = t2
    ratio2 = t2["multi"]["mean"] / t2["single"]["mean"]
    summary["token_ratio"] = dict(
        study3_mean_ratio=ratio3,
        study3_paired=paired_log_ratio(rs3),
        study2_mean_ratio=ratio2,
        study2_paired=paired_log_ratio(rs2),
        study1_ratio=STUDY1_RATIO,
        labels=dict(
            study1="BOUND (100k cap, censored)",
            study2="BOUND (100k cap, 3 multi runs exhausted)",
            study3="ESTIMATE (uncensored)"
            if not summary["censoring_study3"]["censored"]
            else "BOUND (still censored at 300k)",
        ),
    )

    # -- 3. completion ------------------------------------------------------
    comp = {}
    for arm in ("single", "multi"):
        f3 = sum(r["termination_reason"] == "finalised" for r in rs3 if r["architecture"] == arm)
        f2 = sum(r["termination_reason"] == "finalised" for r in rs2 if r["architecture"] == arm)
        comp[arm] = dict(
            # Study 2 is fixed at N=3 (24 runs per arm) and is not re-collected.
            study3=f"{f3}/{8 * n_per_cell}",
            study2=f"{f2}/24",
            fisher_p=float(fisher_exact([[f3, 8 * n_per_cell - f3], [f2, 24 - f2]]).pvalue),
        )
    def n_final(arm: str) -> int:
        return sum(
            r["termination_reason"] == "finalised"
            for r in rs3
            if r["architecture"] == arm
        )

    s3_single, s3_multi = n_final("single"), n_final("multi")
    comp["study3_between_arms_fisher_p"] = float(
        fisher_exact([
            [s3_single, 8 * n_per_cell - s3_single],
            [s3_multi, 8 * n_per_cell - s3_multi],
        ]).pvalue
    )
    summary["completion"] = comp
    summary["termination_reasons"] = {
        arm: dict(Counter(r["termination_reason"] for r in rs3 if r["architecture"] == arm))
        for arm in ("single", "multi")
    }

    # -- 4. selection analysis ----------------------------------------------
    mic = {a: micro(rs3, a) for a in ("single", "multi")}
    summary["selection_study3"] = {
        a: dict(macro=macro(rs3, a), macro_finalised_only=macro(rs3, a, True), micro=mic[a])
        for a in ("single", "multi")
    }
    summary["selection_study2"] = {
        a: dict(macro=macro(rs2, a), macro_finalised_only=macro(rs2, a, True), micro=micro(rs2, a))
        for a in ("single", "multi")
    }
    summary["paired_study3"] = [
        paired(rs3, k)
        for k in ("selection_f1", "selection_precision", "selection_recall", "tokens_used")
    ]
    fin3 = [r for r in rs3 if r["termination_reason"] == "finalised"]
    summary["paired_study3_finalised_only"] = (
        [paired(fin3, k) for k in ("selection_f1", "selection_precision", "selection_recall")]
        if len({r["scenario_id"] for r in fin3}) == 8
        else "insufficient scenario coverage among finalised runs"
    )

    grid = np.linspace(0.1, 5, 491)
    diff = np.array([
        fbeta(mic["multi"]["precision"], mic["multi"]["recall"], b)
        - fbeta(mic["single"]["precision"], mic["single"]["recall"], b)
        for b in grid
    ])
    summary["fbeta_crossover_beta_study3"] = (
        float(grid[np.argmin(abs(diff))]) if np.ptp(np.sign(diff)) else None
    )

    # -- per-scenario table --------------------------------------------------
    per = []
    for arm in ("single", "multi"):
        for sid in sorted({r["scenario_id"] for r in rs3}):
            z = [r for r in rs3 if r["architecture"] == arm and r["scenario_id"] == sid]
            f = [r for r in z if r["termination_reason"] == "finalised"]
            per.append(dict(
                scenario_id=sid, role=role(sid), architecture=arm, n=len(z),
                completion=mean([r["termination_reason"] == "finalised" for r in z]),
                exact=mean([r["scores"].get("selection_exact_match", 0) for r in z]),
                precision=mean([r["scores"].get("selection_precision", 0) for r in z]),
                recall=mean([r["scores"].get("selection_recall", 0) for r in z]),
                f1=mean([r["scores"].get("selection_f1", 0) for r in z]),
                correctness=mean(
                    [r["scores"]["correctness"] for r in f if "correctness" in r["scores"]]
                ),
                parameters_checked=mean([
                    r["scores"]["parameters_checked"]
                    for r in f
                    if "parameters_checked" in r["scores"]
                ]),
                tokens=mean([r["tokens_used"] for r in z]),
                tokens_max=max(r["tokens_used"] for r in z),
                wall_clock_s=mean([r["wall_clock_s"] for r in z]),
            ))
    write("table1_per_scenario_arm.csv", per)

    write("table2_token_distribution.csv", [
        dict(study=s, architecture=a, cap=c, **d)
        for s, c, dd in (("Study 2", CAP_STUDY2, t2), ("Study 3", CAP_STUDY3, tok))
        for a, d in dd.items()
    ])
    write("table3_paired_tests.csv", summary["paired_study3"])
    write("table4_censoring.csv", [
        dict(study=s, architecture=a, cap=c, **{k: v for k, v in cs[a].items()})
        for s, c, cs in (("Study 2", CAP_STUDY2, summary["censoring_study2"]),
                         ("Study 3", CAP_STUDY3, summary["censoring_study3"]))
        for a in ("single", "multi")
    ])
    write("table5_failure_taxonomy.csv", [
        dict(scenario_id=r["scenario_id"], architecture=r["architecture"],
             termination_reason=r["termination_reason"], tokens_used=r["tokens_used"],
             per_agent_tokens=json.dumps(r.get("per_agent_tokens", {})),
             no_spec=r.get("final_spec") is None)
        for r in rs3 if r["termination_reason"] != "finalised"
    ])

    # -- 5. verdict table ----------------------------------------------------
    cens3 = summary["censoring_study3"]
    p1_obs = f"multi {s3_multi}/{8 * n_per_cell} finalised"
    p1_held = s3_multi == 8 * n_per_cell
    p2_held = ratio3 > ratio2
    d_f1 = [p for p in summary["paired_study3"] if p["metric"] == "selection_f1"][0]
    p3_held = d_f1["ci_low"] <= 0 <= d_f1["ci_high"]
    p4_triggered = cens3["multi"]["budget_exhausted"] > 0
    verdict = [
        dict(prediction="P1", statement="Multi completes 24/24 at the raised cap",
             observed=p1_obs, held="HELD" if p1_held else "FAILED"),
        dict(prediction="P2", statement=f"Uncensored ratio exceeds Study 2's {ratio2:.2f}x",
             observed=f"Study 3 ratio {ratio3:.2f}x", held="HELD" if p2_held else "FAILED"),
        dict(prediction="P3", statement="Selection quality indistinguishable between arms",
             observed=f"paired macro F1 delta {d_f1['mean_delta']:+.4f} "
                      f"[{d_f1['ci_low']:+.4f},{d_f1['ci_high']:+.4f}] t={d_f1['t']:.2f}",
             held="HELD" if p3_held else "FAILED — STOP AND REPORT"),
        dict(prediction="P4", statement="Falsifier: multi still exhausts the raised budget",
             observed=(
                 f"{cens3['multi']['budget_exhausted']}/{8 * n_per_cell} "
                 "multi budget_exhausted at 300k"
             ),
             held="TRIGGERED — architectural finding" if p4_triggered else "not triggered"),
    ]
    summary["verdicts"] = verdict
    write("table6_verdicts.csv", verdict)

    with (OUT / "summary.json").open("w") as f:
        json.dump(summary, f, indent=2, default=str)

    # -- 6. figures ----------------------------------------------------------
    plt.style.use("seaborn-v0_8-whitegrid")
    colors = {"single": "#4C72B0", "multi": "#DD8452"}

    fig, ax = plt.subplots(figsize=(9, 4.5))
    for i, arm in enumerate(("single", "multi")):
        v = [r["tokens_used"] for r in rs3 if r["architecture"] == arm]
        ax.scatter(np.random.default_rng(0).normal(i, 0.06, len(v)), v,
                   color=colors[arm], alpha=0.75, s=34, label=f"Study 3 {arm}")
        v2 = [r["tokens_used"] for r in rs2 if r["architecture"] == arm]
        ax.scatter(np.random.default_rng(1).normal(i + 0.28, 0.06, len(v2)), v2,
                   color=colors[arm], alpha=0.35, s=24, marker="^", label=f"Study 2 {arm}")
    ax.axhline(CAP_STUDY2, color="#C44E52", ls="--", lw=1.4, label="Study 2 cap 100k")
    ax.axhline(CAP_STUDY3, color="black", ls="--", lw=1.4, label="Study 3 cap 300k")
    ax.set_xticks([0.14, 1.14], ["single", "multi"])
    ax.set_ylabel("tokens used per run")
    ax.set_title("Token distribution per arm, both caps marked")
    ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(OUT / "fig1_token_distribution.png", dpi=200)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.5, 4))
    s3_label = "(uncensored)" if not cens3["censored"] else "(bound)"
    labels = ["Study 1\n(bound)", "Study 2\n(bound)", f"Study 3\n{s3_label}"]
    vals = [STUDY1_RATIO, ratio2, ratio3]
    bars = ax.bar(labels, vals, color=["#999999", "#999999", "#4C72B0"])
    pr = summary["token_ratio"]["study3_paired"]
    ax.errorbar([2], [pr["geometric_ratio"]],
                yerr=[
                    [pr["geometric_ratio"] - pr["ci_low"]],
                    [pr["ci_high"] - pr["geometric_ratio"]],
                ],
                fmt="o", color="black", capsize=4, label="paired geometric ratio, 95% CI")
    for b, v in zip(bars, vals, strict=True):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.03, f"{v:.2f}x", ha="center")
    ax.set_ylabel("multi / single token ratio")
    ax.set_title("Token ratio across studies")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "fig2_token_ratio_all_studies.png", dpi=200)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    m2 = {a: micro(rs2, a) for a in ("single", "multi")}
    for arm in ("single", "multi"):
        ax.plot(grid, [fbeta(mic[arm]["precision"], mic[arm]["recall"], b) for b in grid],
                color=colors[arm], lw=2, label=f"Study 3 {arm}")
        ax.plot(grid, [fbeta(m2[arm]["precision"], m2[arm]["recall"], b) for b in grid],
                color=colors[arm], lw=1.4, ls=":", label=f"Study 2 {arm}")
    if summary["fbeta_crossover_beta_study3"]:
        ax.axvline(summary["fbeta_crossover_beta_study3"], color="black", ls="--", lw=1,
                   label=f"Study 3 crossover β={summary['fbeta_crossover_beta_study3']:.2f}")
    ax.set(xlabel="beta (recall weight)", ylabel="micro F-beta",
           title="Recall-weighted selection loss: Study 2 vs Study 3")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "fig3_fbeta_study2_vs_study3.png", dpi=200)
    plt.close(fig)

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
