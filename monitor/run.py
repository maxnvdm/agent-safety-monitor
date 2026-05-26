"""CLI: run Inspect evals on Claude Code logs and persist results to SQLite.

Usage:
    uv run python -m monitor.run                                  # default model + paths
    uv run python -m monitor.run --model anthropic/claude-haiku-4-5
    uv run python -m monitor.run --log-dir samples/ --db demo.db
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from inspect_ai import eval as inspect_eval

from monitor.db import DEFAULT_DB, get_scored_session_ids, ingest_inspect_log, init_db
from monitor.tasks import coding_agent_safety


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run safety scorers on Claude Code logs.")
    p.add_argument("--log-dir", default="logs/", help="Directory of Claude Code .jsonl logs.")
    p.add_argument(
        "--inspect-log-dir",
        default="inspect_logs/",
        help="Where Inspect writes its .eval logs.",
    )
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database path.")
    p.add_argument(
        "--model",
        default="anthropic/claude-haiku-4-5",
        help="Model for LLM-graded scorers. Use 'mockllm/model' for dry runs without API calls.",
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Whitelisted host for exfiltration_attempt (repeatable).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    Path(args.inspect_log_dir).mkdir(parents=True, exist_ok=True)

    # init_db and ingest_inspect_log are async, but inspect_eval is sync and
    # starts its own anyio loop. We run them in separate asyncio.run() calls
    # so the loops never overlap.
    asyncio.run(init_db(args.db))

    already_scored = asyncio.run(get_scored_session_ids(args.db))
    if already_scored:
        print(f"Skipping {len(already_scored)} already-scored session(s).")

    eval_logs = inspect_eval(
        coding_agent_safety(
            log_dir=args.log_dir,
            allowed_hosts=args.allowed_host,
            skip_ids=list(already_scored),
        ),
        model=args.model,
        log_dir=args.inspect_log_dir,
    )

    async def ingest_all() -> int:
        total = 0
        for eval_log in eval_logs:
            rows = await ingest_inspect_log(eval_log.location, args.db)
            total += rows
            print(f"Ingested {rows} result rows from {eval_log.location}")
        return total

    total_rows = asyncio.run(ingest_all())
    print(f"\nDone. {total_rows} result rows written to {args.db}.")


if __name__ == "__main__":
    main()
