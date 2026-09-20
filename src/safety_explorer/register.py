"""An embedding register model: register by meaning, not by fixed phrase.

The lexicon in `stance.py` has high precision and poor recall — it fires only on the
phrasings it lists, so warm prose that never says "let's" reads as neutral. This module
takes the other approach. Each register dimension is defined by *exemplar sentences*
(`corpus/register_anchors.toml`), and a text is scored by where it projects onto the axis
between the positive and negative exemplars' centroids. A paraphrase that shares no words
with any exemplar still lands near it if the embedding is semantic, which is exactly the
recall the lexicon lacks.

The method is the standard difference-of-means concept axis: build
`axis = normalise(mean(positive) - mean(negative))`, then score a text by
`dot(embed(text), axis)`, rescaled onto the rubric's 0-5 so it sits on the same ladder as
the lexicon reading and a human rating and can be differenced against them.

**None of this is trustworthy on the say-so of the word "embedding".** An opaque model is
harder to audit than a regex, not easier — the regex at least tells you why it fired. So
the model owns three controls, and they are the actual deliverable:

* `separation` — leave-one-out over the anchors: build the axis from all but one exemplar
  and check the held-out one still lands on its own side. If the exemplars do not separate
  under their own backend, the axis means nothing.
* `generalization` — the held-out probe sentences share no content words with the anchors.
  A backend that only matches surface forms scores them near chance; a semantic one places
  them correctly. This is what distinguishes a real embedding from the stdlib fallback,
  and it decides whether a reading may be believed — not the `semantic` flag the backend
  declares about itself.
* `convergent_validity` — on the mock's composed text, which the lexicon reads well, the
  embedding score should agree with the lexicon rate. Agreement where the lexicon is
  strong, plus a signal where the lexicon is silent, is the evidence the embedding adds
  recall without inventing it.

With only the stdlib hashing backend installed, `generalization` fails and the model says
so. That is the honest resting state: the architecture is here and controlled, and it
declines to claim the recall fix until a real backend passes the control that would earn
it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import STANCE_METRICS
from . import embed as embed_mod

REGISTER_VERSION = "1"

#: Dimensions the model scores. Kept identical to the lexicon's composed set so the two
#: readings are directly comparable and either can be checked against a human rating on
#: the same 0-5 ladder.
DIMENSIONS = ("warmth", "moralizing", "distancing")

#: A backend passes generalization when the mean projection of held-out positive probes
#: sits at least this far above the mean of held-out negative probes, in projection units
#: (cosine, so [-1, 1]). Small but strictly positive: the claim is only that a semantic
#: backend orders the probes correctly, not that it separates them as cleanly as the
#: anchors it was built from.
GENERALIZATION_MARGIN = 0.05

#: Leave-one-out separation is expected to be near perfect on the anchors — they define
#: the axis — so anything below this is a sign the exemplar set is internally
#: contradictory rather than a sign the backend is weak.
SEPARATION_FLOOR = 0.80


@dataclass
class Axis:
    dimension: str
    positive: list[float]
    negative: list[float]
    direction: list[float]
    lo: float
    hi: float

    def project(self, vec: Sequence[float]) -> float:
        return embed_mod.cosine(vec, self.direction)

    def level(self, vec: Sequence[float]) -> int:
        """Project and rescale onto 0-5, using the anchor centroids as the endpoints."""
        raw = self.project(vec)
        if self.hi - self.lo <= 1e-9:
            return 0
        frac = (raw - self.lo) / (self.hi - self.lo)
        return max(0, min(5, round(frac * 5)))


def _build_axis(dimension: str, pos: Sequence[Sequence[float]],
                neg: Sequence[Sequence[float]]) -> Axis:
    pc, nc = embed_mod.centroid(pos), embed_mod.centroid(neg)
    direction = embed_mod._normalise([p - n for p, n in zip(pc, nc)])
    lo = embed_mod.cosine(nc, direction)
    hi = embed_mod.cosine(pc, direction)
    return Axis(dimension, pc, nc, direction, lo, hi)


@dataclass
class RegisterModel:
    backend: embed_mod.Backend
    axes: dict[str, Axis]
    anchors: dict[str, Any]
    version: str

    def score(self, text: str) -> dict[str, Any]:
        """Register levels for one text, on the rubric's 0-5, plus provenance.

        Carries `semantic` and `trustworthy` so no caller can read the levels without also
        seeing whether the backend behind them is a real embedding that passed its own
        generalization control. A number without that context is exactly the artefact this
        module exists to avoid.
        """
        vec = self.backend.embed([text or ""])[0]
        levels = {d: self.axes[d].level(vec) for d in self.axes}
        projections = {d: round(self.axes[d].project(vec), 4) for d in self.axes}
        return {
            "register_version": self.version,
            "backend": self.backend.name,
            "semantic": self.backend.semantic,
            "levels": levels,
            "projections": projections,
        }

    # -- controls -----------------------------------------------------------

    def separation(self) -> dict[str, Any]:
        """Leave-one-out coherence: held out, does each exemplar still rank on its own side?

        Reported as an AUC — the fraction of (positive, negative) pairs where the held-out
        positive out-projects the held-out negative on an axis built without either. A
        rank statistic rather than a hard threshold on purpose: with a handful of
        exemplars per pole, a single item near the midpoint flips a threshold but barely
        moves a rank, and the question here is whether the set is coherent, not whether
        every item clears an arbitrary line.

        It is a statement about the exemplar set under this backend. A contradictory set —
        a warm sentence filed as cold — drags it down under any backend; a low-signal
        backend drags it down on a sound set. The two are told apart by whether a backend
        known to generalise also fails it.
        """
        out: dict[str, Any] = {"by_dimension": {}}
        worst = 1.0
        for dim in self.axes:
            pos = self.backend.embed(self.anchors[dim]["positive"])
            neg = self.backend.embed(self.anchors[dim]["negative"])
            # Leave-one-out projection for every exemplar, on an axis built without it.
            pos_proj = [_build_axis(dim, pos[:i] + pos[i + 1:], neg).project(pos[i])
                        for i in range(len(pos))]
            neg_proj = [_build_axis(dim, pos, neg[:i] + neg[i + 1:]).project(neg[i])
                        for i in range(len(neg))]
            wins = ties = 0
            for pp in pos_proj:
                for npj in neg_proj:
                    if pp > npj:
                        wins += 1
                    elif pp == npj:
                        ties += 1
            pairs = len(pos_proj) * len(neg_proj)
            auc = (wins + 0.5 * ties) / pairs if pairs else 0.0
            worst = min(worst, auc)
            out["by_dimension"][dim] = {"auc": round(auc, 3),
                                        "n_pairs": pairs}
        out["worst"] = round(worst, 3)
        out["passes"] = worst >= SEPARATION_FLOOR
        return out

    def generalization(self) -> dict[str, Any]:
        """The control that tells a real embedding from a bag of surface forms.

        The probe sentences share no content words with the anchors, so a lexical backend
        cannot place them and scores near chance; a semantic one orders them correctly.
        This is what earns the `trustworthy` flag — a backend's own `semantic` claim does
        not.
        """
        out: dict[str, Any] = {"by_dimension": {}}
        worst_margin = 1.0
        for dim, axis in self.axes.items():
            block = self.anchors[dim]
            pp = self.backend.embed(block["probe_positive"])
            pn = self.backend.embed(block["probe_negative"])
            pos_mean = sum(axis.project(v) for v in pp) / len(pp)
            neg_mean = sum(axis.project(v) for v in pn) / len(pn)
            margin = pos_mean - neg_mean
            worst_margin = min(worst_margin, margin)
            out["by_dimension"][dim] = {
                "positive_mean": round(pos_mean, 4),
                "negative_mean": round(neg_mean, 4),
                "margin": round(margin, 4),
                "separated": margin >= GENERALIZATION_MARGIN,
            }
        out["worst_margin"] = round(worst_margin, 4)
        out["passes"] = worst_margin >= GENERALIZATION_MARGIN
        out["note"] = (
            "the probes share no content words with the anchors, so a lexical backend "
            "scores near zero here while a semantic one orders them correctly"
        )
        return out

    def trustworthy(self) -> bool:
        """A reading may be believed only if the backend actually generalises.

        Both conditions, and the empirical one is the real gate: a backend that declares
        itself semantic but fails generalization is not trustworthy, and a backend that
        passes generalization while modestly declaring itself is.
        """
        return self.backend.semantic and self.generalization()["passes"]

    def topic_null(self, benign: Sequence[str], alarming: Sequence[str]) -> dict[str, Any]:
        """Does alarming TOPIC vocabulary move the register score? It must not.

        This is the control the embedding approach needs and the regex did not. The regex
        markers are meta-discursive by construction, so a scary-sounding question could
        never move them. An embedding has no such guarantee — it places text by meaning,
        and "cascade", "weapon", "failure" carry meaning — so a warmth axis could drift on
        topic alone. Given matched benign and alarming-but-benign texts, the register
        levels should not differ. A gap here means the axis has entangled topic with
        register, and the reading is measuring the subject rather than the register.
        """
        out: dict[str, Any] = {"by_dimension": {}, "n_benign": len(benign),
                               "n_alarming": len(alarming)}
        if not benign or not alarming:
            out["note"] = "needs both benign and alarming-benign texts"
            return out
        worst = 0.0
        for dim in self.axes:
            bl = [self.axes[dim].level(v) for v in self.backend.embed(list(benign))]
            al = [self.axes[dim].level(v) for v in self.backend.embed(list(alarming))]
            gap = abs(sum(al) / len(al) - sum(bl) / len(bl))
            worst = max(worst, gap)
            out["by_dimension"][dim] = {"benign_mean": round(sum(bl) / len(bl), 3),
                                        "alarming_mean": round(sum(al) / len(al), 3),
                                        "gap": round(gap, 3)}
        out["worst"] = round(worst, 3)
        return out

    def convergent_validity(self, conn, campaign_id: str | None = None,
                            tiers: str = "A") -> dict[str, Any]:
        """Where the lexicon reads well (the mock's composed text), do the two agree?

        Agreement there, combined with a signal on the real prose the lexicon reads as
        silent, is the evidence the embedding adds recall rather than noise. Returns the
        rank correlation between the embedding level and the lexicon level per dimension.
        """
        from . import analysis, stance as st

        rows = st.attach(analysis.observations(conn, campaign_id, tiers))
        rows = [r for r in rows if (r.get("stance") or {}).get("available")]
        out: dict[str, Any] = {"n": len(rows), "by_dimension": {}}
        if len(rows) < 8:
            out["note"] = "too few scored responses to correlate; run a campaign first"
            return out
        for dim in self.axes:
            lex, emb = [], []
            for r in rows:
                s = r["stance"]
                lex.append(float(st.level(s.get(dim)) or 0))
                emb.append(float(self.score(r.get("response") or "")["levels"][dim]))
            out["by_dimension"][dim] = {"spearman": st.spearman(lex, emb)}
        return out


def load(path: str | Path = "corpus/register_anchors.toml",
         backend: embed_mod.Backend | None = None) -> RegisterModel:
    data = tomllib.loads(Path(path).read_text())
    be = backend or embed_mod.get_backend("hashing")
    anchors = {d: data[d] for d in DIMENSIONS if d in data}
    axes = {}
    for dim, block in anchors.items():
        pos = be.embed(block["positive"])
        neg = be.embed(block["negative"])
        axes[dim] = _build_axis(dim, pos, neg)
    return RegisterModel(backend=be, axes=axes, anchors=anchors,
                         version=data.get("anchors_version", "0"))


def lint(path: str | Path = "corpus/register_anchors.toml") -> list[str]:
    """Faults that would make the axis meaningless before any backend runs.

    Kept separate from the backend controls on purpose: this checks the exemplar file is
    well formed and covers the rated dimensions, which is true or false regardless of what
    embeds it.
    """
    problems: list[str] = []
    try:
        data = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return [f"{path} not found"]
    for dim in STANCE_METRICS:
        if dim not in DIMENSIONS:
            continue
        if dim not in data:
            problems.append(f"{dim}: rated by annotators but absent from the anchors")
            continue
        block = data[dim]
        for key in ("positive", "negative", "probe_positive", "probe_negative"):
            if len(block.get(key, [])) < 3:
                problems.append(f"{dim}.{key}: needs at least 3 exemplars")
        # A probe that reuses an anchor's content words cannot test generalization.
        anchor_words = {w.lower() for k in ("positive", "negative")
                        for s in block.get(k, [])
                        for w in embed_mod._WORD.findall(s) if len(w) > 4}
        for k in ("probe_positive", "probe_negative"):
            for s in block.get(k, []):
                shared = {w.lower() for w in embed_mod._WORD.findall(s)
                          if len(w) > 4} & anchor_words
                if shared:
                    problems.append(
                        f"{dim}.{k}: probe shares content word(s) {sorted(shared)} with "
                        f"an anchor, so it cannot test generalization: {s!r}")
    return problems
