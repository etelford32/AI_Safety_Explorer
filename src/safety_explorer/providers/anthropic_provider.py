"""Anthropic provider.

Imports the SDK lazily so the package works with no optional dependencies installed.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .base import Completion, Provider

MAX_RETRIES = 4
BACKOFF = (2, 4, 8, 16)


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, max_tokens: int = 2048, temperature: float = 0.0,
                 system: str | None = None, **params: Any) -> None:
        super().__init__(model, max_tokens=max_tokens, temperature=temperature,
                         system=system, **params)
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
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            self._client = anthropic.Anthropic()
        return self._client

    def complete(self, messages: list[dict[str, str]], **overrides: Any) -> Completion:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": overrides.get("max_tokens", self.params["max_tokens"]),
            "temperature": overrides.get("temperature", self.params["temperature"]),
            "messages": messages,
        }
        system = overrides.get("system", self.params.get("system"))
        if system:
            kwargs["system"] = system

        last_error: str | None = None
        for attempt in range(MAX_RETRIES):
            t0 = time.time()
            try:
                resp = self.client.messages.create(**kwargs)
                text = "".join(
                    block.text for block in resp.content if getattr(block, "type", "") == "text"
                )
                return Completion(
                    text=text,
                    model_reported=getattr(resp, "model", None),
                    finish_reason=getattr(resp, "stop_reason", None),
                    usage={
                        "input_tokens": getattr(resp.usage, "input_tokens", None),
                        "output_tokens": getattr(resp.usage, "output_tokens", None),
                    },
                    latency_ms=int((time.time() - t0) * 1000),
                    retries=attempt,
                )
            except Exception as exc:  # noqa: BLE001 — transport errors are recorded, not raised
                last_error = f"{type(exc).__name__}: {exc}"
                # A refusal is data. Only transport-level failures are retried; see
                # docs/DATA_INGESTION.md, "No silent retries on refusal".
                if not _is_transient(exc) or attempt == MAX_RETRIES - 1:
                    break
                time.sleep(BACKOFF[attempt])

        return Completion(text="", error=last_error, retries=MAX_RETRIES - 1,
                          finish_reason="error")


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if any(k in name for k in ("ratelimit", "timeout", "connection", "apistatus", "internal", "overloaded")):
        return True
    status = getattr(exc, "status_code", None)
    return status in (408, 429, 500, 502, 503, 504, 529)
