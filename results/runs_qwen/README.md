# Runs excluded from the gpt-4.1-nano collection dataset

31 instrumented runs that carry a git_commit but are not
(commit 8751e47 AND model gpt-4.1-nano-2025-04-14).

| commit  | model                   | n  | what it is |
|---------|-------------------------|----|------------|
| cac0ba1 | qwen2.5:14b             | 24 | the local N=1 pilot, tagged `pilot-local-n1` |
| 2aaad0b | qwen2.5:14b             |  4 | step-5 local verification cells |
| a4727fa | qwen2.5:14b             |  1 | step-5b verification cell (summary payload fix) |
| c55bbc0 | gpt-4.1-nano            |  2 | step-6b smoke test on the *floating alias*, before the dated snapshot was pinned |

They are moved here rather than deleted because they are the pre-switch record
and remain valid observations in their own right.

## Why they had to be moved

`scripts/run_matrix.py:existing_run_count` counts any result carrying a
git_commit, without checking the commit or the model. With these runs present,
`--runs N --resume` treated a cell as already populated by qwen pilot data:
pass 1 of the collection batch executed 0 of 24 cells for exactly this reason.

Correcting the function would have moved HEAD, so the remaining runs would have
recorded a different git_commit from the 44 already collected. Separating the
directories instead makes the filter correct by construction and leaves HEAD at
8751e47.

The two c55bbc0 runs used the floating alias `gpt-4.1-nano`, which OpenAI may
repoint at any time; the collection dataset pins `gpt-4.1-nano-2025-04-14`.
