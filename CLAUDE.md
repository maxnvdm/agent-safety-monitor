# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Python: install deps
uv sync

# Python: run all tests
uv run pytest

# Python: run a single test
uv run pytest tests/test_scorers.py::test_secret_leakage_passes_on_clean_results

# Python: lint + format
uv run ruff check .
uv run ruff format .

# Python: type check
uv run mypy .

# Python: security scan
uv run bandit -r . --exclude ./tests,./build,./.venv

# Run evals against sample logs (uses mockllm to avoid API cost)
uv run python -m monitor.run --log-dir samples/ --model mockllm/model

# Watch a directory and auto-ingest new sessions (30s cooldown by default)
uv run python -m monitor.watcher --log-dir logs/ --model mockllm/model

# Benchmark LLM scorers across models (accuracy/latency/cost vs labelled sessions)
uv run python -m benchmark.run --models mockllm/model
# Re-print latest stored benchmark results without re-running evals
uv run python -m benchmark.run --report-only

# Start the API (default db: monitor.db, override with MONITOR_DB env var)
uv run uvicorn api.main:app --reload --port 8070

# Frontend: install deps
cd frontend && pnpm install

# Frontend: dev server (proxies /api → localhost:8070)
cd frontend && pnpm dev

# Frontend: lint
cd frontend && pnpm lint

# Frontend: type check
cd frontend && pnpm typecheck

# Pre-commit hooks (run once after uv sync to install)
pre-commit install
# Run all hooks manually
uv run pre-commit run --all-files
```

## Architecture

The system is a pipeline: Claude Code JSONL logs → Inspect AI eval → SQLite → FastAPI → Vue 3 dashboard.

**`monitor/ingest.py`** — parses Claude Code's JSONL event format. Events have a `type` field (`user`/`assistant`); tool calls live inside assistant content blocks; tool results come back in the next user event. Produces `ToolCallRecord` and `ToolResultRecord` dataclasses, a rendered transcript string, and session metadata (cwd, git branch, session ID).

**`monitor/scorers.py`** — six `@scorer`-decorated functions, one per failure mode. Each returns an inner `async score(state, target)` that reads from `state.metadata` (populated by `sessions_dataset`). The four pattern-based scorers (`secret_leakage`, `scope_creep`, `exfiltration_attempt`, `privilege_escalation`) work on `tool_calls`/`tool_results` lists. The two LLM-graded scorers (`deceptive_reasoning`, `supply_chain_risk`) operate on the transcript string; `supply_chain_risk` has a pre-filter that skips the LLM if no dependency files were touched.

**`monitor/tasks.py`** — wires scorers into an Inspect `Task` via `coding_agent_safety()`. Uses a `passthrough` no-op solver (avoids burning a model call per sample since scorers work on pre-built metadata, not model output). `sessions_dataset()` builds one `Sample` per `.jsonl` file, with all scorer inputs in `metadata`.

**`monitor/db.py`** — SQLite schema (`sessions`, `results` tables) and `ingest_inspect_log()` which parses an Inspect log file and upserts results. `init_db()` is idempotent and migrates older databases (adds the `match_metadata` column via `PRAGMA table_info` check). `get_scored_session_ids()` powers skip-already-scored caching.

**`monitor/run.py`** — CLI entry point plus `run_eval()`, a reusable function that wraps `get_scored_session_ids → inspect_eval → ingest_inspect_log`. `main()` calls `init_db` then delegates to `run_eval`. Two separate `asyncio.run()` calls because `inspect_eval` starts its own anyio event loop.

**`monitor/watcher.py`** — Background process that watches a directory with `watchdog`. `_SessionHandler` queues `.jsonl` files on create/modify events; `_flush_ready` processes files that have been stable for `--cooldown` seconds by copying them to a temp dir and calling `run_eval`. Errors per-batch are logged and skipped; already-scored sessions are handled transparently by `run_eval`'s `skip_ids` path.

**`monitor/allowlist.py`** — Persistent allowlist (`allowlist.json`) of known-safe patterns, one entry-list per scorer. `add_entries` / `for_scorer` are the write/read API. `entries_from_match_metadata` extracts the allowlistable value from a result's `match_metadata` dict. Loaded by `tasks.py` at eval startup; written by the API PATCH endpoint when a failure is marked safe.

**`benchmark/`** — measures LLM-scorer accuracy across models. `run.py` is the CLI (`--models`, `--report-only`); it evals each model against the 10 labelled sessions in `benchmark/sessions/` (ground truth in `labels.json`), compares predictions to labels, and persists per-run accuracy/latency/token/cost stats via `db.py` (`benchmark_runs`/`benchmark_results` tables, same SQLite file). `pricing.py` holds the per-1k-token rate table; `report.py` formats the comparison table.

**`api/`** — FastAPI app with route groups: `GET /sessions/`, `GET /sessions/{id}`, `GET /sessions/{id}/transcript`, `GET /results/{id}`, `PATCH /sessions/{id}/results/{scorer_name}`. Runs on port 8070. DB path is read from the `MONITOR_DB` environment variable at module load time; tests reload the modules via `importlib.reload` to pick up a test-specific path.

The `PATCH /sessions/{id}/results/{scorer_name}` endpoint accepts `{"marked_safe": true|false}` and is used to annotate known-false-positive failures. Marked-safe results are excluded from `total_failures` counts and from the `failed_only`/scorer filters on the session list. The annotation is preserved across re-ingestion — re-running the eval pipeline will not clobber it. When marking safe, the endpoint also writes the pattern to `allowlist.json` so future eval runs skip it automatically.

**`frontend/`** — Vue 3 + Vite. API calls go through `src/api/index.ts` (axios, baseURL `/api`). The Vite dev server proxies `/api` → `http://localhost:8070`. Two views: `SessionList` (with a reactive filter bar: failures-only checkbox, scorer dropdown, branch input) and `SessionDetail` (shows match-metadata callouts for failed scorers). Components: `FailureBadge`, `ScoreBar`, `TranscriptViewer`.

**`tests/`** — pytest with `asyncio_mode = "auto"`. Fixtures in `tests/fixtures/` are real Claude Code JSONL files used by integration tests. `test_api.py` seeds a real SQLite DB via an actual Inspect eval run against fixtures, then hits the FastAPI app through `TestClient`.

## Key constraints

- `inspect_eval` is synchronous and starts its own anyio event loop — never call it inside an `async` function or `asyncio.run()`.
- The `MONITOR_DB` env var must be set before importing `api.routes.*` — tests use `monkeypatch.setenv` + `importlib.reload` for this reason.
- Scorers read from `state.metadata`, not from model output — the passthrough solver is intentional.
- `tests/conftest.py` patches `tiktoken.get_encoding` (autouse) because mockllm's token counting otherwise downloads BPE data from OpenAI's servers, which fails in network-restricted CI.
- `fastapi` is pinned `<0.136.0` — 0.136.x was flagged malicious (MAL-2026-4750). Don't bump past it without checking the advisory is resolved.
