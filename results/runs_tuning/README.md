# Pre-instrumentation tuning runs

37 runs produced before run provenance landed (commit 130bb00). They carry no
`git_commit`, no per-agent token attribution, and a placeholder model name, so
they cannot be attributed to a known state of the code.

`scripts/run_matrix.py:existing_run_count` already ignores them for that reason.
They are moved out of `results/runs/` for tidiness, and retained rather than
deleted because they document the harness-tuning phase of the project.

They are not part of any reported dataset.
