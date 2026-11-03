"""Token pricing table (USD per 1 000 tokens) for supported models.

Update these rates as providers change their pricing.
Models not in this table report cost as None.
"""

from __future__ import annotations

# {model_id: (input_per_1k, output_per_1k)}
RATES: dict[str, tuple[float, float]] = {
    # Anthropic (first-party API, per-1M rates divided by 1000; verified 2026-10)
    "anthropic/claude-fable-5-1": (0.010, 0.050),
    "anthropic/claude-opus-5-5": (0.004, 0.020),
    "anthropic/claude-opus-5": (0.005, 0.025),
    "anthropic/claude-opus-4-8": (0.005, 0.025),
    "anthropic/claude-opus-4-7": (0.005, 0.025),
    "anthropic/claude-sonnet-5-5": (0.002, 0.010),
    "anthropic/claude-sonnet-5": (0.002, 0.010),
    "anthropic/claude-sonnet-4-6": (0.003, 0.015),
    # Haiku 5.5 is $0.50/$2.50 per 1M for prompts over 100K tokens; the flat rate
    # below is the <=100K tier, which covers typical session transcripts.
    "anthropic/claude-haiku-5-5": (0.0001, 0.0005),
    "anthropic/claude-haiku-4-5": (0.001, 0.005),
    # OpenAI
    "openai/gpt-4o": (0.0025, 0.010),
    "openai/gpt-4o-mini": (0.00015, 0.0006),
    # Groq: no verified rates for the current models (openai/gpt-oss-*), so they report cost as
    # unknown (None) rather than a guess. The Llama models that used to be listed here were shut
    # down for free/developer tiers on 2026-08-16.
    # Ollama / local — zero cost
    "ollama/llama3": (0.0, 0.0),
    "ollama/mistral": (0.0, 0.0),
    # mockllm for tests
    "mockllm/model": (0.0, 0.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Return estimated USD cost, or None if the model is not in the pricing table."""
    if model not in RATES:
        return None
    inp_rate, out_rate = RATES[model]
    return (input_tokens / 1000) * inp_rate + (output_tokens / 1000) * out_rate
