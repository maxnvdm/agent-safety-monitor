"""Cache-aware Claude pricing.

Rates come from a vendored subset of LiteLLM's price table
(``model_prices.json``, refreshed with ``scripts/update_model_prices.py``) so cost
analysis works offline. Prompt caching dominates real Claude Code bills, so rates
carry separate cache-read / 5-minute-write / 1-hour-write prices, plus the
long-context tier some models have and the web-search per-request fee.

A ``MONITOR_PRICES`` env var may point at a JSON file with the same ``models``
schema; its entries override the vendored ones (useful for negotiated rates or
brand-new models).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

_TIER_RE = re.compile(r"^(?P<base>.+?)_above_(?P<k>\d+)k_tokens$")
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")
_CONTEXT_SUFFIX_RE = re.compile(r"\[\d+[km]\]$")
FAST_MODE_MULTIPLIER = 2.0  # Anthropic fast mode is priced at 2x standard


def as_dict(value: Any) -> dict[str, Any]:
    """``value`` if it is a JSON object, else ``{}`` (log fields are not always what we expect)."""
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Usage:
    """Token usage of one API request (cache-write split by TTL)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    web_searches: int = 0
    speed: str = "standard"

    @property
    def cache_write_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def prompt_tokens(self) -> int:
        """Every token the model read for this request (cached or not)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Usage:
        """Build from an Anthropic ``usage`` object as written in Claude Code logs."""
        cc = as_dict(raw.get("cache_creation"))
        if cc and ("ephemeral_5m_input_tokens" in cc or "ephemeral_1h_input_tokens" in cc):
            w5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
            w1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
        else:  # older logs: single total, which is 5-minute cache
            w5, w1 = int(raw.get("cache_creation_input_tokens") or 0), 0
        server = as_dict(raw.get("server_tool_use"))
        return cls(
            input_tokens=int(raw.get("input_tokens") or 0),
            output_tokens=int(raw.get("output_tokens") or 0),
            cache_read_tokens=int(raw.get("cache_read_input_tokens") or 0),
            cache_write_5m_tokens=w5,
            cache_write_1h_tokens=w1,
            web_searches=int(server.get("web_search_requests") or 0),
            speed=str(raw.get("speed") or "standard"),
        )

    def merge_max(self, other: Usage) -> Usage:
        """Combine two snapshots of the same request.

        Claude Code writes one log event per content block, each repeating the request's
        usage, with ``output_tokens`` growing as streaming progresses. Taking the
        per-field maximum recovers the final figures regardless of event order.
        """
        return Usage(
            input_tokens=max(self.input_tokens, other.input_tokens),
            output_tokens=max(self.output_tokens, other.output_tokens),
            cache_read_tokens=max(self.cache_read_tokens, other.cache_read_tokens),
            cache_write_5m_tokens=max(self.cache_write_5m_tokens, other.cache_write_5m_tokens),
            cache_write_1h_tokens=max(self.cache_write_1h_tokens, other.cache_write_1h_tokens),
            web_searches=max(self.web_searches, other.web_searches),
            speed=other.speed if other.speed != "standard" else self.speed,
        )


@dataclass(frozen=True)
class RateCard:
    """USD per token for one model (one pricing tier)."""

    model: str
    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    web_search_per_request: float = 0.0
    fast_multiplier: float = 1.0
    above_threshold: int | None = None  # prompt tokens beyond which ``above`` applies
    above: RateCard | None = field(default=None, compare=False)

    def for_prompt(self, prompt_tokens: int) -> RateCard:
        if self.above is not None and self.above_threshold and prompt_tokens > self.above_threshold:
            return self.above
        return self


@dataclass(frozen=True)
class CostBreakdown:
    """USD cost of one request, split the way the bill is."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    web_search: float = 0.0

    @property
    def total(self) -> float:
        return self.input + self.output + self.cache_read + self.cache_write + self.web_search

    @property
    def write_side(self) -> float:
        """Cost of getting new context in (uncached input + cache writes)."""
        return self.input + self.cache_write


def _card(model: str, e: dict[str, Any], suffix: str = "", fast: float = 1.0) -> RateCard:
    def g(key: str) -> float:
        return float(e.get(key + suffix) or 0.0)

    w5 = g("cache_creation_input_token_cost")
    w1 = float(e.get("cache_creation_input_token_cost_above_1hr" + suffix) or w5)
    return RateCard(
        model=model,
        input=g("input_cost_per_token"),
        output=g("output_cost_per_token"),
        cache_read=g("cache_read_input_token_cost"),
        cache_write_5m=w5,
        cache_write_1h=w1,
        web_search_per_request=float(e.get("web_search_cost_per_request") or 0.0),
        fast_multiplier=fast,
    )


def _build_card(model: str, e: dict[str, Any]) -> RateCard:
    fast = FAST_MODE_MULTIPLIER if e.get("supports_fast_mode") else 1.0
    base = _card(model, e, fast=fast)
    # Long-context tier: keys like ``input_cost_per_token_above_100k_tokens`` replace their
    # untiered twins for any request whose prompt exceeds the threshold.
    tiered: dict[int, dict[str, Any]] = {}
    for key, value in e.items():
        if m := _TIER_RE.match(key):
            tiered.setdefault(int(m["k"]), {})[m["base"]] = value
    if not tiered:
        return base
    k = min(tiered)
    above = _card(model, {**e, **tiered[k]}, fast=fast)
    return RateCard(**{**base.__dict__, "above_threshold": k * 1000, "above": above})


@lru_cache(maxsize=1)
def _table() -> dict[str, dict[str, Any]]:
    vendored = json.loads(
        resources.files("monitor.cost").joinpath("model_prices.json").read_text(encoding="utf-8")
    )
    models: dict[str, dict[str, Any]] = dict(vendored["models"])
    override = os.environ.get("MONITOR_PRICES")
    if override:
        models.update(json.loads(Path(override).read_text(encoding="utf-8")).get("models", {}))
    return models


def reload_prices() -> None:
    """Drop the cached table (tests, or after changing ``MONITOR_PRICES``)."""
    _table.cache_clear()
    rates_for.cache_clear()


@lru_cache(maxsize=256)
def rates_for(model: str) -> RateCard | None:
    """Resolve a model id as it appears in logs to its rate card, or ``None`` if unknown."""
    table = _table()
    name = _CONTEXT_SUFFIX_RE.sub("", model.strip())
    for cand in (name, _DATE_SUFFIX_RE.sub("", name)):
        if cand in table:
            return _build_card(cand, table[cand])
    # longest known id that prefixes the name, e.g. "claude-sonnet-5-5-preview"
    best = max((k for k in table if name.startswith(k)), key=len, default=None)
    return _build_card(best, table[best]) if best else None


def cost_of(usage: Usage, card: RateCard) -> CostBreakdown:
    """Price one request. Applies the long-context tier and fast-mode multiplier."""
    r = card.for_prompt(usage.prompt_tokens)
    mult = card.fast_multiplier if usage.speed == "fast" else 1.0
    return CostBreakdown(
        input=usage.input_tokens * r.input * mult,
        output=usage.output_tokens * r.output * mult,
        cache_read=usage.cache_read_tokens * r.cache_read * mult,
        cache_write=(
            usage.cache_write_5m_tokens * r.cache_write_5m
            + usage.cache_write_1h_tokens * r.cache_write_1h
        )
        * mult,
        web_search=usage.web_searches * r.web_search_per_request,
    )
