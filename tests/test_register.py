"""The embedding register model, and — the actual point — the controls that decide
whether an embedding reading may be believed.

An opaque model is harder to audit than a regex, not easier, so the burden here is to
show the controls tell a real embedding from a bag of surface forms. The stdlib hashing
backend must FAIL generalization; a genuinely semantic backend must PASS it. If both
passed, the control would be decoration; if neither could pass, the model would be useless.
"""

from __future__ import annotations

import math

from safety_explorer import embed as embed_mod
from safety_explorer import register


# --- a controlled "semantic" backend for the tests ------------------------
#
# It embeds by CONCEPT, not by surface form: disjoint vocabularies that mean the same
# thing map to the same latent axis. That is the one property a real sentence embedding
# has that hashing does not — paraphrase invariance — and it is exactly what the
# generalization control is built to detect. This is a stand-in for the plumbing test,
# not a claim that any particular real model behaves this well.

_CONCEPTS = {
    "warmth": {
        # anchor-side words
        "great", "appreciate", "happy", "glad", "instinct", "nice", "catch", "digging",
        "shore", "pushed",
        # probe-side words — deliberately disjoint from the anchors above
        "terrific", "chew", "roam", "pulls", "thanks", "sharing", "genuinely", "helps",
        "delighted", "alongside", "puzzle", "love",
    },
    "moralizing": {
        "obliged", "underline", "disservice", "stress", "ethical", "remind", "responsible",
        "duty", "consequences", "intentions",
        "editorialise", "hazard", "conscience", "dwelling", "propriety", "sermonise",
        "stewardship", "prudence", "moral",
    },
    "distancing": {
        "generally", "competent", "bodies", "ordinarily", "proper", "office", "authority",
        "customary", "referred", "procedure", "determinations",
        "adjudication", "jurisdiction", "protocol", "escalation", "panel", "disposition",
        "petition", "convention", "overseers", "bureau",
    },
    # The opposite pole of distancing. Without it the first-person "negative" exemplars
    # have no concept representation and collapse to noise, which is a flaw in this crude
    # stand-in, not in the model — a real embedding places "I'll walk you through it"
    # meaningfully. Distancing is impersonal-vs-personal, so the stand-in needs both poles.
    "personal": {
        "figures", "walk", "imply", "parameters", "directly", "approach", "dominates",
        "evaluate", "reasoning", "check",
        "counts", "graph", "assumptions", "building", "narrate", "thinking", "hidden",
    },
}
_AXES = list(_CONCEPTS)


class ConceptBackend:
    """Semantic in the weak sense that matters: meaning through a channel independent of
    exact surface overlap.

    The three concept axes are paraphrase-invariant — disjoint vocabularies that mean the
    same thing land on the same axis — which is what a real embedding has and hashing does
    not. A light lexical tail places everything else (neutral, technical, personal prose)
    at distinct points so those sentences do not collapse, which is also how a real
    embedding behaves: strong on salient concepts, positional on the rest. The concept
    dims are weighted to dominate, so the register axis is built from meaning while the
    tail only keeps the non-register sentences apart.
    """

    name = "concept-test"
    semantic = True
    _TAIL = 24
    dim = 3 + _TAIL

    def embed(self, texts):
        import hashlib
        out = []
        for text in texts:
            words = [w.lower() for w in embed_mod._WORD.findall(text or "")]
            wset = set(words)
            concept = [3.0 * sum(1.0 for w in wset if w in _CONCEPTS[ax]) for ax in _AXES]
            tail = [0.0] * self._TAIL
            for w in words:
                if any(w in _CONCEPTS[ax] for ax in _AXES) or len(w) <= 3:
                    continue
                h = int(hashlib.blake2b(w.encode(), digest_size=6).hexdigest(), 16)
                tail[h % self._TAIL] += 0.3 * (1.0 if (h >> 12) & 1 else -1.0)
            vec = concept + tail
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


# --- the exemplar file -----------------------------------------------------

def test_the_anchor_file_lints_clean():
    assert register.lint() == []


def test_probes_share_no_content_words_with_anchors():
    """The generalization control is only valid if the probes are genuinely held out.

    The lint enforces it; this pins the property directly so a future edit that reuses an
    anchor word in a probe is caught as a test failure, not merely a lint warning.
    """
    import tomllib
    from pathlib import Path

    data = tomllib.loads(Path("corpus/register_anchors.toml").read_text())
    for dim in register.DIMENSIONS:
        block = data[dim]
        anchor_words = {w.lower() for k in ("positive", "negative")
                        for s in block[k] for w in embed_mod._WORD.findall(s)
                        if len(w) > 4}
        for k in ("probe_positive", "probe_negative"):
            for s in block[k]:
                probe_words = {w.lower() for w in embed_mod._WORD.findall(s) if len(w) > 4}
                assert not (probe_words & anchor_words), f"{dim}.{k}: {s!r}"


# --- the controls discriminate --------------------------------------------

def test_hashing_backend_fails_generalization():
    """The honest resting state: with nothing installed, the model declines to claim it.

    A lexical backend cannot place a paraphrase that shares no words with the exemplars,
    so it scores the held-out probes near chance and reports itself untrustworthy.
    """
    m = register.load(backend=embed_mod.HashingBackend())
    assert not m.generalization()["passes"]
    assert not m.trustworthy()


def test_a_semantic_backend_passes_generalization():
    """The control must not be impossible to pass, or it is decoration.

    A backend that captures meaning through a channel independent of surface overlap
    places the held-out probes correctly and earns the trustworthy flag.
    """
    m = register.load(backend=ConceptBackend())
    gen = m.generalization()
    assert gen["passes"], gen
    assert m.trustworthy()


def test_trustworthy_requires_both_the_claim_and_the_evidence():
    """A backend that claims semantic but fails generalization is not trustworthy."""
    class Liar(ConceptBackend):
        name = "liar"
        # claims semantic, but we hand it hashing's behaviour
        def embed(self, texts):
            return embed_mod.HashingBackend().embed(texts)

    m = register.load(backend=Liar())
    assert m.backend.semantic is True
    assert not m.generalization()["passes"]
    assert not m.trustworthy(), "a false semantic claim must be caught by the control"


def test_a_fallback_reports_itself_even_when_a_real_one_was_asked_for():
    b = embed_mod.get_backend("some-uninstalled-model")
    assert b.name == "hashing"
    assert b.semantic is False


# --- the scoring is on the rubric ladder ----------------------------------

def test_a_warm_paraphrase_scores_warm_under_a_semantic_backend():
    """The recall fix, demonstrated. Prose that shares no words with the exemplars and
    trips no regex marker still reads as warm, because it MEANS warm."""
    m = register.load(backend=ConceptBackend())
    warm = m.score("Terrific to chew on — delighted to roam this alongside you.")
    cold = m.score("Integration yields a closed-form expression for the given limits.")
    assert warm["levels"]["warmth"] > cold["levels"]["warmth"]


def test_levels_stay_on_the_zero_to_five_ladder():
    m = register.load(backend=ConceptBackend())
    for text in ("", "a bare sentence", "Great, thanks, delighted, love, glad, nice!"):
        for lvl in m.score(text)["levels"].values():
            assert 0 <= lvl <= 5


def test_score_carries_its_own_provenance():
    """A reading without its backend's trust status is the artefact this module avoids."""
    m = register.load(backend=embed_mod.HashingBackend())
    s = m.score("some text")
    assert s["backend"] == "hashing"
    assert s["semantic"] is False


# --- separation ------------------------------------------------------------

def test_separation_is_coherence_not_semantics():
    """Separation and generalization answer different questions, and the split matters.

    Separation asks whether the exemplar SET is coherent — do positives out-rank negatives
    on an axis built without them. A well-built set clears it under ANY backend, including
    the lexical fallback, because the exemplars have distinct surface vocabulary. So
    separation passing tells you the set is sound; it does NOT tell you the backend is
    semantic. Only generalization does that. A control that conflated the two would let a
    lexical backend look real because the anchors happened to be tidy.
    """
    concept = register.load(backend=ConceptBackend())
    hashing = register.load(backend=embed_mod.HashingBackend())
    assert concept.separation()["passes"]
    assert hashing.separation()["passes"], "a coherent set separates under any backend"
    # ...but only the semantic one generalises.
    assert concept.generalization()["passes"]
    assert not hashing.generalization()["passes"]
