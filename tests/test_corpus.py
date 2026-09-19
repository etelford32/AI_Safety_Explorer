"""The corpus must lint clean. This is the gate on every other result."""

from safety_explorer import lint
from safety_explorer.corpus import text_hash


def test_corpus_lints_clean(corpus):
    report = lint.run(corpus)
    assert report.clean, "\n".join(str(f) for f in report.errors)


def test_active_families_are_complete(corpus):
    for fam in corpus.families:
        if fam.is_active:
            assert {v.variant for v in fam.variants} == {"A", "B", "C", "D", "E", "F"}


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
        idx = {v.variant: v for v in fam.variants}
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
