"""Tests for SQLite ingest of Inspect eval logs."""

import asyncio
from pathlib import Path

import aiosqlite
import pytest
from inspect_ai import eval as inspect_eval

from monitor.db import get_scored_session_ids, ingest_inspect_log, init_db
from monitor.tasks import coding_agent_safety


async def run_eval_in_thread(log_dir: str, inspect_log_dir: str):
    """Inspect's eval() starts its own event loop, so we offload to a thread
    when called from inside pytest-asyncio's loop."""
    return await asyncio.to_thread(
        inspect_eval,
        coding_agent_safety(log_dir=log_dir),
        model="mockllm/model",
        log_dir=inspect_log_dir,
    )


@pytest.fixture
async def fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)
    return db_path


@pytest.fixture
def fixtures_dir():
    return str(Path(__file__).parent / "fixtures")


async def test_ingest_inspect_log_writes_session_and_results(fresh_db, fixtures_dir, tmp_path):
    """End-to-end: run an Inspect eval against the tiny fixture, then ingest."""
    inspect_log_dir = tmp_path / "inspect_logs"
    inspect_log_dir.mkdir()

    eval_logs = await run_eval_in_thread(fixtures_dir, str(inspect_log_dir))
    assert len(eval_logs) == 1

    rows = await ingest_inspect_log(eval_logs[0].location, fresh_db)
    # 6 scorers × 1 session = 6 result rows
    assert rows == 6

    async with aiosqlite.connect(fresh_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM sessions") as cur:
            sessions = await cur.fetchall()
        async with db.execute("SELECT * FROM results ORDER BY scorer_name") as cur:
            results = await cur.fetchall()

    assert len(sessions) == 1
    sess = sessions[0]
    assert sess["id"] == "test-session-001"
    assert sess["cwd"] == "/home/example/project"
    assert sess["git_branch"] == "main"
    assert sess["transcript"]  # non-empty

    assert len(results) == 6
    scorer_names = {r["scorer_name"] for r in results}
    assert scorer_names == {
        "secret_leakage",
        "scope_creep",
        "exfiltration_attempt",
        "privilege_escalation",
        "deceptive_reasoning",
        "supply_chain_risk",
    }
    # All scorers should pass on the clean fixture
    assert all(r["passed"] == 1 for r in results)
    # total_failures column reflects it
    assert sess["total_failures"] == 0


async def test_init_db_migration_adds_match_metadata_column(tmp_path):
    """init_db should add match_metadata to a DB created without that column."""
    db_path = str(tmp_path / "old.db")
    # Create DB using old schema (no match_metadata column)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, log_path TEXT, cwd TEXT, git_branch TEXT,
                started_at TEXT, ran_at TEXT, transcript TEXT,
                total_failures INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
                scorer_name TEXT NOT NULL, passed INTEGER NOT NULL, explanation TEXT,
                UNIQUE(session_id, scorer_name)
            );
        """)
        await db.commit()

    await init_db(db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA table_info(results)") as cur:
            cols = {row[1] async for row in cur}
    assert "match_metadata" in cols


async def test_get_scored_session_ids_returns_existing(tmp_path):
    """get_scored_session_ids should return IDs that have at least one result row."""
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO sessions (id, total_failures) VALUES (?, ?)", ("s1", 0))
        await db.execute(
            "INSERT INTO results (session_id, scorer_name, passed) VALUES (?, ?, ?)",
            ("s1", "secret_leakage", 1),
        )
        await db.commit()

    ids = await get_scored_session_ids(db_path)
    assert ids == {"s1"}


async def test_get_scored_session_ids_empty_db(tmp_path):
    """get_scored_session_ids should return an empty set when no results exist."""
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)
    assert await get_scored_session_ids(db_path) == set()


async def test_ingest_is_idempotent(fresh_db, fixtures_dir, tmp_path):
    """Re-ingesting the same log shouldn't duplicate rows (ON CONFLICT clauses)."""
    inspect_log_dir = tmp_path / "inspect_logs"
    inspect_log_dir.mkdir()
    eval_logs = await run_eval_in_thread(fixtures_dir, str(inspect_log_dir))
    await ingest_inspect_log(eval_logs[0].location, fresh_db)
    await ingest_inspect_log(eval_logs[0].location, fresh_db)

    async with aiosqlite.connect(fresh_db) as db:
        async with db.execute("SELECT COUNT(*) FROM sessions") as cur:
            (n_sessions,) = await cur.fetchone()
        async with db.execute("SELECT COUNT(*) FROM results") as cur:
            (n_results,) = await cur.fetchone()
    assert n_sessions == 1
    assert n_results == 6
