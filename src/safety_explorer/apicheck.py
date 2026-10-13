"""`explorer apicheck` — prove each live-API assumption once, cheaply, before a campaign.

The depth sweep rests on things the test suite can only check against a fake client:
that `count_tokens` sizes the filler, that the filler block actually caches and the
second request reads it, that a `max_tokens: 0` pre-warm is accepted, and that a batch
comes back with the same shape a synchronous call does. Each is checked here against
the real API, in order, with what it cost. A failed stage says what to change; later
stages that depend on it are skipped rather than run on a broken premise.

Nothing is written to the campaign database. Tasks are trivial ("reply with one word"),
never corpus prompts, so no measurement is made or recorded by accident.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import db, filler as filler_mod, pricing
from .providers.base import BatchRequest

#: Filler size for the cache checks: comfortably above every model's minimum cacheable
#: prefix (4096 is the largest), and small enough that the whole check costs cents.
CHECK_DEPTH = 6000
#: Room for adaptive thinking before the one-word answer; thinking cannot be disabled on
#: several current models, so a tiny max_tokens would cut the reply off.
CHECK_MAX_TOKENS = 1024


@dataclass
class Stage:
    name: str
    ok: bool | None            # None = skipped
    detail: str = ""
    cost: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)


def _cost(model: str, usage: dict[str, Any], ttl: str = "5m") -> float:
    return pricing.request_cost(model, usage or {}, ttl=ttl) or 0.0


def run(provider, batch: bool = False, batch_timeout: float = 1800.0,
        poll_seconds: float = 15.0, on_stage: Callable[[Stage], None] | None = None,
        sleep: Callable[[float], None] = time.sleep) -> list[Stage]:
    stages: list[Stage] = []
    model = provider.model

    def add(stage: Stage) -> Stage:
        stages.append(stage)
        if on_stage:
            on_stage(stage)
        return stage

    def skip(name: str, why: str) -> None:
        add(Stage(name, None, why))

    # 1. parameters and credentials, at no cost: count_tokens is free.
    try:
        kw = provider._build_kwargs([{"role": "user", "content": "x"}], {})
    except ValueError as exc:
        add(Stage("parameters", False, str(exc)))
        return stages
    shown = {k: v for k, v in kw.items() if k != "messages"}
    n = provider.count_tokens([{"role": "user", "content": "Reply with the word ok."}])
    if n is None:
        why = getattr(provider, "last_count_error", None) or "no error recorded"
        if "anthropic-workspace-id" in why:
            why += (" — this key belongs to the organisation, not one workspace: "
                    "export ANTHROPIC_WORKSPACE_ID=wrkspc_... and run again")
        add(Stage("credentials", False, f"count_tokens failed ({why}) — check "
                  f"ANTHROPIC_API_KEY, the model id, and network access to api.anthropic.com",
                  data=shown))
        return stages
    add(Stage("credentials", True, f"count_tokens answered ({n} tokens); request "
              f"parameters {shown}", data=shown))

    # 2. one plain call.
    c = provider.complete([{"role": "user", "content": "Reply with the word ok."}],
                          max_tokens=CHECK_MAX_TOKENS)
    if c.error:
        add(Stage("sync call", False, c.error))
        return stages
    add(Stage("sync call", True, f"{c.latency_ms} ms, stop_reason={c.finish_reason}, "
              f"model_reported={c.model_reported}, usage={c.usage}",
              _cost(model, c.usage), {"usage": c.usage}))

    # 3. filler sized with the real tokenizer.
    conn = db.init_db(":memory:")
    f = filler_mod.realise(conn, "neutral-v1", CHECK_DEPTH, provider)
    within = abs(f.tokens - CHECK_DEPTH) <= max(filler_mod.MIN_TOLERANCE,
                                               CHECK_DEPTH * filler_mod.TOLERANCE)
    add(Stage("filler calibration", f.count_method == "exact" and within,
              f"{f.tokens:,} tokens for a {CHECK_DEPTH:,} target ({f.count_method})"))

    # 4. the cache: the first request writes the filler, the second reads it.
    caching = provider.params.get("cache", True)
    if not caching:
        skip("cache write", "caching is off (--no-cache)")
        skip("cache read", "caching is off (--no-cache)")
    else:
        usages = []
        for task in ("Reply with the word ok.", "Reply with the word yes."):
            r = provider.complete(filler_mod.compose([{"role": "user", "content": task}], f),
                                  max_tokens=CHECK_MAX_TOKENS)
            usages.append(r)
            if r.error:
                break
        first = usages[0]
        if first.error:
            add(Stage("cache write", False, first.error))
        else:
            wrote = first.usage.get("cache_creation_input_tokens") or 0
            read0 = first.usage.get("cache_read_input_tokens") or 0
            add(Stage("cache write", wrote > 0 or read0 > 0,
                      f"wrote {wrote:,}, read {read0:,} (a read here means a warm entry "
                      f"from an earlier check)", _cost(model, first.usage),
                      {"usage": first.usage}))
            if len(usages) > 1 and not usages[1].error:
                read = usages[1].usage.get("cache_read_input_tokens") or 0
                add(Stage("cache read", read >= 0.9 * f.tokens,
                          f"read {read:,} of {f.tokens:,} filler tokens from cache",
                          _cost(model, usages[1].usage), {"usage": usages[1].usage}))
            else:
                add(Stage("cache read", False, usages[-1].error or "second call failed"))

    # 5. the pre-warm form the batch path uses.
    if not caching or not hasattr(provider, "prewarm"):
        skip("pre-warm", "caching is off" if not caching else "provider has no pre-warm")
    else:
        pw = provider.prewarm(filler_mod.compose(
            [{"role": "user", "content": "Reply with the word ok."}], f))
        if pw.get("ok"):
            how = ("max_tokens=0 accepted" if pw["max_tokens"] == 0
                   else "max_tokens=0 REJECTED; fell back to max_tokens=1")
            add(Stage("pre-warm", True, f"{how}; usage={pw['usage']}",
                      _cost(model, pw["usage"], ttl="1h"),
                      {"max_tokens": pw["max_tokens"]}))
        else:
            add(Stage("pre-warm", False, pw.get("error", "")))

    # 6. a two-request batch sharing the filler.
    if not batch:
        skip("batch", "not requested (add --batch; it can take minutes)")
    elif not getattr(provider, "supports_batch", False):
        add(Stage("batch", False, f"{provider.name} has no batch API"))
    else:
        reqs = [BatchRequest(f"apicheck-{i}", filler_mod.compose(
                    [{"role": "user", "content": t}], f), {"max_tokens": CHECK_MAX_TOKENS})
                for i, t in enumerate(("Reply with the word ok.", "Reply with the word yes."))]
        try:
            bid = provider.batch_submit(reqs)
        except Exception as exc:  # noqa: BLE001
            add(Stage("batch", False, f"submit failed: {type(exc).__name__}: {exc}"))
            return stages
        t0 = time.time()
        status = provider.batch_status(bid)
        while status["status"] != "ended" and time.time() - t0 < batch_timeout:
            sleep(poll_seconds)
            status = provider.batch_status(bid)
        if status["status"] != "ended":
            add(Stage("batch", False, f"{bid} still {status.get('processing_status')} after "
                      f"{batch_timeout:.0f}s — it keeps running; nothing else depends on it",
                      data={"batch_id": bid}))
        else:
            results = dict(provider.batch_results(bid))
            good = [r for r in results.values() if r is not None and not r.error]
            reads = sum((r.usage.get("cache_read_input_tokens") or 0) for r in good)
            cost = sum(_cost(model, r.usage, ttl="1h") for r in good)
            add(Stage("batch", len(good) == len(reqs),
                      f"{bid}: {len(good)}/{len(reqs)} succeeded in {time.time() - t0:.0f}s; "
                      f"{reads:,} tokens read from cache across the batch; usage carries "
                      f"batch={all(r.usage.get('batch') for r in good)}",
                      cost, {"batch_id": bid}))
    return stages


def total_cost(stages: list[Stage]) -> float:
    return round(sum(s.cost for s in stages), 4)
