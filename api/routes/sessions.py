"""GET /sessions, GET /sessions/{id}, GET /sessions/{id}/transcript."""

from __future__ import annotations

import json
import os
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
DB = os.getenv("MONITOR_DB", "monitor.db")


def _parse_result_row(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("match_metadata"):
        try:
            d["match_metadata"] = json.loads(d["match_metadata"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


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
      AND (? IS NULL OR EXISTS (
              SELECT 1 FROM results r2
              WHERE r2.session_id = s.id AND r2.scorer_name = ? AND r2.passed = 0
          ))
    GROUP BY s.id
    ORDER BY s.ran_at DESC
"""


@router.get("/")
async def list_sessions(
    failed_only: bool = Query(False, description="Return only sessions with at least one failure."),
    scorer: str | None = Query(None, description="Filter to sessions that failed this scorer."),
    branch: str | None = Query(None, description="Filter by git branch (exact match)."),
) -> list[dict]:
    """List sessions with optional filtering. Excludes transcript (can be large)."""
    db = await _connect()
    try:
        async with db.execute(
            _LIST_SESSIONS_SQL,
            (1 if failed_only else 0, branch, branch, scorer, scorer),
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
            "SELECT scorer_name, passed, explanation, match_metadata"
            " FROM results WHERE session_id = ? ORDER BY scorer_name",
            (session_id,),
        ) as cur:
            results = await cur.fetchall()
        return {"session": dict(session), "results": [_parse_result_row(r) for r in results]}
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
