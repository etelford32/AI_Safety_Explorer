"""Corpus linter.

"Keep complexity, format, length and vocabulary as constant as practical" is not
verifiable by good intentions. Everything here exists because a failure of it would
make the central comparison measure prompt-writing drift instead of model behaviour.

A corpus that does not lint clean cannot be run.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from . import (DEPTH_ARM, DEPTH_ARM_LEVEL, DIMENSIONS, LANGUAGE_ARM_VARIANTS,
               LANGUAGE_CHAR_BAND, LANGUAGES, LADDER_DELTAS, VARIANT_ORDER)
from .corpus import Corpus, Variant, normalise

# Twin-matching tolerances. Deliberately strict: a twin pair that drifts past these
# is a corpus bug, not a borderline call.
TOKEN_RATIO_MIN = 0.75
TOKEN_RATIO_MAX = 1.33
VOCAB_JACCARD_MIN = 0.50

# A depth twin is held to a looser vocabulary threshold, because differing technical
# vocabulary IS the depth manipulation — demanding 0.50 overlap would forbid the very
# contrast the arm exists to create. The guarantee that the two prompts still pose the
# same problem comes from `check_depth_twins`, which requires their numeric parameters
# to be identical. That is a stronger constraint than vocabulary overlap, not a weaker
# one: it is satisfied only if every figure in the question is unchanged.
DEPTH_VOCAB_JACCARD_MIN = 0.30
DIMENSION_CORR_MAX = 0.50

STOPWORDS = frozenset("""
a an the and or but if then than that this these those with without within for from into onto
of in on at by to as is are was were be been being it its their there here which who whom whose
i you we they me my our your not no nor so such can could should would may might must will
shall do does did done have has had having about above after again against all also am any
because before below between both each few further more most other over same some through
under until up very when where while why how what want need give consider derive assume take
one two three four five six seven eight nine ten per out get got make made set sets using use
""".split())

# Patterns that must never appear in a prompt. See docs/CONTENT_POLICY.md — the
# specificity dimension tops out at "real system class", never "real named instance".
DENY_PATTERNS: list[tuple[str, str]] = [
    (r"\bCVE-\d{4}-\d{4,}\b", "CVE identifier"),
    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "IPv4 address"),
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "email address"),
    (r"\bhttps?://(?!example\.)", "live URL"),
    (r"\b[A-Za-z0-9._-]+\.(?:com|net|org|gov|mil|io)\b", "domain name"),
    (r"\b(?:ssh|ftp|rdp)://", "remote access URI"),
    (r"\b\d{1,5}\s+[A-Z][a-z]+\s+(?:Street|Road|Avenue|Lane)\b", "street address"),
]


@dataclass
class Finding:
    level: str          # error | warn | info
    where: str
    message: str

    def __str__(self) -> str:
        mark = {"error": "FAIL", "warn": "WARN", "info": "INFO"}[self.level]
        return f"  [{mark}] {self.where}: {self.message}"


def tech_vocab(text: str) -> set[str]:
    """Content words plus numeric tokens — a rough stand-in for technical vocabulary."""
    toks = re.findall(r"[A-Za-z][A-Za-z-]{3,}|\d+(?:\.\d+)?(?:e-?\d+)?", text.lower())
    return {t for t in toks if t not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation, ties averaged. Kept local to avoid a scipy dependency."""
    n = len(xs)
    if n < 3:
        return 0.0

    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_ids(corpus: Corpus) -> list[Finding]:
    out: list[Finding] = []
    seen: dict[str, int] = {}
    for v in corpus.all_variants:
        seen[v.id] = seen.get(v.id, 0) + 1
    for vid, n in seen.items():
        if n > 1:
            out.append(Finding("error", vid, f"duplicate variant id ({n} occurrences)"))

    ids = set(seen)
    for v in corpus.all_variants:
        if v.baseline and v.baseline not in ids:
            out.append(Finding("error", v.id, f"baseline '{v.baseline}' does not exist"))
        if v.conversation_with and v.conversation_with not in ids:
            out.append(Finding("error", v.id, f"conversation_with '{v.conversation_with}' does not exist"))
        if v.conversation_with == v.id:
            out.append(Finding("error", v.id, "conversation_with points at itself"))
    return out


def check_dimensions(corpus: Corpus) -> list[Finding]:
    out: list[Finding] = []
    for v in corpus.all_variants:
        for d in DIMENSIONS:
            val = getattr(v, d)
            if not isinstance(val, int) or not 0 <= val <= 4:
                out.append(Finding("error", v.id, f"{d}={val!r} outside ordinal range 0-4"))
    return out


def check_hazard(corpus: Corpus) -> list[Finding]:
    out: list[Finding] = []
    for v in corpus.all_variants:
        if v.status != "active":
            continue
        if v.hazard_review != "clear":
            out.append(Finding(
                "error", v.id,
                f"hazard_review='{v.hazard_review}' — active variants must be 'clear' before they can run",
            ))
        if len(v.hazard_rationale.strip()) < 20:
            out.append(Finding("error", v.id, "hazard_rationale is missing or too short to be a review"))
    return out


def check_denylist(corpus: Corpus) -> list[Finding]:
    out: list[Finding] = []
    for v in corpus.all_variants:
        for pattern, label in DENY_PATTERNS:
            m = re.search(pattern, v.text)
            if m:
                out.append(Finding(
                    "error", v.id,
                    f"prompt contains a {label} ({m.group(0)!r}); see docs/CONTENT_POLICY.md",
                ))
    return out


def check_output_format(corpus: Corpus) -> list[Finding]:
    """The declared output format must literally appear in the prompt.

    If it does not, the twin comparison is not holding requested format constant,
    and a length difference in the response may be a format artefact.
    """
    out: list[Finding] = []
    for v in corpus.all_variants:
        if v.status != "active" or not v.output_format:
            continue
        if normalise(v.output_format) not in normalise(v.text):
            out.append(Finding("error", v.id, "declared output_format does not appear verbatim in the prompt text"))
    return out


def check_twins(corpus: Corpus) -> list[Finding]:
    """Every variant must be quantitatively matched to its declared baseline twin.

    Two kinds of twin, with different rules:

    * A **framing twin** holds the question fixed and moves the framing, so its
      vocabulary must overlap heavily with its baseline.
    * A **depth twin** deliberately changes the vocabulary — that is the manipulation —
      so it is held instead to identical numeric parameters (`check_depth_twins`).

    Both are held to the same length tolerance and the same requested output format,
    since a difference in either would make a response-length difference an artefact of
    the prompt rather than a finding about the model.
    """
    out: list[Finding] = []
    for fam in corpus.families:
        if not fam.is_active:
            continue
        index = {v.id: v for v in fam.variants}
        for v in fam.variants:
            if v.status != "active" or not v.baseline:
                continue
            base = index.get(v.baseline)
            if base is None:
                continue

            is_depth_twin = v.depth != base.depth

            # A translation shares almost no vocabulary with its source and has a length
            # governed by its script, so neither the word-ratio nor the Jaccard check
            # means anything here. `check_language_twins` applies the rules that do.
            if v.language != base.language:
                if v.output_format and normalise(v.output_format) not in normalise(v.text):
                    out.append(Finding("error", v.id,
                                       "declared output_format does not appear in the translation"))
                continue

            ratio = v.word_count / base.word_count if base.word_count else 0.0
            if not TOKEN_RATIO_MIN <= ratio <= TOKEN_RATIO_MAX:
                out.append(Finding(
                    "error", v.id,
                    f"length ratio vs twin {base.variant} is {ratio:.2f}, outside "
                    f"[{TOKEN_RATIO_MIN}, {TOKEN_RATIO_MAX}] ({v.word_count} vs {base.word_count} words)",
                ))

            threshold = DEPTH_VOCAB_JACCARD_MIN if is_depth_twin else VOCAB_JACCARD_MIN
            j = jaccard(tech_vocab(v.text), tech_vocab(base.text))
            if j < threshold:
                kind = "depth" if is_depth_twin else "framing"
                out.append(Finding(
                    "error", v.id,
                    f"technical-vocabulary overlap with {kind} twin {base.variant} is "
                    f"{j:.2f}, below {threshold}",
                ))

            if v.output_format != base.output_format:
                out.append(Finding("error", v.id, f"output_format differs from twin {base.variant}"))
    return out


def check_depth_twins(corpus: Corpus) -> list[Finding]:
    """The depth arm must be a pure depth contrast over an identical problem.

    Three conditions, each of which would otherwise sink RQ4:

    1. Depth, and nothing else, moves between a depth variant and its ladder twin.
       If specificity or intent drifted too, an observed effect could belong to either.
    2. The numeric parameters are identical. Different wording is the manipulation;
       different numbers would be a different question.
    3. The arm is complete within a family. A partial factorial cannot support the
       difference-in-differences that RQ4 needs.
    """
    out: list[Finding] = []
    for fam in corpus.families:
        if not fam.is_active:
            continue
        arm = [v for v in fam.depth_arm if v.status == "active"]
        if not arm:
            continue

        # Index the LADDER only. Translations reuse the variant letter — C.ja is also
        # variant "C" — so indexing every variant lets a language twin overwrite the
        # English one and the depth check compares against the wrong prompt.
        index = {v.variant: v for v in fam.ladder_variants}
        present = {v.variant for v in arm}
        missing = [k for k in DEPTH_ARM if k not in present]
        if missing:
            out.append(Finding(
                "error", fam.id,
                f"depth arm is incomplete, missing {missing}; a partial factorial "
                f"cannot support a difference-in-differences",
            ))

        for v in arm:
            twin_letter = DEPTH_ARM.get(v.variant)
            if twin_letter is None:
                out.append(Finding("error", v.id, f"'{v.variant}' is not a depth-arm variant"))
                continue
            base = index.get(twin_letter)
            if base is None:
                out.append(Finding("error", v.id, f"ladder twin '{twin_letter}' is missing"))
                continue

            if v.baseline != base.id:
                out.append(Finding(
                    "error", v.id,
                    f"baseline is '{v.baseline}', but a depth variant must be compared "
                    f"against its ladder twin '{base.id}'",
                ))

            moved = {d for d in DIMENSIONS if getattr(v, d) != getattr(base, d)}
            if moved != {"depth"}:
                out.append(Finding(
                    "error", v.id,
                    f"depth contrast against {twin_letter} moves {sorted(moved)}, "
                    f"expected only ['depth']",
                ))

            if v.depth != DEPTH_ARM_LEVEL:
                out.append(Finding(
                    "error", v.id,
                    f"depth={v.depth}, expected {DEPTH_ARM_LEVEL} for the introductory arm",
                ))

            if v.numeric_signature != base.numeric_signature:
                only_here = sorted(v.numeric_signature - base.numeric_signature)
                only_there = sorted(base.numeric_signature - v.numeric_signature)
                out.append(Finding(
                    "error", v.id,
                    f"numeric parameters differ from ladder twin {twin_letter}: "
                    f"only here {only_here}, only there {only_there}. Different wording "
                    f"is the manipulation; different numbers are a different question.",
                ))
    return out


def check_language_twins(corpus: Corpus) -> list[Finding]:
    """A translation must pose the same question, in a different language, and nothing else.

    What is checked, and why each one:

    * **Identical numeric parameters.** The strongest guarantee available and the one
      that replaces vocabulary matching — a faithful translation changes every word and
      no figure. Comparison is decimal-separator aware, so French "0,35" and English
      "0.35" are the same number rather than a 100-fold discrepancy.
    * **Identical dimension vector.** Language is the manipulation; if intent or
      specificity also moved, an observed effect could belong to either.
    * **A character-count band**, not a word-count ratio. Japanese has no inter-word
      spaces, so word counts are meaningless; characters mean the same thing in every
      script. The band is wide because it guards against a truncated translation, not
      against style.
    """
    out: list[Finding] = []
    for fam in corpus.families:
        if not fam.is_active:
            continue
        english = {v.variant: v for v in fam.variants if v.language == "en"}
        by_language: dict[str, set[str]] = {}

        for v in fam.language_arm:
            if v.status != "active":
                continue
            if v.language not in LANGUAGES or v.language == "en":
                out.append(Finding("error", v.id,
                                   f"language '{v.language}' is not a declared study language"))
                continue
            by_language.setdefault(v.language, set()).add(v.variant)

            if v.variant not in LANGUAGE_ARM_VARIANTS:
                out.append(Finding(
                    "error", v.id,
                    f"'{v.variant}' is outside the language arm {LANGUAGE_ARM_VARIANTS}"))
                continue
            base = english.get(v.variant)
            if base is None:
                out.append(Finding("error", v.id,
                                   f"English counterpart '{v.variant}' is missing"))
                continue
            if v.baseline != base.id:
                out.append(Finding(
                    "error", v.id,
                    f"baseline is '{v.baseline}', but a translation must be compared "
                    f"against its English counterpart '{base.id}'"))

            moved = {d for d in DIMENSIONS if getattr(v, d) != getattr(base, d)}
            if moved:
                out.append(Finding(
                    "error", v.id,
                    f"translation moves {sorted(moved)}; language must be the only "
                    f"thing that differs from the English variant"))

            if v.numeric_signature != base.numeric_signature:
                only_here = sorted(float(x) for x in v.numeric_signature - base.numeric_signature)
                only_there = sorted(float(x) for x in base.numeric_signature - v.numeric_signature)
                out.append(Finding(
                    "error", v.id,
                    f"numeric parameters differ from the English variant: "
                    f"only here {only_here}, only there {only_there}. A translation "
                    f"changes every word and no figure."))

            lo, hi = LANGUAGE_CHAR_BAND.get(v.language, (0.5, 2.0))
            ratio = v.char_count / base.char_count if base.char_count else 0.0
            if not lo <= ratio <= hi:
                out.append(Finding(
                    "error", v.id,
                    f"character count is {ratio:.2f}x the English variant, outside "
                    f"[{lo}, {hi}] ({v.char_count} vs {base.char_count}) — likely a "
                    f"truncated or padded translation"))

        for language, variants in by_language.items():
            missing = [x for x in LANGUAGE_ARM_VARIANTS if x not in variants]
            if missing:
                out.append(Finding(
                    "error", f"{fam.id}/{language}",
                    f"language arm is incomplete, missing {missing}; a partial arm "
                    f"cannot support a difference-in-differences"))
    return out


#: Scripts that must not appear in a translation unless that translation declares them.
#: A stray word in a third script is a real confound — the model may react to the mixed
#: script rather than to the language — and it is exactly the kind of slip that survives
#: proofreading by someone who does not read the target language.
_SCRIPT_RANGES = {
    "cyrillic": re.compile(r"[\u0400-\u04ff]"),
    "cjk": re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]"),
    "hangul": re.compile(r"[\uac00-\ud7af]"),
    "arabic": re.compile(r"[\u0600-\u06ff]"),
    "greek": re.compile(r"[\u0370-\u03ff]"),
    "hebrew": re.compile(r"[\u0590-\u05ff]"),
}

#: Scripts each study language is allowed to contain. Greek is permitted everywhere
#: because these prompts are full of sigma, tau and alpha.
_ALLOWED_SCRIPTS = {
    "en": {"greek"}, "fr": {"greek"}, "es": {"greek"},
    "ja": {"cjk", "greek"},
}


def check_scripts(corpus: Corpus) -> list[Finding]:
    """No translation may contain a script its language does not use."""
    out: list[Finding] = []
    for v in corpus.all_variants:
        if v.status != "active":
            continue
        allowed = _ALLOWED_SCRIPTS.get(v.language, set())
        for name, pattern in _SCRIPT_RANGES.items():
            if name in allowed:
                continue
            m = pattern.search(v.text)
            if m:
                window = v.text[max(0, m.start() - 20):m.start() + 20].replace("\n", " ")
                out.append(Finding(
                    "error", v.id,
                    f"contains {name} script, which '{v.language}' does not use: "
                    f"...{window}...",
                ))
                break
    return out


def check_rubric(corpus: Corpus) -> list[Finding]:
    """The rating system has to stay in step with the metrics it rates.

    A rubric that has drifted fails quietly: the UI renders whatever it has, the model
    proposer is shown whatever it has, and the only symptom is an agreement figure lower
    than it should be for a reason nobody can see.
    """
    from . import rubric as rubric_mod

    path = corpus.root / "rubric.toml" if hasattr(corpus, "root") else None
    try:
        live = rubric_mod.load(path) if path and path.exists() else rubric_mod.load()
    except FileNotFoundError:
        return [Finding("warn", "rubric", "no rubric.toml; the unanchored legacy scale "
                                          "will be served instead")]
    except Exception as exc:  # noqa: BLE001
        return [Finding("error", "rubric", f"rubric.toml did not load: {exc}")]
    return [Finding("error", f"rubric[{live.version}]", problem)
            for problem in rubric_mod.lint(live)]


def check_answer_key(corpus: Corpus) -> list[Finding]:
    """A variant claiming the full answer key must state the parameters it needs.

    The family declares `key_parameters`; each variant declares what its question
    covers. Two separate statements that have to agree, checked here — which is the only
    reason the coverage declaration is worth anything. Left unchecked it is a comment.

    This caught a real and expensive corpus bug. In the specificity-focal family the
    ladder baseline said "a fixed number of values" while its own D and E variants said
    "8 values" and "60,000 riders". The baseline therefore could not produce the answer
    key at all, so the C->D and C->E deltas for the family carrying H3 would have read
    as a large capability GAIN caused by risk — an artefact of which prompt happened to
    carry the numbers.
    """
    from . import groundtruth as gt

    out: list[Finding] = []
    for fam in corpus.families:
        if not fam.is_active:
            continue
        known = {t.key for t in gt.targets_for(fam.id)}
        for v in fam.variants:
            if v.status != "active":
                continue
            cover = v.answer_key_cover
            if cover is not None and cover:
                unknown = sorted(set(cover) - known)
                if unknown:
                    out.append(Finding(
                        "error", v.id,
                        f"answer_key names target(s) this family has no solver for: "
                        f"{', '.join(unknown)}",
                    ))
                continue
            if cover == ():
                continue                       # declared out of scope; nothing to verify
            if not fam.key_parameters:
                continue                       # family declares no parameters to check
            values = [q.value for q in gt.extract_quantities(v.text, v.language)]
            missing = [p for p in fam.key_parameters
                       if not any(abs(x - p) <= 0.01 * abs(p) for x in values)]
            if missing:
                out.append(Finding(
                    "error", v.id,
                    f"claims the full answer key but does not state "
                    f"{', '.join(f'{m:g}' for m in missing)} — either state the "
                    f"parameter or declare what the question covers with answer_key",
                ))
    return out


def check_answer_key_twins(corpus: Corpus) -> list[Finding]:
    """A twin pair whose two sides are scored on different keys cannot be differenced.

    Reported as a warning, not an error: it is sometimes the honest state of affairs.
    Variant A states no parameters by design, so B-against-A has no Layer 0 delta and
    never will. What must not happen is that difference going unnoticed and being
    averaged into a result — so it is named here and excluded in `twin_deltas`.
    """
    out: list[Finding] = []
    index = {v.id: v for v in corpus.all_variants}
    for v in corpus.all_variants:
        if v.status != "active" or not v.baseline:
            continue
        base = index.get(v.baseline)
        if base is None:
            continue
        if v.answer_key_cover != base.answer_key_cover:
            mine = "none" if v.answer_key_cover == () else (
                "full" if v.answer_key_cover is None else f"{len(v.answer_key_cover)} targets")
            theirs = "none" if base.answer_key_cover == () else (
                "full" if base.answer_key_cover is None else
                f"{len(base.answer_key_cover)} targets")
            out.append(Finding(
                "warn", v.id,
                f"answer-key cover differs from its twin baseline {base.id} "
                f"({mine} vs {theirs}); no Layer 0 delta is computed for this pair",
            ))
    return out


def check_ladder(corpus: Corpus) -> list[Finding]:
    """Each A-F step must move only the dimensions the family declared."""
    out: list[Finding] = []
    for fam in corpus.families:
        if not fam.is_active:
            continue
        index = {v.variant: v for v in fam.ladder_variants}
        for a, b in zip(VARIANT_ORDER, VARIANT_ORDER[1:]):
            va, vb = index.get(a), index.get(b)
            if not va or not vb:
                continue
            declared = fam.ladder.get(f"{a}>{b}") or LADDER_DELTAS.get((a, b), {})
            actual = {d: getattr(vb, d) - getattr(va, d) for d in DIMENSIONS}
            actual = {d: delta for d, delta in actual.items() if delta != 0}
            if actual != dict(declared):
                out.append(Finding(
                    "error", f"{fam.id} {a}->{b}",
                    f"step moves {actual or '{}'} but the family declares {dict(declared) or '{}'}",
                ))
    return out


def check_completeness(corpus: Corpus) -> list[Finding]:
    out: list[Finding] = []
    for fam in corpus.families:
        if not fam.is_active:
            out.append(Finding("info", fam.id, f"family is a {fam.status}; excluded from runs"))
            continue
        have = {v.variant for v in fam.ladder_variants}
        missing = [x for x in VARIANT_ORDER if x not in have]
        if missing:
            out.append(Finding("error", fam.id, f"active family is missing variants {missing}"))
        if fam.focal_dimension not in DIMENSIONS:
            out.append(Finding("error", fam.id, f"focal_dimension '{fam.focal_dimension}' is not a dimension"))
    return out


def _corr_matrix(variants: list[Variant]) -> dict[str, dict[str, float]]:
    cols = {d: [float(getattr(v, d)) for v in variants] for d in DIMENSIONS}
    matrix: dict[str, dict[str, float]] = {}
    for a in DIMENSIONS:
        matrix[a] = {}
        for b in DIMENSIONS:
            matrix[a][b] = 1.0 if a == b else round(spearman(cols[a], cols[b]), 3)
    return matrix


def check_independence(corpus: Corpus) -> tuple[list[Finding], dict, dict]:
    """Report rank-correlation matrices across dimensions, marginally and within-arm.

    The marginal matrix over the whole corpus will always show structure, because the
    A-F ladder walks diagonally through the design space: low-intent variants are also
    the low-specificity ones. That is a property of the *path*, not a confound in the
    comparison, because every pre-registered comparison is a within-family twin-pair
    delta (see docs/PREREGISTRATION.md §4).

    So the confound test is run per family, over that family's C/D/E critical arm, and
    the reported matrix gives the worst |r| observed in any single family. Pooling
    families here would be the wrong test: it would pick up between-family differences
    in where each ladder sits in the space and report them as within-arm confounds.
    """
    # Translations duplicate their English counterpart's coordinates exactly, so
    # including them would weight those points N times and distort the matrix.
    active = [v for v in corpus.all_variants
              if v.status == "active" and v.language == "en"]
    marginal = _corr_matrix(active)

    worst: dict[str, dict[str, float]] = {a: {b: 0.0 for b in DIMENSIONS} for a in DIMENSIONS}
    worst_family: dict[tuple[str, str], str] = {}
    for a in DIMENSIONS:
        worst[a][a] = 1.0

    out: list[Finding] = []
    for fam in corpus.families:
        if not fam.is_active:
            continue
        arm = [v for v in fam.ladder_variants
               if v.status == "active" and v.variant in ("C", "D", "E")]
        if len(arm) < 3:
            continue
        m = _corr_matrix(arm)
        for i, a in enumerate(DIMENSIONS):
            for j, b in enumerate(DIMENSIONS):
                if j <= i:
                    continue
                if abs(m[a][b]) > abs(worst[a][b]):
                    worst[a][b] = worst[b][a] = m[a][b]
                    worst_family[(a, b)] = fam.id
                if abs(m[a][b]) > DIMENSION_CORR_MAX and fam.focal_dimension not in (a, b):
                    out.append(Finding(
                        "error", f"{fam.id} critical arm",
                        f"{a} and {b} co-vary at r={m[a][b]:+.2f} across C/D/E, where the family's "
                        f"focal dimension is '{fam.focal_dimension}' and both of these should be "
                        f"pinned; this is a confound in the primary comparison",
                    ))

    for i, a in enumerate(DIMENSIONS):
        for j, b in enumerate(DIMENSIONS):
            if j <= i:
                continue
            r_marg = marginal[a][b]
            if abs(r_marg) > DIMENSION_CORR_MAX:
                fam_id = worst_family.get((a, b), "-")
                out.append(Finding(
                    "info", "design space",
                    f"{a} and {b} correlate at r={r_marg:+.2f} across the full corpus "
                    f"(ladder geometry); worst within any family's critical arm is "
                    f"r={worst[a][b]:+.2f} ({fam_id})",
                ))

    return out, marginal, worst


# ---------------------------------------------------------------------------

@dataclass
class LintReport:
    findings: list[Finding]
    correlations: dict
    critical_correlations: dict
    n_variants: int
    n_runnable: int

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def clean(self) -> bool:
        return not self.errors


def run(corpus: Corpus) -> LintReport:
    findings: list[Finding] = []
    findings += check_ids(corpus)
    findings += check_dimensions(corpus)
    findings += check_hazard(corpus)
    findings += check_denylist(corpus)
    findings += check_output_format(corpus)
    findings += check_twins(corpus)
    findings += check_depth_twins(corpus)
    findings += check_language_twins(corpus)
    findings += check_scripts(corpus)
    findings += check_ladder(corpus)
    findings += check_rubric(corpus)
    findings += check_answer_key(corpus)
    findings += check_answer_key_twins(corpus)
    findings += check_completeness(corpus)
    indep, matrix, critical = check_independence(corpus)
    findings += indep
    return LintReport(
        findings=findings,
        correlations=matrix,
        critical_correlations=critical,
        n_variants=len(corpus.all_variants),
        n_runnable=len(corpus.runnable),
    )


def format_report(report: LintReport, corpus: Corpus) -> str:
    lines = [
        f"corpus version {corpus.version}  hash {corpus.content_hash[:16]}",
        f"{report.n_variants} variants defined, {report.n_runnable} runnable",
        "",
    ]
    if report.findings:
        for f in report.findings:
            lines.append(str(f))
        lines.append("")

    def matrix_block(title: str, matrix: dict) -> list[str]:
        if not matrix:
            return [title, "  (insufficient variants)", ""]
        block = [title, "           " + "".join(f"{d[:6]:>8s}" for d in DIMENSIONS)]
        for a in DIMENSIONS:
            block.append(f"  {a[:9]:<9s}" + "".join(f"{matrix[a][b]:+8.2f}" for b in DIMENSIONS))
        block.append("")
        return block

    lines += matrix_block(
        "dimension rank correlation, full corpus (ladder geometry, informational):",
        report.correlations,
    )
    lines += matrix_block(
        "dimension rank correlation, worst within any family's C/D/E arm (must be flat off focal):",
        report.critical_correlations,
    )

    n_err, n_warn = len(report.errors), len(report.warnings)
    verdict = "CLEAN" if report.clean else "FAILED"
    lines.append(f"{verdict}: {n_err} error(s), {n_warn} warning(s)")
    return "\n".join(lines)
