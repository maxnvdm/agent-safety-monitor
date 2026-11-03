import importlib.util
import json
from pathlib import Path

import pytest

from monitor import paths

_spec = importlib.util.spec_from_file_location(
    "check_no_private_data",
    Path(__file__).resolve().parent.parent / "scripts" / "check_no_private_data.py",
)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


# ---- default locations -------------------------------------------------------


def test_monitor_home_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MONITOR_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_dir() == tmp_path / "h"
    assert paths.default_db() == str(tmp_path / "h" / "monitor.db")
    assert paths.default_allowlist() == str(tmp_path / "h" / "allowlist.json")


def test_falls_back_to_xdg_then_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MONITOR_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_dir() == tmp_path / "xdg" / "agent-safety-monitor"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert paths.data_dir() == tmp_path / "home" / ".local" / "share" / "agent-safety-monitor"


def test_defaults_are_never_inside_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MONITOR_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    for p in (paths.default_db(), paths.default_allowlist(), paths.default_inspect_logs("watch")):
        assert Path(p).is_absolute()
        assert not Path(p).resolve().is_relative_to(Path.cwd().resolve())


def test_ensure_parent_creates_directories(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "monitor.db"
    paths.ensure_parent(target)
    assert target.parent.is_dir() and not target.exists()


# ---- private-data guard --------------------------------------------------------


def _write(root: Path, rel: str, text: str = "x") -> str:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return rel


# Built at runtime so this file never contains a literal real-looking home directory itself.
NAME = "ja" + "ne"


def _log(cwd: str) -> str:
    return json.dumps({"type": "user", "cwd": cwd}) + "\n"


@pytest.mark.parametrize(
    "rel", ["monitor.db", "data/x.sqlite", "allowlist.json", ".env.local", "m.db-wal"]
)
def test_guard_blocks_data_and_secret_files(tmp_path: Path, rel: str) -> None:
    assert guard.check_files(tmp_path, [_write(tmp_path, rel)])


def test_guard_blocks_session_logs_outside_fixture_dirs(tmp_path: Path) -> None:
    rel = _write(tmp_path, "notes/session.jsonl", _log("/tmp/x"))
    assert any("only be committed under" in p for p in guard.check_files(tmp_path, [rel]))


def test_guard_allows_synthetic_fixtures(tmp_path: Path) -> None:
    rel = _write(tmp_path, "tests/fixtures/s.jsonl", _log("/home/dev/project"))
    assert guard.check_files(tmp_path, [rel]) == []


def test_guard_blocks_real_looking_session_pasted_into_fixtures(tmp_path: Path) -> None:
    rel = _write(tmp_path, "samples/mine.jsonl", _log(f"/Users/{NAME}/code/secret-project"))
    problems = guard.check_files(tmp_path, [rel])
    assert any("looks like a real session" in p for p in problems)
    assert any(f"'{NAME}'" in p for p in problems)


def test_guard_flags_real_home_dirs_in_text_but_not_placeholders(tmp_path: Path) -> None:
    bad = _write(tmp_path, "docs.md", f"see /home/{NAME}/project")
    ok = _write(tmp_path, "ok.md", "see /home/user/project and /Users/me/x and /home/runner/work")
    assert guard.check_files(tmp_path, [bad])
    assert guard.check_files(tmp_path, [ok]) == []


def test_repository_itself_is_clean() -> None:
    root = Path(__file__).resolve().parent.parent
    assert guard.check_files(root, guard.tracked_files(root)) == []
