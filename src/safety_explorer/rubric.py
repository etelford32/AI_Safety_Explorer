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

from . import HUMAN_METRICS, INVERTED_METRICS  # noqa: F401 — HUMAN_METRICS used below

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


# ---------------------------------------------------------------------------
# Is the rating system itself any good?
# ---------------------------------------------------------------------------

#: An interior level nobody ever picks is the signal that an anchor is badly worded:
#: raters go 2, 4, 2, 4 and step over the 3. Unused levels at the ENDS mean something
#: else entirely — usually that the corpus contains no such case — so the two are
#: reported apart and only the interior one is a fault.
INTERIOR_SKIP = "skipped_interior_level"

#: A metric whose ratings never leave one level carries no information about any model,
#: however carefully its anchors are written.
SINGLE_LEVEL = "single_level"

#: Above this share on one level, alpha stops being meaningful — the same alpha paradox
#: `reliability` already guards against, applied to the rubric rather than the raters.
DEGENERATE_SHARE = 0.90


def usage(conn, rubric: "Rubric | None" = None,
          annotator: str | None = None) -> dict[str, Any]:
    """Which anchors are actually used, on whatever ratings exist.

    The mirror of `groundtruth.item_analysis`, pointed at the rating system instead of
    the answer key. Every other check here asks whether a rater or a model is any good;
    this asks whether the scale they were handed can express what they saw.

    It needs no second rater and no ground truth, so it can be run on the first session
    — which matters, because a dead anchor found after sixty responses is sixty
    responses rated on a scale that was quietly five levels wide.
    """
    from .db import query

    rubric = rubric or load()
    sql = "SELECT * FROM annotation WHERE pass_index = 0"
    params: list[Any] = []
    if annotator:
        sql += " AND annotator = ?"
        params.append(annotator)
    rows = query(conn, sql, params)

    out: dict[str, Any] = {"n_annotations": len(rows), "metrics": {},
                           "rubric_version": rubric.version}
    for metric in rubric:
        values = [r[metric.key] for r in rows if r.get(metric.key) is not None]
        na = sum(1 for r in rows if r.get(metric.key) is None)
        histogram = {i: values.count(i) for i in range(LEVELS)}
        used = [i for i, n in histogram.items() if n]
        flags: list[str] = []
        interior_skipped: list[int] = []
        if used:
            lo, hi = min(used), max(used)
            interior_skipped = [i for i in range(lo + 1, hi) if not histogram[i]]
            if interior_skipped:
                flags.append(INTERIOR_SKIP)
            if len(used) == 1:
                flags.append(SINGLE_LEVEL)
        modal = max(histogram.values()) / len(values) if values else None
        if modal is not None and modal >= DEGENERATE_SHARE and len(used) > 1:
            flags.append("near_constant")
        if metric.na_when and not na:
            flags.append("na_never_used")
        if not metric.na_when and na:
            flags.append("na_used_without_a_rule")

        out["metrics"][metric.key] = {
            "n": len(values),
            "n_na": na,
            "histogram": histogram,
            "levels_used": used,
            "unused_tails": sorted(set(range(LEVELS)) - set(used) - set(interior_skipped)),
            "skipped_interior": interior_skipped,
            "modal_share": None if modal is None else round(modal, 3),
            "flags": flags,
            "anchors_never_chosen": {i: metric.levels[i] for i in interior_skipped},
        }

    faulty = [k for k, v in out["metrics"].items() if v["flags"]]
    out["n_faulty"] = len(faulty)
    out["faulty"] = faulty
    out["verdict"] = (
        "no ratings yet" if not rows else
        f"every metric's scale is being used" if not faulty else
        f"{len(faulty)} metric(s) to look at: " + ", ".join(faulty)
    )
    out["note"] = (
        "An unused level at the END of a scale usually means the corpus holds no such "
        "case — `unsafe_assistance` 5 should never occur by construction. An unused "
        "level in the MIDDLE means raters stepped over it, which is a fault in the "
        "anchor's wording rather than a fact about any model."
    )
    return out


def anchor_effect(conn, metrics: tuple[str, ...] | None = None,
                  n_resamples: int = 2000) -> dict[str, Any]:
    """Did anchoring the scale raise agreement between raters?

    This is the question the rubric was written to answer and the one thing about it
    nothing here can settle on its own. It needs the same responses rated by the same
    two people under both scales, which is a real session's worth of work.

    What this function is, precisely: the analysis waiting for that session. It groups
    ratings by the rubric version they were made under, computes inter-rater alpha per
    metric within each, and bootstraps the difference over runs — resampling runs rather
    than ratings, because two ratings of one response are not independent observations.

    **It is validated against simulated raters, and that validates the harness, not the
    claim.** A simulation in which anchored raters are given less noise will show
    anchoring helping, because that is what it was told to do. All it establishes is
    that the measurement responds to a difference of known size and reports nothing when
    there is none — which is worth establishing before betting an evening's annotation
    on it, and is not evidence about anchors.
    """
    from .analysis import bootstrap_ci, krippendorff_alpha
    from .db import query

    metrics = metrics or tuple(HUMAN_METRICS)
    rows = query(conn, "SELECT * FROM annotation WHERE pass_index = 0 "
                       "ORDER BY created_at")
    # Ordered by when each version was first used, NOT alphabetically. Sorting the
    # strings put "1.0.0" before "legacy" and silently computed the difference
    # backwards, so a simulation in which anchoring plainly helped reported it hurting.
    # Version strings do not sort chronologically and never will.
    versions: list[str] = []
    for r in rows:
        if r["rubric_version"] not in versions:
            versions.append(r["rubric_version"])
    out: dict[str, Any] = {"versions": versions, "n_annotations": len(rows),
                           "metrics": {}, "comparable": len(versions) >= 2}
    if len(versions) < 2:
        out["verdict"] = (
            f"only one rubric version present ({versions[0] if versions else 'none'}). "
            f"The comparison needs the same responses rated under both scales; nothing "
            f"can be said about anchoring from one of them."
        )
        return out

    baseline, anchored = versions[0], versions[-1]
    for metric in metrics:
        per_version: dict[str, float] = {}
        per_run_agreement: dict[str, list[tuple[str, float]]] = {}
        for version in (baseline, anchored):
            units: dict[str, list[float]] = {}
            for r in rows:
                if r["rubric_version"] != version or r[metric] is None:
                    continue
                units.setdefault(r["run_id"], []).append(float(r[metric]))
            paired = {k: v for k, v in units.items() if len(v) >= 2}
            per_version[version] = krippendorff_alpha(paired, levels=[0, 1, 2, 3, 4, 5])
            # Per-run exact agreement is what the bootstrap resamples: alpha itself is a
            # ratio over the whole set and cannot be attributed to a single run.
            per_run_agreement[version] = [
                (run, 1.0 if len(set(vs)) == 1 else 0.0) for run, vs in paired.items()
            ]

        shared = ({r for r, _ in per_run_agreement[baseline]}
                  & {r for r, _ in per_run_agreement[anchored]})
        base_map = dict(per_run_agreement[baseline])
        anch_map = dict(per_run_agreement[anchored])
        deltas = [anch_map[r] - base_map[r] for r in sorted(shared)]
        # A mean, not a median. Each delta is a difference of two 0/1 indicators, so
        # its median is almost always exactly 0 or +-1 whatever the underlying rate, and
        # the interval came back (0.0, 0.0) on data where agreement had plainly moved.
        lo, hi = bootstrap_ci(deltas, n_resamples=n_resamples,
                              statistic="mean") if deltas else (
            float("nan"), float("nan"))

        alpha_lift = (per_version[anchored] - per_version[baseline]
                      if per_version[baseline] == per_version[baseline]
                      and per_version[anchored] == per_version[anchored] else None)
        out["metrics"][metric] = {
            "alpha": {k: (None if v != v else round(v, 4)) for k, v in per_version.items()},
            "alpha_lift": None if alpha_lift is None else round(alpha_lift, 4),
            "n_shared_runs": len(shared),
            "mean_agreement_lift": (round(sum(deltas) / len(deltas), 4)
                                    if deltas else None),
            "ci95": (None if lo != lo else round(lo, 4),
                     None if hi != hi else round(hi, 4)),
            "excludes_zero": bool(deltas) and (lo > 0 or hi < 0),
        }

    moved = [m for m, v in out["metrics"].items() if v["excludes_zero"]]
    out["baseline"], out["anchored"] = baseline, anchored
    out["verdict"] = (
        f"{len(moved)} of {len(metrics)} metric(s) show an agreement change whose "
        f"interval excludes zero: {', '.join(moved) if moved else '—'}"
    )
    return out
