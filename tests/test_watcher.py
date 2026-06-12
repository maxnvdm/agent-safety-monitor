"""Tests for monitor.watcher and the refactored monitor.run.run_eval."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from monitor.watcher import _flush_ready, _SessionHandler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_EVENT_DIR = False


def _fake_event(path: str, is_directory: bool = False) -> MagicMock:
    ev = MagicMock()
    ev.src_path = path
    ev.is_directory = is_directory
    return ev


# ---------------------------------------------------------------------------
# _SessionHandler
# ---------------------------------------------------------------------------


def test_handler_queues_jsonl_on_created() -> None:
    pending: dict[Path, float] = {}
    handler = _SessionHandler(pending)
    handler.on_created(_fake_event("/tmp/session.jsonl"))
    assert Path("/tmp/session.jsonl") in pending


def test_handler_queues_jsonl_on_modified() -> None:
    pending: dict[Path, float] = {}
    handler = _SessionHandler(pending)
    handler.on_modified(_fake_event("/tmp/session.jsonl"))
    assert Path("/tmp/session.jsonl") in pending


def test_handler_ignores_non_jsonl() -> None:
    pending: dict[Path, float] = {}
    handler = _SessionHandler(pending)
    handler.on_created(_fake_event("/tmp/somefile.py"))
    handler.on_modified(_fake_event("/tmp/somefile.txt"))
    assert not pending


def test_handler_ignores_directory_events() -> None:
    pending: dict[Path, float] = {}
    handler = _SessionHandler(pending)
    handler.on_created(_fake_event("/tmp/mydir.jsonl", is_directory=True))
    assert not pending


def test_handler_updates_timestamp_on_repeated_modify() -> None:
    pending: dict[Path, float] = {}
    handler = _SessionHandler(pending)
    key = Path("/tmp/session.jsonl")
    handler.on_modified(_fake_event(str(key)))
    t1 = pending[key]
    time.sleep(0.01)
    handler.on_modified(_fake_event(str(key)))
    t2 = pending[key]
    assert t2 > t1


# ---------------------------------------------------------------------------
# _flush_ready
# ---------------------------------------------------------------------------


def _flush_kwargs(db: str = "test.db", model: str = "mockllm/model") -> dict:
    return dict(
        cooldown=10.0,
        db=db,
        model=model,
        allowed_hosts=[],
        inspect_log_dir="inspect_logs/test/",
    )


def test_flush_skips_hot_files() -> None:
    pending: dict[Path, float] = {Path("/tmp/hot.jsonl"): time.monotonic()}
    with patch("monitor.watcher.run_eval") as mock_eval:
        _flush_ready(pending, **_flush_kwargs())
    mock_eval.assert_not_called()
    assert Path("/tmp/hot.jsonl") in pending


def test_flush_processes_cold_files(tmp_path: Path) -> None:
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text('{"type":"user"}\n')
    pending: dict[Path, float] = {jsonl: time.monotonic() - 20.0}

    with patch("monitor.watcher.run_eval", return_value=3) as mock_eval:
        _flush_ready(pending, **_flush_kwargs())

    mock_eval.assert_called_once()
    assert jsonl not in pending


def test_flush_removes_pending_even_on_eval_error(tmp_path: Path) -> None:
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text('{"type":"user"}\n')
    pending: dict[Path, float] = {jsonl: time.monotonic() - 20.0}

    with patch("monitor.watcher.run_eval", side_effect=RuntimeError("boom")):
        _flush_ready(pending, **_flush_kwargs())

    assert jsonl not in pending


def test_flush_skips_deleted_files() -> None:
    pending: dict[Path, float] = {Path("/nonexistent/gone.jsonl"): time.monotonic() - 20.0}
    with patch("monitor.watcher.run_eval") as mock_eval:
        _flush_ready(pending, **_flush_kwargs())
    mock_eval.assert_not_called()


def test_flush_passes_correct_args_to_run_eval(tmp_path: Path) -> None:
    jsonl = tmp_path / "s.jsonl"
    jsonl.write_text("{}\n")
    pending: dict[Path, float] = {jsonl: time.monotonic() - 20.0}

    captured: dict = {}

    def _capture(**kwargs: object) -> int:
        # tempdir is still alive here, inside the `with` block in _flush_ready
        captured.update(kwargs)
        assert Path(str(kwargs["log_dir"])).is_dir()
        return 1

    with patch("monitor.watcher.run_eval", side_effect=_capture):
        _flush_ready(
            pending,
            cooldown=10.0,
            db="mydb.db",
            model="groq/llama-3.3-70b-versatile",
            allowed_hosts=["api.github.com"],
            inspect_log_dir="ilogs/",
        )

    assert captured["db"] == "mydb.db"
    assert captured["model"] == "groq/llama-3.3-70b-versatile"
    assert captured["allowed_hosts"] == ["api.github.com"]
    assert captured["inspect_log_dir"] == "ilogs/"
    assert captured["verbose"] is False


# ---------------------------------------------------------------------------
# run_eval extraction in monitor.run
# ---------------------------------------------------------------------------


def test_run_eval_is_exported() -> None:
    from monitor.run import run_eval as _re

    assert callable(_re)


def test_run_eval_is_callable() -> None:
    from monitor.run import run_eval

    assert callable(run_eval)
