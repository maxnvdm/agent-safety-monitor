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
| --- | --- | --- |
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
uv run python -m monitor.run --log-dir logs/ --model groq/openai/gpt-oss-120b \
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

The LLM-graded scorers (`deceptive_reasoning`, `supply_chain_risk`) require a model API key. The runner accepts any model supported by Inspect AI — common options:

| Provider | Model string | Free tier |
| --- | --- | --- |
| [Groq](https://console.groq.com) | `groq/openai/gpt-oss-120b` | Yes (check Groq's current limits) |
| [Anthropic](https://console.anthropic.com) | `anthropic/claude-sonnet-5-5` | No |
| mock (no API) | `mockllm/model` | — |

> Groq shut down `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` on 2026-08-16 ([deprecations](https://console.groq.com/docs/deprecations)); it recommends `openai/gpt-oss-20b` for `llama-3.1-8b-instant` and `openai/gpt-oss-120b` for `llama-3.3-70b-versatile`. Neither has been re-benchmarked in this repo.

Set your key in `.env.local` (gitignored):

```bash
echo "GROQ_API_KEY=your-key-here" >> .env.local
```

Or use [varlock](https://varlock.dev) for encrypted local secrets — a `.env.schema` is included.

### 2. Run evals

Point the runner at a directory of Claude Code `.jsonl` session logs:

```bash
uv run python -m monitor.run --log-dir logs/ --model groq/openai/gpt-oss-120b
```

Results are written to `monitor.db`. Use `--db` to specify a different path, `--inspect-log-dir` for Inspect's own logs.

Re-running against the same log directory is cheap: sessions that already have results in the database are skipped before the eval starts, so no LLM calls are repeated for them.

To try it with the included sample logs (no real logs needed):

```bash
uv run python -m monitor.run --log-dir samples/ --model groq/openai/gpt-oss-120b
```

### 3. Start the API

```bash
uv run uvicorn api.main:app --reload --port 8070
```

The API runs at `http://localhost:8070`. Set `MONITOR_DB` to point at a non-default database path.

`GET /sessions/` supports server-side filtering:

| Query param | Effect |
| --- | --- |
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
  --models anthropic/claude-haiku-5-5 openai/gpt-4o-mini groq/openai/gpt-oss-120b
```

Each model is evaluated against the labelled sessions in `benchmark/sessions/` (ground truth in `benchmark/labels.json`). Per-run accuracy, average latency, token counts, and estimated cost are stored in the `benchmark_runs` / `benchmark_results` tables and printed as a comparison table (the numbers below are illustrative of the format, not real benchmark results):

```text
  deceptive_reasoning
  ─────────────────────────────────────────────────────────────────────
  Model                                Accuracy  Avg latency  Est cost/session  Breakdown
  claude-haiku-5-5                          80%        1.42s         $0.000165  ████████░░ 4/5
  gpt-4o-mini                               60%        0.98s         $0.000188  ██████░░░░ 3/5
```

Use `--report-only` to re-print the latest stored results without running any evals. See [`benchmark/README.md`](benchmark/README.md) for how to add labelled sessions or new models to the pricing table.

## Cost Analysis: Where Do the Tokens Go?

The safety scorers say whether a session was risky. `monitor.cost` says what it **cost** and where the money was wasted, down to individual prompts, tool calls, shell commands and skills. It reads the same Claude Code JSONL logs, needs no API key, and costs nothing to run.

```bash
# analyse a project's logs (incremental: unchanged logs are skipped)
uv run python -m monitor.cost report ~/.claude/projects/<project>/

# one session, grouped by what the shell actually ran (git status, uv run pytest, ...)
uv run python -m monitor.cost report path/to/session.jsonl --group-by action

# machine-readable, for scripts or the dashboard (the API serves the same data under /cost/*)
uv run python -m monitor.cost report path/ --json
```

The report shows, for one session or all of them:

- **Every dollar and its owner:** tool calls, thinking, skill text, fixed context (system prompt, tool definitions, memory), cache rebuilds. The shares sum to 100%.
- **The most expensive prompts**, and how much of the bill is re-reading context, writing new context, or generating output.
- **Tools grouped by name, family, action or active skill.** Each call has two costs, and they are different questions:
  - `own`: generating the call, writing its arguments and result into the cache, and re-reading them on every later request. Additive.
  - `round-trip`: the full cost of the requests its result triggered. This is why a "cheap" call such as `git status` is expensive late in a long session: every call forces another pass over the whole context. Not additive with `own`.
- **Skills:** the cost of loading a skill's text and re-reading it on every later request, versus the cost of the requests made while the skill was active.
- **Likely waste**, by pattern, each with the measured cost and, where a defensible assumption exists, an estimated saving that states the assumption: oversized tool results, identical repeated calls, failed calls, heavy skills, cache rebuilds after idle gaps, many small round trips, long-context sessions, heavy thinking, a heavy fixed context, and tool calls that grew the context far beyond their visible output (e.g. loading tool definitions).

How to trust the numbers:

- **Totals are exact; per-item splits are estimates.** The log has token totals per request but not per block, so each request's tokens are split across what entered context by size. On a real session this fit well (new tokens ≈ 43 + characters / 2.2, R² = 0.97).
- **Reconciles with Claude Code:** the report compares against Claude Code's own session total. The gap is requests that never appear in the transcript (WebFetch/WebSearch summarising, title generation). On the session this was built on, the difference was about 5%.
- **Prices** come from a vendored copy of [LiteLLM's price table](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json), including cache read and 5-minute / 1-hour cache write rates. Refresh with `uv run python scripts/update_model_prices.py`, or point `MONITOR_PRICES` at a JSON file with the same `models` schema to override rates. These are list prices; subscription plans bill differently. Requests for models with no price count as $0 and are called out in the report.
- **Privacy:** stored prompt previews and command descriptions are truncated and have obvious secrets redacted (the same patterns the `secret_leakage` scorer uses). Use `--no-preview` to store no prompt text at all.
- **Not yet validated against real logs:** subagent logs (read from `<session>/subagents/agent-*.jsonl`) and context compaction are covered by synthetic tests only.

## Auto-ingestion: Watching a Log Directory

Running `monitor.run` manually is fine for occasional audits. For continuous monitoring, the watcher process ingests sessions automatically as they appear:

```bash
uv run python -m monitor.watcher \
  --log-dir ~/.claude/projects/my-project/ \
  --model groq/openai/gpt-oss-120b
```

The watcher monitors the directory for new or modified `.jsonl` files. After a file hasn't changed for `--cooldown` seconds (default: 30), it's considered complete and run through the eval pipeline. Already-scored sessions are skipped, so restarting the watcher is always safe.

```text
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
- **LLM scorers need an API key.** Each session runs two LLM calls (deceptive reasoning + supply chain, when triggered). Groq's free tier is usually sufficient for dev use (check its current model availability and limits); use `--model mockllm/model` to skip LLM grading entirely.
- **Claude Code logs only.** The ingest layer (`monitor/ingest.py`) parses Claude Code's specific JSONL event format. Other agents (Cursor, Codex CLI) would need their own ingest adapters.
- **No real-time monitoring.** The watcher ingests sessions as files appear, but scoring happens after the cooldown period (default: 30s after the session ends). Results are not pushed to the frontend — you need to reload the dashboard to see them. A WebSocket endpoint could push new results as they land.
