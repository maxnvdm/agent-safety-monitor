"""``python -m monitor.cost``: find where Claude Code sessions spend tokens and money.

# analyse a project's logs (ingest is incremental; unchanged logs are skipped)
uv run python -m monitor.cost report ~/.claude/projects/-home-me-myrepo/

# one session, grouped by what the shell actually ran
uv run python -m monitor.cost report path/to/session.jsonl --group-by action

# numbers for scripts / the dashboard
uv run python -m monitor.cost report path/ --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from monitor.cost import report as rpt
from monitor.cost import store


class _UserError(Exception):
    """A usage problem to report as a plain message (raised inside the loop, exited outside it)."""


async def _gather(args: argparse.Namespace) -> dict[str, Any]:
    db = args.db
    await store.init_cost_tables(db)
    scope: list[str] = []  # sessions the user asked about (sessions are keyed by file stem)
    for p in map(Path, args.paths):
        if p.is_dir():
            await store.ingest_dir(
                p, db, force=args.force, preview_chars=0 if args.no_preview else 80
            )
            scope += sorted(f.stem for f in p.glob("*.jsonl"))
        elif p.is_file():
            await store.ingest_session(
                p, db, force=args.force, preview_chars=0 if args.no_preview else 80
            )
            scope.append(p.stem)
        else:
            raise _UserError(f"no such file or directory: {p}")
    # The database may hold sessions from earlier runs: report only on what was asked for.
    sid: str | None = args.session or (scope[0] if len(scope) == 1 else None)
    where: store.Scope = sid or scope
    overall = await store.overall(db, where)
    if not overall["sessions"]:
        raise _UserError("nothing to report: no sessions ingested (pass log files or a directory)")
    return {
        "session_id": sid,
        "overall": overall,
        "detail": await store.session_detail(db, sid) if sid else None,
        "buckets": await store.bucket_totals(db, where),
        "prompts": await store.prompts(db, where, args.top),
        "tools": await store.tools_grouped(db, args.group_by, where, args.top),
        "invocations": await store.top_invocations(db, where, args.top),
        "skills": await store.skills(db, where, args.top),
        "waste": await store.waste_summary(db, where),
        "findings": await store.findings(db, where, limit=args.top),
        "group_by": args.group_by,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m monitor.cost", description="Cost analysis for Claude Code sessions."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="ingest logs and print where the money went")
    r.add_argument("paths", nargs="+", help="session .jsonl files or directories of them")
    r.add_argument(
        "--db", default=store.DEFAULT_DB, help=f"SQLite file (default: {store.DEFAULT_DB})"
    )
    r.add_argument(
        "--session", help="report on one session id (default: everything ingested from PATHS)"
    )
    r.add_argument("--group-by", choices=["name", "family", "action", "skill"], default="action")
    r.add_argument("--top", type=int, default=10, help="rows per table")
    r.add_argument("--force", action="store_true", help="re-ingest even if the log is unchanged")
    r.add_argument(
        "--no-preview", action="store_true", help="do not store any prompt text in the database"
    )
    r.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args(argv)

    try:
        data = asyncio.run(_gather(args))
    except _UserError as e:
        raise SystemExit(str(e)) from None
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(rpt.render(**data))


if __name__ == "__main__":
    main()
