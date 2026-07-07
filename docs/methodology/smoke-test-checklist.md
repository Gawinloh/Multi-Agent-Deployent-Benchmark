# Smoke Test Checklist

Checklist for running the single-agent end-to-end smoke test.
This is the first integration test of the full pipeline.

## Pre-run

1. **Ollama running** with `qwen2.5:7b` pulled:
   ```
   ollama serve          # in one terminal
   ollama run qwen2.5:7b "hello"   # quick check
   ```
2. **RAG index built** — `rag_index/` directory exists with non-zero chunk counts:
   ```
   python -m src.rag.build_index --corpus corpus/ --out rag_index/
   ```
3. **Docker Desktop running** — `docker info` succeeds.
4. **`.env` configured** — `LLM_BACKEND=ollama`, `OLLAMA_MODEL=qwen2.5:7b`, `OLLAMA_HOST=http://localhost:11434`.

## Run command

```bash
python -m src.experiment.runner \
    --scenario benchmark/scenarios/smoke_test.yaml \
    --architecture single \
    --runs 1
```

Result JSON is saved to `results/runs/<run_id>.json`.

## Expected outcome

- Agent completes within 5 minutes.
- `termination_reason` is `"finalised"`.
- `final_spec` is set (non-null).
- Validator report shows smoke tests passing for all three services.
- CIS pass rate >= 0.75.
- Token counts are real (non-zero `input_tokens` and `output_tokens`).
- Agent history contains at least one `query_rag` call (proves RAG is used).

## Failure modes

| Symptom | Likely cause | Where to look |
|---------|-------------|---------------|
| Agent never calls tools | Prompt doesn't instruct tool use clearly enough | `src/agents/single_agent/prompts.py` |
| Budget exhausted before finalise | LLM too verbose or budget too small | `--budget` flag; check token counts per iteration in history |
| Validator timeout | Docker too slow (especially on M1) | `src/validator/runner.py` `budget_seconds`; Docker Desktop resource settings |
| Schema validation failures | Prompt schema instructions unclear to the model | `src/llm/client.py` `_schema_instruction`; `src/agents/single_agent/prompts.py` |
| `generate_config` returns error | StackSpec fields missing or wrong types | Check `ToolObservation.error` in history; compare against `src/schemas/stack.py` |
| wrk benchmark skipped | Expected on Apple Silicon (ARM64) | `src/validator/benchmarks/wrk.py` — nginx correctness still measured via CIS checks |

## Known limitations

- **wrk benchmark**: Descoped on ARM64/Apple Silicon — `williamyeh/wrk` has no native ARM64 image.
  Nginx performance is not benchmarked on these platforms; config correctness and CIS checks are
  still measured. See `src/validator/benchmarks/wrk.py` for the skip logic.

## After the run

1. Inspect result JSON: `cat results/runs/<run_id>.json | python -m json.tool`
2. Check `history` array for tool calls and RAG citations.
3. Check `validator_report.summary` for pass/fail counts.
4. If the run failed, identify the failure mode from the table above and iterate on prompts.
5. Run 3 consecutive times to confirm reliability (not luck).
