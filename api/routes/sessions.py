"""GET /sessions, GET /sessions/{id}, GET /sessions/{id}/transcript."""

from __future__ import annotations

import json
import os
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from monitor.allowlist import DEFAULT_ALLOWLIST, add_entries, entries_from_match_metadata

router = APIRouter()
DB = os.getenv("MONITOR_DB", "monitor.db")
ALLOWLIST = os.getenv("MONITOR_ALLOWLIST", DEFAULT_ALLOWLIST)


def _parse_result_row(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("match_metadata"):
        try:
            d["match_metadata"] = json.loads(d["match_metadata"])
        except (json.JSONDecodeError, TypeError):
            pass
    d["marked_safe"] = bool(d.get("marked_safe", 0))
    return d


class _MarkSafeBody(BaseModel):
    marked_safe: bool


async def _connect() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB)
    db.row_factory = aiosqlite.Row
    return db


_LIST_SESSIONS_SQL = """
    SELECT s.id, s.cwd, s.git_branch, s.started_at, s.ran_at, s.total_failures,
           COUNT(r.id) AS scorer_count
    FROM sessions s
    LEFT JOIN results r ON r.session_id = s.id
    WHERE s.total_failures >= ?
      AND (? IS NULL OR s.git_branch = ?)
      AND (? IS NULL OR s.cwd = ?)
      AND (? IS NULL OR EXISTS (
              SELECT 1 FROM results r2
              WHERE r2.session_id = s.id AND r2.scorer_name = ? AND r2.passed = 0 AND r2.marked_safe = 0
          ))
    GROUP BY s.id
    ORDER BY s.ran_at DESC
"""


@router.get("/")
async def list_sessions(
    failed_only: bool = Query(False, description="Return only sessions with at least one failure."),
    scorer: str | None = Query(None, description="Filter to sessions that failed this scorer."),
    branch: str | None = Query(None, description="Filter by git branch (exact match)."),
    cwd: str | None = Query(
        None, description="Filter by working directory/project path (exact match)."
    ),
) -> list[dict]:
    """List sessions with optional filtering. Excludes transcript (can be large)."""
    db = await _connect()
    try:
        async with db.execute(
            _LIST_SESSIONS_SQL,
            (1 if failed_only else 0, branch, branch, cwd, cwd, scorer, scorer),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.get("/{session_id}")
async def get_session(session_id: str) -> dict:
    """Session metadata + per-scorer results (including match metadata). Excludes transcript."""
    db = await _connect()
    try:
        async with db.execute(
            "SELECT id, cwd, git_branch, started_at, ran_at, total_failures FROM sessions WHERE id = ?",
            (session_id,),
        ) as cur:
            session = await cur.fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        async with db.execute(
            "SELECT scorer_name, passed, explanation, match_metadata, marked_safe"
            " FROM results WHERE session_id = ? ORDER BY scorer_name",
            (session_id,),
        ) as cur:
            results = await cur.fetchall()
        return {"session": dict(session), "results": [_parse_result_row(r) for r in results]}
    finally:
        await db.close()


@router.patch("/{session_id}/results/{scorer_name}")
async def mark_result_safe(session_id: str, scorer_name: str, body: _MarkSafeBody) -> dict:
    """Toggle the marked_safe flag on a single scorer result and recompute session total_failures.

    When marking safe, also persists the pattern to allowlist.json so future eval runs
    skip the same pattern automatically.
    """
    db = await _connect()
    try:
        async with db.execute(
            "SELECT id, match_metadata FROM results WHERE session_id = ? AND scorer_name = ?",
            (session_id, scorer_name),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="result not found")

        await db.execute(
            "UPDATE results SET marked_safe = ? WHERE session_id = ? AND scorer_name = ?",
            (1 if body.marked_safe else 0, session_id, scorer_name),
        )
        await db.execute(
            """
            UPDATE sessions SET total_failures = (
                SELECT COUNT(*) FROM results
                WHERE session_id = ? AND passed = 0 AND marked_safe = 0
            ) WHERE id = ?
            """,
            (session_id, session_id),
        )
        await db.commit()

        added_to_allowlist: list[str] = []
        if body.marked_safe:
            raw_meta = row["match_metadata"]
            if raw_meta:
                try:
                    meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
                    to_add = entries_from_match_metadata(scorer_name, meta)
                    if to_add:
                        added_to_allowlist = add_entries(scorer_name, to_add, ALLOWLIST)
                except (json.JSONDecodeError, TypeError):
                    pass

        return {
            "session_id": session_id,
            "scorer_name": scorer_name,
            "marked_safe": body.marked_safe,
            "added_to_allowlist": added_to_allowlist,
        }
    finally:
        await db.close()


@router.get("/{session_id}/transcript")
async def get_transcript(session_id: str) -> dict:
    """Full rendered transcript for a session. Served separately because it can be large."""
    db = await _connect()
    try:
        async with db.execute("SELECT transcript FROM sessions WHERE id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="session not found")
        return {"session_id": session_id, "transcript": row["transcript"] or ""}
    finally:
        await db.close()
