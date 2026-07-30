# Archived local regression run

One run: scenario_002_oltp_small / single, qwen2.5:14b, commit 8751e47.

Produced by the step-3 local regression check that confirmed the
generate_config mode-default change did not alter qwen's behaviour. It is a
valid observation, but it sits at the same commit as the nano collection matrix
and scripts/run_matrix.py:existing_run_count counts any run carrying a
git_commit without checking the model. Left in place it would have been counted
as one of that cell's three gpt-4.1-nano replicates, mixing models within a cell
and leaving the cell only one fresh nano run.

Retained here for audit and excluded from the collection dataset.
