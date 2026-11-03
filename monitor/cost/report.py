"""Plain-text rendering of cost analysis (the CLI's output)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(value: Any) -> str:
    """Make log-derived text safe to print: control characters (ANSI escapes, cursor moves,
    carriage returns...) could otherwise rewrite or spoof the report in a terminal."""
    return _CONTROL_RE.sub(" ", str(value))


def usd(v: float | None, places: int = 2) -> str:
    if v is None:
        return "-"
    return f"${v:,.{places}f}" if abs(v) >= 0.01 or v == 0 else f"${v:.4f}"


def tok(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v / 1000:.1f}K" if abs(v) >= 1000 else f"{v:.0f}"


def pct(part: float | None, whole: float | None) -> str:
    return f"{100 * (part or 0) / whole:.0f}%" if whole else "-"


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]], left: Sequence[int] = (0,)) -> str:
    cells = [[clean(c) for c in r] for r in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)
    ]

    def fmt(r: Sequence[str]) -> str:
        return "  ".join(
            c.ljust(widths[i]) if i in left else c.rjust(widths[i]) for i, c in enumerate(r)
        )

    return "\n".join([fmt(headers), "  ".join("-" * w for w in widths), *(fmt(r) for r in cells)])


_BUCKET_LABELS = {
    "tool_calls": "tool calls (generating, writing and re-reading args + results)",
    "skill_text": "skill text loaded into context",
    "baseline": "fixed context (system prompt, tool defs, memory)",
    "thinking": "thinking",
    "assistant_text": "assistant replies",
    "user_prompt": "your prompts",
    "cache_rebuild": "cache rebuilds (cache miss rewrites)",
    "other_context": "other context (reminders, attachments)",
    "web_search_fees": "web search fees",
}


def render(
    *,
    session_id: str | None,
    overall: dict[str, Any],
    detail: dict[str, Any] | None,
    buckets: dict[str, float],
    prompts: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    group_by: str,
    invocations: Sequence[dict[str, Any]],
    skills: Sequence[dict[str, Any]],
    waste: Sequence[dict[str, Any]],
    findings: Sequence[dict[str, Any]],
) -> str:
    total = overall["total_usd"] or 0.0
    out: list[str] = []
    title = f"Session {clean(session_id)}" if session_id else f"{overall['sessions']} sessions"
    out.append(
        f"{title}: {usd(total)} over {overall['requests']} API requests and {overall['prompts']} prompts"
    )
    out.append(
        f"  by cost class: re-reading context {usd(overall['read_usd'])} ({pct(overall['read_usd'], total)}), "
        f"writing new context {usd(overall['write_usd'])} ({pct(overall['write_usd'], total)}), "
        f"generating output {usd(overall['output_usd'])} ({pct(overall['output_usd'], total)})"
    )
    if detail and detail.get("reported_usd") is not None:
        out.append(
            f"  Claude Code's own total was {usd(detail['reported_usd'])} when it last wrote one; the transcript "
            f"accounts for {usd(detail['logged_usd_at_report'])} of that. "
            + (
                f"The difference ({usd(detail['unlogged_usd'])}) is requests that never appear in the "
                "transcript (WebFetch/WebSearch summarising, title generation, ...)."
                if detail["unlogged_usd"] >= 0
                else f"The transcript is {usd(-detail['unlogged_usd'])} HIGHER than Claude Code's figure: "
                "check the price table (MONITOR_PRICES) and the logged models."
            )
        )
    if detail and detail.get("unknown_models"):
        out.append(
            f"  WARNING: no price for {clean(', '.join(detail['unknown_models']))}; those requests count as $0."
        )

    out += ["", "Where the money goes (every dollar has exactly one owner)"]
    rows = [
        [_BUCKET_LABELS.get(k, k), usd(v), pct(v, total)]
        for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])
        if v > 0
    ]
    out.append(table(["bucket", "cost", "share"], rows))

    out += ["", f"Most expensive prompts ({len(prompts)} shown)"]
    out.append(
        table(
            ["cost", "share", "reqs", "tools", "origin", "prompt"],
            [
                [
                    usd(p["total_usd"]),
                    pct(p["total_usd"], total),
                    p["requests"],
                    p["tool_calls"],
                    p["origin"],
                    p["preview"][:60],
                ]
                for p in prompts
            ],
            left=(4, 5),
        )
    )

    out += [
        "",
        f"Tools by {group_by}. 'own' = generating the call + writing its args/result + re-reading them on every "
        "later request (additive). 'round-trip' = the whole cost of the requests its result triggered (a different "
        "view; do not add the two).",
    ]
    out.append(
        table(
            [group_by, "calls", "own", "share", "avg own", "round-trip", "avg result", "errors"],
            [
                [
                    r["key"], r["invocations"], usd(r["own_usd"]), pct(r["own_usd"], total), usd(r["avg_own_usd"], 3),
                    usd(r["roundtrip_usd"]), tok(r["avg_result_tokens"]), r["errors"],
                ]
                for r in tools
            ],
        )
    )  # fmt: skip

    if invocations:
        out += ["", "Individual calls that cost the most"]
        out.append(
            table(
                ["own", "round-trip", "result", "re-read by", "call"],
                [
                    [usd(i["own_usd"]), usd(i["roundtrip_usd"]), tok(i["result_tokens"]), f"{i['carried_calls']} reqs",
                     f"{i['action']}: {i['target'][:70]}"]
                    for i in invocations
                ],
                left=(4,),
            )
        )  # fmt: skip

    if skills:
        out += [
            "",
            "Skills. 'load' = writing the skill text into context + re-reading it on every later request; "
            "'active' = requests made while the skill was active.",
        ]
        out.append(
            table(
                ["skill", "uses", "text", "load", "of which re-read", "active reqs", "active cost"],
                [
                    [s["name"], s["invocations"], tok(s["avg_load_tokens"]), usd(s["load_usd"]), usd(s["load_carry_usd"]),
                     s["span_calls"], usd(s["span_usd"])]
                    for s in skills
                ],
            )
        )  # fmt: skip

    if waste:
        out += ["", "Likely waste, by pattern (savings are estimates; each states its assumption)"]
        out.append(
            table(
                ["pattern", "found", "cost", "est. saving"],
                [[w["kind"], w["findings"], usd(w["cost_usd"]), usd(w["savings_usd"]) if w["savings_usd"] else "-"] for w in waste],
            )
        )  # fmt: skip
    if findings:
        out += ["", f"Top findings ({len(findings)} shown)"]
        for f in findings:
            saving = (
                f"saves ~{usd(f['savings_usd'])} if {f['assumption']}"
                if f["savings_usd"]
                else "no estimate"
            )
            out.append(
                f"- [{clean(f['kind'])}] {usd(f['cost_usd'])} ({clean(saving)})\n    {clean(f['summary'])}\n    -> {clean(f['suggestion'])}"
            )
    return "\n".join(out)
