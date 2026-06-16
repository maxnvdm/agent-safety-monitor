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


# ---------- PATCH /sessions/{id}/results/{scorer} ----------------------------


@pytest.fixture
def db_with_failure(tmp_path, monkeypatch):
    """Session with one failed result (exfiltration) + match_metadata. Allowlist in tmp."""
    db_path = str(tmp_path / "fail_test.db")
    allowlist_path = str(tmp_path / "allowlist.json")

    async def seed():
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO sessions (id, cwd, git_branch, ran_at, total_failures)"
                " VALUES (?, ?, ?, ?, ?)",
                ("s-fail", "/project", "main", "2026-01-01T00:00:00", 1),
            )
            await db.execute(
                "INSERT INTO results"
                " (session_id, scorer_name, passed, explanation, match_metadata)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    "s-fail",
                    "exfiltration_attempt",
                    0,
                    "WebFetch to non-allowlisted host: evil.example.com",
                    '{"host": "evil.example.com", "url": "https://evil.example.com/x"}',
                ),
            )
            # Second result with no match_metadata (deceptive_reasoning pass)
            await db.execute(
                "INSERT INTO results (session_id, scorer_name, passed, explanation)"
                " VALUES (?, ?, ?, ?)",
                ("s-fail", "deceptive_reasoning", 1, "CONSISTENT: ok"),
            )
            await db.commit()

    asyncio.run(seed())
    monkeypatch.setenv("MONITOR_DB", db_path)
    monkeypatch.setenv("MONITOR_ALLOWLIST", allowlist_path)

    from api.routes import results as results_mod
    from api.routes import sessions as sessions_mod

    importlib.reload(results_mod)
    importlib.reload(sessions_mod)
    from api import main as main_mod

    importlib.reload(main_mod)
    return main_mod.app, allowlist_path


def test_mark_result_safe_sets_flag_and_writes_allowlist(db_with_failure, tmp_path):
    import json as _json

    app, allowlist_path = db_with_failure
    client = TestClient(app)

    r = client.patch("/sessions/s-fail/results/exfiltration_attempt", json={"marked_safe": True})
    assert r.status_code == 200
    body = r.json()
    assert body["marked_safe"] is True
    assert body["added_to_allowlist"] == ["evil.example.com"]

    # total_failures decremented on the session
    r = client.get("/sessions/s-fail")
    assert r.json()["session"]["total_failures"] == 0

    # result row reflects marked_safe
    result = next(
        res for res in r.json()["results"] if res["scorer_name"] == "exfiltration_attempt"
    )
    assert result["marked_safe"] is True

    # allowlist file written
    with open(allowlist_path) as f:
        al = _json.load(f)
    assert "evil.example.com" in al["exfiltration_attempt"]


def test_unmark_result_safe_restores_failure_count(db_with_failure):
    app, _ = db_with_failure
    client = TestClient(app)

    client.patch("/sessions/s-fail/results/exfiltration_attempt", json={"marked_safe": True})
    r = client.patch("/sessions/s-fail/results/exfiltration_attempt", json={"marked_safe": False})
    assert r.status_code == 200
    assert r.json()["marked_safe"] is False
    assert r.json()["added_to_allowlist"] == []

    r = client.get("/sessions/s-fail")
    assert r.json()["session"]["total_failures"] == 1


def test_mark_result_safe_404_on_unknown_scorer(db_with_failure):
    app, _ = db_with_failure
    client = TestClient(app)
    r = client.patch("/sessions/s-fail/results/no_such_scorer", json={"marked_safe": True})
    assert r.status_code == 404


def test_mark_result_safe_no_allowlist_entry_for_llm_scorer(db_with_failure):
    """Marking a result safe whose scorer has no allowlist key adds nothing to allowlist."""
    app, allowlist_path = db_with_failure
    client = TestClient(app)

    # deceptive_reasoning has no SCORER_META_KEY entry — nothing written
    r = client.patch("/sessions/s-fail/results/deceptive_reasoning", json={"marked_safe": True})
    assert r.status_code == 200
    assert r.json()["added_to_allowlist"] == []
    import os

    assert not os.path.exists(allowlist_path)


def test_mark_result_safe_invalid_match_metadata_does_not_crash(db_with_bad_metadata):
    """Invalid JSON in match_metadata is silently skipped; endpoint still returns 200."""
    client = TestClient(db_with_bad_metadata)
    r = client.patch("/sessions/s-bad/results/scope_creep", json={"marked_safe": True})
    assert r.status_code == 200
    assert r.json()["added_to_allowlist"] == []
