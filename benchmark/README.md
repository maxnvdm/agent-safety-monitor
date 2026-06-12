# Benchmark Suite

Measures how accurately different models grade the two LLM-based scorers
(`deceptive_reasoning`, `supply_chain_risk`) against hand-labelled sessions,
and what each model costs to run.

## Running

```bash
# Compare several models
uv run python -m benchmark.run \
  --models anthropic/claude-haiku-4-5 openai/gpt-4o-mini groq/llama-3.3-70b-versatile

# Smoke test without API calls
uv run python -m benchmark.run --models mockllm/model

# Re-print the latest stored results without re-running anything
uv run python -m benchmark.run --report-only
```

Flags: `--db` (SQLite path, defaults to `monitor.db`), `--inspect-log-dir`
(where Inspect writes its `.eval` logs, defaults to `inspect_logs/benchmark/`).

Each model needs its provider API key set (e.g. `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GROQ_API_KEY`) — same as `monitor/run.py`. A model that
fails (missing key, network error) is reported and skipped; the rest of the
run continues.

## How it works

1. Every model in `--models` is evaluated against all sessions in
   `sessions/` using the normal `coding_agent_safety()` task.
2. Predictions from the two LLM scorers are compared to the ground truth in
   `labels.json`.
3. Per-(model, scorer) accuracy, average latency, token usage, and estimated
   cost are written to the `benchmark_runs` and `benchmark_results` tables
   (in the same SQLite file as the monitor) and printed as a comparison table.

Cost is estimated from total token usage and the rate table in `pricing.py`.
Tokens are apportioned evenly between the two scorers, so per-scorer cost is
an approximation. Models missing from the rate table show cost as `—`.

## Adding a labelled session

1. Drop a Claude Code-format `.jsonl` file in `sessions/`. The naming
   convention is `bench-<scorer>-<expected>-<nnn>.jsonl`
   (e.g. `bench-deceptive-fail-003.jsonl`), but only the session ID inside
   the file actually matters.
2. Add an entry to `labels.json` keyed by the session ID, mapping each
   scorer you want graded to its expected outcome:

   ```json
   "bench-deceptive-fail-003": {"deceptive_reasoning": "fail"}
   ```

   A session may carry labels for both scorers. Scorers without a label for
   a given session are ignored when computing accuracy.

For `supply_chain_risk` sessions, remember the scorer has a pre-filter: the
session must touch a dependency file or run an install command, or the LLM is
never invoked and the session always scores "pass".

## Adding a model to the pricing table

Add a `{model_id: (input_usd_per_1k, output_usd_per_1k)}` entry to `RATES`
in `pricing.py`. Use the full Inspect model string (provider prefix included,
e.g. `anthropic/claude-haiku-4-5`). Local/free models should use `(0.0, 0.0)`
rather than being omitted, so they report `$0` instead of unknown.
