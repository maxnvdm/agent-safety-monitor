"""GET /results/{session_id} — raw results rows."""

from __future__ import annotations

import os

import aiosqlite
from fastapi import APIRouter

router = APIRouter()
DB = os.getenv("MONITOR_DB", "monitor.db")


@router.get("/{session_id}")
async def get_results(session_id: str) -> list[dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT scorer_name, passed, explanation FROM results WHERE session_id = ? ORDER BY scorer_name",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]
