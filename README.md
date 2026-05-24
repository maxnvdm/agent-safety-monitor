# Coding Agent Safety Monitor

Most coding agent failures go undetected. The agent reads a file it shouldn't, leaks a credential into a log, installs a suspicious package, or quietly does something different from what it described — and nobody notices until much later, if at all. This tool monitors Claude Code sessions for those failures automatically, using [Inspect AI](https://inspect.aisi.org.uk/) as the eval harness and a small FastAPI + Vue 3 dashboard to visualise results.

## Architecture

```
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

The exfiltration scorer accepts an allowlist of trusted hosts via `--allowed-host` (repeatable). Hosts and their subdomains are permitted:

```bash
uv run python -m monitor.run --log-dir logs/ --model groq/llama-3.3-70b-versatile \
  --allowed-host api.github.com \
  --allowed-host pypi.org
```

## How to Run

### Prerequisites

- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- Node.js 18+ with [pnpm](https://pnpm.io/)

### Install

```bash
uv sync
cd frontend && pnpm install
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

To try it with the included sample logs (no real logs needed):

```bash
uv run python -m monitor.run --log-dir samples/ --model groq/llama-3.3-70b-versatile
```

### 2. Start the API

```bash
uv run uvicorn api.main:app --reload --port 8070
```

The API runs at `http://localhost:8070`. Set `MONITOR_DB` to point at a non-default database path.

### 3. Start the frontend

```bash
cd frontend && pnpm dev
```

Open `http://localhost:5173`. The dev server proxies `/api` to the FastAPI backend.

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
uv run mypy monitor/ api/

# Tests
uv run pytest

# Lint (frontend)
cd frontend && pnpm lint

# Type check (frontend)
cd frontend && pnpm typecheck
```

## Known Limitations

- **Regex scorers have false positives.** The secret leakage scorer will flag any string that looks like a key, even in test fixtures or documentation. Tune the patterns in `_SECRET_PATTERNS` for your environment.
- **LLM scorers need an API key.** Each session runs two LLM calls (deceptive reasoning + supply chain, when triggered). Groq's free tier is sufficient for most dev use; use `--model mockllm/model` to skip LLM grading entirely.
- **Claude Code logs only.** The ingest layer (`monitor/ingest.py`) parses Claude Code's specific JSONL event format. Other agents (Cursor, Codex CLI) would need their own ingest adapters.
- **No real-time monitoring.** The current flow is batch: run evals, then view results. A WebSocket endpoint could stream Inspect progress to the frontend as sessions are scored.
