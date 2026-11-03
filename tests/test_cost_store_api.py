import asyncio
import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitor.cost import cli, store
from tests.cost_helpers import LogBuilder


def _session(tmp_path: Path, name: str = "sess-1") -> Path:
    b = LogBuilder(tmp_path, session=name)
    b.prompt("read the big file please", pid="pA")
    b.call("m1", [("tool", "t1", "Read", {"file_path": "big.py"})], out=20, new=500)
    b.result("t1", 30_000)
    b.call("m2", [("tool", "t2", "Bash", {"command": "cd /r && git status"})], out=20, new=10_020)
    b.result("t2", 100)
    b.call("m3", [("tool", "s1", "Skill", {"skill": "big-skill"})], out=20, new=60)
    b.result("s1", 20, text="Launching skill")
    b.skill_content("s1", 20_000)
    b.call("m4", [("text", "done")], out=10, new=12_000, skill="big-skill")
    for i in range(5, 9):
        b.call(f"m{i}", [("text", "x")], out=5, new=10)
    b.cost_state(0.5, {"claude-sonnet-5-5": 0.5})
    return b.write()


def test_ingest_is_idempotent_and_incremental(tmp_path: Path) -> None:
    db, log = str(tmp_path / "c.db"), _session(tmp_path)
    first = asyncio.run(store.ingest_session(log, db))
    assert first is not None
    assert asyncio.run(store.ingest_session(log, db)) is None  # unchanged log: skipped
    again = asyncio.run(store.ingest_session(log, db, force=True))
    assert again is not None
    detail = asyncio.run(store.session_detail(db, "sess-1"))
    assert detail is not None and detail["total_usd"] == pytest.approx(first.total_usd)
    tools = asyncio.run(store.tools_grouped(db, "name", "sess-1"))
    assert sum(t["invocations"] for t in tools) == 3  # force re-ingest replaced rows, no duplicates


def test_bucket_totals_account_for_the_whole_bill(tmp_path: Path) -> None:
    db = str(tmp_path / "c.db")
    asyncio.run(store.ingest_session(_session(tmp_path), db))
    total = asyncio.run(store.overall(db, "sess-1"))["total_usd"]
    buckets = asyncio.run(store.bucket_totals(db, "sess-1"))
    assert sum(buckets.values()) == pytest.approx(total)
    assert buckets["skill_text"] > 0 and buckets["tool_calls"] > 0


def test_actions_and_skills_are_queryable(tmp_path: Path) -> None:
    db = str(tmp_path / "c.db")
    asyncio.run(store.ingest_session(_session(tmp_path), db))
    by_action = {r["key"]: r for r in asyncio.run(store.tools_grouped(db, "action", "sess-1"))}
    assert "git status" in by_action and "Read" in by_action
    skills = asyncio.run(store.skills(db, "sess-1"))
    assert skills[0]["name"] == "big-skill" and skills[0]["load_usd"] > 0
    top = asyncio.run(store.top_invocations(db, "sess-1", 1))
    assert top[0]["name"] == "Read"  # the 30K-char result dominates


def test_reconciliation_fields(tmp_path: Path) -> None:
    db = str(tmp_path / "c.db")
    asyncio.run(store.ingest_session(_session(tmp_path), db))
    d = asyncio.run(store.session_detail(db, "sess-1"))
    assert d is not None and d["reported_usd"] == pytest.approx(0.5)
    assert d["unlogged_usd"] is not None and d["unlogged_usd"] >= 0


def test_no_preview_stores_no_prompt_text(tmp_path: Path) -> None:
    db = str(tmp_path / "c.db")
    asyncio.run(store.ingest_session(_session(tmp_path), db, preview_chars=0))
    assert all(p["preview"] == "" for p in asyncio.run(store.prompts(db, "sess-1")))


def test_cli_report_text_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log, db = _session(tmp_path), str(tmp_path / "c.db")
    cli.main(["report", str(log), "--db", db, "--top", "5"])
    text = capsys.readouterr().out
    assert (
        "Session sess-1" in text
        and "git status" in text
        and "big-skill" in text
        and "Top findings" in text
    )
    cli.main(["report", str(tmp_path), "--db", db, "--json", "--group-by", "family"])
    data = json.loads(capsys.readouterr().out)
    assert data["overall"]["sessions"] == 1 and {t["key"] for t in data["tools"]} >= {
        "file",
        "shell",
        "skill",
    }
    with pytest.raises(SystemExit):
        cli.main(["report", str(tmp_path / "missing.jsonl"), "--db", db])


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = str(tmp_path / "api.db")
    asyncio.run(store.ingest_session(_session(tmp_path), db))
    monkeypatch.setenv("MONITOR_DB", db)
    from api.routes import cost as cost_mod

    importlib.reload(cost_mod)
    from api import main as main_mod

    importlib.reload(main_mod)
    return TestClient(main_mod.app)


def test_api_endpoints(client: TestClient) -> None:
    assert client.get("/cost/sessions").json()[0]["session_id"] == "sess-1"
    detail = client.get("/cost/sessions/sess-1").json()
    assert detail["buckets"] and detail["models"][0]["model"] == "claude-sonnet-5-5"
    assert client.get("/cost/sessions/nope").status_code == 404
    prompts = client.get("/cost/sessions/sess-1/prompts").json()
    assert prompts[0]["prompt_id"] == "pA"
    tools = client.get("/cost/tools", params={"group_by": "family", "session_id": "sess-1"}).json()
    assert {t["key"] for t in tools} >= {"file", "shell", "skill"}
    assert client.get("/cost/invocations", params={"by": "roundtrip_usd"}).status_code == 200
    assert client.get("/cost/skills").json()[0]["name"] == "big-skill"
    findings = client.get("/cost/findings", params={"kind": "heavy_skill"}).json()
    assert findings and findings[0]["evidence"]["load_tokens"] > 0
    summary = client.get("/cost/summary").json()
    assert sum(summary["buckets"].values()) == pytest.approx(summary["overall"]["total_usd"])
    assert client.get("/cost/tools", params={"group_by": "bogus"}).status_code == 422
