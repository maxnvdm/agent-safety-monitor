"""Cost analysis endpoints: where do the tokens and dollars go?"""

from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from monitor.cost import store

router = APIRouter()
DB = os.getenv("MONITOR_DB", "monitor.db")


@router.get("/sessions")
async def list_cost_sessions(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return await store.list_sessions(DB, limit)


@router.get("/summary")
async def cost_summary(session_id: str | None = None) -> dict[str, Any]:
    """Totals, the every-dollar-has-one-owner breakdown, and waste by pattern."""
    return {
        "overall": await store.overall(DB, session_id),
        "buckets": await store.bucket_totals(DB, session_id),
        "waste": await store.waste_summary(DB, session_id),
    }


@router.get("/sessions/{session_id}")
async def get_cost_session(session_id: str) -> dict[str, Any]:
    detail = await store.session_detail(DB, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No cost data for that session")
    return detail


@router.get("/sessions/{session_id}/prompts")
async def get_cost_prompts(
    session_id: str,
    limit: int = Query(20, ge=1, le=500),
    by: Literal["total_usd", "ts", "requests", "tool_calls", "read_usd"] = "total_usd",
) -> list[dict[str, Any]]:
    return await store.prompts(DB, session_id, limit, by)


@router.get("/tools")
async def get_cost_tools(
    group_by: Literal["name", "family", "action", "skill"] = "action",
    session_id: str | None = None,
    limit: int = Query(30, ge=1, le=500),
) -> list[dict[str, Any]]:
    return await store.tools_grouped(DB, group_by, session_id, limit)


@router.get("/invocations")
async def get_cost_invocations(
    session_id: str | None = None,
    limit: int = Query(20, ge=1, le=500),
    by: Literal["own_usd", "roundtrip_usd", "result_tokens", "carry_usd"] = "own_usd",
) -> list[dict[str, Any]]:
    return await store.top_invocations(DB, session_id, limit, by)


@router.get("/skills")
async def get_cost_skills(session_id: str | None = None) -> list[dict[str, Any]]:
    return await store.skills(DB, session_id)


@router.get("/findings")
async def get_cost_findings(
    session_id: str | None = None,
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    return await store.findings(DB, session_id, kind, limit)
