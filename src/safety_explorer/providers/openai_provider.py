"""OpenAI-compatible provider.

Also serves local endpoints (llama.cpp, vLLM, Ollama) via `--provider local`, which
matters for longitudinal work: a local model with pinned weights is the only
configuration that cannot silently change under you, so it is the control for RQ8.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .base import Completion, Provider

MAX_RETRIES = 4
BACKOFF = (2, 4, 8, 16)
DEFAULT_LOCAL_BASE = "http://localhost:11434/v1"


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model: str, max_tokens: int = 2048, temperature: float = 0.0,
                 system: str | None = None, local: bool = False,
                 base_url: str | None = None, **params: Any) -> None:
        super().__init__(model, max_tokens=max_tokens, temperature=temperature,
                         system=system, **params)
        self.local = local
        self.base_url = base_url or (
            os.environ.get("OPENAI_BASE_URL") or (DEFAULT_LOCAL_BASE if local else None)
        )
        if local:
            self.name = "local"
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "the openai SDK is not installed: pip install 'safety-explorer[openai]'"
                ) from exc
            kwargs: dict[str, Any] = {}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            # Local endpoints usually ignore the key but the SDK requires one.
            kwargs["api_key"] = os.environ.get("OPENAI_API_KEY") or ("local" if self.local else None)
            if not kwargs["api_key"]:
                raise RuntimeError("OPENAI_API_KEY is not set")
            self._client = OpenAI(**kwargs)
        return self._client

    def alias_risk(self) -> bool:
        # A pinned local model cannot be repointed; a hosted alias can.
        return False if self.local else super().alias_risk()

    def complete(self, messages: list[dict[str, str]], **overrides: Any) -> Completion:
        system = overrides.get("system", self.params.get("system"))
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)

        last_error: str | None = None
        for attempt in range(MAX_RETRIES):
            t0 = time.time()
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=msgs,
                    max_tokens=overrides.get("max_tokens", self.params["max_tokens"]),
                    temperature=overrides.get("temperature", self.params["temperature"]),
                )
                choice = resp.choices[0]
                usage = getattr(resp, "usage", None)
                return Completion(
                    text=choice.message.content or "",
                    model_reported=getattr(resp, "model", None),
                    finish_reason=getattr(choice, "finish_reason", None),
                    usage={
                        "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                        "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                    },
                    latency_ms=int((time.time() - t0) * 1000),
                    retries=attempt,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if not _is_transient(exc) or attempt == MAX_RETRIES - 1:
                    break
                time.sleep(BACKOFF[attempt])

        return Completion(text="", error=last_error, retries=MAX_RETRIES - 1,
                          finish_reason="error")


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if any(k in name for k in ("ratelimit", "timeout", "connection", "internal", "apistatus")):
        return True
    status = getattr(exc, "status_code", None)
    return status in (408, 429, 500, 502, 503, 504)
