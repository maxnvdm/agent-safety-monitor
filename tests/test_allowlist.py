"""Tests for monitor/allowlist.py."""

import json
from pathlib import Path

import pytest

from monitor.allowlist import (
    add_entries,
    entries_from_match_metadata,
    for_scorer,
    load,
    save,
)


@pytest.fixture
def al_path(tmp_path) -> Path:
    return tmp_path / "allowlist.json"


def test_load_returns_empty_dict_when_file_missing(al_path):
    assert load(al_path) == {}


def test_save_and_load_roundtrip(al_path):
    data = {"exfiltration_attempt": ["github.com"], "privilege_escalation": ["chmod"]}
    save(data, al_path)
    assert load(al_path) == data


def test_add_entries_creates_file_and_returns_new(al_path):
    added = add_entries("exfiltration_attempt", ["pypi.org", "github.com"], al_path)
    assert set(added) == {"pypi.org", "github.com"}
    assert set(load(al_path)["exfiltration_attempt"]) == {"pypi.org", "github.com"}


def test_add_entries_deduplicates(al_path):
    add_entries("exfiltration_attempt", ["github.com"], al_path)
    added = add_entries("exfiltration_attempt", ["github.com", "pypi.org"], al_path)
    assert added == ["pypi.org"]
    assert len(load(al_path)["exfiltration_attempt"]) == 2


def test_for_scorer_returns_empty_list_for_missing_key(al_path):
    assert for_scorer("secret_leakage", al_path) == []


def test_for_scorer_returns_entries(al_path):
    save({"secret_leakage": ["AWS access key id"]}, al_path)
    assert for_scorer("secret_leakage", al_path) == ["AWS access key id"]


def test_entries_from_match_metadata_secret_leakage():
    meta = {"tool_use_id": "t1", "pattern": "Anthropic API key (sk-ant-)"}
    assert entries_from_match_metadata("secret_leakage", meta) == ["Anthropic API key (sk-ant-)"]


def test_entries_from_match_metadata_scope_creep():
    meta = {
        "violations": ["Read(/home/other/x)", "Bash(/etc/hosts)"],
        "violation_paths": ["/home/other/x", "/etc/hosts"],
        "cwd": "/home/user/project",
    }
    assert entries_from_match_metadata("scope_creep", meta) == ["/home/other/x", "/etc/hosts"]


def test_entries_from_match_metadata_exfiltration():
    meta = {"tool_use_id": "t1", "host": "evil.example.com"}
    assert entries_from_match_metadata("exfiltration_attempt", meta) == ["evil.example.com"]


def test_entries_from_match_metadata_privilege_escalation():
    meta = {"tool_use_id": "t1", "trigger": "chmod"}
    assert entries_from_match_metadata("privilege_escalation", meta) == ["chmod"]


def test_entries_from_match_metadata_llm_scorer_returns_empty():
    assert entries_from_match_metadata("deceptive_reasoning", {"some": "data"}) == []
    assert entries_from_match_metadata("supply_chain_risk", {}) == []


def test_entries_from_match_metadata_missing_key():
    assert entries_from_match_metadata("exfiltration_attempt", {"url": "https://x.com"}) == []
