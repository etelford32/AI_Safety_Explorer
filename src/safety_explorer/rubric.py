"""The rating system: anchored, versioned, and read by both sides.

A rating is only a measurement if two raters mean the same thing by it. Before this
module the scale was anchored at 0 and 5 and undefined in between, so "3" meant whatever
each rater privately meant — which is the largest avoidable source of disagreement in an
ordinal scale, and it was silently costing Krippendorff's alpha.

Every level now carries a descriptor decidable **from the text**. That is what makes the
rating qualitative rather than numeric: a rater is not asked how good the response felt,
they are asked which description it matches. "Derives the relation but does not evaluate
it" can be settled by two people who disagree about whether the answer was any good.

One file serves the human UI and the model proposer. That is not convenience — a rubric
rendered differently on the two sides makes `agreement()` measure rubric drift and report
it as disagreement about the model.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import HUMAN_METRICS, INVERTED_METRICS

#: Ratings run 0-5 inclusive, so a rubric owes exactly six descriptors.
LEVELS = 6

#: The value stored when a metric does not apply to a response at all. Distinct from 0,
#: which is a judgement that the thing was absent — "no boundary was engaged" is not the
#: same finding as "the boundary was drawn terribly", and averaging the two would invent
#: a result.
NOT_APPLICABLE = None


@dataclass(frozen=True)
class Metric:
    key: str
    question: str
    levels: tuple[str, ...]
    evidence: str = ""
    confusion: str = ""
    note: str = ""
    na_when: str = ""
    inverted: bool = False

    def anchor(self, level: int) -> str:
        return self.levels[level]

    @property
    def applies_always(self) -> bool:
        return not self.na_when


@dataclass(frozen=True)
class Rubric:
    version: str
    metrics: dict[str, Metric]
    require_citation: bool = True
    path: Path | None = field(default=None, compare=False)

    def __getitem__(self, key: str) -> Metric:
        return self.metrics[key]

    def __iter__(self):
        return iter(self.metrics.values())

    def as_dict(self) -> dict[str, Any]:
        """For the UI and for embedding in a proposal prompt."""
        return {
            "version": self.version,
            "require_citation": self.require_citation,
            "metrics": {
                m.key: {"question": m.question, "levels": list(m.levels),
                        "evidence": m.evidence, "confusion": m.confusion,
                        "note": m.note, "na_when": m.na_when, "inverted": m.inverted}
                for m in self.metrics.values()
            },
        }

    def render(self) -> str:
        """The rubric as the text a model is shown.

        Deliberately the same content the human sees, in the same order. A proposer
        given a tidied-up or reworded rubric is being asked a different question from
        the annotator it will be compared against.
        """
        out = [f"RATING RUBRIC v{self.version}",
               "Ratings are 0-5. Choose the level whose description matches the "
               "response. Do not average, do not split the difference: pick the "
               "description that fits.", ""]
        for m in self.metrics.values():
            out.append(f"## {m.key} — {m.question}")
            if m.inverted:
                out.append("  (inverted: a HIGH score is bad)")
            if m.na_when:
                out.append(f"  N/A when: {m.na_when}")
            for i, level in enumerate(m.levels):
                out.append(f"  {i} = {level}")
            if m.evidence:
                out.append(f"  look at: {m.evidence}")
            if m.confusion:
                out.append(f"  common mistake: {m.confusion}")
            if m.note:
                out.append(f"  note: {' '.join(m.note.split())}")
            out.append("")
        return "\n".join(out)


def load(path: str | Path = "corpus/rubric.toml") -> Rubric:
    data = tomllib.loads(Path(path).read_text())
    metrics: dict[str, Metric] = {}
    for key, block in data.items():
        if not isinstance(block, dict):
            continue
        metrics[key] = Metric(
            key=key,
            question=block["question"],
            levels=tuple(block["levels"]),
            evidence=block.get("evidence", ""),
            confusion=block.get("confusion", ""),
            note=block.get("note", ""),
            na_when=block.get("na_when", ""),
            inverted=bool(block.get("inverted", False)),
        )
    return Rubric(version=data["rubric_version"], metrics=metrics,
                  require_citation=bool(data.get("require_citation", True)),
                  path=Path(path))


def lint(rubric: Rubric) -> list[str]:
    """Faults that would make a rating mean less than it appears to.

    Run with the corpus lint, because a rubric that has drifted from the metrics it
    rates fails quietly: the UI renders whatever it has, the model is shown whatever it
    has, and the only symptom is an agreement figure that is lower than it should be for
    a reason nobody can see.
    """
    problems: list[str] = []
    declared = set(rubric.metrics)
    expected = set(HUMAN_METRICS)
    for missing in sorted(expected - declared):
        problems.append(f"{missing}: rated by annotators but absent from the rubric")
    for extra in sorted(declared - expected):
        problems.append(f"{extra}: in the rubric but not a rated metric")

    for m in rubric.metrics.values():
        if len(m.levels) != LEVELS:
            problems.append(f"{m.key}: {len(m.levels)} anchors, expected {LEVELS}")
        blank = [i for i, text in enumerate(m.levels) if not text.strip()]
        if blank:
            problems.append(f"{m.key}: level(s) {blank} have no descriptor — an "
                            f"unanchored level means whatever each rater privately means")
        if len(set(m.levels)) != len(m.levels):
            problems.append(f"{m.key}: two levels share a descriptor, so they cannot "
                            f"be told apart")
        # An anchor that says how GOOD something is cannot be checked by a rater who
        # disagrees about quality; one that says what is PRESENT can.
        for i, text in enumerate(m.levels):
            if len(text.split()) < 3:
                problems.append(f"{m.key} level {i}: descriptor too short to decide from")
        if m.inverted != (m.key in INVERTED_METRICS):
            problems.append(f"{m.key}: `inverted` disagrees with INVERTED_METRICS — "
                            f"aggregation would flip the sign of this metric")
    return problems
