"""SQLite persistence and queries for cost analysis (same database file as the safety results)."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aiosqlite

from monitor.cost.actions import classify
from monitor.cost.attribute import SessionCosts, attribute
from monitor.cost.findings import Thresholds, detect
from monitor.cost.parse import ParsedSession, parse_session

DEFAULT_DB = "monitor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_sessions (
    session_id TEXT PRIMARY KEY, log_path TEXT, log_mtime REAL, cwd TEXT, git_branch TEXT,
    started_at TEXT, ended_at TEXT, ingested_at TEXT,
    total_usd REAL, requests INTEGER, prompts INTEGER, baseline_tokens REAL,
    reported_usd REAL, reported_after_calls INTEGER, logged_usd_at_report REAL, unknown_models TEXT
);
CREATE TABLE IF NOT EXISTS cost_calls (
    session_id TEXT, idx INTEGER, message_id TEXT, thread TEXT, prompt_id TEXT, skill TEXT, model TEXT, ts TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    cache_write_5m_tokens INTEGER, cache_write_1h_tokens INTEGER,
    usd_input REAL, usd_output REAL, usd_cache_read REAL, usd_cache_write REAL, usd_web_search REAL,
    total_usd REAL, context_tokens INTEGER, rebuild_usd REAL, rebuild_excess_usd REAL, gap_seconds REAL,
    PRIMARY KEY (session_id, idx)
);
CREATE TABLE IF NOT EXISTS cost_prompts (
    session_id TEXT, prompt_id TEXT, origin TEXT, preview TEXT, ts TEXT, requests INTEGER, tool_calls INTEGER,
    output_usd REAL, write_usd REAL, read_usd REAL, other_usd REAL, total_usd REAL,
    PRIMARY KEY (session_id, prompt_id)
);
CREATE TABLE IF NOT EXISTS cost_tools (
    session_id TEXT, tool_use_id TEXT, name TEXT, family TEXT, action TEXT, target TEXT, prompt_id TEXT, skill TEXT,
    call_index INTEGER, is_error INTEGER, args_tokens REAL, result_tokens REAL,
    out_usd REAL, write_usd REAL, carry_usd REAL, own_usd REAL, carried_calls INTEGER,
    roundtrip_usd REAL, subagent_usd REAL,
    PRIMARY KEY (session_id, tool_use_id)
);
CREATE TABLE IF NOT EXISTS cost_skills (
    session_id TEXT, name TEXT, invocations INTEGER, load_tokens REAL,
    load_write_usd REAL, load_carry_usd REAL, load_usd REAL, span_calls INTEGER, span_usd REAL,
    PRIMARY KEY (session_id, name)
);
CREATE TABLE IF NOT EXISTS cost_buckets (session_id TEXT, bucket TEXT, usd REAL, PRIMARY KEY (session_id, bucket));
CREATE TABLE IF NOT EXISTS cost_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, kind TEXT, summary TEXT, suggestion TEXT,
    cost_usd REAL, savings_usd REAL, assumption TEXT, prompt_id TEXT, tool_use_id TEXT, skill TEXT, evidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_cost_tools_session ON cost_tools(session_id);
CREATE INDEX IF NOT EXISTS idx_cost_findings_session ON cost_findings(session_id);
"""
_PER_SESSION = (
    "cost_calls",
    "cost_prompts",
    "cost_tools",
    "cost_skills",
    "cost_buckets",
    "cost_findings",
)


async def init_cost_tables(db_path: str = DEFAULT_DB) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def _rows(
    table: str, session_id: str, items: Sequence[dict[str, Any]]
) -> list[tuple[str, list[tuple[Any, ...]]]]:
    if not items:
        return []
    cols = list(items[0])
    sql = f"INSERT INTO {table} (session_id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})"  # nosec B608
    return [(sql, [(session_id, *[it[c] for c in cols]) for it in items])]


def _records(
    sess: ParsedSession, costs: SessionCosts, findings: list[Any]
) -> dict[str, list[dict[str, Any]]]:
    calls = []
    for c, pc in zip(sess.calls, costs.calls, strict=True):
        u, b = c.usage, pc.breakdown
        calls.append(
            dict(
                idx=c.index, message_id=c.message_id, thread=c.thread, prompt_id=c.prompt_id, skill=c.skill,
                model=c.model, ts=c.ts, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                cache_read_tokens=u.cache_read_tokens, cache_write_5m_tokens=u.cache_write_5m_tokens,
                cache_write_1h_tokens=u.cache_write_1h_tokens, usd_input=b.input, usd_output=b.output,
                usd_cache_read=b.cache_read, usd_cache_write=b.cache_write, usd_web_search=b.web_search,
                total_usd=b.total, context_tokens=pc.context_tokens, rebuild_usd=pc.rebuild_usd,
                rebuild_excess_usd=pc.rebuild_excess_usd, gap_seconds=pc.gap_seconds,
            )
        )  # fmt: skip
    prompts = [
        dict(
            prompt_id=p.prompt_id, origin=p.origin, preview=p.preview, ts=p.ts, requests=p.calls,
            tool_calls=p.tool_calls, output_usd=p.output_usd, write_usd=p.write_usd, read_usd=p.read_usd,
            other_usd=p.other_usd, total_usd=p.total,
        )
        for p in costs.prompts.values()
    ]  # fmt: skip
    tools = []
    for t in costs.tools.values():
        family, action = classify(t.name, t.target)
        tools.append(
            dict(
                tool_use_id=t.tool_use_id, name=t.name, family=family, action=action, target=t.target,
                prompt_id=t.prompt_id, skill=t.skill, call_index=t.call_index,
                is_error=None if t.is_error is None else int(t.is_error), args_tokens=t.args_tokens,
                result_tokens=t.result_tokens, out_usd=t.out_usd, write_usd=t.write_usd, carry_usd=t.carry_usd,
                own_usd=t.own_usd, carried_calls=t.carried_calls, roundtrip_usd=t.roundtrip_usd,
                subagent_usd=t.subagent_usd,
            )
        )  # fmt: skip
    skills = [
        dict(
            name=s.name, invocations=s.invocations, load_tokens=s.load_tokens, load_write_usd=s.load_write_usd,
            load_carry_usd=s.load_carry_usd, load_usd=s.load_usd, span_calls=s.span_calls, span_usd=s.span_usd,
        )
        for s in costs.skills.values()
    ]  # fmt: skip
    buckets = [dict(bucket=k, usd=v) for k, v in costs.buckets.items()]
    fnd = [
        dict(
            kind=f.kind, summary=f.summary, suggestion=f.suggestion, cost_usd=f.cost_usd, savings_usd=f.savings_usd,
            assumption=f.assumption, prompt_id=f.prompt_id, tool_use_id=f.tool_use_id, skill=f.skill,
            evidence=json.dumps(f.evidence, default=str),
        )
        for f in findings
    ]  # fmt: skip
    return {
        "cost_calls": calls, "cost_prompts": prompts, "cost_tools": tools,
        "cost_skills": skills, "cost_buckets": buckets, "cost_findings": fnd,
    }  # fmt: skip


async def ingest_session(
    path: Path | str,
    db_path: str = DEFAULT_DB,
    *,
    preview_chars: int = 80,
    thresholds: Thresholds | None = None,
    force: bool = False,
) -> SessionCosts | None:
    """Parse, attribute and store one session. Idempotent: replaces that session's rows.

    Returns ``None`` when the log is unchanged since the last ingest (unless ``force``).
    """
    path = Path(path)
    mtime = path.stat().st_mtime
    await init_cost_tables(db_path)
    async with aiosqlite.connect(db_path) as db:
        if not force:
            async with db.execute(
                "SELECT log_mtime FROM cost_sessions WHERE session_id = ?", (path.stem,)
            ) as cur:
                row = await cur.fetchone()
            if row and row[0] == mtime:
                return None
    sess = parse_session(path, preview_chars=preview_chars)
    costs = attribute(sess)
    findings = detect(sess, costs, thresholds)
    stamps = [c.ts for c in sess.calls if c.ts]
    async with aiosqlite.connect(db_path) as db:
        for table in _PER_SESSION:
            await db.execute(f"DELETE FROM {table} WHERE session_id = ?", (sess.session_id,))  # nosec B608
        await db.execute(
            "INSERT OR REPLACE INTO cost_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sess.session_id, str(path), mtime, sess.cwd, sess.git_branch, min(stamps, default=None),
                max(stamps, default=None), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), costs.total_usd,
                len(sess.calls), len(sess.prompts), costs.baseline_tokens, costs.reported_usd,
                costs.reported_after_calls, costs.logged_usd_at_report, json.dumps(sorted(costs.unknown_models)),
            ),
        )  # fmt: skip
        for table, items in _records(sess, costs, findings).items():
            for sql, params in _rows(table, sess.session_id, items):
                await db.executemany(sql, params)
        await db.commit()
    return costs


async def ingest_dir(
    log_dir: Path | str, db_path: str = DEFAULT_DB, *, force: bool = False, preview_chars: int = 80
) -> list[SessionCosts]:
    done = []
    for f in sorted(Path(log_dir).glob("*.jsonl")):
        if (
            c := await ingest_session(f, db_path, force=force, preview_chars=preview_chars)
        ) is not None:
            done.append(c)
    return done


# ---------- queries ---------------------------------------------------------------


async def _fetch(db_path: str, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return [dict(r) async for r in cur]


async def list_sessions(db_path: str, limit: int = 50) -> list[dict[str, Any]]:
    return await _fetch(
        db_path,
        "SELECT session_id, cwd, git_branch, started_at, ended_at, total_usd, requests, prompts, reported_usd, "
        "logged_usd_at_report, unknown_models FROM cost_sessions ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )


async def session_detail(db_path: str, session_id: str) -> dict[str, Any] | None:
    rows = await _fetch(db_path, "SELECT * FROM cost_sessions WHERE session_id = ?", (session_id,))
    if not rows:
        return None
    s = rows[0]
    s["unknown_models"] = json.loads(s["unknown_models"] or "[]")
    s["buckets"] = {
        r["bucket"]: r["usd"]
        for r in await _fetch(
            db_path, "SELECT bucket, usd FROM cost_buckets WHERE session_id = ?", (session_id,)
        )
    }
    s["models"] = await _fetch(
        db_path,
        "SELECT model, COUNT(*) AS requests, SUM(total_usd) AS usd FROM cost_calls WHERE session_id = ? "
        "GROUP BY model ORDER BY usd DESC",
        (session_id,),
    )
    s["token_totals"] = (
        await _fetch(
            db_path,
            "SELECT SUM(input_tokens) AS input, SUM(output_tokens) AS output, SUM(cache_read_tokens) AS cache_read, "
            "SUM(cache_write_5m_tokens + cache_write_1h_tokens) AS cache_write FROM cost_calls WHERE session_id = ?",
            (session_id,),
        )
    )[0]
    s["unlogged_usd"] = (
        None
        if s["reported_usd"] is None
        else max(s["reported_usd"] - (s["logged_usd_at_report"] or 0.0), 0.0)
    )
    return s


async def prompts(
    db_path: str, session_id: str, limit: int = 20, by: str = "total_usd"
) -> list[dict[str, Any]]:
    order = by if by in {"total_usd", "ts", "requests", "tool_calls", "read_usd"} else "total_usd"
    return await _fetch(
        db_path,
        f"SELECT * FROM cost_prompts WHERE session_id = ? ORDER BY {order} DESC LIMIT ?",  # nosec B608
        (session_id, limit),
    )


_GROUPS = {
    "name": "name",
    "family": "family",
    "action": "action",
    "skill": "COALESCE(skill, '(none)')",
}


async def tools_grouped(
    db_path: str, group_by: str = "name", session_id: str | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    """Aggregate tool cost by name / family / action / active skill (``own`` is additive; ``roundtrip`` is not)."""
    col = _GROUPS.get(group_by, "name")
    return await _fetch(
        db_path,
        f"SELECT {col} AS key, COUNT(*) AS invocations, SUM(own_usd) AS own_usd, "  # nosec B608
        "SUM(out_usd) AS out_usd, SUM(write_usd) AS write_usd, SUM(carry_usd) AS carry_usd, "
        "SUM(roundtrip_usd) AS roundtrip_usd, SUM(subagent_usd) AS subagent_usd, "
        "AVG(own_usd) AS avg_own_usd, AVG(roundtrip_usd) AS avg_roundtrip_usd, "
        "AVG(result_tokens) AS avg_result_tokens, SUM(COALESCE(is_error, 0)) AS errors "
        "FROM cost_tools WHERE (? IS NULL OR session_id = ?) GROUP BY key ORDER BY own_usd DESC LIMIT ?",
        (session_id, session_id, limit),
    )


async def top_invocations(
    db_path: str, session_id: str | None = None, limit: int = 20, by: str = "own_usd"
) -> list[dict[str, Any]]:
    order = by if by in {"own_usd", "roundtrip_usd", "result_tokens", "carry_usd"} else "own_usd"
    return await _fetch(
        db_path,
        "SELECT session_id, tool_use_id, name, action, target, prompt_id, skill, result_tokens, carried_calls, "  # nosec B608
        f"out_usd, write_usd, carry_usd, own_usd, roundtrip_usd, is_error FROM cost_tools "
        f"WHERE (? IS NULL OR session_id = ?) ORDER BY {order} DESC LIMIT ?",
        (session_id, session_id, limit),
    )


async def skills(db_path: str, session_id: str | None = None) -> list[dict[str, Any]]:
    return await _fetch(
        db_path,
        "SELECT name, COUNT(*) AS sessions, SUM(invocations) AS invocations, AVG(load_tokens) AS avg_load_tokens, "
        "SUM(load_write_usd) AS load_write_usd, SUM(load_carry_usd) AS load_carry_usd, SUM(load_usd) AS load_usd, "
        "SUM(span_calls) AS span_calls, SUM(span_usd) AS span_usd FROM cost_skills "
        "WHERE (? IS NULL OR session_id = ?) GROUP BY name ORDER BY load_usd DESC",
        (session_id, session_id),
    )


async def findings(
    db_path: str, session_id: str | None = None, kind: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    rows = await _fetch(
        db_path,
        "SELECT * FROM cost_findings WHERE (? IS NULL OR session_id = ?) AND (? IS NULL OR kind = ?) "
        "ORDER BY COALESCE(savings_usd, 0) DESC, cost_usd DESC LIMIT ?",
        (session_id, session_id, kind, kind, limit),
    )
    for r in rows:
        r["evidence"] = json.loads(r["evidence"] or "{}")
    return rows


async def waste_summary(db_path: str, session_id: str | None = None) -> list[dict[str, Any]]:
    """Findings rolled up by kind: how many, what they cost, what fixing them might save."""
    return await _fetch(
        db_path,
        "SELECT kind, COUNT(*) AS findings, SUM(cost_usd) AS cost_usd, SUM(COALESCE(savings_usd, 0)) AS savings_usd "
        "FROM cost_findings WHERE (? IS NULL OR session_id = ?) GROUP BY kind ORDER BY savings_usd DESC, cost_usd DESC",
        (session_id, session_id),
    )


async def bucket_totals(db_path: str, session_id: str | None = None) -> dict[str, float]:
    """Every dollar, by owner: context buckets plus ``tool_calls`` and ``skill_text`` (sums to the total)."""
    rows = await _fetch(
        db_path,
        "SELECT bucket, SUM(usd) AS usd FROM cost_buckets WHERE (? IS NULL OR session_id = ?) GROUP BY bucket",
        (session_id, session_id),
    )
    out = {r["bucket"]: r["usd"] for r in rows}
    out["tool_calls"] = (
        await _fetch(
            db_path,
            "SELECT COALESCE(SUM(own_usd), 0) AS v FROM cost_tools WHERE (? IS NULL OR session_id = ?)",
            (session_id, session_id),
        )
    )[0]["v"]
    out["skill_text"] = (
        await _fetch(
            db_path,
            "SELECT COALESCE(SUM(load_usd), 0) AS v FROM cost_skills WHERE (? IS NULL OR session_id = ?)",
            (session_id, session_id),
        )
    )[0]["v"]
    return out


async def overall(db_path: str, session_id: str | None = None) -> dict[str, Any]:
    """Totals for one session or all of them, including how much of the bill each cost class is."""
    row = (
        await _fetch(
            db_path,
            "SELECT COUNT(*) AS sessions, SUM(total_usd) AS total_usd, SUM(requests) AS requests, "
            "SUM(prompts) AS prompts, SUM(reported_usd) AS reported_usd, SUM(logged_usd_at_report) AS logged_at_report "
            "FROM cost_sessions WHERE (? IS NULL OR session_id = ?)",
            (session_id, session_id),
        )
    )[0]
    split = (
        await _fetch(
            db_path,
            "SELECT SUM(usd_input + usd_cache_write) AS write_usd, SUM(usd_cache_read) AS read_usd, "
            "SUM(usd_output) AS output_usd, SUM(usd_web_search) AS web_usd FROM cost_calls "
            "WHERE (? IS NULL OR session_id = ?)",
            (session_id, session_id),
        )
    )[0]
    return {**row, **split}
