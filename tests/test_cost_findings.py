from pathlib import Path

import pytest

from monitor.cost.attribute import attribute
from monitor.cost.findings import Thresholds, detect
from monitor.cost.parse import parse_session
from tests.cost_helpers import LogBuilder

LOW = Thresholds(
    min_usd=0.0, large_result_tokens=500, heavy_skill_tokens=1_000, many_roundtrips_calls=4
)


def _run(b: LogBuilder, th: Thresholds = LOW):
    sess = parse_session(b.write())
    costs = attribute(sess)
    return detect(sess, costs, th), costs


def _kinds(findings) -> set[str]:
    return {f.kind for f in findings}


def test_large_result_flagged_with_tool_specific_advice(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("read it")
    b.call("m1", [("tool", "t1", "Read", {"file_path": "big.py"})], out=20, new=500)
    b.result("t1", 30_000)
    for i in range(2, 6):
        b.call(f"m{i}", [("text", "x")], out=5, new=10 if i > 2 else 10_020)
    findings, _ = _run(b)
    f = next(f for f in findings if f.kind == "large_tool_result")
    assert f.tool_use_id == "t1" and "offset/limit" in f.suggestion
    assert f.savings_usd is not None and 0 < f.savings_usd < f.cost_usd
    assert "trimmed" in f.assumption


def test_identical_repeat_is_flagged_but_changed_result_is_not(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("poll")
    args = {"command": "git status"}
    for n, (tid, body) in enumerate(
        [("t1", "clean"), ("t2", "clean"), ("t3", "modified")], start=1
    ):
        b.call(f"m{n}", [("tool", tid, "Bash", args)], out=20, new=600 if n == 1 else 60)
        b.result(tid, 0, text=body)
    b.call("m4", [("text", "done")], out=5, new=30)
    findings, _ = _run(b)
    repeats = [f for f in findings if f.kind == "repeated_identical_call"]
    assert [f.tool_use_id for f in repeats] == [
        "t2"
    ]  # t3 returned something new: a legitimate re-check
    assert repeats[0].savings_usd == pytest.approx(repeats[0].cost_usd)


def test_failed_call_is_reported(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call("m1", [("tool", "t1", "Bash", {"command": "nope"})], out=20, new=600)
    b.result("t1", 40, error=True)
    b.call("m2", [("text", "oops")], out=5, new=30)
    findings, _ = _run(b)
    assert "failed_tool_call" in _kinds(findings)


def test_heavy_skill_flagged_with_cost_comparison(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call("m1", [("tool", "s1", "Skill", {"skill": "big-skill"})], out=20, new=500)
    b.result("s1", 20, text="Launching skill: big-skill")
    b.skill_content("s1", 20_000)
    b.call("m2", [("text", "x")], out=10, new=7_000, skill="big-skill")
    for i in range(3, 7):
        b.call(f"m{i}", [("text", "x")], out=5, new=10)
    findings, _ = _run(b)
    f = next(f for f in findings if f.kind == "heavy_skill")
    assert f.skill == "big-skill" and "progressive disclosure" in f.suggestion
    assert f.savings_usd == pytest.approx(f.cost_usd * 0.5)


def test_idle_cache_miss_is_flagged(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call("m1", [("text", "a")], out=10, new=40_000)
    b.call("m2", [("text", "b")], out=10, new=100, cold=True, gap=900)
    findings, costs = _run(b)
    f = next(f for f in findings if f.kind == "cache_rebuild")
    assert "idle" in f.suggestion and f.savings_usd == pytest.approx(
        costs.calls[1].rebuild_excess_usd
    )


def test_many_roundtrips_per_prompt(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("grind")
    for i in range(1, 6):
        b.call(
            f"m{i}",
            [("tool", f"t{i}", "Bash", {"command": f"echo {i}"})],
            out=15,
            new=300 if i == 1 else 40,
        )
        b.result(f"t{i}", 20, text=f"{i}")
    b.call("m6", [("text", "done")], out=5, new=30)
    findings, _ = _run(b)
    assert "many_roundtrips" in _kinds(findings)


def test_share_based_findings_need_a_big_enough_share(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("think hard")
    b.call("m1", [("thinking", ""), ("text", "ok")], out=5_000, new=300)
    b.call("m2", [("text", "x")], out=10, new=5_100)
    findings, _ = _run(b, Thresholds(min_usd=0.0, thinking_share=0.2, baseline_share=0.2))
    assert "thinking_heavy" in _kinds(findings)
    quiet, _ = _run(b, Thresholds(min_usd=0.0, thinking_share=0.99))
    assert "thinking_heavy" not in _kinds(quiet)


def test_findings_below_min_usd_are_dropped_and_sorted_by_savings(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("go")
    b.call("m1", [("tool", "t1", "Read", {"file_path": "a"})], out=20, new=500)
    b.result("t1", 30_000)
    b.call("m2", [("tool", "t2", "Read", {"file_path": "b"})], out=20, new=10_020)
    b.result("t2", 9_000)
    for i in range(3, 7):
        b.call(f"m{i}", [("text", "x")], out=5, new=3_010 if i == 3 else 10)
    findings, _ = _run(b)
    large = [f for f in findings if f.kind == "large_tool_result"]
    assert len(large) >= 2, "both oversized results must be reported for the order to mean anything"
    savings = [f.savings_usd or 0 for f in findings]
    assert savings == sorted(savings, reverse=True)
    none, _ = _run(b, Thresholds(min_usd=1e6))
    assert none == []


def test_hidden_context_growth_is_attributed_to_the_call_that_caused_it(tmp_path: Path) -> None:
    b = LogBuilder(tmp_path)
    b.prompt("load some tools")
    b.call("m1", [("tool", "t1", "ToolSearch", {"query": "select:Foo"})], out=20, new=800)
    b.result("t1", 150)  # the transcript shows 150 chars...
    b.call("m2", [("text", "x")], out=10, new=20 + 4_000)  # ...but the context grew by ~4K tokens
    for i in range(3, 6):
        b.call(f"m{i}", [("text", "x")], out=5, new=10)
    findings, _ = _run(b)
    f = next(f for f in findings if f.kind == "hidden_context_growth")
    assert f.tool_use_id == "t1" and "tool definitions" in f.suggestion and f.cost_usd > 0
    # an ordinary result of matching size is not flagged
    (tmp_path / "ok").mkdir()
    ok = LogBuilder(tmp_path / "ok")
    ok.prompt("read")
    ok.call("m1", [("tool", "t1", "Read", {"file_path": "a"})], out=20, new=800)
    ok.result("t1", 9_000)
    ok.call("m2", [("text", "x")], out=10, new=20 + 4_100)
    ok.call("m3", [("text", "x")], out=5, new=10)
    quiet, _ = _run(ok)
    assert "hidden_context_growth" not in {f.kind for f in quiet}
