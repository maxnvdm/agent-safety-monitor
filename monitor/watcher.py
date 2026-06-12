"""Background watcher: auto-ingest Claude Code session logs as they appear.

Usage:
    uv run python -m monitor.watcher \\
        --log-dir ~/.claude/projects/my-project/ \\
        --model groq/llama-3.3-70b-versatile

Each .jsonl file is queued when first seen (created or modified). After
`--cooldown` seconds with no further writes to that file, it is copied to a
temporary directory and run through the full eval pipeline. Already-scored
sessions are skipped automatically via the skip_ids mechanism in run_eval.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from monitor.db import DEFAULT_DB, init_db
from monitor.run import run_eval

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN = 30.0
DEFAULT_POLL = 5.0


class _SessionHandler(FileSystemEventHandler):
    """Records the last-seen timestamp for each .jsonl file that appears."""

    def __init__(self, pending: dict[Path, float]) -> None:
        self._pending = pending

    def _touch(self, path: str | bytes) -> None:
        p = path.decode() if isinstance(path, bytes) else path
        if p.endswith(".jsonl"):
            self._pending[Path(p).resolve()] = time.monotonic()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._touch(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._touch(event.src_path)


def _flush_ready(
    pending: dict[Path, float],
    *,
    cooldown: float,
    db: str,
    model: str,
    allowed_hosts: list[str],
    inspect_log_dir: str,
) -> None:
    """Process files that have been stable for at least `cooldown` seconds."""
    now = time.monotonic()
    ready = [p for p, t in list(pending.items()) if now - t >= cooldown]
    if not ready:
        return

    for p in ready:
        pending.pop(p, None)

    existing = [p for p in ready if p.exists()]
    if not existing:
        return

    with tempfile.TemporaryDirectory(prefix="asm_watch_") as tmpdir:
        tmp = Path(tmpdir)
        for path in existing:
            shutil.copy2(path, tmp / path.name)

        names = [p.name for p in existing]
        logger.info("Processing %d session(s): %s", len(existing), names)
        try:
            rows = run_eval(
                log_dir=str(tmp),
                db=db,
                model=model,
                allowed_hosts=allowed_hosts,
                inspect_log_dir=inspect_log_dir,
                verbose=False,
            )
            logger.info("Ingested %d result row(s)", rows)
        except Exception:
            logger.exception("Eval failed for %s", names)


def watch(
    log_dir: str,
    *,
    model: str,
    db: str = DEFAULT_DB,
    allowed_hosts: list[str] | None = None,
    cooldown: float = DEFAULT_COOLDOWN,
    inspect_log_dir: str = "inspect_logs/watch/",
    poll_interval: float = DEFAULT_POLL,
) -> None:
    """Watch log_dir and ingest each .jsonl file after it stops being written to."""
    asyncio.run(init_db(db))
    Path(inspect_log_dir).mkdir(parents=True, exist_ok=True)

    pending: dict[Path, float] = {}
    observer = Observer()
    observer.schedule(_SessionHandler(pending), log_dir, recursive=True)
    observer.start()

    logger.info("Watching %s  model=%s  cooldown=%gs  db=%s", log_dir, model, cooldown, db)
    try:
        while True:
            time.sleep(poll_interval)
            _flush_ready(
                pending,
                cooldown=cooldown,
                db=db,
                model=model,
                allowed_hosts=allowed_hosts or [],
                inspect_log_dir=inspect_log_dir,
            )
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Watch a log directory and auto-ingest new Claude Code sessions."
    )
    p.add_argument(
        "--log-dir",
        required=True,
        help="Directory to watch (e.g. ~/.claude/projects/my-project/).",
    )
    p.add_argument(
        "--model",
        required=True,
        help="Model for LLM-graded scorers (e.g. groq/llama-3.3-70b-versatile).",
    )
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database path.")
    p.add_argument(
        "--cooldown",
        type=float,
        default=DEFAULT_COOLDOWN,
        help="Seconds of inactivity before a file is ingested (default: %(default)s).",
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        dest="allowed_hosts",
        default=[],
        metavar="HOST",
        help="Allowlisted hostname for exfiltration scorer (repeatable).",
    )
    p.add_argument(
        "--inspect-log-dir",
        default="inspect_logs/watch/",
        help="Where Inspect writes its .eval logs.",
    )
    return p.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()
    watch(
        log_dir=args.log_dir,
        model=args.model,
        db=args.db,
        cooldown=args.cooldown,
        allowed_hosts=args.allowed_hosts,
        inspect_log_dir=args.inspect_log_dir,
    )


if __name__ == "__main__":
    main()
