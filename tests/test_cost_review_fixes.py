"""Regression tests for the behaviours fixed after review of the cost module."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitor.cost import cli, report, store
from monitor.cost.actions import bash_action, split_commands
from monitor.cost.attribute import attribute
from monitor.cost.findings import Thresholds, detect
from monitor.cost.parse import parse_session, redact_structure, summarize_input
from monitor.cost.pricing import RateCard, Usage, cost_of, rates_for
from tests.cost_helpers import LogBuilder

LOW = Thresholds(
    min_usd=0.0, large_result_tokens=500, heavy_skill_tokens=1_000, many_roundtrips_calls=4
)


def _balanced(costs) -> None:
    assert costs.attributed_usd == pytest.approx(costs.total_usd, rel=1e-9, abs=1e-12)


# ---------- actions -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("sudo -u deploy git status", "git status"),
        ("sudo -- ls", "ls"),
        ("timeout 30 uv run pytest -q", "uv run pytest"),
        ("timeout -s KILL 5 make", "make"),
        ("FOO=1 env -u X BAR=2 npm test", "npm test"),
        ("nohup python3 -m http.server &", "python -m http.server"),
        ("grep 'a|b' file | wc -l", "grep"),
        ('git commit -m "fix a && b"', "git commit"),
        ("ls\nrm -rf build", "ls"),
        ("cd x && 'quoted-cmd' arg", "quoted-cmd"),
        ("echo 'unterminated", "(shell builtin)"),
        ("a\\ b c", "a b"),
        ("a || b", "a"),
    ],
)
def test_bash_action_is_quote_and_wrapper_aware(command: str, expected: str) -> None:
    assert bash_action(command) == expected


def test_split_commands_keeps_quoted_operators() -> None:
    assert split_commands("grep 'a|b;c' f | head && echo \"x && y\"") == [
        ["grep", "a|b;c", "f"],
        ["head"],
        ["echo", "x && y"],
    ]
    assert split_commands('echo "a\\"b" c') == [["echo", 'a"b', "c"]]


def test_action_comes_from_the_full_command_not_the_truncated_target(tmp_path: Path) -> None:
    long_prefix = "echo " + "x" * 400 + " && "
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call("m1", [("tool", "t1", "Bash", {"command": long_prefix + "git status"})], out=10, new=100)
    b.result("t1", 10)
    sess = parse_session(b.write())
    assert len(sess.tool_uses["t1"].target) <= 160
    assert sess.tool_uses["t1"].action == "git status"
    db = str(tmp_path / "c.db")
    asyncio.run(store.ingest_session(b.path, db))
    rows = asyncio.run(store.tools_grouped(db, "action"))
    assert [r["key"] for r in rows] == ["git status"]


# ---------- redaction ---------------------------------------------------------------


def test_structured_input_is_masked_by_key_before_serialising() -> None:
    payload = {
        "headers": {"Authorization": "Bearer abc", "X-Trace": "ok"},
        "body": [{"password": "hunter2"}, {"note": "password=oops"}],
        "api_key": "k",
    }
    masked = json.dumps(redact_structure(payload))
    assert "Bearer abc" not in masked and "hunter2" not in masked and "oops" not in masked
    assert '"k"' not in masked and "ok" in masked
    out = summarize_input("mcp__x__call", payload)
    assert "hunter2" not in out and "Bearer abc" not in out


# ---------- pricing -----------------------------------------------------------------


def _tiered_card() -> RateCard:
    base = RateCard("m", 1.0, 2.0, 0.1, 1.25, 2.0)
    mid = RateCard("m", 3.0, 2.0, 0.3, 1.25, 2.0)
    top = RateCard("m", 5.0, 2.0, 0.5, 1.25, 2.0)
    return RateCard(**{**base.__dict__, "tiers": ((100_000, mid), (200_000, top))})


def test_highest_exceeded_tier_is_used() -> None:
    card = _tiered_card()
    assert card.above_threshold == 100_000
    assert card.for_prompt(50_000).input == 1.0
    assert card.for_prompt(150_000).input == 3.0
    assert card.for_prompt(250_000).input == 5.0
    assert cost_of(Usage(input_tokens=250_000), card).input == pytest.approx(250_000 * 5.0)


def test_every_claude_model_prices_web_search() -> None:
    for model in ("claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-5-5"):
        card = rates_for(model)
        assert card is not None and card.web_search_per_request == pytest.approx(0.01)


# ---------- attribution -------------------------------------------------------------


def test_output_with_no_parsed_blocks_stays_balanced(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call("m1", [("text", "x")], out=200, new=500)
    b.events[-1]["message"]["content"] = []  # usage is logged but no block survives parsing
    b.call("m2", [("text", "next")], out=10, new=300)
    costs = attribute(parse_session(b.write()))
    _balanced(costs)


def test_compaction_does_not_resurrect_the_previous_output(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call("m1", [("text", "a" * 4000)], out=1500, new=500)
    b.compact()
    b.prompt("after", pid="p2")
    b.call("m2", [("text", "ok")], out=10, new=2_000)
    b.call("m3", [("text", "ok")], out=10, new=10)
    costs = attribute(parse_session(b.write()))
    _balanced(costs)
    # Everything in the assistant_text bucket beyond generating the replies is the write/re-read
    # of replies that are still in context. m1's 1,500 tokens were wiped by the compaction, so only
    # m2's 10 output tokens can be left (had m1 been re-added it would be >= 1,500 * write rate).
    generated = sum(c.breakdown.output for c in costs.calls)
    card = rates_for("claude-sonnet-5-5")
    assert card is not None
    leftover = costs.buckets["assistant_text"] - generated
    assert 0 <= leftover < 100 * card.cache_write_1h


def test_cache_rebuild_excess_uses_the_long_context_read_rate(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path, model="claude-haiku-5-5")
    b.prompt("go")
    b.call("m1", [("text", "x")], out=10, new=150_000)
    b.call("m2", [("text", "y")], out=10, new=1_000, cold=True)
    costs = attribute(parse_session(b.write()))
    card = rates_for("claude-haiku-5-5")
    assert card is not None
    c = costs.calls[1]
    assert c.rebuild_tokens > 0 and c.context_tokens > 100_000
    tier = card.for_prompt(c.context_tokens)
    assert tier.cache_read != card.cache_read  # the long-context tier really differs
    w_tokens = 151_000  # a cold request rewrites the 150K previous prompt plus 1K new
    rate = c.breakdown.write_side / w_tokens
    assert c.rebuild_excess_usd == pytest.approx(c.rebuild_tokens * (rate - tier.cache_read))
    _balanced(costs)


# ---------- findings ----------------------------------------------------------------


def test_large_result_saving_excludes_argument_cost(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call(
        "m1",
        [("tool", "t1", "Write", {"file_path": "f", "content": "z" * 40_000})],
        out=9_000,
        new=500,
    )
    b.result("t1", 30_000)
    b.call("m2", [("text", "x")], out=5, new=9_000 + 12_000)  # args re-enter, then the result
    for i in range(3, 6):
        b.call(f"m{i}", [("text", "x")], out=5, new=10)
    sess = parse_session(b.write())
    costs = attribute(sess)
    t = costs.tools["t1"]
    assert 0 < t.result_write_usd < t.write_usd and 0 < t.result_carry_usd < t.carry_usd
    f = next(f for f in detect(sess, costs, LOW) if f.kind == "large_tool_result")
    assert f.cost_usd == pytest.approx(t.result_write_usd + t.result_carry_usd)


def test_repeated_call_needs_consecutive_identical_results(tmp_path: Path) -> None:
    def run(results: list[str]) -> set[str]:
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        d.mkdir()
        b = LogBuilder(d)
        b.prompt("poll")
        for i, text in enumerate(results, 1):
            b.call(f"m{i}", [("tool", f"t{i}", "Bash", {"command": "status"})], out=10, new=100)
            b.result(f"t{i}", 0, text=text)
        b.call("mz", [("text", "done")], out=5, new=10)
        sess = parse_session(b.write())
        findings = detect(sess, attribute(sess), LOW)
        return {str(f.tool_use_id) for f in findings if f.kind == "repeated_identical_call"}

    assert run(["A", "A"]) == {"t2"}
    assert run(["A", "B", "A"]) == set()  # state changed in between: the second A was a re-check
    assert run(["A", "B", "B"]) == {"t3"}


def test_one_batched_request_is_not_many_round_trips(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("batch")
    b.call(
        "m1",
        [("tool", f"t{i}", "Read", {"file_path": f"f{i}"}) for i in range(12)],
        out=100,
        new=500,
    )
    for i in range(12):
        b.result(f"t{i}", 50)
    b.call("m2", [("text", "done")], out=5, new=700)
    sess = parse_session(b.write())
    costs = attribute(sess)
    p = next(iter(costs.prompts.values()))
    assert p.tool_calls == 12 and p.tool_requests == 1
    assert "many_roundtrips" not in {f.kind for f in detect(sess, costs, LOW)}


# ---------- subagent prompts --------------------------------------------------------


def test_subagent_tool_uses_inherit_the_prompt(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("delegate", pid="pX")
    b.call("m1", [("tool", "task1", "Task", {"prompt": "dig"})], out=30, new=800)
    b.result("task1", 200, tool_use_result={"agentId": "a1"})
    b.call("m2", [("text", "done")], out=10, new=300)
    path = b.write()
    (tmp_path / "scratch").mkdir()
    sub = LogBuilder(tmp_path / "scratch")
    sub.prompt("dig")
    sub.call("h1", [("tool", "inner", "Read", {"file_path": "a"})], out=20, new=3000)
    sub.result("inner", 100)
    sub.call("h2", [("text", "found")], out=10, new=100)
    sub_dir = tmp_path / "sess-1" / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-a1.jsonl").write_text(sub.write().read_text())
    sess = parse_session(path)
    assert sess.tool_uses["inner"].prompt_id == "pX"
    assert all(c.prompt_id == "pX" for c in sess.calls)


def test_subagent_prompt_falls_back_to_timestamps(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("one", pid="pA").call("m1", [("text", "a")], out=5, new=100)
    path = b.write()
    (tmp_path / "sess-1").mkdir()
    (tmp_path / "sess-1" / "subagents").mkdir()
    sub = LogBuilder(tmp_path / "sess-1" / "subagents", session="agent-orphan")
    sub.prompt("x").call("h1", [("tool", "orph", "Read", {"file_path": "a"})], out=5, new=100)
    sub.write()
    sess = parse_session(path)
    orphan = next(c for c in sess.calls if c.is_subagent)
    assert orphan.prompt_id == "pA" and sess.tool_uses["orph"].prompt_id == "pA"


# ---------- store / CLI / API -------------------------------------------------------


def _log(tmp_path: Path, name: str, prompt: str) -> Path:
    b = LogBuilder(tmp_path, session=name)
    b.prompt(prompt, pid=f"p-{name}")
    b.call("m-" + name, [("tool", "t-" + name, "Skill", {"skill": f"sk-{name}"})], out=20, new=600)
    b.result("t-" + name, 10, text="Launching")
    b.skill_content("t-" + name, 5_000)
    b.call("n-" + name, [("text", "ok")], out=5, new=1_500)
    return b.write()


def test_directory_report_is_scoped_to_the_requested_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "c.db")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _log(tmp_path / "a", "alpha", "first thing")
    _log(tmp_path / "b", "beta", "second thing")
    cli.main(["report", str(tmp_path / "a"), "--db", db, "--json"])
    only_a = json.loads(capsys.readouterr().out)
    assert only_a["overall"]["sessions"] == 1
    cli.main(["report", str(tmp_path / "a"), str(tmp_path / "b"), "--db", db, "--json"])
    both = json.loads(capsys.readouterr().out)
    assert both["overall"]["sessions"] == 2
    assert {p["prompt_id"] for p in both["prompts"]} == {
        "p-alpha",
        "p-beta",
    }  # prompts in aggregate
    cli.main(["report", str(tmp_path / "a"), "--db", db, "--json"])  # the db still holds beta
    again = json.loads(capsys.readouterr().out)
    assert again["overall"]["sessions"] == 1 and {s["name"] for s in again["skills"]} == {
        "sk-alpha"
    }
    cli.main(
        ["report", str(tmp_path / "a"), str(tmp_path / "b"), "--db", db, "--top", "1", "--json"]
    )
    top1 = json.loads(capsys.readouterr().out)
    assert len(top1["skills"]) == len(top1["prompts"]) == 1


def test_empty_directory_reports_nothing_rather_than_the_whole_db(tmp_path: Path) -> None:
    db = str(tmp_path / "c.db")
    (tmp_path / "full").mkdir()
    (tmp_path / "empty").mkdir()
    _log(tmp_path / "full", "alpha", "x")
    cli.main(["report", str(tmp_path / "full"), "--db", db, "--json"])
    with pytest.raises(SystemExit, match="nothing to report"):
        cli.main(["report", str(tmp_path / "empty"), "--db", db])


def test_no_preview_scrubs_text_stored_by_an_earlier_run(tmp_path: Path) -> None:
    db, log = str(tmp_path / "c.db"), _log(tmp_path, "alpha", "please refactor the billing module")
    asyncio.run(store.ingest_session(log, db))
    assert asyncio.run(store.prompts(db, "alpha"))[0]["preview"].startswith("please refactor")
    scrubbed = asyncio.run(store.ingest_session(log, db, preview_chars=0))  # log is unchanged
    assert scrubbed is not None
    assert all(p["preview"] == "" for p in asyncio.run(store.prompts(db, "alpha")))
    assert asyncio.run(store.ingest_session(log, db, preview_chars=0)) is None  # now it can skip
    restored = asyncio.run(store.ingest_session(log, db))
    assert restored is not None
    assert asyncio.run(store.prompts(db, "alpha"))[0]["preview"].startswith("please refactor")


def test_reconciliation_gap_keeps_its_sign(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go").call("m1", [("text", "a")], out=100, new=1000)
    b.cost_state(0.000001, {"claude-sonnet-5-5": 0.000001})  # Claude Code says far less
    db = str(tmp_path / "c.db")
    asyncio.run(store.ingest_session(b.write(), db))
    detail = asyncio.run(store.session_detail(db, "sess-1"))
    assert detail is not None and detail["unlogged_usd"] < 0
    text = report.render(
        session_id="sess-1", overall=asyncio.run(store.overall(db, "sess-1")), detail=detail,
        buckets={}, prompts=[], tools=[], group_by="action", invocations=[], skills=[], waste=[],
        findings=[],
    )  # fmt: skip
    assert "HIGHER than Claude Code" in text


def test_list_and_detail_agree_on_unknown_models(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path, model="mystery-model-9")
    b.prompt("go").call("m1", [("text", "a")], out=10, new=100)
    db = str(tmp_path / "c.db")
    asyncio.run(store.ingest_session(b.write(), db))
    listed = asyncio.run(store.list_sessions(db))[0]["unknown_models"]
    detail = asyncio.run(store.session_detail(db, "sess-1"))
    assert detail is not None and listed == detail["unknown_models"] == ["mystery-model-9"]


def test_report_strips_terminal_control_sequences() -> None:
    assert report.clean("a\x1b[31mred\x1b[0m\rspoof\x07") == "a [31mred [0m spoof "
    rendered = report.table(["h"], [["x\x1b[2Jy"]])
    assert "\x1b" not in rendered


def test_cost_endpoints_work_before_any_cost_data_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "fresh.db")
    sqlite3.connect(db).close()  # a database that monitor.db created but the cost CLI never touched
    monkeypatch.setenv("MONITOR_DB", db)
    from api import main as main_mod
    from api.routes import cost as cost_mod

    importlib.reload(cost_mod)
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    assert client.get("/cost/sessions").json() == []
    assert client.get("/cost/skills").json() == []
    assert client.get("/cost/summary").status_code == 200


# ---------- parser robustness -------------------------------------------------------


def test_parser_tolerates_odd_events(tmp_path: Path) -> None:
    from monitor.cost.parse import _text_of, parse_ts, redact_structure

    assert parse_ts(None) is None and parse_ts("not a date") is None
    assert (
        _text_of(None) == ""
        and _text_of([{"type": "image", "x": 1}, "raw", {"type": "tool_result", "content": "r"}])
        == '{"type": "image", "x": 1}rawr'
    )
    assert redact_structure(3) == 3

    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call("m1", [("text", "x")], out=10, new=300)
    # a server-side tool block, a block of unknown type, junk content, and a synthetic message
    b.events[-1]["message"]["content"] = [
        {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {"query": "q"}},
        {"type": "mystery"},
        "junk",
    ]
    b.events.append(
        {"type": "assistant", "message": {"id": "syn", "model": "<synthetic>", "usage": {}}}
    )
    b.events.append({"type": "assistant", "message": {"id": "nousage", "content": []}})
    b.events.append({"type": "progress"})
    # tool result carrying extra text, a compaction summary, an unattributed meta event
    b.events.append(
        {
            "type": "user",
            "uuid": "r1",
            "timestamp": "2026-11-03T10:00:30.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "srv1", "content": "res"},
                    {"type": "text", "text": "<system-reminder>look</system-reminder>"},
                ],
            },
        }
    )
    b.events.append(
        {
            "type": "user",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "summary"},
        }
    )
    b.events.append(
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}}
    )
    b.events.append({"type": "user", "message": {"role": "user", "content": ""}})
    b.call("m2", [("text", "next")], out=10, new=300)
    sess = parse_session(b.write())
    assert [c.message_id for c in sess.calls] == ["m1", "m2"]
    kinds = [i.kind for i in sess.calls[1].new_items]
    assert "compaction_summary" in kinds and kinds.count("meta") >= 2
    assert sess.calls[1].context_reset
    _balanced(attribute(sess))
