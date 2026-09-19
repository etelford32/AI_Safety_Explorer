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
    assert any("INVERTED_METRICS" in f for f in faults)
