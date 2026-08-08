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

Usage::

    python scripts/analyse_study3.py
"""

from __future__ import annotations

# Analysis implementation follows collection; see the plan above.
