"""GET /sessions, GET /sessions/{id}, GET /sessions/{id}/transcript."""

from __future__ import annotations

import os

import aiosqlite
from fastapi import APIRouter, HTTPException

router = APIRouter()
DB = os.getenv("MONITOR_DB", "monitor.db")


async def _connect() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB)
    db.row_factory = aiosqlite.Row
    return db


@router.get("/")
async def list_sessions() -> list[dict]:
    """List all sessions with summary stats. Excludes transcript (can be large)."""
    db = await _connect()
    try:
        async with db.execute(
            """
            SELECT s.id, s.cwd, s.git_branch, s.started_at, s.ran_at, s.total_failures,
                   COUNT(r.id) AS scorer_count
            FROM sessions s
            LEFT JOIN results r ON r.session_id = s.id
            GROUP BY s.id
            ORDER BY s.ran_at DESC
            """
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.get("/{session_id}")
async def get_session(session_id: str) -> dict:
    """Session metadata + per-scorer results. Excludes transcript."""
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
            "SELECT scorer_name, passed, explanation FROM results WHERE session_id = ? ORDER BY scorer_name",
            (session_id,),
        ) as cur:
            results = await cur.fetchall()
        return {"session": dict(session), "results": [dict(r) for r in results]}
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
