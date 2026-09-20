"""The rating system: anchored, versioned, and identical on both sides.

Before `corpus/rubric.toml` the scale was pinned at 0 and 5 and undefined in between,
so "3" meant whatever each rater privately meant — the largest avoidable source of
disagreement in an ordinal scale, and one that was silently costing alpha. These tests
hold the anchors to being decidable and hold the two surfaces to using the same ones.
"""

import pytest

from safety_explorer import HUMAN_METRICS, INVERTED_METRICS, annotate
from safety_explorer import rubric as rb


@pytest.fixture(scope="module")
def live():
    return rb.load()


def test_the_shipped_rubric_lints_clean(live):
    assert rb.lint(live) == []


def test_every_rated_metric_has_a_rubric(live):
    """A metric an annotator is asked to score with no anchors is scored on vibes."""
    assert set(live.metrics) == set(HUMAN_METRICS)


def test_every_level_is_anchored(live):
    """The point of the file. An unanchored level is not a measurement."""
    for metric in live:
        assert len(metric.levels) == rb.LEVELS, metric.key
        for i, text in enumerate(metric.levels):
            assert text.strip(), (metric.key, i)
            assert len(text.split()) >= 3, (metric.key, i, text)


def test_anchors_within_a_metric_are_distinguishable(live):
    for metric in live:
        assert len(set(metric.levels)) == len(metric.levels), metric.key


def test_inversion_agrees_with_the_aggregation_table(live):
    """If these disagree, aggregation flips a metric's sign and nothing says so."""
    for metric in live:
        assert metric.inverted == (metric.key in INVERTED_METRICS), metric.key


def test_both_surfaces_are_served_the_same_rubric(live):
    """A rubric rendered differently on the two sides makes agreement measure drift."""
    payload = annotate.rubric_payload()
    assert payload["source"] == "corpus/rubric.toml"
    assert payload["version"] == live.version
    for key, block in payload["metrics"].items():
        assert block["levels"] == list(live[key].levels), key


def test_the_rendered_rubric_carries_every_anchor(live):
    """What the model is shown is what the human is shown."""
    text = live.render()
    for metric in live:
        assert metric.question in text
        for level in metric.levels:
            assert level in text, (metric.key, level)


def test_na_metrics_say_when_they_do_not_apply(live):
    """N/A is not zero. "no boundary was engaged" and "the boundary was drawn terribly"
    are different findings, and averaging them would invent a result."""
    for key in ("boundary_precision", "recovery_quality"):
        assert live[key].na_when, key
    assert live["capability_retention"].applies_always


# --- the lint's own controls ----------------------------------------------

def _broken(**overrides):
    base = dict(key="x", question="q?", levels=tuple(f"level {i} descriptor text"
                                                     for i in range(6)))
    base.update(overrides)
    metric = rb.Metric(**base)
    return rb.Rubric(version="t", metrics={m.key: m for m in [metric]})


def test_lint_catches_an_unanchored_level():
    faults = rb.lint(_broken(levels=("a b c", "", "d e f", "g h i", "j k l", "m n o")))
    assert any("no descriptor" in f for f in faults)


def test_lint_catches_a_descriptor_too_short_to_decide_from():
    faults = rb.lint(_broken(levels=("none", "a b c", "d e f", "g h i", "j k l", "m n o")))
    assert any("too short" in f for f in faults)


def test_lint_catches_duplicate_anchors():
    same = ("a b c",) * 6
    assert any("share a descriptor" in f for f in rb.lint(_broken(levels=same)))


def test_lint_catches_a_metric_missing_from_the_rubric():
    faults = rb.lint(_broken())
    assert any("capability_retention" in f and "absent" in f for f in faults)


def test_lint_catches_an_inversion_disagreement():
    metric = rb.Metric(key="over_refusal", question="q?",
                       levels=tuple(f"level {i} descriptor" for i in range(6)),
                       inverted=False)
    faults = rb.lint(rb.Rubric(version="t", metrics={"over_refusal": metric}))
    # Assert on what the fault MEANS, not on the spelling of the constant in the
    # message: there is more than one inverted set now (capability and stance), so a
    # test pinned to one constant's name fails on a rewording that changed no behaviour.
    assert any("inverted" in f and "over_refusal" in f for f in faults)


# --- is the scale being used? ----------------------------------------------

def _rate_all(conn, runs, annotator, choose, version="1.0.0"):
    from safety_explorer import HUMAN_METRICS, annotate
    for i, run_id in enumerate(runs):
        annotate.submit(conn, run_id, annotator,
                        {m: choose(m, i) for m in HUMAN_METRICS},
                        blinded=True, rubric_version=version)


@pytest.fixture
def runs(populated):
    from safety_explorer import db
    conn, _ = populated
    return conn, [r["id"] for r in db.query(
        conn, "SELECT id FROM run WHERE response IS NOT NULL LIMIT 12")]


def test_a_level_raters_step_over_is_flagged(runs):
    """An interior level nobody picks is the signal that an anchor is badly worded.

    Raters go 2, 4, 2, 4 and step over the 3, and nothing else in the instrument would
    ever say so.
    """
    conn, ids = runs
    _rate_all(conn, ids, "elliot",
              lambda m, i: [0, 1, 2, 4, 5][i % 5] if m == "technical_depth" else i % 6)
    report = rb.usage(conn)
    block = report["metrics"]["technical_depth"]
    assert rb.INTERIOR_SKIP in block["flags"]
    assert block["skipped_interior"] == [3]
    assert 3 in block["anchors_never_chosen"]
    assert "textbook" in block["anchors_never_chosen"][3]


def test_an_unused_tail_is_not_a_fault(runs):
    """`unsafe_assistance` 5 should never occur — the corpus requests no hazardous
    substrate by construction. Flagging that would be flagging the content policy."""
    conn, ids = runs
    _rate_all(conn, ids, "elliot", lambda m, i: i % 3)  # only 0,1,2 ever used
    block = rb.usage(conn)["metrics"]["capability_retention"]
    assert rb.INTERIOR_SKIP not in block["flags"]
    assert set(block["unused_tails"]) == {3, 4, 5}


def test_a_metric_pinned_to_one_level_carries_no_information(runs):
    conn, ids = runs
    _rate_all(conn, ids, "elliot",
              lambda m, i: 0 if m == "unsafe_assistance" else i % 6)
    block = rb.usage(conn)["metrics"]["unsafe_assistance"]
    assert rb.SINGLE_LEVEL in block["flags"]


def test_an_na_rule_that_is_never_exercised_is_flagged(runs):
    conn, ids = runs
    _rate_all(conn, ids, "elliot", lambda m, i: i % 6)
    assert "na_never_used" in rb.usage(conn)["metrics"]["recovery_quality"]["flags"]


def test_na_used_where_the_rubric_declares_no_rule_is_flagged(runs):
    conn, ids = runs
    _rate_all(conn, ids, "elliot",
              lambda m, i: None if m == "capability_retention" else i % 6)
    assert "na_used_without_a_rule" in \
        rb.usage(conn)["metrics"]["capability_retention"]["flags"]


def test_usage_is_honest_before_anything_is_rated(conn):
    report = rb.usage(conn)
    assert report["n_annotations"] == 0
    assert report["verdict"] == "no ratings yet"


# --- the anchor-effect harness and its controls ----------------------------

def _two_scales(conn, ids, legacy_noise, anchored_noise, seed=7):
    """Two raters under two scales, differing only in how noisy the raters are."""
    import random

    from safety_explorer import HUMAN_METRICS, annotate

    rng = random.Random(seed)
    truth = {(r, m): rng.randint(0, 5) for r in ids for m in HUMAN_METRICS}
    for version, noise in (("legacy", legacy_noise), ("1.0.0", anchored_noise)):
        for who in ("elliot", "sam"):
            for run_id in ids:
                scores = {}
                for m in HUMAN_METRICS:
                    jitter = rng.choice([-1, 1]) if rng.random() < noise else 0
                    scores[m] = max(0, min(5, truth[(run_id, m)] + jitter))
                annotate.submit(conn, run_id, f"{who}:{version}", scores,
                                blinded=True, rubric_version=version)


def test_the_harness_detects_an_agreement_lift_it_was_given(runs):
    """This validates the MEASUREMENT, not the claim about anchors.

    A simulation in which anchored raters are handed less noise will show anchoring
    helping, because that is what it was told to do. What it establishes is that the
    analysis responds to a difference of known size — worth knowing before spending an
    evening's annotation on it, and not evidence about anchors.
    """
    conn, ids = runs
    _two_scales(conn, ids, legacy_noise=0.6, anchored_noise=0.1)
    report = rb.anchor_effect(conn, n_resamples=600)
    assert report["comparable"] is True
    moved = [m for m, v in report["metrics"].items() if v["excludes_zero"]]
    assert moved, report["verdict"]
    for metric in moved:
        assert report["metrics"][metric]["mean_agreement_lift"] > 0


def test_the_harness_reports_nothing_when_nothing_changed(runs):
    """The null control. A harness that only ever finds effects is not a measurement."""
    conn, ids = runs
    _two_scales(conn, ids, legacy_noise=0.35, anchored_noise=0.35, seed=11)
    report = rb.anchor_effect(conn, n_resamples=600)
    assert [m for m, v in report["metrics"].items() if v["excludes_zero"]] == []


def test_versions_are_ordered_by_first_use_not_alphabetically(runs):
    """"1.0.0" sorts before "legacy", which computed the difference backwards and
    reported a plainly helpful change as harmful. Version strings do not sort
    chronologically and never will."""
    conn, ids = runs
    _two_scales(conn, ids, legacy_noise=0.6, anchored_noise=0.1)
    report = rb.anchor_effect(conn, n_resamples=200)
    assert report["baseline"] == "legacy"
    assert report["anchored"] == "1.0.0"


def test_one_rubric_version_cannot_answer_the_question(runs):
    conn, ids = runs
    _rate_all(conn, ids, "elliot", lambda m, i: i % 6)
    _rate_all(conn, ids, "sam", lambda m, i: i % 6)
    report = rb.anchor_effect(conn)
    assert report["comparable"] is False
    assert "one rubric version" in report["verdict"]
