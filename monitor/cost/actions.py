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
# Wrapper commands and the options that consume the next word as an operand.
_WRAPPER_OPTS_WITH_ARG: dict[str, set[str]] = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "--user", "--group", "--host"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "time": {"-f", "-o", "--format", "--output"},
    "exec": {"-a"},
    "nohup": set(),
    "command": set(),
}
_WRAPPER_POSITIONAL = {"timeout": 1}  # `timeout 30 cmd`: one DURATION operand before the command
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SKIP_HEADS = {"cd", "export", "set", "echo", "true", "source", ".", "unset", "pushd", "popd"}


def family_of(tool_name: str) -> str:
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        return f"mcp:{parts[1]}" if len(parts) > 1 else "mcp"
    return _FAMILIES.get(tool_name, "other")


def split_commands(command: str) -> list[list[str]]:
    """Split a shell command line into simple commands (lists of words), quote-aware.

    ``&&``, ``||``, ``;``, ``|``, ``&`` and newlines separate commands *outside* quotes only, so
    ``grep 'a|b' f`` is one command. Quotes are removed from the resulting words. This is a
    classifier, not a shell: heredoc bodies and ``$(...)`` are not interpreted.
    """
    segments: list[list[str]] = []
    words: list[str] = []
    buf: list[str] = []
    has_word = False
    quote: str | None = None
    i, n = 0, len(command)

    def end_word() -> None:
        nonlocal has_word
        if has_word:
            words.append("".join(buf))
        buf.clear()
        has_word = False

    def end_segment() -> None:
        end_word()
        if words:
            segments.append(words[:])
        words.clear()

    while i < n:
        ch = command[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < n:
                i += 1
                buf.append(command[i])
            else:
                buf.append(ch)
        elif ch in "'\"":
            quote, has_word = ch, True
        elif ch == "\\" and i + 1 < n:
            i += 1
            buf.append(command[i])
            has_word = True
        elif ch in " \t\r":
            end_word()
        elif ch in "\n;|&":
            end_segment()
            if ch in "|&" and command[i + 1 : i + 2] == ch:
                i += 1
        else:
            buf.append(ch)
            has_word = True
        i += 1
    end_segment()
    return segments


def _strip_wrappers(words: list[str]) -> list[str]:
    """Drop leading ``VAR=x`` assignments and wrapper commands *with* their options/operands."""
    while words:
        head = PurePosixPath(words[0]).name
        if _ASSIGN_RE.match(words[0]):
            words = words[1:]
        elif head in _WRAPPER_OPTS_WITH_ARG:
            with_arg = _WRAPPER_OPTS_WITH_ARG[head]
            rest = words[1:]
            while rest and rest[0].startswith("-") and rest[0] != "-":
                opt = rest[0]
                rest = rest[2:] if opt in with_arg else rest[1:]
                if opt == "--":
                    break
            if head == "env":  # env also takes assignments before the command
                while rest and _ASSIGN_RE.match(rest[0]):
                    rest = rest[1:]
            rest = rest[_WRAPPER_POSITIONAL.get(head, 0) :]
            words = rest
        else:
            break
    return words


def bash_action(command: str) -> str:
    """Name the command a Bash call really runs (skipping ``cd x &&``, env vars, sudo...)."""
    for segment in split_commands(command):
        words = _strip_wrappers(segment)
        if not words or PurePosixPath(words[0]).name in _SKIP_HEADS:
            continue
        head = PurePosixPath(words[0]).name
        rest = words[1:]
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
