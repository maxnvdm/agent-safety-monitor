"""SQLite schema + Inspect eval-log ingestion."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
from inspect_ai.log import read_eval_log
from inspect_ai.scorer import CORRECT

DEFAULT_DB = "monitor.db"

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    log_path      TEXT,
    cwd           TEXT,
    git_branch    TEXT,
    started_at    TEXT,
    ran_at        TEXT,
    transcript    TEXT,
    total_failures INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    scorer_name    TEXT NOT NULL,
    passed         INTEGER NOT NULL,
    explanation    TEXT,
    match_metadata TEXT,
    UNIQUE(session_id, scorer_name)
);

CREATE INDEX IF NOT EXISTS idx_results_session ON results(session_id);
"""

_MIGRATE_MATCH_METADATA = """
ALTER TABLE results ADD COLUMN match_metadata TEXT;
"""


async def init_db(db_path: str = DEFAULT_DB) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(CREATE_TABLES)
        # Idempotent migration: add match_metadata column if missing (pre-existing DBs).
        async with db.execute("PRAGMA table_info(results)") as cur:
            cols = {row[1] async for row in cur}
        if "match_metadata" not in cols:
            await db.execute(_MIGRATE_MATCH_METADATA)
        await db.commit()


async def ingest_inspect_log(eval_log_path: str | Path, db_path: str = DEFAULT_DB) -> int:
    """Parse an Inspect `.eval` (or `.json`) log and write its sample scores to SQLite.

    Returns the number of (session, scorer) result rows written.
    """
    log = read_eval_log(str(eval_log_path))
    ran_at = log.eval.created if hasattr(log.eval, "created") else ""
    rows_written = 0

    async with aiosqlite.connect(db_path) as db:
        for sample in log.samples or []:
            meta = sample.metadata or {}
            session_id = meta.get("session_id") or str(sample.id)
            session_meta = meta.get("session_meta") or {}
            scores = sample.scores or {}

            failures = sum(1 for s in scores.values() if s.value != CORRECT)

            await db.execute(
                """
                INSERT INTO sessions
                    (id, log_path, cwd, git_branch, started_at, ran_at, transcript, total_failures)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    log_path = excluded.log_path,
                    cwd = excluded.cwd,
                    git_branch = excluded.git_branch,
                    started_at = excluded.started_at,
                    ran_at = excluded.ran_at,
                    transcript = excluded.transcript,
                    total_failures = excluded.total_failures
                """,
                (
                    session_id,
                    meta.get("log_path", ""),
                    session_meta.get("cwd"),
                    session_meta.get("git_branch"),
                    session_meta.get("started_at"),
                    ran_at,
                    meta.get("transcript", ""),
                    failures,
                ),
            )

            for scorer_name, score in scores.items():
                match_meta = score.metadata if hasattr(score, "metadata") else None
                await db.execute(
                    """
                    INSERT INTO results (session_id, scorer_name, passed, explanation, match_metadata)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, scorer_name) DO UPDATE SET
                        passed = excluded.passed,
                        explanation = excluded.explanation,
                        match_metadata = excluded.match_metadata
                    """,
                    (
                        session_id,
                        scorer_name,
                        1 if score.value == CORRECT else 0,
                        score.explanation or "",
                        json.dumps(match_meta) if match_meta else None,
                    ),
                )
                rows_written += 1

        await db.commit()

    return rows_written


async def get_scored_session_ids(db_path: str = DEFAULT_DB) -> set[str]:
    """Return session IDs that already have at least one result row (already scored)."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT DISTINCT session_id FROM results") as cur:
            return {row[0] async for row in cur}
