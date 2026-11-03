"""Turn a Claude Code JSONL session log into cost-analysis records.

What the log gives us, and the traps in it (all verified against real logs):

* One API request is written as *several* events (one per content block), each
  repeating the request's ``usage`` while ``output_tokens`` grows. Summing events
  over-counts ~3x; we merge by ``message.id`` instead.
* ``promptId`` lives on *user* events only. Every API call belongs to the prompt in
  force when it was made.
* A tool result arrives in a later user event (``tool_result`` blocks). Skill text is
  injected as an ``isMeta`` user event whose ``sourceToolUseID`` is the ``Skill`` call.
* ``attributionSkill`` / ``attributionMcpServer`` / ``attributionMcpTool`` on assistant
  events say which skill or MCP tool Claude Code was acting under.
* ``cost-state`` is Claude Code's own running cost estimate: our reconciliation target.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from monitor.cost.actions import classify
from monitor.cost.pricing import Usage, as_dict
from monitor.ingest import iter_events
from monitor.scorers import SECRET_PATTERNS

MAIN = "main"
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|secret|passw(?:or)?d|passwd|api[_-]?key|authorization)\b(\s*[=:]\s*)(\S+)"
)


_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|secret|passw(?:or)?d|passwd|api[_-]?key|authorization|cookie|credential|bearer|private[_-]?key)"
)


def redact(text: str) -> str:
    """Mask obvious secrets before a command/path description is stored."""
    for _label, pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return _ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", text)


def redact_structure(value: Any) -> Any:
    """Copy of a JSON-like value with the value of every sensitive-looking key masked.

    Regexes over serialized JSON miss quoted keys (``"password": "x"``) and nested headers, so
    structured tool input is masked by key *before* it is serialized.
    """
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k)
            else redact_structure(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_structure(v) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value


# ---------- records -----------------------------------------------------------


@dataclass
class Block:
    """One content block of an assistant message."""

    kind: str  # thinking | text | tool_use | server_tool_use
    chars: int
    tool_use_id: str | None = None
    name: str | None = None


@dataclass
class ContextItem:
    """Something that entered the model's context from the user side."""

    kind: str  # user_prompt | tool_result | skill_content | meta | compaction_summary
    chars: int
    tool_use_id: str | None = (
        None  # tool_result: the call it answers; skill_content: the Skill call
    )
    skill: str | None = None


@dataclass
class ApiCall:
    index: int
    thread: str
    message_id: str
    request_id: str | None
    ts: str | None
    model: str
    usage: Usage
    prompt_id: str | None
    skill: str | None = None
    mcp_server: str | None = None
    mcp_tool: str | None = None
    blocks: list[Block] = field(default_factory=list)
    new_items: list[ContextItem] = field(default_factory=list)
    context_reset: bool = False
    stop_reason: str | None = None
    effort: str | None = None
    is_subagent: bool = False
    parent_tool_use_id: str | None = None  # for subagent threads: the Task call that spawned it
    _block_keys: set[tuple[Any, ...]] = field(default_factory=set, repr=False)


@dataclass
class ToolUse:
    tool_use_id: str
    name: str
    call_index: int
    prompt_id: str | None
    skill: str | None
    target: str  # short, redacted description of what it acted on
    input_chars: int
    input_hash: str
    action: str = ""  # classified from the full input, before ``target`` is truncated
    is_error: bool | None = None
    result_chars: int | None = None
    result_hash: str | None = None
    agent_id: str | None = None  # set when the result says which subagent ran


@dataclass
class Prompt:
    prompt_id: str
    origin: str  # human | task_notification | other
    ts: str | None
    chars: int
    preview: str


@dataclass
class ParsedSession:
    session_id: str
    path: str
    cwd: str | None = None
    git_branch: str | None = None
    calls: list[ApiCall] = field(default_factory=list)
    prompts: dict[str, Prompt] = field(default_factory=dict)
    tool_uses: dict[str, ToolUse] = field(default_factory=dict)
    reported_cost_usd: float | None = None  # Claude Code's own session total (cost-state)
    reported_after_calls: int = 0  # how many requests the log held when that figure was written
    reported_model_cost: dict[str, float] = field(default_factory=dict)


# ---------- helpers -----------------------------------------------------------


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
                elif b.get("type") == "tool_result":
                    parts.append(_text_of(b.get("content")))
                else:
                    parts.append(json.dumps(b, sort_keys=True))
            else:
                parts.append(str(b))
        return "".join(parts)
    return ""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def summarize_input(name: str, tool_input: dict[str, Any]) -> str:
    """Short, redacted description of what a tool call acted on."""
    for key in ("command", "file_path", "path", "url", "pattern", "query", "skill", "prompt"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return redact(" ".join(val.split()))[:160]
    return json.dumps(redact_structure(tool_input), sort_keys=True)[:160]


def _action_for(name: str, tool_input: dict[str, Any]) -> str:
    """Classify from the untruncated input so long commands are not grouped by a prefix."""
    full = tool_input.get("command") if name == "Bash" else None
    return classify(name, full if isinstance(full, str) else "")[1]


# ---------- parsing -----------------------------------------------------------


class _Thread:
    """Per-conversation state: subagents have their own context, separate from main."""

    def __init__(self) -> None:
        self.by_message: dict[str, ApiCall] = {}
        self.pending: list[ContextItem] = []
        self.reset = False


def _add_blocks(call: ApiCall, content: Any) -> list[dict[str, Any]]:
    """Add not-yet-seen content blocks to ``call``; return the new ``tool_use`` blocks."""
    new_tools: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return new_tools
    for b in content:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "tool_use":
            key: tuple[Any, ...] = ("tool_use", b.get("id"))
            chars = len(json.dumps(b.get("input", {}), sort_keys=True))
            block = Block("tool_use", chars, b.get("id"), b.get("name"))
        elif kind == "server_tool_use":
            key = ("server_tool_use", b.get("id"))
            block = Block(
                "server_tool_use", len(json.dumps(b.get("input", {}))), b.get("id"), b.get("name")
            )
        elif kind == "text":
            text = str(b.get("text", ""))
            key, block = ("text", _hash(text)), Block("text", len(text))
        elif kind == "thinking":
            text = str(b.get("thinking", ""))
            key, block = (
                ("thinking", _hash(text + str(b.get("signature", "")))),
                Block("thinking", len(text)),
            )
        else:
            continue
        if key in call._block_keys:
            continue
        call._block_keys.add(key)
        call.blocks.append(block)
        if kind == "tool_use":
            new_tools.append(b)
    return new_tools


def parse_session(
    path: Path | str, *, preview_chars: int = 80, include_subagents: bool = True
) -> ParsedSession:
    """Parse one session log (and, if present, its subagent logs) into cost records."""
    path = Path(path)
    sess = ParsedSession(session_id=path.stem, path=str(path))
    _parse_file(sess, path, MAIN, False, preview_chars, None)
    if include_subagents:
        agent_dir = path.parent / path.stem / "subagents"
        owner = {tu.agent_id: tu.tool_use_id for tu in sess.tool_uses.values() if tu.agent_id}
        for f in sorted(agent_dir.glob("agent-*.jsonl")) if agent_dir.is_dir() else []:
            agent_id = f.stem.removeprefix("agent-")
            _parse_file(sess, f, f"agent:{agent_id}", True, preview_chars, owner.get(agent_id))
        _assign_subagent_prompts(sess)
    return sess


def _parse_file(
    sess: ParsedSession,
    path: Path,
    default_thread: str,
    subagent_file: bool,
    preview_chars: int,
    parent_tool_use_id: str | None,
) -> None:
    threads: dict[str, _Thread] = {}
    current_prompt: str | None = None

    for ev in iter_events(path):
        etype = ev.get("type")
        if etype == "cost-state":
            if (cost := ev.get("totalCostUSD")) is not None and not subagent_file:
                sess.reported_cost_usd = float(cost)
                sess.reported_after_calls = len(sess.calls)
            usage = ev.get("modelUsage")
            if isinstance(usage, dict):
                sess.reported_model_cost = {
                    m: float(u.get("costUSD") or 0.0)
                    for m, u in usage.items()
                    if isinstance(u, dict)
                }
            continue
        if etype not in ("user", "assistant", "system"):
            continue
        sidechain = bool(ev.get("isSidechain")) or subagent_file
        tkey = (
            default_thread
            if (subagent_file or not sidechain)
            else f"agent:{ev.get('agentId') or 'sidechain'}"
        )
        th = threads.setdefault(tkey, _Thread())
        if sess.cwd is None and ev.get("cwd"):
            sess.cwd, sess.git_branch = ev.get("cwd"), ev.get("gitBranch")

        if etype == "system":
            if ev.get("subtype") == "compact_boundary":
                th.reset = True
            continue

        msg = as_dict(ev.get("message"))
        if etype == "user":
            if ev.get("promptId") and tkey == MAIN:
                current_prompt = ev["promptId"]
            _handle_user(sess, th, ev, msg, tkey == MAIN, current_prompt, preview_chars)
            continue

        # assistant
        raw_usage = msg.get("usage")
        if (
            not isinstance(raw_usage, dict)
            or not msg.get("id")
            or msg.get("model") == "<synthetic>"
        ):
            continue
        usage = Usage.from_api(raw_usage)
        call = th.by_message.get(msg["id"])
        if call is None:
            call = ApiCall(
                index=len(sess.calls),
                thread=tkey,
                message_id=msg["id"],
                request_id=ev.get("requestId"),
                ts=ev.get("timestamp"),
                model=str(msg.get("model") or "unknown"),
                usage=usage,
                prompt_id=current_prompt if tkey == MAIN else None,
                skill=ev.get("attributionSkill"),
                mcp_server=ev.get("attributionMcpServer"),
                mcp_tool=ev.get("attributionMcpTool"),
                new_items=th.pending,
                context_reset=th.reset,
                effort=ev.get("effort"),
                is_subagent=tkey != MAIN,
                parent_tool_use_id=parent_tool_use_id,
            )
            th.pending, th.reset = [], False
            th.by_message[msg["id"]] = call
            sess.calls.append(call)
        else:
            call.usage = call.usage.merge_max(usage)
            call.skill = call.skill or ev.get("attributionSkill")
            call.mcp_server = call.mcp_server or ev.get("attributionMcpServer")
            call.mcp_tool = call.mcp_tool or ev.get("attributionMcpTool")
        if msg.get("stop_reason"):
            call.stop_reason = msg["stop_reason"]
        for b in _add_blocks(call, msg.get("content")):
            tool_input = as_dict(b.get("input"))
            sess.tool_uses[b["id"]] = ToolUse(
                tool_use_id=b["id"],
                name=str(b.get("name") or "unknown"),
                call_index=call.index,
                prompt_id=call.prompt_id,
                skill=call.skill,
                target=summarize_input(str(b.get("name")), tool_input),
                action=_action_for(str(b.get("name")), tool_input),
                input_chars=len(json.dumps(tool_input, sort_keys=True)),
                input_hash=_hash(json.dumps([b.get("name"), tool_input], sort_keys=True)),
            )


def _handle_user(
    sess: ParsedSession,
    th: _Thread,
    ev: dict[str, Any],
    msg: dict[str, Any],
    is_main: bool,
    prompt_id: str | None,
    preview_chars: int,
) -> None:
    content = msg.get("content")
    blocks = content if isinstance(content, list) else []
    results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
    for b in results:
        text = _text_of(b.get("content"))
        tid = b.get("tool_use_id")
        th.pending.append(ContextItem("tool_result", len(text), tool_use_id=tid))
        if tid in sess.tool_uses:
            tu = sess.tool_uses[tid]
            tu.is_error = bool(b.get("is_error"))
            tu.result_chars, tu.result_hash = len(text), _hash(text)
            tur = ev.get("toolUseResult")
            if isinstance(tur, dict) and tur.get("agentId"):
                tu.agent_id = str(tur["agentId"])
    if results:
        rest = [b for b in blocks if not (isinstance(b, dict) and b.get("type") == "tool_result")]
        text = _text_of(rest)
        if text:
            th.pending.append(ContextItem("meta", len(text)))
        return

    text = _text_of(content)
    if not text:
        return
    source = ev.get("sourceToolUseID")
    if ev.get("isCompactSummary"):
        th.reset = True
        th.pending.append(ContextItem("compaction_summary", len(text)))
    elif ev.get("isMeta") and source:
        src = sess.tool_uses.get(source)
        skill = None
        if src is not None and src.name == "Skill":
            skill = src.target.split()[0] if src.target else None
        th.pending.append(
            ContextItem(
                "skill_content" if skill else "meta", len(text), tool_use_id=source, skill=skill
            )
        )
    elif ev.get("isMeta"):
        th.pending.append(ContextItem("meta", len(text)))
    else:
        th.pending.append(ContextItem("user_prompt", len(text)))
        pid = ev.get("promptId")
        if is_main and pid and pid not in sess.prompts:
            origin = ev.get("turnOrigin") or (ev.get("origin") or {}).get("kind") or "other"
            sess.prompts[pid] = Prompt(
                prompt_id=pid,
                origin=str(origin).replace("-", "_"),
                ts=ev.get("timestamp"),
                chars=len(text),
                preview=redact(" ".join(text.split()))[:preview_chars] if preview_chars else "",
            )


def _assign_subagent_prompts(sess: ParsedSession) -> None:
    """Subagent logs carry no prompt id: attribute them via the spawning call, else by time."""
    tool_prompt = {tid: tu.prompt_id for tid, tu in sess.tool_uses.items()}
    timeline = sorted(
        ((parse_ts(p.ts), p.prompt_id) for p in sess.prompts.values() if parse_ts(p.ts)),
        key=lambda x: x[0],  # type: ignore[arg-type,return-value]
    )
    for c in sess.calls:
        if not c.is_subagent or c.prompt_id:
            continue
        if c.parent_tool_use_id and tool_prompt.get(c.parent_tool_use_id):
            c.prompt_id = tool_prompt[c.parent_tool_use_id]
            continue
        when = parse_ts(c.ts)
        if when is None:
            continue
        for t, pid in timeline:
            if t is not None and t <= when:
                c.prompt_id = pid
    by_index = {c.index: c for c in sess.calls}
    for tu in sess.tool_uses.values():
        if tu.prompt_id is None and (owner := by_index.get(tu.call_index)) is not None:
            tu.prompt_id = owner.prompt_id
