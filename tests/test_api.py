"""Tests for the FastAPI endpoints. Uses TestClient against an in-process app."""

import asyncio
import importlib
from pathlib import Path

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
