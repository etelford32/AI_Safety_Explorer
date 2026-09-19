"""The corpus must lint clean. This is the gate on every other result."""

from safety_explorer import lint
from safety_explorer.corpus import text_hash


def test_corpus_lints_clean(corpus):
    report = lint.run(corpus)
    assert report.clean, "\n".join(str(f) for f in report.errors)


def test_active_families_are_complete(corpus):
    for fam in corpus.families:
        if fam.is_active:
            assert {v.variant for v in fam.ladder_variants} == {"A", "B", "C", "D", "E", "F"}


def test_stubs_are_not_runnable(corpus):
    """An unfinished prompt must never be able to reach a model."""
    runnable = {v.id for v in corpus.runnable}
    for v in corpus.all_variants:
        if v.status != "active" or v.hazard_review != "clear":
            assert v.id not in runnable


def test_every_runnable_variant_has_a_hazard_review(corpus):
    for v in corpus.runnable:
        assert v.hazard_review == "clear"
        assert len(v.hazard_rationale) >= 20


def test_critical_arm_moves_exactly_one_dimension(corpus):
    """C->D and D->E must move only the family's focal dimension.

    This is the whole experiment: if anything else moves, an observed effect cannot be
    attributed to the focal dimension.
    """
    from safety_explorer import DIMENSIONS

    for fam in corpus.families:
        if not fam.is_active:
            continue
        idx = {v.variant: v for v in fam.ladder_variants}
        for a, b in (("C", "D"), ("D", "E")):
            moved = {
                d for d in DIMENSIONS
                if getattr(idx[b], d) != getattr(idx[a], d)
            }
            assert moved == {fam.focal_dimension}, (
                f"{fam.id} {a}->{b} moved {moved}, expected only {fam.focal_dimension}"
            )


def test_twin_lengths_are_matched(corpus):
    from safety_explorer.lint import TOKEN_RATIO_MAX, TOKEN_RATIO_MIN

    for fam in corpus.families:
        if not fam.is_active:
            continue
        idx = {v.id: v for v in fam.variants}
        for v in fam.variants:
            if not v.baseline:
                continue
            ratio = v.word_count / idx[v.baseline].word_count
            assert TOKEN_RATIO_MIN <= ratio <= TOKEN_RATIO_MAX, f"{v.id} ratio {ratio:.2f}"


def test_controls_declare_both_arms(corpus):
    arms = {c.control_arm for c in corpus.controls}
    assert arms == {"alarming_benign", "innocuous_consequential"}
    # The two arms must disagree about whether a full answer is appropriate, or they
    # are not testing opposite failure modes.
    for c in corpus.controls:
        assert c.expected_benign == (c.control_arm == "alarming_benign")


def test_prompt_hash_is_stable_under_whitespace(corpus):
    v = corpus.runnable[0]
    assert text_hash(v.text) == text_hash(v.text.replace("\n", "  \n "))


# --- depth arm (v0.2) ------------------------------------------------------

def test_depth_arm_is_complete_for_active_families(corpus):
    from safety_explorer import DEPTH_ARM

    for fam in corpus.families:
        if not fam.is_active:
            continue
        assert {v.variant for v in fam.depth_arm} == set(DEPTH_ARM), (
            f"{fam.id} has an incomplete depth arm; a partial factorial cannot support "
            f"a difference-in-differences"
        )


def test_depth_contrast_moves_only_depth(corpus):
    """The point of the arm. If anything else moves, RQ4 is unanswerable."""
    from safety_explorer import DEPTH_ARM, DIMENSIONS

    for fam in corpus.families:
        index = {v.variant: v for v in fam.variants}
        for intro, expert in DEPTH_ARM.items():
            if intro not in index:
                continue
            a, b = index[intro], index[expert]
            moved = {d for d in DIMENSIONS if getattr(a, d) != getattr(b, d)}
            assert moved == {"depth"}, f"{a.id} vs {b.id} moved {moved}"


def test_depth_twins_pose_an_identical_problem(corpus):
    """Different wording is the manipulation; different numbers are a different question."""
    from safety_explorer import DEPTH_ARM

    for fam in corpus.families:
        index = {v.variant: v for v in fam.variants}
        for intro, expert in DEPTH_ARM.items():
            if intro not in index:
                continue
            a, b = index[intro], index[expert]
            assert a.numeric_signature == b.numeric_signature, (
                f"{a.id} and {b.id} differ in numeric parameters: "
                f"{sorted(a.numeric_signature ^ b.numeric_signature)}"
            )


def test_depth_twins_actually_differ_in_register(corpus):
    """A depth twin that reads identically has not manipulated anything.

    The framing-twin threshold is 0.50 overlap; a genuine depth contrast must come in
    below that, or the prompts differ only cosmetically.
    """
    from safety_explorer import DEPTH_ARM
    from safety_explorer.lint import DEPTH_VOCAB_JACCARD_MIN, VOCAB_JACCARD_MIN, jaccard, tech_vocab

    for fam in corpus.families:
        index = {v.variant: v for v in fam.variants}
        for intro, expert in DEPTH_ARM.items():
            if intro not in index:
                continue
            j = jaccard(tech_vocab(index[intro].text), tech_vocab(index[expert].text))
            assert j < VOCAB_JACCARD_MIN, f"{index[intro].id} reads too much like its twin (j={j:.2f})"
            assert j >= DEPTH_VOCAB_JACCARD_MIN, f"{index[intro].id} has drifted too far (j={j:.2f})"


def test_depth_arm_carries_no_expertise_claim(corpus):
    """Depth is register, not credentials.

    A stated-expertise claim is a social/credentialing manipulation. Mixing it into the
    depth arm would mean any observed effect could be deference to authority rather
    than a response to the level of the question.
    """
    import re

    banned = re.compile(
        r"\b(?:i am|i'm) an? (?:expert|professor|phd|doctor|specialist|researcher)\b"
        r"|\bmy (?:phd|doctorate|credentials)\b"
        r"|\bi (?:have|hold) an? (?:phd|doctorate)\b"
        r"|\bi (?:have not|haven't|don't|do not) (?:done|know|understand)\b"
        r"|\bkeep it (?:simple|basic|introductory)\b"
        r"|\bi'?m new to\b|\bbeginner\b|\bno background\b",
        re.IGNORECASE,
    )
    for fam in corpus.families:
        for v in fam.depth_arm:
            assert not banned.search(v.text), (
                f"{v.id} states the requester's expertise level; depth must be "
                f"manipulated through register and formalism only"
            )
