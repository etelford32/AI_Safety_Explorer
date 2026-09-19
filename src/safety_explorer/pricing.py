"""Cached model pricing, for costing a campaign before running it.

These are list prices in USD per million tokens, cached on the date below. They move,
and a stale table quoted as fact is worse than no table — so every estimate printed
from here carries the cache date and is labelled an estimate.
"""

from __future__ import annotations

PRICING_AS_OF = "2026-06-24"

#: model id prefix -> (input $/1M, output $/1M)
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

#: Batch API runs asynchronously at half price. A 246-cell campaign is exactly the
#: latency-insensitive workload it exists for.
BATCH_DISCOUNT = 0.5


def price_for(model: str) -> tuple[float, float] | None:
    best: tuple[str, tuple[float, float]] | None = None
    for prefix, p in PRICES.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, p)
    return best[1] if best else None


def estimate(model: str, input_tokens: int, output_tokens: int,
             batch: bool = False) -> dict[str, float | str | None]:
    p = price_for(model)
    if p is None:
        return {"model": model, "known": False, "as_of": PRICING_AS_OF,
                "note": "no cached price for this model id"}
    in_rate, out_rate = p
    factor = BATCH_DISCOUNT if batch else 1.0
    cost_in = input_tokens / 1_000_000 * in_rate * factor
    cost_out = output_tokens / 1_000_000 * out_rate * factor
    return {
        "model": model, "known": True, "as_of": PRICING_AS_OF,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "input_rate": in_rate, "output_rate": out_rate,
        "batch": batch,
        "cost_input": round(cost_in, 2),
        "cost_output": round(cost_out, 2),
        "cost_total": round(cost_in + cost_out, 2),
    }
