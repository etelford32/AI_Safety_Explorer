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


class Provider:
    """Base provider. Subclasses implement `complete`."""

    name = "base"
    #: Set on providers whose model id may be repointed server-side (e.g. "-latest"
    #: aliases). Recorded per run so a drift analysis can exclude them.
    alias_suffixes = ("-latest", "latest")

    def __init__(self, model: str, **params: Any) -> None:
        self.model = model
        self.params = params

    def alias_risk(self) -> bool:
        return any(self.model.endswith(s) for s in self.alias_suffixes)

    def complete(self, messages: list[dict[str, str]], **overrides: Any) -> Completion:
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
