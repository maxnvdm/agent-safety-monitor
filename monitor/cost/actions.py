"""Group tool calls into families and concrete actions.

A tool *name* is too coarse to act on: ``Bash`` is ``git status`` and ``uv run pytest`` and
``curl``. We classify each call as ``(family, action)``:

* family: file / search / shell / web / agent / skill / planning / mcp:<server> / other
* action: the MCP tool, or for Bash the command that actually ran (``git status``,
  ``uv run pytest``), otherwise the tool name.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_FAMILIES = {
    "Read": "file",
    "Edit": "file",
    "MultiEdit": "file",
    "Write": "file",
    "NotebookEdit": "file",
    "Grep": "search",
    "Glob": "search",
    "Bash": "shell",
    "BashOutput": "shell",
    "KillShell": "shell",
    "WebFetch": "web",
    "WebSearch": "web",
    "Task": "agent",
    "Agent": "agent",
    "Skill": "skill",
    "TodoWrite": "planning",
    "TaskCreate": "planning",
    "TaskUpdate": "planning",
    "TaskList": "planning",
    "TaskGet": "planning",
    "ExitPlanMode": "planning",
    "EnterPlanMode": "planning",
}
_SUBCOMMAND_TOOLS = {
    "git", "gh", "docker", "kubectl", "npm", "pnpm", "yarn", "pip", "pip3", "cargo", "go",
    "apt", "apt-get", "brew", "systemctl", "terraform", "helm", "poetry", "bun",
}  # fmt: skip
_WRAPPERS = {"sudo", "time", "nohup", "env", "command", "exec", "timeout"}
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\n|\|)\s*")


def family_of(tool_name: str) -> str:
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        return f"mcp:{parts[1]}" if len(parts) > 1 else "mcp"
    return _FAMILIES.get(tool_name, "other")


def bash_action(command: str) -> str:
    """Name the command a Bash call really runs (skipping ``cd x &&``, env vars, sudo...)."""
    for segment in _SPLIT_RE.split(command.strip()):
        words = segment.split()
        while words and (_ASSIGN_RE.match(words[0]) or words[0] in _WRAPPERS):
            words = words[1:]
        if not words or words[0] in {"cd", "export", "set", "echo", "true", "source", "."}:
            continue
        head = PurePosixPath(words[0]).name
        rest = [w for w in words[1:]]
        sub = rest[0] if rest and not rest[0].startswith("-") else None
        if head == "uv" and sub == "run":
            tail = [w for w in rest[1:] if not w.startswith("-")]
            return f"uv run {PurePosixPath(tail[0]).name}" if tail else "uv run"
        if head in {"python", "python3"}:
            if rest[:1] == ["-m"] and len(rest) > 1:
                return f"python -m {rest[1]}"
            return "python"
        if head in _SUBCOMMAND_TOOLS and sub:
            return f"{head} {sub}"
        return head
    return "(shell builtin)"


def classify(tool_name: str, target: str) -> tuple[str, str]:
    """Return ``(family, action)`` for a tool call. ``target`` is the redacted input summary."""
    family = family_of(tool_name)
    if tool_name == "Bash":
        return family, bash_action(target)
    if tool_name.startswith("mcp__"):
        return family, tool_name.split("__", 2)[-1]
    return family, tool_name
