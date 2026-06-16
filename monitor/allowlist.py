"""Persistent allowlist for known-safe patterns, one entry-list per scorer.

JSON structure (allowlist.json):
{
  "secret_leakage":       ["Anthropic API key (sk-ant-)", ...],
  "scope_creep":          ["/home/max/.config", ...],
  "exfiltration_attempt": ["github.com", "pypi.org", ...],
  "privilege_escalation": ["chmod", ...]
}

Each string is the exact value from match_metadata for that scorer:
  secret_leakage       → metadata["pattern"]
  scope_creep          → each path in metadata["violation_paths"]
  exfiltration_attempt → metadata["host"]
  privilege_escalation → metadata["trigger"]
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_ALLOWLIST = "allowlist.json"

AllowlistData = dict[str, list[str]]

# Which metadata key holds the allowlist-able value(s) for each scorer.
# "scope_creep" uses a list key; all others are scalar strings.
SCORER_META_KEY: dict[str, str] = {
    "secret_leakage": "pattern",
    "scope_creep": "violation_paths",  # list[str]
    "exfiltration_attempt": "host",
    "privilege_escalation": "trigger",
}


def load(path: str | Path = DEFAULT_ALLOWLIST) -> AllowlistData:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        return json.load(f)


def save(data: AllowlistData, path: str | Path = DEFAULT_ALLOWLIST) -> None:
    p = Path(path)
    with p.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def add_entries(
    scorer_name: str, entries: list[str], path: str | Path = DEFAULT_ALLOWLIST
) -> list[str]:
    """Merge entries into the scorer's allowlist. Returns the newly-added entries."""
    data = load(path)
    existing = set(data.get(scorer_name, []))
    new = [e for e in entries if e not in existing]
    if new:
        data[scorer_name] = sorted(existing | set(new))
        save(data, path)
    return new


def entries_from_match_metadata(
    scorer_name: str, match_metadata: dict
) -> list[str]:
    """Extract the allowlistable values from a result's match_metadata."""
    key = SCORER_META_KEY.get(scorer_name)
    if not key:
        return []
    val = match_metadata.get(key)
    if val is None:
        return []
    return list(val) if isinstance(val, list) else [str(val)]


def for_scorer(scorer_name: str, path: str | Path = DEFAULT_ALLOWLIST) -> list[str]:
    return load(path).get(scorer_name, [])
