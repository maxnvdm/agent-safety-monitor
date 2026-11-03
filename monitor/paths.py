"""Where this tool keeps its data: never inside the repository checkout.

Session databases, allowlists and Inspect logs describe your private work. Writing them to the
current directory (the old default) put them one ``git add -A`` away from a public repo, so the
defaults live under a per-user data directory instead:

1. ``$MONITOR_HOME`` if set
2. ``$XDG_DATA_HOME/agent-safety-monitor``
3. ``~/.local/share/agent-safety-monitor`` (``%LOCALAPPDATA%\\agent-safety-monitor`` on Windows)

Every path can still be overridden explicitly with a CLI flag or its own environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = "agent-safety-monitor"


def data_dir() -> Path:
    """The per-user data directory (not created here; writers create it on demand)."""
    if home := os.environ.get("MONITOR_HOME"):
        return Path(home).expanduser()
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser() / APP_DIR


def default_db() -> str:
    return str(data_dir() / "monitor.db")


def default_allowlist() -> str:
    return str(data_dir() / "allowlist.json")


def default_inspect_logs(*sub: str) -> str:
    return str(data_dir().joinpath("inspect_logs", *sub)) + os.sep


def ensure_parent(path: str | Path) -> None:
    """Create the directory a file will live in (SQLite will not)."""
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
