from pathlib import Path

import pytest

from monitor.cost.attribute import REBUILD, attribute
from monitor.cost.parse import parse_session, redact
from monitor.cost.pricing import rates_for
from tests.cost_helpers import LogBuilder

CARD = rates_for("claude-sonnet-5-5")
assert CARD is not None
R_OUT, R_READ, R_W1 = CARD.output, CARD.cache_read, CARD.cache_write_1h


def _check_balanced(costs) -> None:
    """Every dollar of API cost must be owned by exactly one tool, skill or bucket."""
    assert costs.attributed_usd == pytest.approx(costs.total_usd, rel=1e-9, abs=1e-12)


def _two_tool_session(tmp_path: Path) -> Path:
    b = LogBuilder(tmp_path)
    b.prompt("do it")
    b.call("m1", [("tool", "t1", "Bash", {"command": "ls"})], out=50, new=1000)
    b.result("t1", 1800)
    b.call("m2", [("tool", "t2", "Bash", {"command": "ls -la"})], out=40, new=650)
    b.result("t2", 600)
    b.call("m3", [("text", "done")], out=10, new=240)
    b.call("m4", [("text", "ok")], out=5, new=10)
    return b.write()


def test_requests_are_deduplicated_by_message_id(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call(
        "m1",
        [("thinking", "hmm"), ("text", "hi"), ("tool", "t1", "Read", {"file_path": "a.py"})],
        out=90,
        new=500,
    )
    sess = parse_session(b.write())
    assert len(sess.calls) == 1  # three events, one API request
    assert sess.calls[0].usage.output_tokens == 90  # final streaming figure, not the first snapshot
    assert [blk.kind for blk in sess.calls[0].blocks] == ["thinking", "text", "tool_use"]


def test_exact_tool_cost_decomposition(tmp_path: Path) -> None:
    costs = attribute(parse_session(_two_tool_session(tmp_path)))
    t1, t2 = costs.tools["t1"], costs.tools["t2"]
    # t1: generated 50 tokens of args; args(50)+result(600) written once, re-read by m3 and m4
    assert t1.out_usd == pytest.approx(50 * R_OUT)
    assert t1.write_usd == pytest.approx(650 * R_W1)
    assert t1.carry_usd == pytest.approx(2 * 650 * R_READ)
    assert t1.carried_calls == 2
    assert t1.result_tokens == pytest.approx(600)
    # t2: args(40)+result(200), re-read only by m4
    assert t2.out_usd == pytest.approx(40 * R_OUT)
    assert t2.write_usd == pytest.approx(240 * R_W1)
    assert t2.carry_usd == pytest.approx(240 * R_READ)
    _check_balanced(costs)


def test_roundtrip_view_charges_triggering_result(tmp_path: Path) -> None:
    costs = attribute(parse_session(_two_tool_session(tmp_path)))
    # t1's result triggered request m2; t2's result triggered m3
    m2, m3 = costs.calls[1].total, costs.calls[2].total
    assert costs.tools["t1"].roundtrip_usd == pytest.approx(m2)
    assert costs.tools["t2"].roundtrip_usd == pytest.approx(m3)


def test_prompt_totals_and_origin(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("first", pid="pA").call("m1", [("text", "a")], out=10, new=300)
    b.prompt("bg event", pid="pB", origin="task_notification").call(
        "m2", [("text", "b")], out=10, new=50
    )
    costs = attribute(parse_session(b.write()))
    assert set(costs.prompts) == {"pA", "pB"}
    assert costs.prompts["pB"].origin == "task_notification"
    assert costs.prompts["pA"].total == pytest.approx(costs.calls[0].total)
    assert sum(p.total for p in costs.prompts.values()) == pytest.approx(costs.total_usd)


def test_skill_load_carry_outlives_its_span(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("use the skill")
    b.call("m1", [("tool", "s1", "Skill", {"skill": "my-skill"})], out=20, new=500)
    b.result("s1", 20, text="Launching skill: my-skill")
    b.skill_content("s1", 30_000)
    b.call(
        "m2", [("text", "working")], out=30, new=10_000, skill="my-skill"
    )  # 10K tokens of skill text
    for i in range(3, 9):  # many later requests re-read the skill text after the skill is done
        b.call(f"m{i}", [("text", "x")], out=5, new=10)
    costs = attribute(parse_session(b.write()))
    skill = costs.skills["my-skill"]
    assert skill.invocations == 1
    assert skill.load_tokens > 9_000
    assert skill.span_calls == 1
    # the skill text is re-read by all six later requests, long after the skill itself finished
    assert skill.load_carry_usd == pytest.approx(skill.load_tokens * 6 * R_READ)
    assert skill.load_usd == pytest.approx(skill.load_write_usd + skill.load_carry_usd)
    _check_balanced(costs)


def test_cache_miss_is_detected_and_priced(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call("m1", [("tool", "t1", "Bash", {"command": "a"})], out=20, new=2000)
    b.result("t1", 300)
    b.call("m2", [("text", "x")], out=10, new=120)
    b.call("m3", [("text", "y")], out=10, new=100, cold=True, gap=400)  # idle > TTL: cache gone
    costs = attribute(parse_session(b.write()))
    c3 = costs.calls[2]
    assert c3.rebuild_tokens == pytest.approx(2120)  # the whole previous prompt had to be rewritten
    assert c3.gap_seconds == pytest.approx(400, abs=1)
    assert c3.rebuild_usd == pytest.approx(2120 * R_W1)
    assert c3.rebuild_excess_usd == pytest.approx(2120 * (R_W1 - R_READ))
    assert costs.buckets[REBUILD] == pytest.approx(c3.rebuild_usd)
    _check_balanced(costs)


def test_compaction_stops_carrying_old_context(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call("m1", [("tool", "t1", "Bash", {"command": "a"})], out=20, new=1000)
    b.result("t1", 3000)
    b.call("m2", [("text", "x")], out=10, new=1020)  # writes t1's args + result into the cache
    b.call("m2b", [("text", "x")], out=10, new=10)  # first request that re-reads them
    b.compact()
    b.call("m3", [("text", "y")], out=10, new=300)
    b.call("m4", [("text", "z")], out=10, new=10)
    costs = attribute(parse_session(b.write()))
    assert costs.tools["t1"].carried_calls == 1  # read once before compaction, never after
    _check_balanced(costs)


def test_unknown_model_costs_nothing_but_is_flagged(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path, model="mystery-model-1")
    b.prompt("go").call("m1", [("text", "a")], out=10, new=100)
    costs = attribute(parse_session(b.write()))
    assert costs.unknown_models == {"mystery-model-1"}
    assert costs.total_usd == 0.0
    _check_balanced(costs)


def test_reconciles_against_claude_codes_reported_total(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go").call("m1", [("text", "a")], out=10, new=100)
    b.cost_state(0.0123, {"claude-sonnet-5-5": 0.0123})
    b.call("m2", [("text", "b")], out=10, new=100)  # happened after the snapshot
    costs = attribute(parse_session(b.write()))
    assert costs.reported_usd == pytest.approx(0.0123)
    assert costs.reported_after_calls == 1
    assert costs.logged_usd_at_report == pytest.approx(costs.calls[0].total)
    assert costs.total_usd > costs.logged_usd_at_report


def test_subagent_cost_is_linked_to_spawning_call(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("delegate")
    b.call("m1", [("tool", "task1", "Task", {"prompt": "dig"})], out=30, new=800)
    b.result("task1", 200, tool_use_result={"agentId": "a1"})
    b.call("m2", [("text", "done")], out=10, new=300)
    path = b.write()
    sub = LogBuilder(tmp_path / "scratch", model="claude-haiku-5-5")
    (tmp_path / "scratch").mkdir()
    sub.prompt("dig").call("h1", [("text", "found")], out=100, new=5000)
    sub_dir = tmp_path / "sess-1" / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-a1.jsonl").write_text(sub.write().read_text())
    costs = attribute(parse_session(path))
    assert costs.tools["task1"].subagent_usd > 0
    assert any(c.thread == "agent:a1" for c in costs.calls)
    assert costs.calls[-1].prompt_id == costs.calls[0].prompt_id  # rolled up under the same prompt
    _check_balanced(costs)


def test_commands_are_redacted_before_storage(tmp_path: Path) -> None:
    secret = "sk-ant-api03-" + "a" * 30
    b = LogBuilder(tmp_path)
    b.prompt("deploy with token=hunter2hunter2hunter2")
    b.call(
        "m1",
        [("tool", "t1", "Bash", {"command": f"curl -H 'Authorization: Bearer {secret}' x"})],
        out=20,
        new=100,
    )
    sess = parse_session(b.write())
    assert secret not in sess.tool_uses["t1"].target
    assert "hunter2" not in next(iter(sess.prompts.values())).preview
    assert redact("password=abc123") == "password=[REDACTED]"
