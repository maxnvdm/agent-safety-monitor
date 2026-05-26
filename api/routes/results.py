"""GET /results/{session_id} — raw results rows."""

from __future__ import annotations

import json
import os
from typing import Any

import aiosqlite
from fastapi import APIRouter

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


@router.get("/{session_id}")
async def get_results(session_id: str) -> list[dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT scorer_name, passed, explanation, match_metadata"
            " FROM results WHERE session_id = ? ORDER BY scorer_name",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [_parse_result_row(r) for r in rows]
