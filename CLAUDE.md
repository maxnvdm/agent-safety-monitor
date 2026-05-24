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
uv run mypy monitor/ api/

# Run evals against sample logs (uses mockllm to avoid API cost)
uv run python -m monitor.run --log-dir samples/ --model mockllm/model

# Start the API (default db: monitor.db, override with MONITOR_DB env var)
uv run uvicorn api.main:app --reload --port 8070

# Frontend: install deps
cd frontend && pnpm install

# Frontend: dev server (proxies /api → localhost:8000)
cd frontend && pnpm dev

# Frontend: lint
cd frontend && pnpm lint

# Frontend: type check
cd frontend && pnpm typecheck
```

## Architecture

The system is a pipeline: Claude Code JSONL logs → Inspect AI eval → SQLite → FastAPI → Vue 3 dashboard.

**`monitor/ingest.py`** — parses Claude Code's JSONL event format. Events have a `type` field (`user`/`assistant`); tool calls live inside assistant content blocks; tool results come back in the next user event. Produces `ToolCallRecord` and `ToolResultRecord` dataclasses, a rendered transcript string, and session metadata (cwd, git branch, session ID).

**`monitor/scorers.py`** — six `@scorer`-decorated functions, one per failure mode. Each returns an inner `async score(state, target)` that reads from `state.metadata` (populated by `sessions_dataset`). The four pattern-based scorers (`secret_leakage`, `scope_creep`, `exfiltration_attempt`, `privilege_escalation`) work on `tool_calls`/`tool_results` lists. The two LLM-graded scorers (`deceptive_reasoning`, `supply_chain_risk`) operate on the transcript string; `supply_chain_risk` has a pre-filter that skips the LLM if no dependency files were touched.

**`monitor/tasks.py`** — wires scorers into an Inspect `Task` via `coding_agent_safety()`. Uses a `passthrough` no-op solver (avoids burning a model call per sample since scorers work on pre-built metadata, not model output). `sessions_dataset()` builds one `Sample` per `.jsonl` file, with all scorer inputs in `metadata`.

**`monitor/db.py`** — SQLite schema (`sessions`, `results` tables) and `ingest_inspect_log()` which parses an Inspect log file and upserts results. `init_db()` is idempotent.

**`monitor/run.py`** — CLI entry point. Parses args, calls `init_db`, runs `inspect_eval` (sync), then ingests all resulting logs. Two separate `asyncio.run()` calls because `inspect_eval` starts its own event loop.

**`api/`** — FastAPI app with three route groups: `GET /sessions/`, `GET /sessions/{id}`, `GET /sessions/{id}/transcript`, `GET /results/{id}`. Runs on port 8070. DB path is read from the `MONITOR_DB` environment variable at module load time; tests reload the modules via `importlib.reload` to pick up a test-specific path.

**`frontend/`** — Vue 3 + Vite. API calls go through `src/api/index.ts` (axios, baseURL `/api`). The Vite dev server proxies `/api` → `http://localhost:8000`. Two views: `SessionList` and `SessionDetail`. Components: `FailureBadge`, `ScoreBar`, `TranscriptViewer`.

**`tests/`** — pytest with `asyncio_mode = "auto"`. Fixtures in `tests/fixtures/` are real Claude Code JSONL files used by integration tests. `test_api.py` seeds a real SQLite DB via an actual Inspect eval run against fixtures, then hits the FastAPI app through `TestClient`.

## Key constraints

- `inspect_eval` is synchronous and starts its own anyio event loop — never call it inside an `async` function or `asyncio.run()`.
- The `MONITOR_DB` env var must be set before importing `api.routes.*` — tests use `monkeypatch.setenv` + `importlib.reload` for this reason.
- Scorers read from `state.metadata`, not from model output — the passthrough solver is intentional.
