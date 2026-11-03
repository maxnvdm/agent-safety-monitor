import json

import pytest

from monitor.cost import pricing
from monitor.cost.pricing import Usage, cost_of, rates_for


def test_sonnet_55_matches_published_rates() -> None:
    card = rates_for("claude-sonnet-5-5")
    assert card is not None
    # $2 in / $10 out / $0.10 cache read / $2.50 5m write / $4 1h write per MTok
    assert card.input == pytest.approx(2e-6)
    assert card.output == pytest.approx(10e-6)
    assert card.cache_read == pytest.approx(0.10e-6)
    assert card.cache_write_5m == pytest.approx(2.5e-6)
    assert card.cache_write_1h == pytest.approx(4e-6)


def test_cost_reproduces_claude_codes_own_figure() -> None:
    """Claude Code reported $4.964559 for exactly these Sonnet 5.5 tokens (1h cache writes)."""
    usage = Usage(
        input_tokens=5315,
        output_tokens=66160,
        cache_read_tokens=28_204_690,
        cache_write_1h_tokens=367_965,
    )
    card = rates_for("claude-sonnet-5-5")
    assert card is not None
    assert cost_of(usage, card).total == pytest.approx(4.964559, abs=1e-6)


def test_cost_breakdown_sums_to_total() -> None:
    card = rates_for("claude-sonnet-5-5")
    assert card is not None
    b = cost_of(Usage(100, 200, 300, 400, 500, web_searches=2), card)
    assert b.total == pytest.approx(
        b.input + b.output + b.cache_read + b.cache_write + b.web_search
    )
    assert b.web_search == pytest.approx(0.02)


def test_long_context_tier_applies_above_threshold() -> None:
    card = rates_for("claude-haiku-5-5")
    assert card is not None and card.above_threshold == 100_000
    small = cost_of(Usage(input_tokens=1000), card)
    big = cost_of(Usage(input_tokens=150_000), card)
    assert small.input == pytest.approx(1000 * 0.10e-6)
    assert big.input == pytest.approx(150_000 * 0.50e-6)  # whole request moves to the higher tier


def test_fast_mode_doubles_supported_models_only() -> None:
    opus = rates_for("claude-opus-5-5")
    sonnet = rates_for("claude-sonnet-5-5")
    assert opus is not None and sonnet is not None
    std = cost_of(Usage(output_tokens=1000), opus).total
    fast = cost_of(Usage(output_tokens=1000, speed="fast"), opus).total
    assert fast == pytest.approx(2 * std)
    assert cost_of(Usage(output_tokens=1000, speed="fast"), sonnet).total == pytest.approx(
        cost_of(Usage(output_tokens=1000), sonnet).total
    )


@pytest.mark.parametrize(
    "name", ["claude-sonnet-5-5[1m]", "claude-haiku-4-5-20251001", "claude-opus-4-7-20260416"]
)
def test_model_id_variants_resolve(name: str) -> None:
    assert rates_for(name) is not None


def test_unknown_model_is_none() -> None:
    assert rates_for("gpt-4o") is None


def test_usage_from_api_splits_cache_ttl_and_defaults_to_5m() -> None:
    split = Usage.from_api(
        {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 9,
            "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 5},
            "server_tool_use": {"web_search_requests": 1},
            "speed": "fast",
        }
    )
    assert (split.cache_write_5m_tokens, split.cache_write_1h_tokens) == (4, 5)
    assert split.web_searches == 1 and split.speed == "fast"
    legacy = Usage.from_api({"cache_creation_input_tokens": 7})
    assert (legacy.cache_write_5m_tokens, legacy.cache_write_1h_tokens) == (7, 0)


def test_merge_max_recovers_final_streaming_usage() -> None:
    a = Usage(input_tokens=2, output_tokens=10, cache_read_tokens=100)
    b = Usage(input_tokens=2, output_tokens=180, cache_read_tokens=100)
    assert a.merge_max(b) == b.merge_max(a)
    assert a.merge_max(b).output_tokens == 180


def test_override_file_wins(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "prices.json"
    f.write_text(
        json.dumps(
            {
                "models": {
                    "claude-sonnet-5-5": {
                        "input_cost_per_token": 1e-6,
                        "output_cost_per_token": 1e-6,
                    },
                    "my-new-model": {"input_cost_per_token": 3e-6, "output_cost_per_token": 9e-6},
                }
            }
        )
    )
    monkeypatch.setenv("MONITOR_PRICES", str(f))
    pricing.reload_prices()
    try:
        card = rates_for("claude-sonnet-5-5")
        assert card is not None and card.input == pytest.approx(1e-6)
        assert rates_for("my-new-model") is not None
    finally:
        monkeypatch.delenv("MONITOR_PRICES")
        pricing.reload_prices()
