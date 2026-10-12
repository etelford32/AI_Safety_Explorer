"""Anthropic provider.

Imports the SDK lazily so the package works with no optional dependencies installed.

Two model-capability facts drive most of the code here, and getting either wrong
breaks a whole campaign rather than one call:

* **Sampling parameters were removed on the current frontier models.** Sending
  `temperature` (or `top_p` / `top_k`) to Opus 5, Sonnet 5, Opus 4.8/4.7 or the Fable
  family returns a 400. Older models still accept them. So sampling is sent only where
  it is legal, and what was actually sent is recorded on the run.
* **Model ids carry no date suffix.** `claude-opus-5` *is* the id; there is no
  `claude-opus-5-20260401` to pin to. That means a hosted model id is inherently
  alias-like for longitudinal purposes — the weights behind it can move without the
  string changing. The honest response is not to pretend otherwise but to record
  `model_reported` (what the server says it served) on every run and to treat a local
  pinned-weight model as the only true control for drift.

Prompt caching is set here because it is the difference between an affordable depth
sweep and an expensive one: every cell at a depth shares the same filler prefix, so the
filler block is marked as a cache breakpoint and every cell after the first reads it at
a tenth of the input price (less on some models). Nothing else is marked — a corpus
prompt alone is shorter than the minimum cacheable prefix, so a marker there would buy
nothing. Batches default to the 1-hour TTL: requests in a batch run in no particular
order and over longer than five minutes, so a 5-minute entry would expire between them.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .base import BatchRequest, Completion, Provider

MAX_RETRIES = 4
BACKOFF = (2, 4, 8, 16)

#: Model families that reject `temperature` / `top_p` / `top_k` with a 400.
#: Prefix-matched, so future point releases in these families are covered.
NO_SAMPLING_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
)


def rejects_sampling(model: str) -> bool:
    return any(model.startswith(p) for p in NO_SAMPLING_PREFIXES)


CACHE_TTLS = ("5m", "1h")


def to_api_messages(messages: list[dict[str, Any]], cache: bool,
                    ttl: str = "5m") -> list[dict[str, Any]]:
    """Strip the instrument's block annotations and, if caching, mark the filler block.

    The filler block carries a `filler` reference for storage; the API would reject an
    unknown key, so it never leaves this function.
    """
    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append({"role": m["role"], "content": content})
            continue
        blocks = []
        for b in content:
            block = {k: v for k, v in b.items() if k != "filler"}
            if b.get("filler") and cache:
                block["cache_control"] = ({"type": "ephemeral", "ttl": "1h"} if ttl == "1h"
                                          else {"type": "ephemeral"})
            blocks.append(block)
        out.append({"role": m["role"], "content": blocks})
    return out


def _usage(usage: Any) -> dict[str, Any]:
    created = getattr(usage, "cache_creation", None)
    out = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
    }
    if created is not None:
        out["cache_creation"] = {
            "ephemeral_5m_input_tokens": getattr(created, "ephemeral_5m_input_tokens", None),
            "ephemeral_1h_input_tokens": getattr(created, "ephemeral_1h_input_tokens", None),
        }
    return out


def completion_from_message(resp: Any, latency_ms: int = 0, retries: int = 0,
                            batch: bool = False) -> Completion:
    text = "".join(
        block.text for block in resp.content
        if getattr(block, "type", "") == "text"
    )

    # A classifier-level refusal arrives as HTTP 200 with stop_reason="refusal" and a
    # category. That is a categorically different event from the model declining in
    # prose, and it is exactly the boundary this instrument exists to characterise —
    # so capture it rather than flattening both into "the response was short".
    details = getattr(resp, "stop_details", None)
    raw: dict[str, Any] = {}
    if details is not None:
        raw["stop_details"] = {
            "type": getattr(details, "type", None),
            "category": getattr(details, "category", None),
            "explanation": getattr(details, "explanation", None),
        }
    usage = _usage(getattr(resp, "usage", None))
    if batch:
        usage["batch"] = True
    return Completion(
        text=text,
        model_reported=getattr(resp, "model", None),
        finish_reason=getattr(resp, "stop_reason", None),
        usage=usage,
        latency_ms=latency_ms,
        retries=retries,
        raw=raw,
    )


class AnthropicProvider(Provider):
    name = "anthropic"
    counts_tokens = True
    supports_batch = True

    def __init__(self, model: str, max_tokens: int = 8000,
                 temperature: float | None = None, system: str | None = None,
                 thinking: str | None = None, effort: str | None = None,
                 cache: bool = True, cache_ttl: str = "5m",
                 **params: Any) -> None:
        # `temperature=None` means "send nothing", which is both the safe default on
        # current models and an honest record: the run used the model's own sampling
        # behaviour rather than a value we chose.
        if cache_ttl not in CACHE_TTLS:
            raise ValueError(f"cache_ttl must be one of {CACHE_TTLS}, not {cache_ttl!r}")
        super().__init__(model, max_tokens=max_tokens, temperature=temperature,
                         system=system, thinking=thinking, effort=effort,
                         cache=cache, cache_ttl=cache_ttl, **params)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise RuntimeError(
                    "the anthropic SDK is not installed: pip install 'safety-explorer[anthropic]'"
                ) from exc
            self._client = anthropic.Anthropic()
        return self._client

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d["sampling_supported"] = not rejects_sampling(self.model)
        return d

    def _build_kwargs(self, messages: list[dict[str, str]], overrides: dict[str, Any]) -> dict[str, Any]:
        cache = overrides.get("cache", self.params.get("cache", True))
        ttl = overrides.get("cache_ttl", self.params.get("cache_ttl", "5m"))
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": overrides.get("max_tokens", self.params["max_tokens"]),
            "messages": to_api_messages(messages, bool(cache), ttl),
        }

        temperature = overrides.get("temperature", self.params.get("temperature"))
        if temperature is not None:
            if rejects_sampling(self.model):
                raise ValueError(
                    f"{self.model} rejects sampling parameters with a 400 — "
                    f"temperature={temperature} cannot be sent. Omit it (the model's own "
                    f"sampling applies) or use a model that still accepts it."
                )
            kwargs["temperature"] = temperature

        system = overrides.get("system", self.params.get("system"))
        if system:
            kwargs["system"] = system

        thinking = overrides.get("thinking", self.params.get("thinking"))
        if thinking == "off":
            kwargs["thinking"] = {"type": "disabled"}
        elif thinking in ("adaptive", "on"):
            kwargs["thinking"] = {"type": "adaptive"}
        # thinking is None -> send nothing, i.e. the model's own default. On Opus 5
        # that means adaptive thinking is ON. This is a real experimental variable,
        # so whatever it resolves to is recorded in the run's params.

        effort = overrides.get("effort", self.params.get("effort"))
        if effort:
            kwargs["output_config"] = {"effort": effort}

        return kwargs

    def complete(self, messages: list[dict[str, str]], **overrides: Any) -> Completion:
        kwargs = self._build_kwargs(messages, overrides)

        last_error: str | None = None
        for attempt in range(MAX_RETRIES):
            t0 = time.time()
            try:
                resp = self.client.messages.create(**kwargs)
                return completion_from_message(
                    resp, latency_ms=int((time.time() - t0) * 1000), retries=attempt)
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001 — transport errors are recorded, not raised
                last_error = f"{type(exc).__name__}: {exc}"
                # A refusal is data. Only transport-level failures are retried; see
                # docs/DATA_INGESTION.md, "No silent retries on refusal".
                if not _is_transient(exc) or attempt == MAX_RETRIES - 1:
                    break
                time.sleep(BACKOFF[attempt])

        return Completion(text="", error=last_error, retries=MAX_RETRIES - 1,
                          finish_reason="error")

    def count_tokens(self, messages: list[dict[str, Any]]) -> int | None:
        """Exact input token count, for costing a campaign and sizing filler."""
        try:
            kwargs: dict[str, Any] = {"model": self.model,
                                      "messages": to_api_messages(messages, cache=False)}
            system = self.params.get("system")
            if system:
                kwargs["system"] = system
            return self.client.messages.count_tokens(**kwargs).input_tokens
        except Exception:  # noqa: BLE001 — costing is best-effort, never fatal
            return None

    # ------------------------------------------------------------------ batches

    def batch_submit(self, requests: list[BatchRequest]) -> str:
        """Submit a Message Batch. Parameters are built exactly as a sync call builds
        them, so an illegal parameter fails here, before anything is queued."""
        body = []
        for r in requests:
            overrides = {"cache_ttl": "1h", **r.overrides}
            body.append({"custom_id": r.custom_id,
                         "params": self._build_kwargs(r.messages, overrides)})
        batch = self.client.messages.batches.create(requests=body)
        return batch.id

    def batch_status(self, batch_id: str) -> dict[str, Any]:
        b = self.client.messages.batches.retrieve(batch_id)
        counts = getattr(b, "request_counts", None)
        return {
            "status": "ended" if b.processing_status == "ended" else "in_progress",
            "processing_status": b.processing_status,
            "counts": {k: getattr(counts, k, 0) for k in
                       ("processing", "succeeded", "errored", "canceled", "expired")},
        }

    def batch_results(self, batch_id: str):
        for item in self.client.messages.batches.results(batch_id):
            kind = item.result.type
            if kind == "succeeded":
                yield item.custom_id, completion_from_message(item.result.message, batch=True)
            elif kind == "errored":
                err = getattr(getattr(item.result, "error", None), "error", None)
                etype = getattr(err, "type", "error")
                msg = getattr(err, "message", "")
                yield item.custom_id, Completion(
                    text="", error=f"batch {etype}: {msg}", finish_reason="error",
                    usage={"batch": True})
            else:
                # Cancelled or expired: the request never ran. Not a measurement.
                yield item.custom_id, None

    def prewarm(self, messages: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
        """Write the cache entry for a shared prefix before a batch fans out over it.

        Without this, the first requests of a batch run concurrently and each pays the
        cache write for the same filler. `max_tokens=0` asks for no output at all; it
        is rejected inside a batch, which is why this is a separate synchronous call.
        Best-effort: a failure costs only the write the batch would have made anyway.
        """
        try:
            kwargs = self._build_kwargs(messages, {"cache_ttl": "1h", **overrides,
                                                   "max_tokens": 0})
            resp = self.client.messages.create(**kwargs)
            return {"ok": True, "usage": _usage(getattr(resp, "usage", None))}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if any(k in name for k in ("ratelimit", "timeout", "connection", "apistatus",
                               "internal", "overloaded")):
        return True
    status = getattr(exc, "status_code", None)
    return status in (408, 429, 500, 502, 503, 504, 529)
