"""Tests for monitor/tasks.py and monitor/run.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from monitor.tasks import sessions_dataset

FIXTURES_DIR = str(Path(__file__).parent / "fixtures")


def test_sessions_dataset_includes_all_by_default():
    dataset = sessions_dataset(FIXTURES_DIR)
    assert len(dataset) == 1


def test_sessions_dataset_skips_matching_ids():
    dataset = sessions_dataset(FIXTURES_DIR, skip_ids={"test-session-001"})
    assert len(dataset) == 0


def test_sessions_dataset_skip_ids_no_match():
    dataset = sessions_dataset(FIXTURES_DIR, skip_ids={"some-other-id"})
    assert len(dataset) == 1


def _run_main_patched(tmp_path, already_scored: set, ingest_rows: int) -> str:
    """Helper: run monitor.run.main() with all external I/O mocked out."""
    import io
    from contextlib import redirect_stdout

    import monitor.run as run_mod  # ensure module is loaded before patching

    db_path = str(tmp_path / "test.db")
    mock_eval_result = MagicMock()
    mock_eval_result.location = str(tmp_path / "eval.eval")

    with (
        patch.object(sys, "argv", ["monitor.run", "--log-dir", str(tmp_path), "--db", db_path]),
        patch("monitor.run.init_db", new_callable=AsyncMock),
        patch(
            "monitor.run.get_scored_session_ids",
            new_callable=AsyncMock,
            return_value=already_scored,
        ),
        patch("monitor.run.coding_agent_safety", return_value=MagicMock()),
        patch("monitor.run.inspect_eval", return_value=[mock_eval_result]),
        patch("monitor.run.ingest_inspect_log", new_callable=AsyncMock, return_value=ingest_rows),
    ):
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_mod.main()
        return buf.getvalue()


def test_run_main_skips_already_scored_sessions(tmp_path):
    """main() should skip already-scored sessions and print a notice."""
    out = _run_main_patched(tmp_path, already_scored={"s1"}, ingest_rows=3)
    assert "Skipping 1 already-scored session(s)." in out
    assert "3 result rows" in out


def test_run_main_no_skip_when_db_empty(tmp_path):
    """main() should not print a skip message when the DB has no scored sessions."""
    out = _run_main_patched(tmp_path, already_scored=set(), ingest_rows=6)
    assert "Skipping" not in out
    assert "6 result rows" in out
