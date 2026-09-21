"""Text embedding backends, behind one interface, honest about which are real.

The regex lexicon has high precision and poor recall: it fires only on the phrasings it
lists, so warm prose that never says "let's" reads as neutral. An embedding places a
sentence by its *meaning*, so a paraphrase that shares no vocabulary with an exemplar
still lands near it — which is exactly the recall the lexicon lacks.

The catch is the zero-dependency rule, which is deliberate and applies to the runtime: a
real sentence embedding needs a model, either an API call or a heavyweight local package,
and neither belongs in `dependencies = []`. So embeddings work the way the provider SDKs
do — a real backend is an optional extra, imported lazily, and the instrument still runs
and is tested with nothing installed.

**What runs with nothing installed is deliberately not semantic, and says so.** The
stdlib fallback hashes word n-grams into a fixed vector. It is a bag of surface forms, so
it behaves like the lexicon it was meant to improve on: a paraphrase with new words looks
far away. Shipping it as though it were semantic would be worse than the regex, because
the regex at least tells you why it fired. So every backend carries a `semantic` flag it
declares about itself, and — because a declaration is not evidence — `register.py` runs a
generalization control that measures whether a backend actually places paraphrases
together. A backend that claims to be semantic and fails that control is caught, the same
way a stated stance is checked against a measured one everywhere else here.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Callable, Protocol, Sequence

EMBED_VERSION = "1"

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")


class Backend(Protocol):
    """Anything that turns texts into unit vectors.

    `semantic` is the backend's own claim about itself, used to label output and to skip
    controls that only make sense for a real model. It is never trusted on its own: the
    generalization control checks the claim against behaviour.
    """

    name: str
    dim: int
    semantic: bool

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 1e-12:
        return vec
    return [x / norm for x in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Dot product; both operands are expected already L2-normalised, so this is cosine."""
    return sum(x * y for x, y in zip(a, b))


def centroid(vectors: Sequence[Sequence[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            acc[i] += x
    return _normalise([x / len(vectors) for x in acc])


class HashingBackend:
    """Stdlib fallback. Word uni- and bi-grams hashed into a fixed vector.

    Deterministic, dependency-free, and **not semantic** — it is a bag of surface forms,
    which is the whole reason it exists as a fallback rather than as the answer. Two
    sentences that mean the same thing in different words are far apart here. It is what
    lets the module run and be tested with nothing installed, and its own controls report
    that it does not generalise, so it is never mistaken for the real thing.
    """

    name = "hashing"
    semantic = False

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _tokens(self, text: str) -> list[str]:
        words = [w.lower() for w in _WORD.findall(text or "")]
        grams = list(words)
        grams += [f"{a}_{b}" for a, b in zip(words, words[1:])]
        return grams

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in self._tokens(text):
                h = int(hashlib.blake2b(tok.encode(), digest_size=8).hexdigest(), 16)
                # A second hash bit sets the sign, so unrelated tokens cancel rather than
                # only ever adding — the standard signed hashing-trick construction.
                sign = 1.0 if (h >> 20) & 1 else -1.0
                vec[h % self.dim] += sign
            out.append(_normalise(vec))
        return out


#: Optional real backends, imported lazily so nothing here forces an install. Each entry
#: is a factory. Kept tiny on purpose: adding one is writing a class with `embed`, a
#: `dim`, and `semantic = True`, then registering it — and then letting the generalization
#: control decide whether the claim holds.
_BACKENDS: dict[str, Callable[..., Backend]] = {"hashing": HashingBackend}


def register_backend(name: str, factory: Callable[..., Backend]) -> None:
    _BACKENDS[name] = factory


def get_backend(name: str = "hashing", **kwargs: Any) -> Backend:
    """Resolve a backend by name, falling back to hashing with a stated reason.

    A caller that asks for a real backend and does not have it installed gets the stdlib
    one back rather than an exception — but the returned object still reports
    `semantic = False`, so nothing downstream silently treats a fallback as the model that
    was asked for.
    """
    if name in _BACKENDS:
        try:
            return _BACKENDS[name](**kwargs)
        except Exception:  # noqa: BLE001
            # A registered backend whose package is not installed (e.g. `minilm` without
            # sentence-transformers) falls back rather than crashing. The fallback is
            # honest — it reports `semantic = False` — so the caller gets a working lexical
            # reading and the generalization control still refuses to trust it.
            return HashingBackend()
    return HashingBackend(**kwargs)


def available() -> list[dict[str, Any]]:
    """What backends this install can build, and whether each claims to be semantic."""
    out = []
    for name, factory in _BACKENDS.items():
        try:
            b = factory()
            out.append({"name": name, "semantic": b.semantic, "dim": b.dim})
        except Exception as exc:  # noqa: BLE001 — a backend that cannot construct is reported
            out.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
    return out
