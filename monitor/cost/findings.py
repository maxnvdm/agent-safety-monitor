"""Spot where a session spent more than it needed to.

Every finding says what the pattern *cost* (a measured number) and, only where a defensible
assumption exists, an *estimated saving* with that assumption spelled out. Detectors are
deliberately conservative: a finding should be worth a human's attention, not noise.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from monitor.cost.attribute import BASELINE, THINKING, SessionCosts
from monitor.cost.parse import ParsedSession


@dataclass(frozen=True)
class Thresholds:
    min_usd: float = 0.01  # ignore findings costing less than this
    large_result_tokens: float = 5_000
    result_target_tokens: float = 2_000  # what a "reasonable" result would have been
    heavy_skill_tokens: float = 8_000
    skill_halved: float = 0.5  # assumed saving if skill text were halved
    many_roundtrips_calls: int = 15
    long_context_tokens: int = 150_000
    long_context_share: float = 0.25
    thinking_share: float = 0.25
    baseline_share: float = 0.20
    hidden_growth_tokens: float = 1_500  # context growth beyond what the result's text explains


@dataclass(frozen=True)
class Finding:
    kind: str
    session_id: str
    summary: str
    suggestion: str
    cost_usd: float  # measured: what this pattern cost
    savings_usd: float | None = None  # estimated, only when ``assumption`` is stated
    assumption: str = ""
    prompt_id: str | None = None
    tool_use_id: str | None = None
    skill: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


_RESULT_ADVICE = {
    "Read": "Read with offset/limit, or Grep for the lines you need instead of the whole file.",
    "Bash": "Filter at the source: pipe through head/tail/grep/wc, or redirect to a file and read the part you need.",
    "WebFetch": "Ask a narrower question in the fetch prompt, or fetch a more specific URL.",
    "Grep": "Tighten the pattern, add a glob/type filter, or use files_with_matches mode first.",
    "Glob": "Use a more specific pattern.",
}


# Fitted on a real session: tokens a result adds ~= 43 + chars / 2.18 (R^2 0.97).
_RESULT_OVERHEAD_TOKENS = 43.0
_RESULT_CHARS_PER_TOKEN = 2.18


def _advice_for(name: str) -> str:
    if name in _RESULT_ADVICE:
        return _RESULT_ADVICE[name]
    if name.startswith("mcp__"):
        return "Ask the MCP tool for fewer fields (minimal output, pagination, filters) rather than everything."
    return "Return less from this tool, or summarise its output before it enters the conversation."


def detect(sess: ParsedSession, costs: SessionCosts, th: Thresholds | None = None) -> list[Finding]:
    th = th or Thresholds()
    found: list[Finding] = []
    for fn in (
        _cache_rebuilds,
        _large_results,
        _repeated_identical_calls,
        _failed_calls,
        _heavy_skills,
        _many_roundtrips,
        _long_context,
        _thinking_heavy,
        _baseline_heavy,
        _hidden_context,
    ):
        found.extend(fn(sess, costs, th))
    found = [f for f in found if f.cost_usd >= th.min_usd]
    return sorted(found, key=lambda f: (f.savings_usd or 0.0, f.cost_usd), reverse=True)


# ---- detectors ---------------------------------------------------------------


def _cache_rebuilds(sess: ParsedSession, costs: SessionCosts, th: Thresholds) -> list[Finding]:
    out = []
    for c in costs.calls:
        if c.rebuild_tokens <= 0 or c.rebuild_excess_usd < th.min_usd:
            continue
        idle = c.gap_seconds is not None and c.gap_seconds > 300
        out.append(
            Finding(
                kind="cache_rebuild",
                session_id=costs.session_id,
                prompt_id=c.prompt_id,
                cost_usd=c.rebuild_usd,
                savings_usd=c.rebuild_excess_usd,
                assumption="the context would have been read from cache instead of rewritten",
                summary=(
                    f"Cache miss rewrote {c.rebuild_tokens:,.0f} tokens"
                    + (f" after a {c.gap_seconds:,.0f}s idle gap" if idle else "")
                ),
                suggestion=(
                    "The prompt cache expired while idle (5 min, or 1 h for 1h writes). Batch work so requests "
                    "arrive within the TTL, or avoid pausing mid-task."
                    if idle
                    else "The cache missed without a long idle gap: something early in the prompt changed "
                    "(tool list, MCP reconnect, system prompt/memory edit). Keep the prefix stable."
                ),
                evidence={
                    "gap_seconds": c.gap_seconds,
                    "tokens": c.rebuild_tokens,
                    "call_index": c.index,
                },
            )
        )
    return out


def _large_results(sess: ParsedSession, costs: SessionCosts, th: Thresholds) -> list[Finding]:
    out = []
    for t in costs.tools.values():
        if t.result_tokens < th.large_result_tokens:
            continue
        lifetime = t.result_write_usd + t.result_carry_usd  # the result only, not the call's args
        keep = min(th.result_target_tokens / t.result_tokens, 1.0)
        out.append(
            Finding(
                kind="large_tool_result",
                session_id=costs.session_id,
                prompt_id=t.prompt_id,
                tool_use_id=t.tool_use_id,
                cost_usd=lifetime,
                savings_usd=lifetime * (1 - keep),
                assumption=f"result trimmed to ~{th.result_target_tokens:,.0f} tokens",
                summary=(
                    f"{t.name} returned ~{t.result_tokens:,.0f} tokens that were re-read by "
                    f"{t.carried_calls} later requests: {t.target[:80]}"
                ),
                suggestion=_advice_for(t.name),
                evidence={
                    "result_tokens": t.result_tokens,
                    "carried_calls": t.carried_calls,
                    "tool": t.name,
                },
            )
        )
    return out


def _repeated_identical_calls(
    sess: ParsedSession, costs: SessionCosts, th: Thresholds
) -> list[Finding]:
    groups: dict[str, list[str]] = defaultdict(list)
    for tid, tu in sess.tool_uses.items():
        if tu.result_hash is not None:
            groups[tu.input_hash].append(tid)
    out = []
    for ids in groups.values():
        ids.sort(key=lambda i: sess.tool_uses[i].call_index)
        # Only *consecutive* identical results count: A, B, A means the state changed in between,
        # so the second A was a legitimate re-check.
        first = ids[0]
        prev_hash = sess.tool_uses[ids[0]].result_hash
        for tid in ids[1:]:
            tu = sess.tool_uses[tid]
            if tu.result_hash != prev_hash:
                first, prev_hash = tid, tu.result_hash
                continue
            t = costs.tools[tid]
            out.append(
                Finding(
                    kind="repeated_identical_call",
                    session_id=costs.session_id,
                    prompt_id=tu.prompt_id,
                    tool_use_id=tid,
                    cost_usd=t.own_usd,
                    savings_usd=t.own_usd,
                    assumption="the repeat was unnecessary (same call, byte-identical result)",
                    summary=f"{tu.name} repeated with an identical result: {tu.target[:80]}",
                    suggestion="Reuse the earlier result (it is still in context) or wait for a real change "
                    "before polling again.",
                    evidence={
                        "first_call": first,
                        "roundtrip_usd": t.roundtrip_usd,
                        "tool": tu.name,
                    },
                )
            )
    return out


def _failed_calls(sess: ParsedSession, costs: SessionCosts, th: Thresholds) -> list[Finding]:
    out = []
    for t in costs.tools.values():
        if t.is_error:
            out.append(
                Finding(
                    kind="failed_tool_call",
                    session_id=costs.session_id,
                    prompt_id=t.prompt_id,
                    tool_use_id=t.tool_use_id,
                    cost_usd=t.own_usd,
                    summary=f"{t.name} failed: {t.target[:80]}",
                    suggestion="Check arguments before calling (paths, flags, required params); repeated "
                    "failures of one tool usually mean a missing precondition or an unclear tool description.",
                    evidence={"roundtrip_usd": t.roundtrip_usd, "tool": t.name},
                )
            )
    return out


def _heavy_skills(sess: ParsedSession, costs: SessionCosts, th: Thresholds) -> list[Finding]:
    out = []
    for s in costs.skills.values():
        if s.load_tokens < th.heavy_skill_tokens:
            continue
        out.append(
            Finding(
                kind="heavy_skill",
                session_id=costs.session_id,
                skill=s.name,
                cost_usd=s.load_usd,
                savings_usd=s.load_usd * th.skill_halved,
                assumption=f"skill text reduced by {th.skill_halved:.0%}",
                summary=(
                    f"Skill '{s.name}' put ~{s.load_tokens:,.0f} tokens in context: ${s.load_usd:.2f} to load and "
                    f"re-read, versus ${s.span_usd:.2f} for the {s.span_calls} requests made while it was active"
                ),
                suggestion=(
                    "Keep SKILL.md to the essentials and move reference material into separate files the skill "
                    "reads only when needed (progressive disclosure); invoke the skill only for tasks that need it."
                ),
                evidence={
                    "load_tokens": s.load_tokens,
                    "load_carry_usd": s.load_carry_usd,
                    "span_usd": s.span_usd,
                    "span_calls": s.span_calls,
                },
            )
        )
    return out


def _many_roundtrips(sess: ParsedSession, costs: SessionCosts, th: Thresholds) -> list[Finding]:
    out = []
    for p in costs.prompts.values():
        if p.tool_requests < th.many_roundtrips_calls:
            continue
        out.append(
            Finding(
                kind="many_roundtrips",
                session_id=costs.session_id,
                prompt_id=p.prompt_id,
                cost_usd=p.read_usd,
                summary=(
                    f"{p.tool_requests} tool round trips ({p.tool_calls} calls) across {p.calls} requests: ${p.read_usd:.2f} went on re-reading "
                    f"context on every round trip ({p.preview[:60]!r})"
                ),
                suggestion=(
                    "Each tool call is a full round trip over the whole context. Batch independent calls into one "
                    "turn (parallel tool use), or replace a chain of small calls with one script."
                ),
                evidence={
                    "round_trips": p.tool_requests,
                    "tool_calls": p.tool_calls,
                    "requests": p.calls,
                    "read_usd": p.read_usd,
                },
            )
        )
    return out


def _long_context(sess: ParsedSession, costs: SessionCosts, th: Thresholds) -> list[Finding]:
    total = costs.total_usd
    big = [c for c in costs.calls if c.context_tokens >= th.long_context_tokens]
    spent = sum(c.total for c in big)
    if not big or total <= 0 or spent / total < th.long_context_share:
        return []
    return [
        Finding(
            kind="long_context",
            session_id=costs.session_id,
            cost_usd=spent,
            summary=f"{len(big)} requests ran with >={th.long_context_tokens:,} tokens of context "
            f"({spent / total:.0%} of the session's cost)",
            suggestion="Compact or start a fresh session before context grows; delegate exploration-heavy work "
            "(large reads, searches) to subagents so their output never enters the main context.",
            evidence={"requests": len(big), "share": spent / total},
        )
    ]


def _thinking_heavy(sess: ParsedSession, costs: SessionCosts, th: Thresholds) -> list[Finding]:
    total = costs.total_usd
    spent = costs.buckets.get(THINKING, 0.0)
    if total <= 0 or spent / total < th.thinking_share:
        return []
    efforts = sorted({c.effort for c in sess.calls if c.effort})
    return [
        Finding(
            kind="thinking_heavy",
            session_id=costs.session_id,
            cost_usd=spent,
            summary=f"Thinking tokens cost ${spent:.2f} ({spent / total:.0%} of the session; effort: "
            f"{', '.join(efforts) or 'unknown'})",
            suggestion="Use a lower effort level for routine or mechanical work (edits, polling, simple lookups) "
            "and keep high effort for genuinely hard steps.",
            evidence={"share": spent / total, "efforts": efforts},
        )
    ]


def _baseline_heavy(sess: ParsedSession, costs: SessionCosts, th: Thresholds) -> list[Finding]:
    total = costs.total_usd
    spent = costs.buckets.get(BASELINE, 0.0)
    if total <= 0 or spent / total < th.baseline_share:
        return []
    return [
        Finding(
            kind="baseline_heavy",
            session_id=costs.session_id,
            cost_usd=spent,
            summary=f"The fixed context (system prompt, tool definitions, memory; ~{costs.baseline_tokens:,.0f} "
            f"tokens) cost ${spent:.2f} ({spent / total:.0%}) because it is re-read on every request",
            suggestion="Trim CLAUDE.md and memory, disconnect MCP servers you are not using (their tool definitions "
            "are paid for on every request), and prefer on-demand tool loading.",
            evidence={"baseline_tokens": costs.baseline_tokens, "share": spent / total},
        )
    ]


def _hidden_context(sess: ParsedSession, costs: SessionCosts, th: Thresholds) -> list[Finding]:
    """Calls that grew the context far more than their visible output explains.

    Typical cause: ToolSearch (or similar) loading tool definitions the transcript never shows. The
    tokens are real and are re-read on every later request, so they belong to that call.
    """
    out = []
    for t in costs.tools.values():
        explained = _RESULT_OVERHEAD_TOKENS + t.result_chars / _RESULT_CHARS_PER_TOKEN
        excess = t.result_tokens - explained
        if excess < th.hidden_growth_tokens or t.result_tokens < 3 * explained:
            continue
        share = excess / t.result_tokens
        out.append(
            Finding(
                kind="hidden_context_growth",
                session_id=costs.session_id,
                prompt_id=t.prompt_id,
                tool_use_id=t.tool_use_id,
                cost_usd=(t.write_usd + t.carry_usd) * share,
                summary=(
                    f"{t.name} showed {t.result_chars:,} characters but grew the context by ~{t.result_tokens:,.0f} "
                    f"tokens (~{excess:,.0f} not visible in the transcript), re-read by {t.carried_calls} later requests"
                ),
                suggestion=(
                    "Content was injected that the log does not show, most often tool definitions loaded on "
                    "demand. Load only the tools you need, or avoid loading them in sessions that will not use them."
                    if t.name == "ToolSearch"
                    else "Hooks, reminders or injected content rode along with this call. Check hook output and "
                    "system-reminder size."
                ),
                evidence={
                    "result_chars": t.result_chars,
                    "result_tokens": t.result_tokens,
                    "tool": t.name,
                },
            )
        )
    return out
