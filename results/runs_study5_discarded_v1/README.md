# Discarded Study 5 partial collection (v1)

34 of 80 runs, collected 2026-08-26 and **stopped mid-collection**. Retained as
evidence of the defect that stopped it, and deliberately **not analysed**.

The parallel arm had less retry resilience than the star arm it is compared
against: service agents got one instance with accumulating history, while the
star orchestrator re-delegates to a freshly constructed worker. Of 19 postgres
agents here, all 14 successes arrived within 3 iterations and all 5 failures
exhausted 4 without recovering — the signature of an agent looping on its own
failed output. Three runs consequently produced no StackSpec at all.

Diagnosis, measurements and fix: `docs/planning/study5-amendment-1.md`.
The reported Study 5 dataset is `results/runs_study5/`, collected after the fix.
