"""
Multi-provider pricing / model registry.

Gives the collector a way to turn (provider, model, prompt_tokens,
completion_tokens) into an estimated USD cost, and to expose known
context-window sizes. This is intentionally a small, hand-maintained table
rather than a live-pricing integration -- prices drift, so treat the
numbers below as reasonable defaults and override via PRICING_OVERRIDES_JSON
(a JSON blob shaped like PRICING_TABLE below) when you need accuracy for
billing purposes.

All prices are USD per 1,000 tokens.
"""
import json
import os
from typing import Optional, TypedDict


class ModelPricing(TypedDict):
    input_per_1k: float
    output_per_1k: float
    context_window: int


# Defaults -- approximate, meant for relative cost tracking across a fleet,
# not for exact invoicing. Update freely, or override via env (see below).
PRICING_TABLE: dict[str, dict[str, ModelPricing]] = {
    "openai": {
        "gpt-4o": {"input_per_1k": 0.0025, "output_per_1k": 0.010, "context_window": 128_000},
        "gpt-4o-mini": {"input_per_1k": 0.00015, "output_per_1k": 0.0006, "context_window": 128_000},
        "gpt-4-turbo": {"input_per_1k": 0.010, "output_per_1k": 0.030, "context_window": 128_000},
        "gpt-3.5-turbo": {"input_per_1k": 0.0005, "output_per_1k": 0.0015, "context_window": 16_000},
    },
    "anthropic": {
        "claude-opus-4": {"input_per_1k": 0.015, "output_per_1k": 0.075, "context_window": 200_000},
        "claude-sonnet-4": {"input_per_1k": 0.003, "output_per_1k": 0.015, "context_window": 200_000},
        "claude-haiku-4": {"input_per_1k": 0.0008, "output_per_1k": 0.004, "context_window": 200_000},
    },
    "mock-openai": {
        "mock-model": {"input_per_1k": 0.0, "output_per_1k": 0.0, "context_window": 8_000},
    },
}


def _load_overrides() -> None:
    """Merge PRICING_OVERRIDES_JSON (env var) into PRICING_TABLE at import time."""
    raw = os.getenv("PRICING_OVERRIDES_JSON")
    if not raw:
        return
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError:
        return
    for provider, models in overrides.items():
        PRICING_TABLE.setdefault(provider, {})
        for model, pricing in models.items():
            PRICING_TABLE[provider][model] = pricing


_load_overrides()


def get_pricing(provider: Optional[str], model: Optional[str]) -> Optional[ModelPricing]:
    if not provider or not model:
        return None
    provider_table = PRICING_TABLE.get(provider.lower())
    if not provider_table:
        return None
    return provider_table.get(model)


def estimate_cost_usd(
    provider: Optional[str],
    model: Optional[str],
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
) -> Optional[float]:
    """Returns None (rather than 0.0) when the provider/model isn't in the
    table, so callers can distinguish "known free/zero cost" from
    "unknown, don't trust this number"."""
    pricing = get_pricing(provider, model)
    if pricing is None:
        return None
    prompt_tokens = prompt_tokens or 0
    completion_tokens = completion_tokens or 0
    cost = (
        (prompt_tokens / 1000.0) * pricing["input_per_1k"]
        + (completion_tokens / 1000.0) * pricing["output_per_1k"]
    )
    return round(cost, 8)
