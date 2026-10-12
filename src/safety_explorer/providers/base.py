"""Provider interface.

Deliberately minimal: a provider turns a message list into a `Completion`. Everything
the instrument cares about — provenance, features, scoring — happens above this layer,
so adding a provider never touches the experimental machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Completion:
    text: str
    model_reported: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    retries: int = 0
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def message_text(message: dict[str, Any]) -> str:
    """A message's text, whether its content is a string or a list of text blocks."""
    content = message.get("content")
    if isinstance(content, list):
        return "\n\n".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
    return content or ""


def task_text(message: dict[str, Any]) -> str:
    """A message's text without any context-depth filler in front of it."""
    content = message.get("content")
    if isinstance(content, list):
        return "\n\n".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                            and not b.get("filler"))
    return content or ""


def flatten(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Plain-string messages, for providers with no notion of content blocks."""
    return [{"role": m["role"], "content": message_text(m)} for m in messages]


@dataclass
class BatchRequest:
    custom_id: str
    messages: list[dict[str, Any]]
    overrides: dict[str, Any] = field(default_factory=dict)


class Provider:
    """Base provider. Subclasses implement `complete`.

    Messages may carry content as a list of text blocks; a block marked `filler` is
    context-depth padding (see `filler.py`). A provider that can cache a prefix marks
    that block as a cache breakpoint; one that cannot simply joins the blocks.
    """

    name = "base"
    #: True when `count_tokens` returns the provider's own exact count.
    counts_tokens = False
    #: True when the provider implements the batch_* methods.
    supports_batch = False
    #: Set on providers whose model id may be repointed server-side (e.g. "-latest"
    #: aliases). Recorded per run so a drift analysis can exclude them.
    alias_suffixes = ("-latest", "latest")

    def __init__(self, model: str, **params: Any) -> None:
        self.model = model
        self.params = params

    def alias_risk(self) -> bool:
        return any(self.model.endswith(s) for s in self.alias_suffixes)

    def complete(self, messages: list[dict[str, Any]], **overrides: Any) -> Completion:
        raise NotImplementedError

    def count_tokens(self, messages: list[dict[str, Any]]) -> int | None:
        return None

    # Batch interface. `batch_submit` returns the provider's batch id; `batch_status`
    # returns {"status": "in_progress" | "ended", "counts": {...}}; `batch_results`
    # yields (custom_id, Completion | None) — None for a request that never ran
    # (expired or cancelled), which must not be recorded as a measurement.
    def batch_submit(self, requests: list[BatchRequest]) -> str:
        raise NotImplementedError(f"{self.name} has no batch API")

    def batch_status(self, batch_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def batch_results(self, batch_id: str):
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "params": dict(self.params)}


def get_provider(name: str, model: str, **params: Any) -> Provider:
    """Resolve a provider by name. Imports lazily so optional SDKs stay optional."""
    name = name.lower()
    if name == "mock":
        from .mock import MockProvider
        return MockProvider(model, **params)
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(model, **params)
    if name in ("openai", "local", "openai_compatible"):
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(model, local=(name == "local"), **params)
    raise ValueError(
        f"unknown provider '{name}'. Available: mock, anthropic, openai, local"
    )
