"""Token pricing table (USD per 1 000 tokens) for supported models.

Update these rates as providers change their pricing.
Models not in this table report cost as None.
"""

from __future__ import annotations

# {model_id: (input_per_1k, output_per_1k)}
RATES: dict[str, tuple[float, float]] = {
    # Anthropic
    "anthropic/claude-haiku-4-5": (0.00025, 0.00125),
    "anthropic/claude-sonnet-4-6": (0.003, 0.015),
    "anthropic/claude-opus-4-7": (0.015, 0.075),
    # OpenAI
    "openai/gpt-4o": (0.0025, 0.010),
    "openai/gpt-4o-mini": (0.00015, 0.0006),
    # Groq
    "groq/llama-3.3-70b-versatile": (0.00059, 0.00079),
    "groq/llama-3.1-8b-instant": (0.00005, 0.00008),
    "groq/mixtral-8x7b-32768": (0.00024, 0.00024),
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
