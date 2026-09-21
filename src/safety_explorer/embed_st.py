"""A real, semantic embedding backend — the optional extra that closes the recall gap.

This is the concrete answer to "how do we get this working": install the extra and point
the environment at it.

    pip install 'safety-explorer[embeddings]'
    export EXPLORER_EMBED_BACKEND=minilm
    explorer register        # confirm it passes generalization
    explorer serve           # the drift alert now reads the embedding channels

Nothing here is imported unless asked for, so the zero-dependency runtime is untouched. The
backend registers itself on import under `minilm`; `get_backend("minilm")` triggers that
import lazily, and if `sentence-transformers` is not installed the registry falls back to
the stdlib hashing backend with `semantic = False`, so the caller gets a working (if
lexical) reading rather than a crash — and the generalization control still refuses to
trust it.

The model default is `all-MiniLM-L6-v2`: small, fast, CPU-friendly, and good enough to
place a paraphrase near its exemplar, which is all the register axis needs. A larger model
is a one-line change to `MODEL`. Whatever the choice, it is not trusted on its say-so: the
same generalization control that rejects the hashing fallback decides whether this backend's
reading, and the drift built on it, may be believed.
"""

from __future__ import annotations

from typing import Sequence

MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class SentenceTransformerBackend:
    """Wraps a sentence-transformers model as an Explorer embedding backend.

    Semantic by construction — a real bi-encoder places paraphrases together — but the
    claim is still checked, not taken: `register.generalization` decides trust.
    """

    name = "minilm"
    semantic = True

    def __init__(self, model_name: str = MODEL) -> None:
        # Imported here, not at module top, so importing this file to register the backend
        # does not require the package. The failure is loud and specific if someone selects
        # `minilm` without the extra installed.
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # normalize_embeddings=True so the vectors are unit length and the Explorer's
        # cosine == dot assumption holds, exactly as the hashing backend guarantees.
        vecs = self._model.encode(list(texts), normalize_embeddings=True,
                                  show_progress_bar=False)
        return [list(map(float, v)) for v in vecs]


def _factory(**kwargs):
    return SentenceTransformerBackend(**kwargs)


def register() -> None:
    """Add `minilm` to the embedding registry. Safe to call more than once."""
    from . import embed as embed_mod

    embed_mod.register_backend("minilm", _factory)


register()
