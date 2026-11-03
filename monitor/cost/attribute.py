"""Attribute session cost to prompts, tools, skills and context buckets.

An API request's bill has three parts, and each is owned differently:

* **output** (``out``): generating tokens. Owned by the blocks the model emitted:
  tool-call arguments go to that tool invocation; thinking and text to their own buckets.
* **write** (``input`` + cache writes): getting *new* content into context. That content is
  the previous request's output (which re-enters context, thinking included - verified on
  real logs) plus whatever the user side added (prompt, tool results, skill text).
* **read** (cache reads): re-reading everything already in context, on *every* request.
  This is the "carry" cost and is usually the largest part of a Claude Code bill: a tool
  result costs its write once, then its tokens times the read rate for every later call.

We keep a token ledger of what is in context. Reads are split across ledger entries by
token share, writes across the new entries. Token counts per entry are *measured totals
split by character share* (the log has no per-block token counts), so per-item figures are
estimates; totals are exact and always reconcile with the per-request cost.

Two views of a tool's cost:

``own_usd`` (resource view)
    generation + write + carry of its arguments and result. Additive across tools.
``roundtrip_usd`` (trigger view)
    the full cost of the API calls its result triggered. Not additive with ``own_usd``;
    shows what a call costs *because it forces another round trip over the whole context*.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from monitor.cost.parse import MAIN, ApiCall, Block, ParsedSession, parse_ts
from monitor.cost.pricing import CostBreakdown, cost_of, rates_for

# Only used for relative weights; totals always come from measured usage. Fitted on a real Claude Code
# session (112 single-tool-result transitions): new tokens ~= 43 + chars / 2.18, R^2 = 0.97. Dense JSON, URLs
# and markdown tokenise at ~2.2 chars/token; plain prose is nearer 4, so 2.5 is a deliberate middle.
CHARS_PER_TOKEN = 2.5
CACHE_TTL_5M = 300.0
CACHE_TTL_1H = 3600.0
REBUILD_TOLERANCE = 0.9  # cache read below this fraction of the previous prompt = a cache miss
BASELINE = "baseline"
REBUILD = "cache_rebuild"
THINKING = "thinking"
TEXT = "assistant_text"
USER_PROMPT = "user_prompt"
OTHER_CONTEXT = "other_context"


@dataclass
class CallCost:
    index: int
    message_id: str
    thread: str
    prompt_id: str | None
    skill: str | None
    model: str
    known_model: bool
    breakdown: CostBreakdown
    context_tokens: int  # everything the model read for this request
    rebuild_tokens: float = 0.0  # tokens re-written because the cache missed
    rebuild_usd: float = 0.0  # what that rewrite cost
    rebuild_excess_usd: float = 0.0  # extra paid versus reading it from cache
    gap_seconds: float | None = None  # idle time since the previous request in this thread

    @property
    def total(self) -> float:
        return self.breakdown.total


@dataclass
class ToolCost:
    tool_use_id: str
    name: str
    target: str
    prompt_id: str | None
    skill: str | None
    call_index: int
    is_error: bool | None
    result_chars: int = (
        0  # what the transcript shows; compare with result_tokens to spot hidden growth
    )
    args_tokens: float = 0.0
    result_tokens: float = 0.0
    out_usd: float = 0.0  # generating the arguments
    write_usd: float = 0.0  # writing arguments + result into the cache
    carry_usd: float = 0.0  # re-reading them on every later request
    action: str = ""  # what Bash really ran, from the full command (empty: derive from the target)
    result_write_usd: float = 0.0  # the result's share of write_usd (excludes the call's args)
    result_carry_usd: float = 0.0  # the result's share of carry_usd
    carried_calls: int = 0  # how many later requests re-read the result
    roundtrip_usd: float = 0.0  # cost of the requests this result triggered (trigger view)
    subagent_usd: float = 0.0  # cost of a subagent this call spawned (reported separately)

    @property
    def own_usd(self) -> float:
        return self.out_usd + self.write_usd + self.carry_usd


@dataclass
class SkillCost:
    name: str
    invocations: int = 0
    load_tokens: float = 0.0  # skill text injected into context
    load_write_usd: float = 0.0
    load_carry_usd: float = 0.0  # re-reading the skill text on later requests
    span_calls: int = 0  # requests Claude Code made while this skill was active
    span_usd: float = 0.0  # total cost of those requests (includes the load's carry)

    @property
    def load_usd(self) -> float:
        return self.load_write_usd + self.load_carry_usd


@dataclass
class PromptCost:
    prompt_id: str
    origin: str
    preview: str
    ts: str | None
    calls: int = 0
    tool_calls: int = 0  # tool_use blocks emitted
    tool_requests: int = 0  # requests that emitted at least one tool call (= round trips)
    output_usd: float = 0.0
    write_usd: float = 0.0
    read_usd: float = 0.0
    other_usd: float = 0.0  # web search fees

    @property
    def total(self) -> float:
        return self.output_usd + self.write_usd + self.read_usd + self.other_usd


@dataclass
class SessionCosts:
    session_id: str
    calls: list[CallCost] = field(default_factory=list)
    tools: dict[str, ToolCost] = field(default_factory=dict)
    skills: dict[str, SkillCost] = field(default_factory=dict)
    prompts: dict[str, PromptCost] = field(default_factory=dict)
    buckets: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    by_model: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    unknown_models: set[str] = field(default_factory=set)
    reported_usd: float | None = None
    reported_after_calls: int = 0
    baseline_tokens: float = (
        0.0  # system prompt + tool definitions + memory, re-read on every request
    )
    reported_models: dict[str, float] = field(default_factory=dict)

    @property
    def total_usd(self) -> float:
        return sum(c.total for c in self.calls)

    @property
    def logged_usd_at_report(self) -> float:
        """Our cost for the requests that existed when Claude Code wrote its own total."""
        return sum(c.total for c in self.calls[: self.reported_after_calls])

    @property
    def attributed_usd(self) -> float:
        """Everything the ledger assigned; must equal ``total_usd`` (checked in tests)."""
        tools = sum(t.own_usd for t in self.tools.values())
        skills = sum(s.load_usd for s in self.skills.values())
        return tools + skills + sum(self.buckets.values())


# ---------- ledger -------------------------------------------------------------


@dataclass
class _Entry:
    kind: str  # baseline | user_prompt | other_context | tool_args | tool_result | skill_content | ...
    tokens: float
    key: str | None = None  # tool_use_id / skill name owner


def _call_weights(call: ApiCall, out_tokens: int) -> list[tuple[Block, float]]:
    """Split a request's output tokens across its blocks (measured total, estimated split).

    Visible blocks are weighted by size. Thinking text is usually omitted from logs, so its
    share is whatever output the visible blocks cannot account for.
    """
    visible = {id(b): b.chars / CHARS_PER_TOKEN for b in call.blocks if b.kind != "thinking"}
    thinking = [b for b in call.blocks if b.kind == "thinking"]
    spare = max(out_tokens - sum(visible.values()), 0.0)
    weights: list[tuple[Block, float]] = []
    for b in call.blocks:
        if b.kind == "thinking":
            weights.append((b, spare / len(thinking)))
        else:
            weights.append((b, visible[id(b)]))
    total = sum(w for _, w in weights)
    if total <= 0:
        return [(b, out_tokens / len(weights)) for b, _ in weights] if weights else []
    return [(b, w * out_tokens / total) for b, w in weights]


def attribute(sess: ParsedSession) -> SessionCosts:
    out = SessionCosts(
        sess.session_id,
        reported_usd=sess.reported_cost_usd,
        reported_after_calls=sess.reported_after_calls,
        reported_models=dict(sess.reported_model_cost),
    )
    for pid, p in sess.prompts.items():
        out.prompts[pid] = PromptCost(pid, p.origin, p.preview, p.ts)
    for tid, tu in sess.tool_uses.items():
        out.tools[tid] = ToolCost(
            tid,
            tu.name,
            tu.target,
            tu.prompt_id,
            tu.skill,
            tu.call_index,
            tu.is_error,
            tu.result_chars or 0,
            action=tu.action,
        )

    ledgers: dict[str, list[_Entry]] = defaultdict(list)
    prev: dict[str, ApiCall] = {}
    # tool_use_id -> number of reads that included its result (for carried_calls)
    carried: dict[str, int] = defaultdict(int)

    for call in sess.calls:
        card = rates_for(call.model)
        known = card is not None
        bd = cost_of(call.usage, card) if card else CostBreakdown()
        if not known:
            out.unknown_models.add(call.model)
        cc = CallCost(
            index=call.index,
            message_id=call.message_id,
            thread=call.thread,
            prompt_id=call.prompt_id,
            skill=call.skill,
            model=call.model,
            known_model=known,
            breakdown=bd,
            context_tokens=call.usage.prompt_tokens,
        )
        out.calls.append(cc)
        out.by_model[call.model] += bd.total
        ledger = ledgers[call.thread]
        p_call = prev.get(call.thread)

        # ---- compaction wipes the conversation but keeps the cached prefix (baseline)
        if call.context_reset:
            ledger[:] = [e for e in ledger if e.kind == BASELINE]

        # ---- READ: carry cost of everything already in context
        ledger_total = sum(e.tokens for e in ledger)
        if ledger_total > 0:
            for e in ledger:
                share = bd.cache_read * e.tokens / ledger_total
                _credit(out, e, "carry", share)
                if e.kind == "tool_result" and e.key:
                    carried[e.key] += 1
        else:  # first request: whatever was already cached belongs to the baseline
            out.buckets[BASELINE] += bd.cache_read
            if call.usage.cache_read_tokens:
                ledger.append(_Entry(BASELINE, float(call.usage.cache_read_tokens)))

        # ---- WRITE: new content (and, after a cache miss, a rewrite of old content)
        w_tokens = float(call.usage.input_tokens + call.usage.cache_write_tokens)
        write_cost = bd.write_side
        surplus = 0.0
        if p_call is not None and not call.context_reset:
            expected = float(p_call.usage.prompt_tokens)
            if expected > 1000 and call.usage.cache_read_tokens < REBUILD_TOLERANCE * expected:
                surplus = min(expected - call.usage.cache_read_tokens, w_tokens)
        if surplus > 0 and w_tokens > 0:
            rate = write_cost / w_tokens
            cc.rebuild_tokens, cc.rebuild_usd = surplus, surplus * rate
            read_rate = (
                bd.cache_read / call.usage.cache_read_tokens
                if call.usage.cache_read_tokens
                else (card.for_prompt(call.usage.prompt_tokens).cache_read if card else 0.0)
            )
            cc.rebuild_excess_usd = surplus * max(rate - read_rate, 0.0)
            out.buckets[REBUILD] += cc.rebuild_usd
            write_cost -= cc.rebuild_usd
        new_tokens = w_tokens - surplus
        if p_call is not None and not call.context_reset:
            # (after a compaction the previous output is gone from context: the summary replaces it)
            _promote_previous_output(out, ledger, p_call, new_tokens, write_cost)
            carried_out = min(float(p_call.usage.output_tokens), new_tokens)
            write_cost_user = write_cost * (1 - carried_out / new_tokens) if new_tokens > 0 else 0.0
            new_tokens -= carried_out
        else:
            write_cost_user = write_cost
        _write_user_side(out, ledger, call, new_tokens, write_cost_user, first=p_call is None)

        # ---- OUTPUT: generating this request's blocks
        for block, tokens in _call_weights(call, call.usage.output_tokens):
            share = (
                bd.output * tokens / call.usage.output_tokens if call.usage.output_tokens else 0.0
            )
            if block.kind in ("tool_use", "server_tool_use") and block.tool_use_id in out.tools:
                out.tools[block.tool_use_id].out_usd += share
                out.tools[block.tool_use_id].args_tokens += tokens
            elif block.kind == "thinking":
                out.buckets[THINKING] += share
            else:
                out.buckets[TEXT] += share
        if not call.blocks:  # no blocks recorded: keep the books balanced
            out.buckets[TEXT] += bd.output
        # web-search fees belong to the call but not to any context entry
        out.buckets["web_search_fees"] += bd.web_search

        _roundtrip(out, call, bd.total)
        _rollup_prompt_and_skill(out, call, cc)
        prev[call.thread] = call
        cc.gap_seconds = _gap(p_call, call)

    out.baseline_tokens = sum(e.tokens for e in ledgers.get(MAIN, []) if e.kind == BASELINE)
    for tid, n in carried.items():
        if tid in out.tools:
            out.tools[tid].carried_calls = n
    _link_subagents(sess, out)
    out.buckets = dict(out.buckets)
    out.by_model = dict(out.by_model)
    return out


def _gap(p_call: ApiCall | None, call: ApiCall) -> float | None:
    if p_call is None:
        return None
    a, b = parse_ts(p_call.ts), parse_ts(call.ts)
    return (b - a).total_seconds() if a and b else None


def _credit(out: SessionCosts, e: _Entry, how: str, usd: float) -> None:
    """Credit ``usd`` of carry (re-read) or write cost to whoever owns ledger entry ``e``."""
    if e.kind in ("tool_args", "tool_result") and e.key in out.tools:
        t = out.tools[e.key]
        if how == "carry":
            t.carry_usd += usd
        else:
            t.write_usd += usd
        if e.kind == "tool_result":
            if how == "carry":
                t.result_carry_usd += usd
            else:
                t.result_write_usd += usd
            t.result_tokens = max(t.result_tokens, e.tokens)
    elif e.kind == "skill_content" and e.key:
        s = out.skills.setdefault(e.key, SkillCost(e.key))
        if how == "carry":
            s.load_carry_usd += usd
        else:
            s.load_write_usd += usd
    else:
        out.buckets[e.kind] += usd


def _promote_previous_output(
    out: SessionCosts,
    ledger: list[_Entry],
    p_call: ApiCall,
    new_tokens: float,
    write_cost: float,
) -> None:
    """The previous request's output re-enters context now: write it and add it to the ledger."""
    carried_out = min(float(p_call.usage.output_tokens), new_tokens)
    if carried_out <= 0 or new_tokens <= 0:
        return
    cost_per_token = write_cost / new_tokens
    weights = _call_weights(p_call, p_call.usage.output_tokens)
    total_w = sum(w for _, w in weights)
    if not weights or total_w <= 0:
        # no parsed blocks to split over: still book the dollars (caller removed them from the
        # user-side write) so attributed_usd stays equal to total_usd
        entry = _Entry(TEXT, carried_out)
        _credit(out, entry, "write", carried_out * cost_per_token)
        ledger.append(entry)
        return
    for block, w in weights:
        tokens = carried_out * w / total_w
        if block.kind in ("tool_use", "server_tool_use") and block.tool_use_id in out.tools:
            entry = _Entry("tool_args", tokens, block.tool_use_id)
        else:
            entry = _Entry(THINKING if block.kind == "thinking" else TEXT, tokens)
        _credit(out, entry, "write", tokens * cost_per_token)
        ledger.append(entry)


def _write_user_side(
    out: SessionCosts, ledger: list[_Entry], call: ApiCall, tokens: float, usd: float, first: bool
) -> None:
    """Split the user-side new tokens (prompt, tool results, skill text) over their items."""
    items = call.new_items
    if first:
        est = sum(i.chars for i in items) / CHARS_PER_TOKEN
        base = max(tokens - est, 0.0)
        if tokens > 0:
            out.buckets[BASELINE] += usd * base / tokens
            ledger.append(_Entry(BASELINE, base))
        item_tokens, item_usd = tokens - base, usd - (usd * base / tokens if tokens > 0 else 0.0)
    else:
        item_tokens, item_usd = tokens, usd
    if item_tokens <= 0:
        out.buckets[OTHER_CONTEXT] += max(item_usd, 0.0)
        return
    total_chars = sum(i.chars for i in items)
    if not items or total_chars <= 0:
        out.buckets[OTHER_CONTEXT] += item_usd
        ledger.append(_Entry(OTHER_CONTEXT, item_tokens))
        return
    for it in items:
        share = it.chars / total_chars
        tok, cost = item_tokens * share, item_usd * share
        if it.kind == "tool_result" and it.tool_use_id in out.tools:
            entry = _Entry("tool_result", tok, it.tool_use_id)
        elif it.kind == "skill_content" and it.skill:
            entry = _Entry("skill_content", tok, it.skill)
            out.skills.setdefault(it.skill, SkillCost(it.skill)).load_tokens += tok
        elif it.kind == "user_prompt":
            entry = _Entry(USER_PROMPT, tok)
        else:
            entry = _Entry(OTHER_CONTEXT, tok)
        _credit(out, entry, "write", cost)
        ledger.append(entry)


def _roundtrip(out: SessionCosts, call: ApiCall, total: float) -> None:
    """Trigger view: charge this request to the tool results that caused it."""

    def triggers(kind: str) -> list[tuple[str, int]]:
        return [
            (i.tool_use_id, max(i.chars, 1))
            for i in call.new_items
            if i.kind == kind and i.tool_use_id is not None and i.tool_use_id in out.tools
        ]

    chosen = triggers("tool_result") or triggers("skill_content")
    weight = sum(w for _, w in chosen)
    for tool_use_id, w in chosen:
        out.tools[tool_use_id].roundtrip_usd += total * w / weight


def _rollup_prompt_and_skill(out: SessionCosts, call: ApiCall, cc: CallCost) -> None:
    if call.prompt_id and call.prompt_id in out.prompts:
        p = out.prompts[call.prompt_id]
        p.calls += 1
        tool_blocks = sum(1 for b in call.blocks if b.kind == "tool_use")
        p.tool_calls += tool_blocks
        p.tool_requests += 1 if tool_blocks else 0
        p.output_usd += cc.breakdown.output
        p.write_usd += cc.breakdown.write_side
        p.read_usd += cc.breakdown.cache_read
        p.other_usd += cc.breakdown.web_search
    if call.skill:
        s = out.skills.setdefault(call.skill, SkillCost(call.skill))
        s.span_calls += 1
        s.span_usd += cc.total
    for b in call.blocks:
        if b.kind == "tool_use" and b.name == "Skill" and b.tool_use_id in out.tools:
            name = (
                out.tools[b.tool_use_id].target.split()[0]
                if out.tools[b.tool_use_id].target
                else ""
            )
            if name:
                out.skills.setdefault(name, SkillCost(name)).invocations += 1


def _link_subagents(sess: ParsedSession, out: SessionCosts) -> None:
    for c, cc in zip(sess.calls, out.calls, strict=True):
        if c.parent_tool_use_id and c.parent_tool_use_id in out.tools:
            out.tools[c.parent_tool_use_id].subagent_usd += cc.total
