"""CLI table formatter for benchmark results."""

from __future__ import annotations

SCORERS = ["deceptive_reasoning", "supply_chain_risk"]
COL_WIDTHS = {"model": 34, "scorer": 22, "accuracy": 10, "latency": 13, "cost": 18}


def _bar(accuracy: float, width: int = 10) -> str:
    filled = round(accuracy * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_cost(cost: float | None) -> str:
    if cost is None:
        return "—"
    if cost < 0.000001:
        return "<$0.000001"
    return f"${cost:.6f}"


def _fmt_latency(ms: float | None) -> str:
    if ms is None:
        return "—"
    return f"{ms / 1000:.2f}s"


def print_report(runs: list[dict]) -> None:
    """Print a grouped comparison table from benchmark_runs rows."""
    if not runs:
        print("No benchmark runs found.")
        return

    # Group by scorer for side-by-side readability
    by_scorer: dict[str, list[dict]] = {}
    for r in runs:
        by_scorer.setdefault(r["scorer"], []).append(r)

    header = (
        f"{'Model':<{COL_WIDTHS['model']}}"
        f"{'Accuracy':>{COL_WIDTHS['accuracy']}}"
        f"{'Avg latency':>{COL_WIDTHS['latency']}}"
        f"{'Est cost/session':>{COL_WIDTHS['cost']}}"
        f"  Breakdown"
    )
    sep = "─" * (
        COL_WIDTHS["model"]
        + COL_WIDTHS["accuracy"]
        + COL_WIDTHS["latency"]
        + COL_WIDTHS["cost"]
        + 14
    )

    for scorer, scorer_runs in sorted(by_scorer.items()):
        print(f"\n  {scorer}")
        print(f"  {sep}")
        print(f"  {header}")
        print(f"  {sep}")
        for r in scorer_runs:
            acc = r["accuracy"]
            bar = _bar(acc)
            correct = r["sessions_correct"]
            total = r["sessions_total"]
            model_short = r["model"].split("/", 1)[-1]  # strip provider prefix
            print(
                f"  {model_short:<{COL_WIDTHS['model']}}"
                f"{acc * 100:>{COL_WIDTHS['accuracy'] - 1}.0f}%"
                f"{_fmt_latency(r.get('avg_latency_ms')):>{COL_WIDTHS['latency']}}"
                f"{_fmt_cost(r.get('est_cost_usd')):>{COL_WIDTHS['cost']}}"
                f"  {bar} {correct}/{total}"
            )
        print(f"  {sep}")
