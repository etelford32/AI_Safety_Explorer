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
"""

from __future__ import annotations

import os
import time
from typing import Any

from .base import Completion, Provider

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


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, max_tokens: int = 8000,
                 temperature: float | None = None, system: str | None = None,
                 thinking: str | None = None, effort: str | None = None,
                 **params: Any) -> None:
        # `temperature=None` means "send nothing", which is both the safe default on
        # current models and an honest record: the run used the model's own sampling
        # behaviour rather than a value we chose.
        super().__init__(model, max_tokens=max_tokens, temperature=temperature,
                         system=system, thinking=thinking, effort=effort, **params)
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
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": overrides.get("max_tokens", self.params["max_tokens"]),
            "messages": messages,
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
                text = "".join(
                    block.text for block in resp.content
                    if getattr(block, "type", "") == "text"
                )

                # A classifier-level refusal arrives as HTTP 200 with
                # stop_reason="refusal" and a category. That is a categorically
                # different event from the model declining in prose, and it is exactly
                # the boundary this instrument exists to characterise — so capture it
                # rather than flattening both into "the response was short".
                details = getattr(resp, "stop_details", None)
                raw: dict[str, Any] = {}
                if details is not None:
                    raw["stop_details"] = {
                        "type": getattr(details, "type", None),
                        "category": getattr(details, "category", None),
                        "explanation": getattr(details, "explanation", None),
                    }

                usage = getattr(resp, "usage", None)
                return Completion(
                    text=text,
                    model_reported=getattr(resp, "model", None),
                    finish_reason=getattr(resp, "stop_reason", None),
                    usage={
                        "input_tokens": getattr(usage, "input_tokens", None),
                        "output_tokens": getattr(usage, "output_tokens", None),
                        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                    },
                    latency_ms=int((time.time() - t0) * 1000),
                    retries=attempt,
                    raw=raw,
                )
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

    def count_tokens(self, messages: list[dict[str, str]]) -> int | None:
        """Exact input token count, for costing a campaign before running it."""
        try:
            kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
            system = self.params.get("system")
            if system:
                kwargs["system"] = system
            return self.client.messages.count_tokens(**kwargs).input_tokens
        except Exception:  # noqa: BLE001 — costing is best-effort, never fatal
            return None


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if any(k in name for k in ("ratelimit", "timeout", "connection", "apistatus",
                               "internal", "overloaded")):
        return True
    status = getattr(exc, "status_code", None)
    return status in (408, 429, 500, 502, 503, 504, 529)
