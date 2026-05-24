"""Parse Claude Code JSONL session logs.

Claude Code writes one JSON event per line to
`~/.claude/projects/<encoded-path>/<session-uuid>.jsonl`. Events have a top-level
`type` field; only `user` and `assistant` carry messages. Tool calls live inside
assistant `message.content` as `{type: "tool_use", id, name, input}` blocks;
tool results come back inside the *next* user event as
`{type: "tool_result", tool_use_id, content}` blocks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageTool,
    ChatMessageUser,
)
from inspect_ai.tool import ToolCall


# ---------- data classes ----------------------------------------------------


@dataclass(frozen=True)
class ToolCallRecord:
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    assistant_uuid: str


@dataclass(frozen=True)
class ToolResultRecord:
    tool_use_id: str
    content: str
    user_uuid: str


# ---------- iterators -------------------------------------------------------


def iter_events(path: Path | str) -> Iterator[dict[str, Any]]:
    """Yield every JSON event from a Claude Code session log, one per line."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def iter_tool_calls(events: Iterable[dict[str, Any]]) -> Iterator[ToolCallRecord]:
    """Yield structured tool-use records from every assistant event."""
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        content = ev.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        assistant_uuid = ev.get("uuid", "")
        for block in content:
            if block.get("type") != "tool_use":
                continue
            yield ToolCallRecord(
                tool_name=block.get("name", ""),
                tool_input=block.get("input", {}) or {},
                tool_use_id=block.get("id", ""),
                assistant_uuid=assistant_uuid,
            )


def iter_tool_results(events: Iterable[dict[str, Any]]) -> Iterator[ToolResultRecord]:
    """Yield tool_result blocks that come back inside user events."""
    for ev in events:
        if ev.get("type") != "user":
            continue
        content = ev.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        user_uuid = ev.get("uuid", "")
        for block in content:
            if block.get("type") != "tool_result":
                continue
            raw = block.get("content", "")
            text = raw if isinstance(raw, str) else json.dumps(raw)
            yield ToolResultRecord(
                tool_use_id=block.get("tool_use_id", ""),
                content=text,
                user_uuid=user_uuid,
            )


# ---------- session metadata -----------------------------------------------


def extract_session_meta(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pull canonical session metadata from the first user event that carries it.

    User events carry `sessionId`, `cwd`, `gitBranch`, `permissionMode`, and
    `timestamp` at the root. We treat the first user event as authoritative.
    """
    meta: dict[str, Any] = {
        "session_id": None,
        "cwd": None,
        "git_branch": None,
        "permission_mode": None,
        "started_at": None,
    }
    for ev in events:
        if ev.get("type") == "user" and meta["session_id"] is None:
            meta["session_id"] = ev.get("sessionId")
            meta["cwd"] = ev.get("cwd")
            meta["git_branch"] = ev.get("gitBranch")
            meta["permission_mode"] = ev.get("permissionMode")
            meta["started_at"] = ev.get("timestamp")
            break
    return meta


# ---------- transcript rendering -------------------------------------------


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n... [truncated, {len(text) - cap} chars omitted]"


def render_transcript(
    events: Iterable[dict[str, Any]],
    max_chars_per_payload: int = 4_000,
) -> str:
    """Render a session as a single human-readable string.

    Used both by LLM-graded scorers (input to the judge model) and by the
    frontend's transcript viewer. Tool inputs and outputs are truncated to
    `max_chars_per_payload` so the rendered transcript fits in a context window.
    """
    lines: list[str] = []
    for ev in events:
        kind = ev.get("type")
        msg = ev.get("message") or {}
        content = msg.get("content")

        if kind == "user":
            if isinstance(content, str):
                lines.append(f"[USER]: {content}")
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        tid = block.get("tool_use_id", "?")
                        raw = block.get("content", "")
                        text = raw if isinstance(raw, str) else json.dumps(raw)
                        lines.append(
                            f"[TOOL_RESULT {tid}]: {_truncate(text, max_chars_per_payload)}"
                        )
                    elif block.get("type") == "text":
                        lines.append(f"[USER]: {block.get('text', '')}")

        elif kind == "assistant":
            if not isinstance(content, list):
                continue
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    lines.append(f"[ASSISTANT]: {block.get('text', '')}")
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp = json.dumps(block.get("input", {}))
                    lines.append(
                        f"[TOOL_CALL] {name}({_truncate(inp, max_chars_per_payload)})"
                    )

    return "\n".join(lines)


# ---------- Inspect ChatMessage conversion ---------------------------------


def to_inspect_messages(events: Iterable[dict[str, Any]]) -> list[ChatMessage]:
    """Convert events into Inspect ChatMessage objects.

    Only user and assistant events become messages. Tool-result blocks inside
    user events are emitted as ChatMessageTool entries (Inspect's structured
    representation), not folded into ChatMessageUser.
    """
    out: list[ChatMessage] = []
    for ev in events:
        kind = ev.get("type")
        msg = ev.get("message") or {}
        content = msg.get("content")

        if kind == "user":
            if isinstance(content, str):
                out.append(ChatMessageUser(content=content))
            elif isinstance(content, list):
                user_text_parts: list[str] = []
                for block in content:
                    btype = block.get("type")
                    if btype == "tool_result":
                        raw = block.get("content", "")
                        text = raw if isinstance(raw, str) else json.dumps(raw)
                        out.append(
                            ChatMessageTool(
                                content=text,
                                tool_call_id=block.get("tool_use_id"),
                            )
                        )
                    elif btype == "text":
                        user_text_parts.append(block.get("text", ""))
                if user_text_parts:
                    out.append(ChatMessageUser(content="\n".join(user_text_parts)))

        elif kind == "assistant":
            if not isinstance(content, list):
                continue
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append(
                        ToolCall(
                            id=block.get("id", ""),
                            function=block.get("name", ""),
                            arguments=block.get("input", {}) or {},
                        )
                    )
            out.append(
                ChatMessageAssistant(
                    content="\n".join(text_parts),
                    tool_calls=tool_calls or None,
                )
            )

    return out
