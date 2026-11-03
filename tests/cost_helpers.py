"""Build synthetic Claude Code session logs with exact token counts for cost tests.

The event shapes mirror real logs: one assistant event per content block (each repeating
the request's usage), ``promptId`` on user events only, tool results in later user events,
skill text as an ``isMeta`` user event with ``sourceToolUseID``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

Block = tuple[Any, ...]  # ("tool", id, name, input) | ("text", str) | ("thinking", str)


class LogBuilder:
    def __init__(
        self, tmp_path: Path, session: str = "sess-1", model: str = "claude-sonnet-5-5"
    ) -> None:
        self.path = tmp_path / f"{session}.jsonl"
        self.model = model
        self.events: list[dict[str, Any]] = []
        self.prompt_id = "p1"
        self._uuid = 0
        self._prev_prompt_tokens = 0
        self._t = 0.0

    # ---- plumbing
    def _next(self) -> str:
        self._uuid += 1
        return f"u{self._uuid}"

    def _ts(self, advance: float = 5.0) -> str:
        self._t += advance
        h, rem = divmod(int(self._t), 3600)
        m, s = divmod(rem, 60)
        return f"2026-11-03T{10 + h:02d}:{m:02d}:{s:02d}.000Z"

    def _base(self, etype: str, **extra: Any) -> dict[str, Any]:
        return {
            "type": etype,
            "uuid": self._next(),
            "sessionId": "sess-1",
            "cwd": "/work/repo",
            "gitBranch": "main",
            "timestamp": self._ts(extra.pop("advance", 5.0)),
            **extra,
        }

    # ---- user side
    def prompt(self, text: str, pid: str = "p1", origin: str = "human") -> LogBuilder:
        self.prompt_id = pid
        self.events.append(
            self._base(
                "user",
                promptId=pid,
                turnOrigin=origin,
                message={"role": "user", "content": text},
            )
        )
        return self

    def result(
        self,
        tool_use_id: str,
        chars: int,
        *,
        error: bool = False,
        text: str | None = None,
        tool_use_result: dict[str, Any] | None = None,
    ) -> LogBuilder:
        body = text if text is not None else "x" * chars
        self.events.append(
            self._base(
                "user",
                promptId=self.prompt_id,
                message={
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": body,
                            "is_error": error,
                        }
                    ],
                },
                toolUseResult=tool_use_result or {"stdout": body[:10]},
            )
        )
        return self

    def skill_content(self, tool_use_id: str, chars: int) -> LogBuilder:
        self.events.append(
            self._base(
                "user",
                promptId=self.prompt_id,
                isMeta=True,
                sourceToolUseID=tool_use_id,
                message={"role": "user", "content": [{"type": "text", "text": "s" * chars}]},
            )
        )
        return self

    def compact(self) -> LogBuilder:
        self.events.append(self._base("system", subtype="compact_boundary"))
        return self

    # ---- assistant side
    def call(
        self,
        mid: str,
        blocks: list[Block],
        *,
        out: int,
        new: int,
        cold: bool = False,
        skill: str | None = None,
        gap: float = 5.0,
        input_tokens: int = 0,
        model: str | None = None,
        write_ttl: str = "1h",
        searches: int = 0,
    ) -> LogBuilder:
        """One API request. ``new`` = tokens written to cache; cache read is the whole previous prompt
        (a hit) unless ``cold`` (a miss: everything is rewritten)."""
        cr = 0 if cold else self._prev_prompt_tokens
        cw = new + (self._prev_prompt_tokens if cold else 0)
        self._prev_prompt_tokens = cr + cw + input_tokens
        for i, blk in enumerate(blocks):
            usage: dict[str, Any] = {
                "input_tokens": input_tokens,
                "output_tokens": max(
                    out * (i + 1) // len(blocks), 1
                ),  # grows as streaming progresses
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cw if write_ttl == "5m" else 0,
                    "ephemeral_1h_input_tokens": cw if write_ttl == "1h" else 0,
                },
                "server_tool_use": {"web_search_requests": searches},
                "speed": "standard",
            }
            if i == len(blocks) - 1:
                usage["output_tokens"] = out
            if blk[0] == "tool":
                content: dict[str, Any] = {
                    "type": "tool_use",
                    "id": blk[1],
                    "name": blk[2],
                    "input": blk[3],
                }
            elif blk[0] == "thinking":
                content = {"type": "thinking", "thinking": blk[1], "signature": f"sig{i}"}
            else:
                content = {"type": "text", "text": blk[1]}
            ev = self._base(
                "assistant",
                advance=gap if i == 0 else 0.1,
                requestId=f"req_{mid}",
                message={
                    "id": mid,
                    "role": "assistant",
                    "model": model or self.model,
                    "content": [content],
                    "usage": usage,
                    "stop_reason": "tool_use"
                    if any(b[0] == "tool" for b in blocks)
                    else "end_turn",
                },
            )
            if skill:
                ev["attributionSkill"] = skill
            self.events.append(ev)
        return self

    def cost_state(self, total: float, models: dict[str, float] | None = None) -> LogBuilder:
        self.events.append(
            {
                "type": "cost-state",
                "totalCostUSD": total,
                "modelUsage": {m: {"costUSD": c} for m, c in (models or {}).items()},
            }
        )
        return self

    def write(self) -> Path:
        self.path.write_text("\n".join(json.dumps(e) for e in self.events) + "\n")
        return self.path
