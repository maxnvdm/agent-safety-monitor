import pytest

from benchmark.pricing import RATES, estimate_cost


def test_estimate_cost_known_model() -> None:
    # Sonnet 5.5: $2 / $10 per 1M tokens
    cost = estimate_cost("anthropic/claude-sonnet-5-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(12.0)


def test_estimate_cost_unknown_model_is_none() -> None:
    assert estimate_cost("anthropic/not-a-model", 100, 100) is None


def test_local_models_are_free() -> None:
    assert estimate_cost("mockllm/model", 10_000, 10_000) == 0.0


def test_output_never_cheaper_than_input() -> None:
    for model, (inp, out) in RATES.items():
        assert out >= inp, model
