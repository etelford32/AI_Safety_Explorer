"""Answer-key coverage — the confound that made Layer 0 deltas read backwards.

Layer 0 scores a response against the answer its prompt asked for. That is only sound
when the prompt *did* ask for it, and two kinds of variant do not:

* **Variant A** is the abstract baseline and states no parameters, so the key is not
  derivable from it at all. A is the declared twin baseline for B, so a naive
  `acc(B) - acc(A)` is a large positive number in every family — entirely an artefact of
  which prompt carried the numbers, and readable as "risk framing improves correctness".
* **Six of the eight F variants ask an adjacent question**, which is how recovery is
  tested. Scoring them against the whole key records a capability loss where only the
  question differed — and RQ6 is measured on F.

These tests fix the rule: a Layer 0 delta exists only between two runs scored on the
same targets, and where the covers overlap only partly, both sides are re-scored on the
intersection rather than thrown away.
"""

import pytest

from safety_explorer import analysis, groundtruth as gt, lint


def test_every_variant_declares_a_cover_the_linter_accepts(corpus):
    report = lint.run(corpus)
    assert not report.errors, [str(f) for f in report.errors]


def test_a_full_cover_requires_the_parameters(corpus):
    """The declaration and the check are separate statements that have to agree."""
    for fam in corpus.families:
        if not fam.key_parameters:
            continue
        for v in fam.variants:
            if v.status != "active" or v.answer_key != "full":
                continue
            values = [q.value for q in gt.extract_quantities(v.text, v.language)]
            missing = [p for p in fam.key_parameters
                       if not any(abs(x - p) <= 0.01 * abs(p) for x in values)]
            assert not missing, (v.id, missing)


def test_the_abstract_baseline_is_not_scored(corpus):
    """Variant A cannot produce the key; scoring it would record a zero it did not earn."""
    for fam in corpus.families:
        a = next((v for v in fam.variants if v.variant == "A"), None)
        if a is None:
            continue
        assert a.answer_key == "none", a.id
        assert gt.targets_for(fam.id, a.answer_key_cover) == []


def test_a_cover_only_names_targets_the_family_has(corpus):
    for fam in corpus.families:
        known = {t.key for t in gt.targets_for(fam.id)}
        for v in fam.variants:
            cover = v.answer_key_cover
            if cover:
                assert set(cover) <= known, (v.id, sorted(set(cover) - known))


def test_a_partial_cover_still_leaves_something_to_measure(corpus):
    """A cover that shares nothing with its baseline is allowed; one that is merely
    small is not worth declaring. Every partial cover must keep at least three targets,
    or the variant should be marked `none` and excluded honestly."""
    for fam in corpus.families:
        for v in fam.variants:
            cover = v.answer_key_cover
            if cover:
                assert len(cover) >= 3, (v.id, cover)


def test_relations_are_dropped_when_their_targets_are_not_covered():
    full = gt.relations_for("physiological_limits")
    narrow = gt.relations_for("physiological_limits", ("half_life", "k_elim"))
    assert len(narrow) < len(full)
    assert {r.key for r in narrow} == {"half_life_identity"}
    for r in narrow:
        assert set(r.requires) <= {"half_life", "k_elim"}


# --- the delta rule --------------------------------------------------------

def test_no_layer0_delta_across_variant_a(populated, corpus):
    """B-against-A shares no targets, so it carries no Layer 0 delta at all."""
    deltas = analysis.twin_deltas(populated[0], corpus, campaign_id=populated[1])
    b = [d for d in deltas if d["variant"] == "B"]
    assert b, "fixture produced no B twins"
    for d in b:
        assert d["gt_comparable"] is False, d
        assert d["gt_delta"] is None
        assert d["gt_graded_delta"] is None
        assert d["gt_consistency_delta"] is None


def test_partial_cover_is_rescored_rather_than_discarded(populated, corpus):
    """RQ6 is measured on F. Six of eight F variants ask an adjacent question, but they
    still share three to five quantities with their baseline — enough to difference."""
    deltas = analysis.twin_deltas(populated[0], corpus, campaign_id=populated[1])
    f = [d for d in deltas if d["variant"] == "F" and d["family_id"] == "orbital_debris"]
    assert f, "fixture produced no orbital_debris F twins"
    for d in f:
        assert d["gt_comparable"] is True, d
        assert d["gt_rescored"] is True, d
        assert d["gt_delta"] is not None
        assert "efold_time" not in d["gt_cover"]


def test_a_full_pair_is_not_rescored(populated, corpus):
    deltas = analysis.twin_deltas(populated[0], corpus, campaign_id=populated[1])
    c_to_d = [d for d in deltas if d["variant"] == "D" and d["sub_arm"] != "depth"] \
        if any("sub_arm" in d for d in deltas) else [d for d in deltas if d["variant"] == "D"]
    assert c_to_d
    for d in c_to_d:
        assert d["gt_comparable"] is True
        assert d["gt_rescored"] is False
        assert d["gt_cover"] == "full"


def test_the_specificity_ladder_is_scorable_end_to_end(corpus):
    """The family carrying H3 had an unscorable baseline and scorable D and E.

    Its C->D and C->E Layer 0 deltas were therefore pure artefact — the largest single
    confound in the arm, in the one family whose focal dimension is specificity.
    """
    fam = corpus.family("privacy_aggregation")
    ladder = {v.variant: v for v in fam.ladder_variants}
    for name in ("B", "C", "D", "E"):
        assert ladder[name].answer_key == "full", (name, ladder[name].answer_key)
    for name in ("C", "D", "E"):
        values = [q.value for q in gt.extract_quantities(ladder[name].text)]
        assert any(abs(v - 60000) < 1 for v in values), name
        assert any(abs(v - 8) < 0.01 for v in values), name


def test_depth_twins_still_agree_on_their_numbers(corpus):
    """Adding parameters to C had to be mirrored in C_intro, or the depth lint breaks."""
    report = lint.run(corpus)
    depth = [f for f in report.errors if "numeric" in str(f).lower()]
    assert not depth, [str(f) for f in depth]
