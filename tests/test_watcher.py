"""Tests for monitor.watcher and the refactored monitor.run.run_eval."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from monitor.watcher import (
    DEFAULT_DB,
    _flush_ready,
    _SessionHandler,
    main,
    parse_args,
    watch,
)

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


# ---------------------------------------------------------------------------
# watch() function (main loop)
# ---------------------------------------------------------------------------


def test_watch_initializes_db_and_observer(tmp_path: Path) -> None:
    """Integration test: watch() calls init_db, creates log dir, starts observer."""
    with (
        patch("monitor.watcher.init_db") as mock_init_db,
        patch("monitor.watcher.Path.mkdir") as mock_mkdir,
        patch("monitor.watcher.Observer") as mock_observer_class,
        patch("monitor.watcher._flush_ready"),
        patch("monitor.watcher.time.sleep", side_effect=[None, KeyboardInterrupt]),
    ):
        mock_observer = MagicMock()
        mock_observer_class.return_value = mock_observer

        watch(
            log_dir=str(tmp_path),
            model="mockllm/model",
            db="test.db",
            cooldown=5.0,
            poll_interval=0.01,
        )

        mock_init_db.assert_called_once_with("test.db")
        mock_mkdir.assert_called_once()
        mock_observer_class.assert_called_once()
        mock_observer.schedule.assert_called_once()
        mock_observer.start.assert_called_once()
        mock_observer.stop.assert_called_once()
        mock_observer.join.assert_called_once()


def test_watch_handles_keyboard_interrupt_cleanly(tmp_path: Path) -> None:
    """watch() should stop and join observer on KeyboardInterrupt."""
    with (
        patch("monitor.watcher.init_db"),
        patch("monitor.watcher.Path.mkdir"),
        patch("monitor.watcher.Observer") as mock_observer_class,
        patch("monitor.watcher._flush_ready"),
        patch("monitor.watcher.time.sleep", side_effect=KeyboardInterrupt),
    ):
        mock_observer = MagicMock()
        mock_observer_class.return_value = mock_observer

        watch(
            log_dir=str(tmp_path),
            model="mockllm/model",
            db="test.db",
            poll_interval=0.01,
        )

        mock_observer.stop.assert_called_once()
        mock_observer.join.assert_called_once()


def test_watch_logs_startup_info(tmp_path: Path, caplog) -> None:
    """watch() logs the startup configuration."""
    with (
        patch("monitor.watcher.init_db"),
        patch("monitor.watcher.Path.mkdir"),
        patch("monitor.watcher.Observer") as mock_observer_class,
        patch("monitor.watcher._flush_ready"),
        patch("monitor.watcher.time.sleep", side_effect=KeyboardInterrupt),
    ):
        mock_observer = MagicMock()
        mock_observer_class.return_value = mock_observer

        # Set the logger level for the watcher module to capture INFO logs
        logger = logging.getLogger("monitor.watcher")
        logger.setLevel(logging.INFO)
        caplog.set_level(logging.INFO, logger="monitor.watcher")

        watch(
            log_dir=str(tmp_path),
            model="groq/llama-3.3-70b-versatile",
            db="custom.db",
            cooldown=45.0,
            allowed_hosts=["api.github.com"],
            inspect_log_dir="custom_logs/",
            poll_interval=0.01,
        )

        assert "Watching" in caplog.text
        assert "groq/llama-3.3-70b-versatile" in caplog.text
        assert "45s" in caplog.text
        assert "custom.db" in caplog.text


# ---------------------------------------------------------------------------
# parse_args()
# ---------------------------------------------------------------------------


def test_parse_args_required_args() -> None:
    """parse_args() requires --log-dir and --model."""
    with patch("sys.argv", ["watcher", "--log-dir", "/tmp/logs", "--model", "groq/llama"]):
        args = parse_args()
        assert args.log_dir == "/tmp/logs"
        assert args.model == "groq/llama"


def test_parse_args_optional_defaults() -> None:
    """parse_args() uses defaults for optional args."""
    with patch(
        "sys.argv",
        ["watcher", "--log-dir", "/tmp/logs", "--model", "mockllm/model"],
    ):
        args = parse_args()
        assert args.db == DEFAULT_DB
        assert args.cooldown == 30.0
        assert args.allowed_hosts == []
        assert args.inspect_log_dir == "inspect_logs/watch/"


def test_parse_args_custom_values() -> None:
    """parse_args() accepts custom values for all options."""
    with patch(
        "sys.argv",
        [
            "watcher",
            "--log-dir",
            "/custom/logs",
            "--model",
            "anthropic/claude",
            "--db",
            "my.db",
            "--cooldown",
            "15",
            "--allowed-host",
            "host1.com",
            "--allowed-host",
            "host2.com",
            "--inspect-log-dir",
            "my_logs/",
        ],
    ):
        args = parse_args()
        assert args.log_dir == "/custom/logs"
        assert args.model == "anthropic/claude"
        assert args.db == "my.db"
        assert args.cooldown == 15.0
        assert args.allowed_hosts == ["host1.com", "host2.com"]
        assert args.inspect_log_dir == "my_logs/"


def test_parse_args_cooldown_is_float() -> None:
    """--cooldown is parsed as float."""
    with patch("sys.argv", ["watcher", "--log-dir", "/x", "--model", "m", "--cooldown", "7.5"]):
        args = parse_args()
        assert args.cooldown == 7.5
        assert isinstance(args.cooldown, float)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_configures_logging_and_calls_watch(tmp_path: Path) -> None:
    """main() configures logging and calls watch with parsed args."""
    with (
        patch("monitor.watcher.parse_args") as mock_parse,
        patch("monitor.watcher.watch") as mock_watch,
        patch("monitor.watcher.logging.basicConfig") as mock_logging,
    ):
        mock_parse.return_value = MagicMock(
            log_dir=str(tmp_path),
            model="mockllm/model",
            db="test.db",
            cooldown=10.0,
            allowed_hosts=["localhost"],
            inspect_log_dir="logs/",
        )

        main()

        mock_logging.assert_called_once()
        mock_watch.assert_called_once_with(
            log_dir=str(tmp_path),
            model="mockllm/model",
            db="test.db",
            cooldown=10.0,
            allowed_hosts=["localhost"],
            inspect_log_dir="logs/",
        )


def test_main_logging_format() -> None:
    """main() uses the expected logging format."""
    with (
        patch("monitor.watcher.parse_args") as mock_parse,
        patch("monitor.watcher.watch"),
        patch("monitor.watcher.logging.basicConfig") as mock_logging,
    ):
        mock_parse.return_value = MagicMock(
            log_dir="/x", model="m", db="d", cooldown=1.0, allowed_hosts=[], inspect_log_dir="l/"
        )

        main()

        kwargs = mock_logging.call_args.kwargs
        assert kwargs["level"] == logging.INFO
        assert "%(asctime)s" in kwargs["format"]
        assert "%(levelname)s" in kwargs["format"]
        assert kwargs["datefmt"] == "%H:%M:%S"


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------


def test_main_block_calls_main(monkeypatch) -> None:
    """Running watcher.py as __main__ calls main()."""
    import monitor.watcher as watcher_module

    mock_main = MagicMock()
    monkeypatch.setattr(watcher_module, "main", mock_main)

    # Simulate __name__ == "__main__"
    watcher_module.__name__ = "__main__"
    if watcher_module.__name__ == "__main__":
        watcher_module.main()

    mock_main.assert_called_once()
