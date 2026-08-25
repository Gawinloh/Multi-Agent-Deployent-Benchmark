"""Study 4: service selection with a complete retrieval index.

Study 4 repeats the Study 3 matrix — same 8 scenarios, same two arms, same
model, budget and iteration cap, same N=10 — changing exactly one thing: the
retrieval index now contains the five ``corpus/rabbitmq/`` documents that were
on disk but never indexed. Studies 1-3 ran against a 26-document index built
2026-07-07; Study 4 runs against the 31-document rebuild.

The question is narrow and was registered before collection in
``docs/planning/study4-prereg.md`` (SHA-256 committed at 6a641ba): how much of
the C5 effect — RabbitMQ recall of 3.4% (single) and 10.0% (multi) alongside 19
and 32 Redis false positives — was caused by the corpus gap rather than by a
fixed per-service disposition.

Statistics are deliberately the same as Study 3 so the two are directly
comparable, and the metric helpers are imported from ``analyse_study2`` rather
than reimplemented so no definition can drift:

  * Macro metrics have one value per run, so they take the paired
    scenario-level t-test of ``analyse_study2.paired()`` (n=8, df=7).
  * Pooled per-service counts (recall, false positives) have no per-run value,
    so they take a scenario-level cluster bootstrap, the same treatment
    ``analyse_study3_n10.py`` applies to pooled micro metrics. Here the
    resampling is across *studies*: each draw takes a scenario's Study 3 and
    Study 4 runs together, which preserves both the pairing and the clustering
    of decisions within a scenario.

Sections:
  1. Integrity: run count, cells, model, commit, and the index actually used.
  2. Per-service confusion (TP/FP/FN/TN) for all four services, both arms.
  3. Disposition on the four adversarial scenarios s2_002-s2_005.
  4. Paired scenario-level differences for macro F1, precision, recall, tokens.
  5. Study 3 vs Study 4 side by side, RabbitMQ recall and Redis FP first.
  6. Retrieval trace audit: did query_rag actually start returning RabbitMQ
     material, and did queue questions start retrieving queue documents.
  7. Verdict table for the pre-registered outcomes O1-O4.

Usage::

    python scripts/analyse_study4.py
"""

from __future__ import annotations

import csv
import glob
import json
import sys
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from scipy.stats import fisher_exact  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reuse Study 2's helpers verbatim so metric definitions cannot drift.
from analyse_study2 import mean, paired, role  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNS4 = ROOT / "results/runs_study4"
RUNS3 = ROOT / "results/runs_study3"
OUT = ROOT / "results/analysis_study4"

SERVICES = ("postgres", "nginx", "redis", "rabbitmq")
ARMS = ("single", "multi")

BOOTSTRAP_DRAWS = 10_000
RNG = np.random.default_rng(20260825)

#: The four adversarial scenarios and the service whose disposition each
#: probes, with the role that makes it adversarial.
ADVERSARIAL = {
    "s2_002_distractor_cache": ("redis", "named, unwarranted"),
    "s2_003_distractor_queue": ("rabbitmq", "named, unwarranted"),
    "s2_004_inferred_queue": ("rabbitmq", "unnamed, required"),
    "s2_005_inferred_cache": ("redis", "unnamed, required"),
}

#: Pre-registered decision thresholds. Fixed before collection; see
#: docs/planning/study4-prereg.md. Changing these invalidates the
#: pre-registration, which is the point of writing them down here.
O1_MIN_RECALL_RISE = 0.20
O2_MAX_RECALL = 0.15

#: Study 3's paired 95% CIs, the integrity band for O4.
O4_BAND = {
    "selection_f1": (-0.041914721556068554, 0.14298615012749705),
    "tokens_used": (-6862.501150694479, 42322.72615069448),
}


# ---------------------------------------------------------------------------
# Loading and small helpers
# ---------------------------------------------------------------------------


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


def svc_counts(rs: list[dict], arm: str, svc: str) -> tuple[int, int, int, int]:
    """Pooled TP/FP/FN/TN for one service in one arm."""
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


def recall_of(tp: int, fn: int) -> float:
    return tp / (tp + fn) if (tp + fn) else 0.0


def precision_of(tp: int, fp: int) -> float:
    return tp / (tp + fp) if (tp + fp) else 0.0


# ---------------------------------------------------------------------------
# Cross-study cluster bootstrap
# ---------------------------------------------------------------------------


def cross_study_bootstrap(
    rs4: list[dict], rs3: list[dict], arm: str, svc: str, statistic: str
) -> dict:
    """Study 4 - Study 3 difference with a scenario-level cluster bootstrap.

    ``statistic`` is "recall" or "false_positives". Each bootstrap draw picks
    8 scenarios with replacement and takes *both studies'* runs for each drawn
    scenario, so the comparison stays paired on scenario and the clustering of
    service decisions within a scenario is respected. A pooled count has no
    per-run value to feed a paired t-test, which is why the interval is a
    bootstrap rather than a t interval.

    False-positive counts scale with the number of runs drawn, so they are
    reported as a rate per run as well as a raw count; the interval is
    computed on the rate, which is what stays comparable under resampling.
    """
    sids = sorted({r["scenario_id"] for r in rs4})
    by4 = {s: [r for r in rs4 if r["scenario_id"] == s] for s in sids}
    by3 = {s: [r for r in rs3 if r["scenario_id"] == s] for s in sids}

    def stat(sample: list[dict]) -> float:
        tp, fp, fn, _ = svc_counts(sample, arm, svc)
        if statistic == "recall":
            return recall_of(tp, fn)
        n = len([r for r in sample if r["architecture"] == arm]) or 1
        return fp / n

    obs = stat(rs4) - stat(rs3)
    draws = np.empty(BOOTSTRAP_DRAWS)
    for i in range(BOOTSTRAP_DRAWS):
        pick = RNG.choice(len(sids), len(sids), replace=True)
        s4 = [r for k in pick for r in by4[sids[k]]]
        s3 = [r for k in pick for r in by3[sids[k]]]
        draws[i] = stat(s4) - stat(s3)
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return dict(
        delta=float(obs),
        ci_low=float(lo),
        ci_high=float(hi),
        excludes_null=bool(lo > 0 or hi < 0),
    )


# ---------------------------------------------------------------------------
# Retrieval trace audit
# ---------------------------------------------------------------------------

QUEUE_WORDS = ("queue", "broker", "rabbit", "amqp", "messag")


def rag_audit(rs: list[dict], label: str) -> dict:
    """How often query_rag returned RabbitMQ material, overall and for
    queue-related questions.

    This is the mechanism check Section 6.4 ran by hand over Study 3: of the
    four query_rag calls asking about message brokers, none returned a
    RabbitMQ source. Automating it makes the before/after directly comparable.
    """
    total = with_rmq = queue_q = queue_with_rmq = 0
    examples = []
    for r in rs:
        for entry in r.get("history") or []:
            call = entry.get("tool_call") or {}
            if call.get("name") != "query_rag":
                continue
            obs = entry.get("observation") or {}
            chunks = obs.get("result") or []
            if not isinstance(chunks, list):
                continue
            total += 1
            hits = sum(
                1
                for c in chunks
                if isinstance(c, dict) and (c.get("service") or "").lower() == "rabbitmq"
            )
            question = (call.get("args") or {}).get("question", "")
            is_queue = any(w in question.lower() for w in QUEUE_WORDS)
            with_rmq += hits > 0
            if is_queue:
                queue_q += 1
                queue_with_rmq += hits > 0
                examples.append(
                    dict(
                        study=label,
                        scenario_id=r["scenario_id"],
                        architecture=r["architecture"],
                        question=question[:160],
                        rabbitmq_chunks=hits,
                        services_returned=",".join(
                            (c.get("service") or "unspecified")
                            for c in chunks
                            if isinstance(c, dict)
                        ),
                    )
                )
    return dict(
        study=label,
        query_rag_calls=total,
        calls_returning_rabbitmq=with_rmq,
        queue_related_calls=queue_q,
        queue_calls_returning_rabbitmq=queue_with_rmq,
        _examples=examples,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rs4, rs3 = load(RUNS4), load(RUNS3)

    # --- 1. Integrity ----------------------------------------------------
    integrity = dict(
        n_study4=len(rs4),
        n_study3=len(rs3),
        models=sorted({r["model"] for r in rs4}),
        max_iterations=sorted({r["max_iterations"] for r in rs4}),
        commits=sorted({r["git_commit"][:7] for r in rs4}),
        scenarios=sorted({r["scenario_id"] for r in rs4}),
        cells=sorted(
            Counter((r["scenario_id"], r["architecture"]) for r in rs4).values()
        ),
        termination_reasons={
            a: dict(
                Counter(r["termination_reason"] for r in rs4 if r["architecture"] == a)
            )
            for a in ARMS
        },
    )
    assert len(rs4) == 160, f"expected 160 Study 4 runs, found {len(rs4)}"
    assert integrity["models"] == ["gpt-4.1-nano-2025-04-14"], integrity["models"]
    assert set(integrity["cells"]) == {10}, integrity["cells"]
    assert sorted({r["scenario_id"] for r in rs4}) == sorted(
        {r["scenario_id"] for r in rs3}
    ), "scenario sets differ between studies"

    manifest4 = json.loads((ROOT / "rag_index/manifest.json").read_text())
    manifest3 = json.loads(
        (ROOT / "rag_index_prestudy4_2026-07-07/manifest.json").read_text()
    )
    integrity["index_study4"] = manifest4
    integrity["index_study3"] = manifest3
    assert manifest4["documents"] == 31, manifest4["documents"]
    assert manifest4["chunks_per_service"].get("rabbitmq", 0) > 0, "no rabbitmq chunks"

    # --- 2. Per-service confusion ---------------------------------------
    confusion = []
    for svc in SERVICES:
        for arm in ARMS:
            t4 = svc_counts(rs4, arm, svc)
            t3 = svc_counts(rs3, arm, svc)
            confusion.append(
                dict(
                    service=svc,
                    architecture=arm,
                    TP=t4[0],
                    FP=t4[1],
                    FN=t4[2],
                    TN=t4[3],
                    precision=round(precision_of(t4[0], t4[1]), 4),
                    recall=round(recall_of(t4[0], t4[2]), 4),
                    study3_TP=t3[0],
                    study3_FP=t3[1],
                    study3_FN=t3[2],
                    study3_TN=t3[3],
                    study3_precision=round(precision_of(t3[0], t3[1]), 4),
                    study3_recall=round(recall_of(t3[0], t3[2]), 4),
                )
            )
    write("table1_per_service_confusion.csv", confusion)

    # --- 3. Disposition on the adversarial scenarios ---------------------
    disposition = []
    for sid, (svc, role_label) in ADVERSARIAL.items():
        for arm in ARMS:
            row = dict(scenario_id=sid, service=svc, role=role_label, architecture=arm)
            for label, rs in (("study4", rs4), ("study3", rs3)):
                det = [
                    d
                    for r in rs
                    if r["scenario_id"] == sid and r["architecture"] == arm
                    for d in r["scores"].get("selection_detail", [])
                    if d["service"] == svc
                ]
                row[f"{label}_deployed"] = sum(1 for d in det if d["selected"])
                row[f"{label}_decisions"] = len(det)
            disposition.append(row)
    write("table2_disposition_adversarial.csv", disposition)

    # --- 4. Paired scenario-level tests ----------------------------------
    metrics = (
        "selection_f1",
        "selection_precision",
        "selection_recall",
        "tokens_used",
    )
    paired4 = [paired(rs4, k) for k in metrics]
    paired3 = [paired(rs3, k) for k in metrics]
    paired_rows = [
        dict(
            metric=p4["metric"],
            study4_mean_delta=p4["mean_delta"],
            study4_ci_low=p4["ci_low"],
            study4_ci_high=p4["ci_high"],
            study4_t=p4["t"],
            study3_mean_delta=p3["mean_delta"],
            study3_ci_low=p3["ci_low"],
            study3_ci_high=p3["ci_high"],
            study3_t=p3["t"],
        )
        for p4, p3 in zip(paired4, paired3, strict=True)
    ]
    write("table3_paired_tests.csv", paired_rows)

    # --- 5. Study 3 vs Study 4, per service ------------------------------
    comparison = []
    for svc in SERVICES:
        for arm in ARMS:
            tp4, fp4, fn4, _ = svc_counts(rs4, arm, svc)
            tp3, fp3, fn3, _ = svc_counts(rs3, arm, svc)
            rec = cross_study_bootstrap(rs4, rs3, arm, svc, "recall")
            fpr = cross_study_bootstrap(rs4, rs3, arm, svc, "false_positives")
            comparison.append(
                dict(
                    service=svc,
                    architecture=arm,
                    study3_recall=round(recall_of(tp3, fn3), 4),
                    study4_recall=round(recall_of(tp4, fn4), 4),
                    recall_delta=round(rec["delta"], 4),
                    recall_ci_low=round(rec["ci_low"], 4),
                    recall_ci_high=round(rec["ci_high"], 4),
                    recall_excludes_null=rec["excludes_null"],
                    study3_FP=fp3,
                    study4_FP=fp4,
                    fp_rate_delta=round(fpr["delta"], 4),
                    fp_ci_low=round(fpr["ci_low"], 4),
                    fp_ci_high=round(fpr["ci_high"], 4),
                    fp_excludes_null=fpr["excludes_null"],
                    fisher_p_deployed=float(
                        fisher_exact([[tp4 + fp4, fn4], [tp3 + fp3, fn3]]).pvalue
                    ),
                )
            )
    write("table4_study3_vs_study4.csv", comparison)

    # --- 6. Retrieval trace audit ----------------------------------------
    audit4, audit3 = rag_audit(rs4, "study4"), rag_audit(rs3, "study3")
    write(
        "table5_rag_audit_queue_queries.csv",
        audit3["_examples"] + audit4["_examples"],
    )
    audit = [
        {k: v for k, v in a.items() if not k.startswith("_")} for a in (audit3, audit4)
    ]
    write("table6_rag_audit_summary.csv", audit)

    # --- 7. Pre-registered verdicts --------------------------------------
    def rmq(rs, arm):
        tp, _, fn, _ = svc_counts(rs, arm, "rabbitmq")
        return recall_of(tp, fn)

    rmq4 = {a: rmq(rs4, a) for a in ARMS}
    rmq3 = {a: rmq(rs3, a) for a in ARMS}
    rmq_boot = {
        a: cross_study_bootstrap(rs4, rs3, a, "rabbitmq", "recall") for a in ARMS
    }
    redis_boot = {
        a: cross_study_bootstrap(rs4, rs3, a, "redis", "false_positives") for a in ARMS
    }

    o1 = all(
        rmq_boot[a]["delta"] >= O1_MIN_RECALL_RISE and rmq_boot[a]["excludes_null"]
        for a in ARMS
    )
    o2 = all(
        rmq4[a] <= O2_MAX_RECALL and not rmq_boot[a]["excludes_null"] for a in ARMS
    )
    redis_fp4 = {a: svc_counts(rs4, a, "redis")[1] for a in ARMS}
    redis_fp3 = {a: svc_counts(rs3, a, "redis")[1] for a in ARMS}
    o3 = all(
        redis_fp4[a] < redis_fp3[a] and redis_boot[a]["excludes_null"] for a in ARMS
    )

    p4map = {p["metric"]: p for p in paired4}
    o4_flags = [
        m
        for m, (lo, hi) in O4_BAND.items()
        if not (lo <= p4map[m]["mean_delta"] <= hi)
    ]

    if o1:
        outcome = "O1 - corpus gap was a material cause; weaken/withdraw the disposition claim"
    elif o2:
        outcome = "O2 - corpus gap was not the driver; disposition claim strengthened"
    else:
        outcome = "INTERMEDIATE - neither O1 nor O2 rule met; partial attribution"

    verdicts = [
        dict(
            outcome="O1",
            rule=f"both arms: RabbitMQ recall rise >= {O1_MIN_RECALL_RISE} and CI excludes 0",
            observed="; ".join(
                f"{a}: {rmq3[a]:.3f} -> {rmq4[a]:.3f} (delta {rmq_boot[a]['delta']:+.3f}, "
                f"CI [{rmq_boot[a]['ci_low']:+.3f}, {rmq_boot[a]['ci_high']:+.3f}])"
                for a in ARMS
            ),
            met=o1,
        ),
        dict(
            outcome="O2",
            rule=f"both arms: Study 4 RabbitMQ recall <= {O2_MAX_RECALL} and CI includes 0",
            observed="; ".join(f"{a}: {rmq4[a]:.3f}" for a in ARMS),
            met=o2,
        ),
        dict(
            outcome="O3",
            rule="both arms: Redis FP fall and CI on the rate excludes 0",
            observed="; ".join(
                f"{a}: {redis_fp3[a]} -> {redis_fp4[a]} FP "
                f"(rate delta {redis_boot[a]['delta']:+.3f}, "
                f"CI [{redis_boot[a]['ci_low']:+.3f}, {redis_boot[a]['ci_high']:+.3f}])"
                for a in ARMS
            ),
            met=o3,
        ),
        dict(
            outcome="O4",
            rule="paired macro F1 and token deltas stay inside Study 3's 95% CIs",
            observed=(
                "within band"
                if not o4_flags
                else f"OUTSIDE BAND: {', '.join(o4_flags)} - investigate the re-run"
            ),
            met=not o4_flags,
        ),
    ]
    write("table7_verdicts.csv", verdicts)

    summary = dict(
        registered="docs/planning/study4-prereg.md (sha256 committed at 6a641ba)",
        manipulated_variable="retrieval index contents: 26 -> 31 documents",
        integrity=integrity,
        rabbitmq_recall=dict(study3=rmq3, study4=rmq4, bootstrap=rmq_boot),
        redis_false_positives=dict(
            study3=redis_fp3, study4=redis_fp4, bootstrap=redis_boot
        ),
        paired_study4=paired4,
        paired_study3=paired3,
        rag_audit=audit,
        verdicts=verdicts,
        primary_outcome=outcome,
    )
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))

    # --- Figures ---------------------------------------------------------
    plt.style.use("seaborn-v0_8-whitegrid")
    colors = {"single": "#4C72B0", "multi": "#DD8452"}

    # fig1: RabbitMQ recall, Study 3 vs Study 4, both arms
    fig, ax = plt.subplots(figsize=(7, 4))
    x = np.arange(2)
    for i, arm in enumerate(ARMS):
        ax.bar(
            x + i * 0.35,
            [rmq3[arm], rmq4[arm]],
            0.35,
            label=arm,
            color=colors[arm],
        )
    ax.set_xticks(x + 0.175, ["Study 3\n(26-doc index)", "Study 4\n(31-doc index)"])
    ax.set_ylabel("pooled RabbitMQ recall")
    ax.set_title("RabbitMQ recall before and after indexing the RabbitMQ corpus")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "fig1_rabbitmq_recall.png", dpi=200)
    plt.close(fig)

    # fig2: per-service recall, both studies, both arms
    fig, axs = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    for ax, arm in zip(axs, ARMS, strict=True):
        w = 0.35
        xs = np.arange(len(SERVICES))
        for j, (label, rs) in enumerate((("Study 3", rs3), ("Study 4", rs4))):
            vals = [
                recall_of(svc_counts(rs, arm, s)[0], svc_counts(rs, arm, s)[2])
                for s in SERVICES
            ]
            ax.bar(xs + j * w, vals, w, label=label, color=["#999999", colors[arm]][j])
        ax.set_xticks(xs + w / 2, SERVICES)
        ax.set_title(arm)
    axs[0].set_ylabel("pooled recall")
    axs[0].legend()
    fig.tight_layout()
    fig.savefig(OUT / "fig2_per_service_recall.png", dpi=200)
    plt.close(fig)

    # fig3: Redis false positives
    fig, ax = plt.subplots(figsize=(7, 4))
    for i, arm in enumerate(ARMS):
        ax.bar(
            np.arange(2) + i * 0.35,
            [redis_fp3[arm], redis_fp4[arm]],
            0.35,
            label=arm,
            color=colors[arm],
        )
    ax.set_xticks(np.arange(2) + 0.175, ["Study 3", "Study 4"])
    ax.set_ylabel("Redis false positives (of 80 runs)")
    ax.set_title("Redis over-provisioning before and after")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "fig3_redis_false_positives.png", dpi=200)
    plt.close(fig)

    print(json.dumps(summary, indent=2, default=str))
    print("\n" + "=" * 70)
    print("PRE-REGISTERED OUTCOME:", outcome)
    print("=" * 70)


if __name__ == "__main__":
    main()
