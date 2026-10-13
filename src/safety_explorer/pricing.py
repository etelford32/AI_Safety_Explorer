"""Cached model pricing, for costing a campaign before running it.

These are list prices in USD per million tokens, cached on the date below. They move,
and a stale table quoted as fact is worse than no table — so every estimate printed
from here carries the cache date and is labelled an estimate.

Costing is cache-aware because at long context depths the cache decides the bill. A
request's input splits three ways, each at its own rate:

* uncached input at the base rate;
* cache writes at 1.25x base (5-minute TTL) or 2x (1-hour TTL);
* cache reads at a tenth of base on most models, and less on some (`CACHE_READ`).

The Batch API halves all of it, and the discounts stack.
"""

from __future__ import annotations

from typing import Any

PRICING_AS_OF = "2026-10-06"

#: model id prefix -> (input $/1M, output $/1M)
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-mythos-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}

#: Cache-read price ($/1M) where it is not a tenth of the base input price.
CACHE_READ: dict[str, float] = {
    "claude-fable-5-1": 0.25,
    "claude-mythos-5-1": 0.25,
    "claude-opus-5-5": 0.20,
}

#: Long-context pricing: above this many input tokens in ONE request, the request is
#: billed at the second rate pair. A depth sweep crosses it at its deepest levels, so it
#: is applied per request, never to a campaign total.
LONG_CONTEXT: dict[str, tuple[int, float, float]] = {
    "claude-haiku-5-5": (100_000, 0.50, 2.50),
}

CACHE_WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.0}
CACHE_READ_MULTIPLIER = 0.1

#: Batch API runs asynchronously at half price. A 246-cell campaign is exactly the
#: latency-insensitive workload it exists for.
BATCH_DISCOUNT = 0.5

#: Shortest prefix that caches at all, by model prefix (default below). A marker on a
#: shorter prefix is silently ignored, so an estimate must not assume a hit there.
MIN_CACHEABLE = {
    "claude-opus-4-6": 4096, "claude-haiku-4-5": 4096,
    "claude-sonnet-5": 1024, "claude-opus-4-8": 1024, "claude-sonnet-4-6": 1024,
}
MIN_CACHEABLE_DEFAULT = 512


def _best(table: dict[str, Any], model: str) -> Any:
    best: tuple[str, Any] | None = None
    for prefix, value in table.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, value)
    return best[1] if best else None


def price_for(model: str) -> tuple[float, float] | None:
    return _best(PRICES, model)


def min_cacheable(model: str) -> int:
    # "claude-sonnet-5" must not catch "claude-sonnet-5-5", whose minimum is lower.
    if model.startswith(("claude-sonnet-5-5", "claude-opus-5", "claude-fable",
                         "claude-mythos", "claude-haiku-5")):
        return MIN_CACHEABLE_DEFAULT
    return _best(MIN_CACHEABLE, model) or MIN_CACHEABLE_DEFAULT


def rates(model: str, request_input_tokens: int = 0) -> dict[str, float] | None:
    """Per-million rates for one request of the given total input size."""
    p = price_for(model)
    if p is None:
        return None
    in_rate, out_rate = p
    long = _best(LONG_CONTEXT, model)
    if long and request_input_tokens > long[0]:
        in_rate, out_rate = long[1], long[2]
    read = _best(CACHE_READ, model)
    if read is None or (long and request_input_tokens > long[0]):
        read = in_rate * CACHE_READ_MULTIPLIER
    return {"input": in_rate, "output": out_rate, "cache_read": read,
            "cache_write_5m": in_rate * CACHE_WRITE_MULTIPLIER["5m"],
            "cache_write_1h": in_rate * CACHE_WRITE_MULTIPLIER["1h"]}


def request_cost(model: str, usage: dict[str, Any], batch: bool | None = None,
                 ttl: str = "5m") -> float | None:
    """What one recorded request cost, from its `usage`.

    `input_tokens` in the API's usage is the UNCACHED part only; cache reads and writes
    are reported beside it. The write TTL comes from `cache_creation` when the response
    broke it down, else from `ttl`.
    """
    uncached = usage.get("input_tokens") or 0
    read = usage.get("cache_read_input_tokens") or 0
    write = usage.get("cache_creation_input_tokens") or 0
    out = usage.get("output_tokens") or 0
    r = rates(model, uncached + read + write)
    if r is None:
        return None
    split = usage.get("cache_creation") or {}
    w1h = split.get("ephemeral_1h_input_tokens")
    w5m = split.get("ephemeral_5m_input_tokens")
    if w1h is None and w5m is None:
        w1h, w5m = (write, 0) if ttl == "1h" else (0, write)
    cost = (uncached * r["input"] + read * r["cache_read"]
            + (w5m or 0) * r["cache_write_5m"] + (w1h or 0) * r["cache_write_1h"]
            + out * r["output"]) / 1_000_000
    if batch if batch is not None else usage.get("batch"):
        cost *= BATCH_DISCOUNT
    return cost


def estimate(model: str, input_tokens: int, output_tokens: int,
             batch: bool = False) -> dict[str, float | str | None]:
    """Uncached estimate for a campaign total — the shape `preflight` has always printed."""
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


def depth_estimate(model: str, task_tokens: int, n_prompts: int, depths: list[int],
                   repeats: int, output_tokens: int, n_cues: int = 1,
                   filler_tokens: dict[int, int] | None = None,
                   cache: bool = True) -> dict[str, Any]:
    """Cost a depth sweep four ways: sync or batch, with or without the prompt cache.

    The cache model is deliberately conservative. Each (depth, repeat) block runs its
    cells back to back, so the filler is written once per block and read by every other
    cell in it; a block is assumed to outlive the cache TTL, so the next block writes it
    again. Sync writes use the 5-minute TTL, batch writes the 1-hour TTL (plus one
    pre-warm write). A filler shorter than the model's minimum cacheable prefix is
    costed as uncached, because that is how it would be billed.

    `task_tokens` is the summed input of one sweep of the corpus at depth zero, so the
    per-cell task size is its mean.
    """
    if price_for(model) is None:
        return {"model": model, "known": False, "as_of": PRICING_AS_OF,
                "note": "no cached price for this model id"}
    mean_task = task_tokens / max(1, n_prompts)
    cells_per_block = n_prompts * n_cues
    rows = []
    totals = {k: 0.0 for k in ("sync_uncached", "sync_cached", "batch_uncached", "batch_cached")}
    for d in sorted(set(depths)):
        f = (filler_tokens or {}).get(d, d) if d else 0
        per_request_in = int(f + mean_task)
        r = rates(model, per_request_in)
        out_cost = output_tokens * r["output"] / 1_000_000
        cells = cells_per_block * repeats
        uncached = cells * (per_request_in * r["input"] / 1_000_000 + out_cost)

        cacheable = cache and f >= min_cacheable(model)
        if cacheable:
            task_cost = mean_task * r["input"] / 1_000_000
            reads = (cells - repeats) * f * r["cache_read"] / 1_000_000
            sync_w = repeats * f * r["cache_write_5m"] / 1_000_000
            batch_w = (repeats + 1) * f * r["cache_write_1h"] / 1_000_000
            sync_cached = cells * (task_cost + out_cost) + reads + sync_w
            batch_cached = (cells * (task_cost + out_cost) + reads + batch_w) * BATCH_DISCOUNT
        else:
            sync_cached = uncached
            batch_cached = uncached * BATCH_DISCOUNT
        row = {"depth": d, "filler_tokens": f, "cells": cells,
               "input_per_request": per_request_in, "cacheable": cacheable,
               "sync_uncached": uncached, "sync_cached": sync_cached,
               "batch_uncached": uncached * BATCH_DISCOUNT, "batch_cached": batch_cached}
        for k in totals:
            totals[k] += row[k]
        rows.append({k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()})
    return {"model": model, "known": True, "as_of": PRICING_AS_OF,
            "min_cacheable": min_cacheable(model), "by_depth": rows,
            "totals": {k: round(v, 2) for k, v in totals.items()}}


def campaign_cost(conn, campaign_id: str) -> dict[str, Any]:
    """What a campaign's recorded runs actually cost, from their stored usage."""
    from .db import loads, query

    total, priced, unpriced = 0.0, 0, 0
    read = write = uncached = 0
    for r in query(conn, "SELECT model_id, usage, params FROM run WHERE campaign_id = ?",
                   (campaign_id,)):
        usage = loads(r["usage"], {})
        ttl = loads(r["params"], {}).get("cache_ttl", "5m")
        c = request_cost(r["model_id"], usage, ttl=ttl)
        if c is None:
            unpriced += 1
            continue
        total += c
        priced += 1
        read += usage.get("cache_read_input_tokens") or 0
        write += usage.get("cache_creation_input_tokens") or 0
        uncached += usage.get("input_tokens") or 0
    seen = read + write + uncached
    return {"cost": round(total, 4), "runs_priced": priced, "runs_unpriced": unpriced,
            "cache_read_tokens": read, "cache_write_tokens": write,
            "uncached_input_tokens": uncached,
            "cache_hit_share": round(read / seen, 4) if seen else None,
            "as_of": PRICING_AS_OF}
