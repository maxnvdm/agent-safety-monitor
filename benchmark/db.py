"""SQLite schema and persistence for benchmark runs."""

from __future__ import annotations

import aiosqlite

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS benchmark_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              TEXT NOT NULL,
    model               TEXT NOT NULL,
    scorer              TEXT NOT NULL,
    sessions_total      INTEGER NOT NULL,
    sessions_correct    INTEGER NOT NULL,
    accuracy            REAL NOT NULL,
    avg_latency_ms      REAL,
    total_input_tokens  INTEGER,
    total_output_tokens INTEGER,
    est_cost_usd        REAL
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES benchmark_runs(id) ON DELETE CASCADE,
    session_id  TEXT NOT NULL,
    scorer      TEXT NOT NULL,
    predicted   TEXT NOT NULL,
    expected    TEXT NOT NULL,
    correct     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bench_results_run ON benchmark_results(run_id);
"""


async def init_benchmark_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(CREATE_TABLES)
        await db.commit()


async def save_run(
    db_path: str,
    *,
    run_at: str,
    model: str,
    scorer: str,
    per_session: list[dict],
    avg_latency_ms: float | None,
    total_input_tokens: int | None,
    total_output_tokens: int | None,
    est_cost_usd: float | None,
) -> int:
    """Persist one (model, scorer) benchmark run. Returns the new run_id."""
    correct = sum(1 for r in per_session if r["correct"])
    total = len(per_session)
    accuracy = correct / total if total else 0.0

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO benchmark_runs
                (run_at, model, scorer, sessions_total, sessions_correct, accuracy,
                 avg_latency_ms, total_input_tokens, total_output_tokens, est_cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_at,
                model,
                scorer,
                total,
                correct,
                accuracy,
                avg_latency_ms,
                total_input_tokens,
                total_output_tokens,
                est_cost_usd,
            ),
        )
        run_id = cursor.lastrowid
        await db.executemany(
            """
            INSERT INTO benchmark_results (run_id, session_id, scorer, predicted, expected, correct)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (run_id, r["session_id"], scorer, r["predicted"], r["expected"], r["correct"])
                for r in per_session
            ],
        )
        await db.commit()
    return run_id  # type: ignore[return-value]


async def load_latest_runs(db_path: str, limit: int = 50) -> list[dict]:
    """Return the most recent benchmark_runs rows for report display."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM benchmark_runs
            ORDER BY run_at DESC, model, scorer
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
