"""Benchmark LLM-graded scorers across multiple models.

Usage:
    uv run python -m benchmark.run \\
        --models anthropic/claude-haiku-4-5 openai/gpt-4o-mini \\
        --db monitor.db

Each model is evaluated against the 10 labelled sessions in benchmark/sessions/.
Results (accuracy, latency, token counts, estimated cost) are stored in
benchmark_runs / benchmark_results tables and printed as a comparison table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from inspect_ai import eval as inspect_eval
from inspect_ai.log import EvalLog

from benchmark.db import init_benchmark_db, load_latest_runs, save_run
from benchmark.pricing import estimate_cost
from benchmark.report import print_report
from monitor.db import DEFAULT_DB, init_db
from monitor.tasks import coding_agent_safety

BENCHMARK_DIR = Path(__file__).parent
LABELS_PATH = BENCHMARK_DIR / "labels.json"
SESSIONS_DIR = BENCHMARK_DIR / "sessions"

LLM_SCORERS = ("deceptive_reasoning", "supply_chain_risk")


def _load_labels() -> dict[str, dict[str, str]]:
    with open(LABELS_PATH) as f:
        data: dict[str, dict[str, str]] = json.load(f)
        return data


def _run_eval_for_model(model: str, inspect_log_dir: Path) -> tuple[list[EvalLog], float]:
    """Run inspect_eval for one model. Returns (logs, elapsed_ms)."""
    start = time.monotonic()
    logs = inspect_eval(
        coding_agent_safety(log_dir=str(SESSIONS_DIR)),
        model=model,
        log_dir=str(inspect_log_dir),
    )
    elapsed_ms = (time.monotonic() - start) * 1000
    return logs, elapsed_ms


def _extract_usage(log: EvalLog) -> tuple[int, int]:
    """Extract total input + output tokens from an eval log."""
    input_tokens = 0
    output_tokens = 0
    if log.stats and log.stats.model_usage:
        for usage in log.stats.model_usage.values():
            input_tokens += getattr(usage, "input_tokens", 0) or 0
            output_tokens += getattr(usage, "output_tokens", 0) or 0
    return input_tokens, output_tokens


def _score_value_to_label(value: object) -> str:
    """Convert an Inspect score value to 'pass' or 'fail'."""
    from inspect_ai.scorer import CORRECT

    return "pass" if value == CORRECT else "fail"


async def _process_model(
    model: str,
    logs: list[EvalLog],
    elapsed_ms: float,
    labels: dict[str, dict[str, str]],
    db_path: str,
    run_at: str,
) -> dict[str, dict]:
    """Parse eval results for one model; persist + return per-scorer summary."""
    if not logs:
        return {}

    log = logs[0]
    n_sessions = len(log.samples or [])
    input_tokens, output_tokens = _extract_usage(log)
    avg_latency_ms = elapsed_ms / n_sessions if n_sessions else None

    # Build per-session predictions for each LLM scorer
    per_scorer: dict[str, list[dict]] = {s: [] for s in LLM_SCORERS}

    for sample in log.samples or []:
        meta = sample.metadata or {}
        session_id = meta.get("session_id") or str(sample.id)
        expected_map = labels.get(session_id, {})

        for scorer_name in LLM_SCORERS:
            if scorer_name not in expected_map:
                continue  # session not labelled for this scorer
            expected = expected_map[scorer_name]
            score = (sample.scores or {}).get(scorer_name)
            if score is None:
                continue
            predicted = _score_value_to_label(score.value)
            per_scorer[scorer_name].append(
                {
                    "session_id": session_id,
                    "predicted": predicted,
                    "expected": expected,
                    "correct": int(predicted == expected),
                }
            )

    summaries: dict[str, dict] = {}
    for scorer_name, results in per_scorer.items():
        if not results:
            continue
        # Apportion tokens evenly across scorers (both use the model)
        scorer_input = input_tokens // len(LLM_SCORERS)
        scorer_output = output_tokens // len(LLM_SCORERS)
        cost = estimate_cost(model, scorer_input, scorer_output)

        run_id = await save_run(
            db_path,
            run_at=run_at,
            model=model,
            scorer=scorer_name,
            per_session=results,
            avg_latency_ms=avg_latency_ms,
            total_input_tokens=scorer_input or None,
            total_output_tokens=scorer_output or None,
            est_cost_usd=cost,
        )
        correct = sum(r["correct"] for r in results)
        summaries[scorer_name] = {
            "run_id": run_id,
            "accuracy": correct / len(results),
            "correct": correct,
            "total": len(results),
        }

    return summaries


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark LLM scorers across models.")
    p.add_argument(
        "--models",
        nargs="+",
        default=["mockllm/model"],
        help="Models to benchmark (space-separated, e.g. anthropic/claude-haiku-4-5).",
    )
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database path.")
    p.add_argument(
        "--inspect-log-dir",
        default="inspect_logs/benchmark/",
        help="Where Inspect writes its .eval logs.",
    )
    p.add_argument(
        "--report-only",
        action="store_true",
        help="Skip evaluation; just print the latest stored results.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    asyncio.run(init_db(args.db))
    asyncio.run(init_benchmark_db(args.db))

    if args.report_only:
        runs = asyncio.run(load_latest_runs(args.db))
        print_report(runs)
        return

    Path(args.inspect_log_dir).mkdir(parents=True, exist_ok=True)
    labels = _load_labels()
    run_at = datetime.now(UTC).isoformat()

    for model in args.models:
        print(f"\nEvaluating {model}...")
        try:
            logs, elapsed_ms = _run_eval_for_model(model, Path(args.inspect_log_dir))
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ Failed: {exc}")
            continue

        summaries = asyncio.run(_process_model(model, logs, elapsed_ms, labels, args.db, run_at))

        for scorer_name, s in summaries.items():
            print(
                f"  {scorer_name}: {s['correct']}/{s['total']} correct ({s['accuracy'] * 100:.0f}%)"
            )

    print("\n─── Benchmark report ───")
    runs = asyncio.run(load_latest_runs(args.db))
    # Show only this run's results (matching run_at)
    this_run = [r for r in runs if r["run_at"] == run_at]
    print_report(this_run if this_run else runs)


if __name__ == "__main__":
    main()
