"""Fail if anything that looks like private session data is tracked by git.

This repository is public, but its whole purpose is to analyse private Claude Code transcripts.
Run in CI and as a pre-commit hook, it blocks the ways those could be committed by accident:

1. database / allowlist / env files
2. ``*.jsonl`` session logs anywhere except the synthetic-fixture directories
3. fixture logs whose working directory is not obviously synthetic (i.e. a real session pasted in)
4. real-looking home directories (``/Users/alice/``, ``/home/alice/``) in any tracked text file

    uv run python scripts/check_no_private_data.py
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404 - fixed `git ls-files` invocation below
import sys
from pathlib import Path

FORBIDDEN_NAMES = ("allowlist.json", ".env", ".env.local")
FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3")
FORBIDDEN_SUBSTRINGS = (".db-",)
FIXTURE_DIRS = ("tests/fixtures/", "samples/", "benchmark/sessions/")
SYNTHETIC_CWD_PREFIXES = (
    "/home/dev/",
    "/home/example/",
    "/tmp/",  # nosec B108 - a path prefix to match, not a temp file
    "/work/",
    "/workspace/",
    "/example/",
    "/repo",
)
SAFE_HOME_NAMES = {
    "user", "dev", "me", "you", "runner", "example", "name", "username", "yourname", "root",
    "other", "otheruser", "someone", "alice", "bob", "test", "tester", "foo", "bar",
}  # fmt: skip
_HOME_RE = re.compile(r"(?:/Users|/home)/([A-Za-z0-9._-]+)/")
_MAX_SCAN_BYTES = 2_000_000


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(  # nosec B603 B607 - fixed arguments, no user input
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return [p for p in out.split("\0") if p]


def check_files(root: Path, rel_paths: list[str]) -> list[str]:
    problems: list[str] = []
    for rel in rel_paths:
        name = rel.rsplit("/", 1)[-1]
        if (
            name in FORBIDDEN_NAMES
            or name.endswith(FORBIDDEN_SUFFIXES)
            or any(s in name for s in FORBIDDEN_SUBSTRINGS)
        ):
            problems.append(f"{rel}: data/secret file must never be tracked")
            continue
        path = root / rel
        if not path.is_file() or path.stat().st_size > _MAX_SCAN_BYTES:
            continue
        if rel.endswith(".jsonl"):
            if not rel.startswith(FIXTURE_DIRS):
                problems.append(
                    f"{rel}: session logs may only be committed under {', '.join(FIXTURE_DIRS)}"
                )
                continue
            problems += _check_fixture(rel, path)
        problems += _check_home_dirs(rel, path)
    return problems


def _check_fixture(rel: str, path: Path) -> list[str]:
    bad: set[str] = set()
    for line in path.read_text(errors="replace").splitlines():
        try:
            cwd = json.loads(line).get("cwd")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(cwd, str) and not cwd.startswith(SYNTHETIC_CWD_PREFIXES):
            bad.add(cwd)
    return [
        f"{rel}: working directory {cwd!r} looks like a real session, not a synthetic fixture"
        for cwd in sorted(bad)
    ]


def _check_home_dirs(rel: str, path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # binary file
    names = {m.group(1) for m in _HOME_RE.finditer(text)} - SAFE_HOME_NAMES
    return [f"{rel}: contains a real-looking home directory for {n!r}" for n in sorted(names)]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    problems = check_files(root, tracked_files(root))
    for p in problems:
        print(f"PRIVATE-DATA GUARD: {p}", file=sys.stderr)
    if problems:
        print(
            f"\n{len(problems)} problem(s). Keep real transcripts outside the repo (see README, 'Privacy model').",
            file=sys.stderr,
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
