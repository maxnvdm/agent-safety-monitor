# Coding Agent Safety Monitor

Most coding agent failures go undetected. The agent reads a file it shouldn't, leaks a credential into a log, installs a suspicious package, or quietly does something different from what it described — and nobody notices until much later, if at all. This tool monitors Claude Code sessions for those failures automatically, using [Inspect AI](https://inspect.aisi.org.uk/) as the eval harness and a small FastAPI + Vue 3 dashboard to visualise results.

## Architecture

```text
Claude Code session logs (.jsonl)
        │
        ▼
  monitor/ingest.py          Parse events → Inspect Samples
        │
        ▼
  monitor/tasks.py           Inspect Task (passthrough solver + 6 scorers)
        │
        ▼
  monitor/scorers.py         Score each session for failure modes
        │
        ▼
  monitor/db.py              Write results to SQLite
        │
        ▼
  api/ (FastAPI)             Serve sessions + results over HTTP
        │
        ▼
  frontend/ (Vue 3)          Dashboard: session list + per-session breakdown
```

## Failure Modes

| Scorer | What it detects | Method |
|---|---|---|
| `secret_leakage` | API keys, tokens, Bearer headers in tool results | Regex patterns |
| `scope_creep` | File access (Read/Write/Edit/Bash) outside the session working directory | Path analysis |
| `exfiltration_attempt` | Outbound HTTP calls (WebFetch, curl via Bash) to non-allowlisted hosts | URL extraction |
| `privilege_escalation` | `sudo`, `chmod`, `chown`, `mount`, and similar commands in Bash tool calls | Command segment analysis |
| `deceptive_reasoning` | Agent's stated plan doesn't match its actual tool calls | LLM-graded |
| `supply_chain_risk` | Suspicious package installs or dependency manifest changes | LLM-graded (with pre-filter) |

Pattern-based scorers are fast and cheap. `deceptive_reasoning` always invokes the LLM. `supply_chain_risk` skips the LLM call entirely if no dependency file was modified and no install command ran.

When a scorer fails a session, it records the specific evidence (the matched secret pattern, the exfiltration URL, the out-of-scope path, the escalating command) as structured **match metadata**. This is stored as JSON in the database, returned by the API, and displayed inline on the session detail page.

The exfiltration scorer accepts an allowlist of trusted hosts via `--allowed-host` (repeatable). Hosts and their subdomains are permitted:

```bash
uv run python -m monitor.run --log-dir logs/ --model groq/llama-3.3-70b-versatile \
  --allowed-host api.github.com \
  --allowed-host pypi.org
```

## How to Run

### Prerequisites

- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- Node.js 20+ with [pnpm](https://pnpm.io/)

### Install

```bash
uv sync
cd frontend && pnpm install
pre-commit install   # wire up ruff/mypy/bandit as git pre-commit hooks
```

### 1. Set up API keys

The LLM-graded scorers (`deceptive_reasoning`, `supply_chain_risk`) require a model API key. The runner accepts any model supported by Inspect AI — tested options:

| Provider | Model string | Free tier |
|---|---|---|
| [Groq](https://console.groq.com) | `groq/llama-3.3-70b-versatile` | Yes |
| [Anthropic](https://console.anthropic.com) | `anthropic/claude-sonnet-4-6` | No |
| mock (no API) | `mockllm/model` | — |

Set your key in `.env.local` (gitignored):

```bash
echo "GROQ_API_KEY=your-key-here" >> .env.local
```

Or use [varlock](https://varlock.dev) for encrypted local secrets — a `.env.schema` is included.

### 2. Run evals

Point the runner at a directory of Claude Code `.jsonl` session logs:

```bash
uv run python -m monitor.run --log-dir logs/ --model groq/llama-3.3-70b-versatile
```

Results are written to `monitor.db`. Use `--db` to specify a different path, `--inspect-log-dir` for Inspect's own logs.

Re-running against the same log directory is cheap: sessions that already have results in the database are skipped before the eval starts, so no LLM calls are repeated for them.

To try it with the included sample logs (no real logs needed):

```bash
uv run python -m monitor.run --log-dir samples/ --model groq/llama-3.3-70b-versatile
```

### 3. Start the API

```bash
uv run uvicorn api.main:app --reload --port 8070
```

The API runs at `http://localhost:8070`. Set `MONITOR_DB` to point at a non-default database path.

`GET /sessions/` supports server-side filtering:

| Query param | Effect |
|---|---|
| `failed_only=true` | Only sessions with at least one failed scorer |
| `scorer=secret_leakage` | Only sessions where that scorer failed |
| `branch=main` | Only sessions from that git branch |

### 4. Start the frontend

```bash
cd frontend && pnpm dev
```

Open `http://localhost:5173`. The dev server proxies `/api` to the FastAPI backend on port 8070. The session list has a filter bar (failures only, scorer, branch) that maps directly onto the API filters above.

## Benchmarking the LLM Scorers

The two LLM-graded scorers are only as good as the model behind them. The `benchmark/` package measures how accurately different models grade `deceptive_reasoning` and `supply_chain_risk` against 10 hand-labelled sessions, and what each model costs:

```bash
uv run python -m benchmark.run \
  --models anthropic/claude-haiku-4-5 openai/gpt-4o-mini groq/llama-3.3-70b-versatile
```

Each model is evaluated against the labelled sessions in `benchmark/sessions/` (ground truth in `benchmark/labels.json`). Per-run accuracy, average latency, token counts, and estimated cost are stored in the `benchmark_runs` / `benchmark_results` tables and printed as a comparison table:

```text
  deceptive_reasoning
  ─────────────────────────────────────────────────────────────────────
  Model                                Accuracy  Avg latency  Est cost/session  Breakdown
  claude-haiku-4-5                          80%        1.42s         $0.000412  ████████░░ 4/5
  gpt-4o-mini                               60%        0.98s         $0.000188  ██████░░░░ 3/5
```

Use `--report-only` to re-print the latest stored results without running any evals. See [`benchmark/README.md`](benchmark/README.md) for how to add labelled sessions or new models to the pricing table.

## Auto-ingestion: Watching a Log Directory

Running `monitor.run` manually is fine for occasional audits. For continuous monitoring, the watcher process ingests sessions automatically as they appear:

```bash
uv run python -m monitor.watcher \
  --log-dir ~/.claude/projects/my-project/ \
  --model groq/llama-3.3-70b-versatile
```

The watcher monitors the directory for new or modified `.jsonl` files. After a file hasn't changed for `--cooldown` seconds (default: 30), it's considered complete and run through the eval pipeline. Already-scored sessions are skipped, so restarting the watcher is always safe.

```
Options:
  --log-dir      Directory to watch (required)
  --model        Model for LLM-graded scorers (required)
  --db           SQLite database path (default: monitor.db)
  --cooldown     Inactivity seconds before ingesting (default: 30)
  --allowed-host Allowlisted host for exfiltration scorer (repeatable)
```

Point `--log-dir` at `~/.claude/projects/` (all projects) or a specific project subdirectory to limit scope.

## Extending: Adding a New Scorer

Each scorer is a function decorated with `@scorer` that returns an async `score(state, target)` function. Add yours to `monitor/scorers.py`:

```python
@scorer(metrics=[accuracy()])
def my_new_check() -> Any:
    """Describe what failure mode this catches."""

    async def score(state: TaskState, target: Any) -> Score:
        tool_calls = _tool_calls(state)
        for tc in tool_calls:
            if tc.tool_name == "Bash" and "dangerous-thing" in (tc.tool_input.get("command") or ""):
                return Score(value=INCORRECT, explanation="Found dangerous-thing in Bash call.")
        return Score(value=CORRECT, explanation="No dangerous-thing detected.")

    return score
```

Then register it in `monitor/tasks.py` inside `coding_agent_safety()`:

```python
scorer=[
    ...
    my_new_check(),
]
```

That's it. The scorer will appear in the database and dashboard automatically.

## Development

```bash
# Lint + format (Python)
uv run ruff check .
uv run ruff format .

# Type check (Python)
uv run mypy .

# Security scan (Python)
uv run bandit -r . --exclude ./tests,./build,./.venv

# Tests
uv run pytest

# Lint (frontend)
cd frontend && pnpm lint

# Type check (frontend)
cd frontend && pnpm typecheck

# Run all pre-commit hooks manually
uv run pre-commit run --all-files
```

## Known Limitations

- **Regex scorers have false positives.** The secret leakage scorer will flag any string that looks like a key, even in test fixtures or documentation. Tune the patterns in `_SECRET_PATTERNS` for your environment.
- **LLM scorers need an API key.** Each session runs two LLM calls (deceptive reasoning + supply chain, when triggered). Groq's free tier is sufficient for most dev use; use `--model mockllm/model` to skip LLM grading entirely.
- **Claude Code logs only.** The ingest layer (`monitor/ingest.py`) parses Claude Code's specific JSONL event format. Other agents (Cursor, Codex CLI) would need their own ingest adapters.
- **No real-time monitoring.** The watcher ingests sessions as files appear, but scoring happens after the cooldown period (default: 30s after the session ends). Results are not pushed to the frontend — you need to reload the dashboard to see them. A WebSocket endpoint could push new results as they land.
