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

from monitor.allowlist import DEFAULT_ALLOWLIST
from monitor.db import DEFAULT_DB, get_scored_session_ids, ingest_inspect_log, init_db
from monitor.tasks import coding_agent_safety


def run_eval(
    *,
    log_dir: str,
    db: str = DEFAULT_DB,
    model: str = "anthropic/claude-haiku-4-5",
    allowed_hosts: list[str] | None = None,
    allowlist_path: str = DEFAULT_ALLOWLIST,
    inspect_log_dir: str = "inspect_logs/",
    verbose: bool = True,
) -> int:
    """Eval log_dir against all safety scorers and persist results.

    Returns the total number of result rows written. Already-scored sessions
    are skipped to avoid redundant LLM calls.

    Note: inspect_eval starts its own anyio loop, so this must NOT be called
    from inside an async function or an existing asyncio.run() call.
    """
    Path(inspect_log_dir).mkdir(parents=True, exist_ok=True)

    already_scored = asyncio.run(get_scored_session_ids(db))
    if already_scored and verbose:
        print(f"Skipping {len(already_scored)} already-scored session(s).")

    eval_logs = inspect_eval(
        coding_agent_safety(
            log_dir=log_dir,
            allowed_hosts=allowed_hosts or [],
            skip_ids=list(already_scored),
            allowlist_path=allowlist_path,
        ),
        model=model,
        log_dir=inspect_log_dir,
    )

    async def _ingest() -> int:
        total = 0
        for eval_log in eval_logs:
            rows = await ingest_inspect_log(eval_log.location, db)
            total += rows
            if verbose:
                print(f"Ingested {rows} result rows from {eval_log.location}")
        return total

    return asyncio.run(_ingest())


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
    p.add_argument(
        "--allowlist",
        default=DEFAULT_ALLOWLIST,
        help="Path to the allowlist JSON file (default: allowlist.json).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    # init_db is async; inspect_eval starts its own anyio loop. Keep them in
    # separate asyncio.run() calls so the loops never overlap.
    asyncio.run(init_db(args.db))
    total_rows = run_eval(
        log_dir=args.log_dir,
        db=args.db,
        model=args.model,
        allowed_hosts=args.allowed_host,
        allowlist_path=args.allowlist,
        inspect_log_dir=args.inspect_log_dir,
    )
    print(f"\nDone. {total_rows} result rows written to {args.db}.")


if __name__ == "__main__":
    main()
