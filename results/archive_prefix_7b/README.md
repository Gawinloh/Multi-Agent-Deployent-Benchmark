# Archived pre-pilot runs

Two instrumented runs produced at commit 8587f1a on model qwen2.5:7b.

They carry a git_commit, so `run_matrix.py --resume` would have counted them as
usable and under-collected the scenario_002 cell, mixing both commits and models
within a single cell. They are retained here for audit and excluded from analysis.

The 7b model was used unintentionally: .env sets OLLAMA_MODEL=qwen2.5:14b, but
load_dotenv() is called only in scripts/phase_b_sanity.py, so the runner fell back
to the hardcoded default in src/llm/client.py. From the pilot onward the matrix is
launched with an explicit --model qwen2.5:14b.
