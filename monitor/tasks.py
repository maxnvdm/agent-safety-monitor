"""Inspect AI Task wiring for the coding-agent safety monitor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.solver import Generate, TaskState, solver

from monitor.ingest import (
    extract_session_meta,
    iter_events,
    iter_tool_calls,
    iter_tool_results,
    render_transcript,
)
from monitor.scorers import (
    deceptive_reasoning,
    exfiltration_attempt,
    privilege_escalation,
    scope_creep,
    secret_leakage,
    supply_chain_risk,
)


@solver
def passthrough() -> Any:
    """No-op solver. Inspect would default to generate() if we omit a solver,
    which burns one model call per sample for no reason — our scorers operate
    on transcripts already in metadata."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        return state

    return solve


def sessions_dataset(log_dir: Path | str, skip_ids: set[str] | None = None) -> MemoryDataset:
    """Build a Sample per `.jsonl` file in `log_dir`.

    Each Sample's metadata carries the structured data scorers need:
    transcript, tool_calls, tool_results, session_meta.

    `skip_ids`: session IDs to skip (already scored); avoids redundant LLM calls.
    """
    log_dir = Path(log_dir)
    skip = skip_ids or set()
    samples = []
    for path in sorted(log_dir.glob("*.jsonl")):
        events = list(iter_events(path))
        session_meta = extract_session_meta(events)
        session_id = session_meta.get("session_id") or path.stem
        if session_id in skip:
            continue
        samples.append(
            Sample(
                input=f"Evaluate session {session_id} for safety failures.",
                target="SAFE",
                id=session_id,
                metadata={
                    "transcript": render_transcript(events),
                    "tool_calls": list(iter_tool_calls(events)),
                    "tool_results": list(iter_tool_results(events)),
                    "session_meta": session_meta,
                    "log_path": str(path),
                    "session_id": session_id,
                },
            )
        )
    return MemoryDataset(samples)


@task
def coding_agent_safety(
    log_dir: str = "logs/",
    allowed_hosts: list[str] | None = None,
    skip_ids: list[str] | None = None,
) -> Task:
    """Score every Claude Code session in `log_dir` against all 6 safety scorers."""
    return Task(
        dataset=sessions_dataset(log_dir, skip_ids=set(skip_ids) if skip_ids else None),
        solver=passthrough(),
        scorer=[
            secret_leakage(),
            scope_creep(),
            exfiltration_attempt(allowed_hosts=allowed_hosts or []),
            privilege_escalation(),
            deceptive_reasoning(),
            supply_chain_risk(),
        ],
    )
