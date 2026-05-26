"""Tests for the FastAPI endpoints. Uses TestClient against an in-process app."""

import asyncio
import importlib
from pathlib import Path

import aiosqlite
import pytest
from fastapi.testclient import TestClient
from inspect_ai import eval as inspect_eval

from monitor.db import ingest_inspect_log, init_db
from monitor.tasks import coding_agent_safety


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Seed a DB by running an eval against the tiny fixture, point MONITOR_DB at it."""
    db_path = str(tmp_path / "api_test.db")
    inspect_log_dir = tmp_path / "inspect_logs"
    inspect_log_dir.mkdir()

    async def seed():
        await init_db(db_path)
        eval_logs = await asyncio.to_thread(
            inspect_eval,
            coding_agent_safety(log_dir=str(Path(__file__).parent / "fixtures")),
            model="mockllm/model",
            log_dir=str(inspect_log_dir),
        )
        await ingest_inspect_log(eval_logs[0].location, db_path)

    asyncio.run(seed())
    monkeypatch.setenv("MONITOR_DB", db_path)

    # Force-reload the route modules so they pick up the new DB env var.
    from api.routes import results as results_mod
    from api.routes import sessions as sessions_mod

    importlib.reload(results_mod)
    importlib.reload(sessions_mod)
    from api import main as main_mod

    importlib.reload(main_mod)
    return main_mod.app


def test_healthz(seeded_db):
    client = TestClient(seeded_db)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_list_sessions(seeded_db):
    client = TestClient(seeded_db)
    r = client.get("/sessions/")
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["id"] == "test-session-001"
    assert s["scorer_count"] == 6
    assert s["total_failures"] == 0


def test_get_session_detail(seeded_db):
    client = TestClient(seeded_db)
    r = client.get("/sessions/test-session-001")
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["id"] == "test-session-001"
    assert len(body["results"]) == 6
    assert {r["scorer_name"] for r in body["results"]} == {
        "secret_leakage",
        "scope_creep",
        "exfiltration_attempt",
        "privilege_escalation",
        "deceptive_reasoning",
        "supply_chain_risk",
    }


def test_get_session_404(seeded_db):
    client = TestClient(seeded_db)
    r = client.get("/sessions/does-not-exist")
    assert r.status_code == 404


def test_get_transcript(seeded_db):
    client = TestClient(seeded_db)
    r = client.get("/sessions/test-session-001/transcript")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "test-session-001"
    assert "[USER]" in body["transcript"]


def test_results_endpoint(seeded_db):
    client = TestClient(seeded_db)
    r = client.get("/results/test-session-001")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 6


def test_transcript_404(seeded_db):
    client = TestClient(seeded_db)
    r = client.get("/sessions/does-not-exist/transcript")
    assert r.status_code == 404


def test_list_sessions_failed_only_filter(seeded_db):
    client = TestClient(seeded_db)
    # Clean fixture has 0 failures, so failed_only=true should return nothing.
    r = client.get("/sessions/?failed_only=true")
    assert r.status_code == 200
    assert r.json() == []


def test_list_sessions_scorer_filter(seeded_db):
    client = TestClient(seeded_db)
    # No sessions failed secret_leakage in the clean fixture.
    r = client.get("/sessions/?scorer=secret_leakage")
    assert r.status_code == 200
    assert r.json() == []


def test_list_sessions_branch_filter(seeded_db):
    client = TestClient(seeded_db)
    r = client.get("/sessions/?branch=main")
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = client.get("/sessions/?branch=nonexistent")
    assert r.status_code == 200
    assert r.json() == []


@pytest.fixture
def db_with_match_metadata(tmp_path, monkeypatch):
    """DB seeded with a result row that has JSON match_metadata."""
    db_path = str(tmp_path / "meta_test.db")

    async def seed():
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO sessions (id, cwd, git_branch, ran_at, total_failures)"
                " VALUES (?, ?, ?, ?, ?)",
                ("s-meta", "/project", "main", "2026-01-01T00:00:00", 1),
            )
            await db.execute(
                "INSERT INTO results"
                " (session_id, scorer_name, passed, explanation, match_metadata)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    "s-meta",
                    "secret_leakage",
                    0,
                    "Secret leakage detected",
                    '{"pattern": "sk-ant-", "tool_use_id": "t-123"}',
                ),
            )
            await db.commit()

    asyncio.run(seed())
    monkeypatch.setenv("MONITOR_DB", db_path)

    from api.routes import results as results_mod
    from api.routes import sessions as sessions_mod

    importlib.reload(results_mod)
    importlib.reload(sessions_mod)
    from api import main as main_mod

    importlib.reload(main_mod)
    return main_mod.app


def test_results_endpoint_parses_match_metadata(db_with_match_metadata):
    client = TestClient(db_with_match_metadata)
    r = client.get("/results/s-meta")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert isinstance(rows[0]["match_metadata"], dict)
    assert rows[0]["match_metadata"]["pattern"] == "sk-ant-"


def test_session_detail_parses_match_metadata(db_with_match_metadata):
    client = TestClient(db_with_match_metadata)
    r = client.get("/sessions/s-meta")
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    assert isinstance(results[0]["match_metadata"], dict)
    assert results[0]["match_metadata"]["tool_use_id"] == "t-123"


@pytest.fixture
def db_with_bad_metadata(tmp_path, monkeypatch):
    """DB seeded with a result row whose match_metadata is invalid JSON."""
    db_path = str(tmp_path / "bad_meta.db")

    async def seed():
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO sessions (id, ran_at, total_failures) VALUES (?, ?, ?)",
                ("s-bad", "2026-01-01T00:00:00", 0),
            )
            await db.execute(
                "INSERT INTO results"
                " (session_id, scorer_name, passed, explanation, match_metadata)"
                " VALUES (?, ?, ?, ?, ?)",
                ("s-bad", "scope_creep", 1, "ok", "not-valid-json{{"),
            )
            await db.commit()

    asyncio.run(seed())
    monkeypatch.setenv("MONITOR_DB", db_path)

    from api.routes import results as results_mod
    from api.routes import sessions as sessions_mod

    importlib.reload(results_mod)
    importlib.reload(sessions_mod)
    from api import main as main_mod

    importlib.reload(main_mod)
    return main_mod.app


def test_invalid_match_metadata_returned_as_raw_string(db_with_bad_metadata):
    """Unparseable match_metadata should be passed through unchanged (not crash)."""
    client = TestClient(db_with_bad_metadata)

    r = client.get("/results/s-bad")
    assert r.status_code == 200
    assert r.json()[0]["match_metadata"] == "not-valid-json{{"

    r = client.get("/sessions/s-bad")
    assert r.status_code == 200
    assert r.json()["results"][0]["match_metadata"] == "not-valid-json{{"
