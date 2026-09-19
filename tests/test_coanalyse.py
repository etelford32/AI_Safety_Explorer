"""Span-level co-analysis: a model proposes, a human decides, and the gap is measured.

The blinding rule is the whole design, so most of these tests are about it. An analyst
shown a proposal before judging agrees with it more often, and that agreement is not
evidence the proposal was right — it is evidence the analyst was anchored. A
co-analysis that did not record which happened could not tell the two apart, and would
report the anchoring as accuracy.
"""

import pytest

from safety_explorer import coanalyse as co, conversation as cv


@pytest.fixture
def run_with_spans(populated):
    from safety_explorer import db
    conn, _ = populated
    row = db.query(conn, "SELECT id FROM run WHERE response IS NOT NULL LIMIT 1")[0]
    convo = cv.assemble(conn, row["id"])
    spans = [s for t in convo["turns"] for s in t.get("spans", [])]
    assert spans, "fixture produced a run with no spans"
    return conn, row["id"], spans


def _pair(conn, run_id, span, human, model, blinded=True):
    co.record(conn, run_id, span["index"], span["hash"], model,
              source="model", author="judge", confidence=0.8)
    co.record(conn, run_id, span["index"], span["hash"], human,
              source="human", author="elliot", blinded=blinded)


# --- storage ---------------------------------------------------------------

def test_a_proposal_and_an_adjudication_sit_side_by_side(run_with_spans):
    """Neither overwrites the other: a proposal is data about the proposer, the human
    row is the record."""
    conn, run_id, spans = run_with_spans
    _pair(conn, run_id, spans[0], "capability", "hedge")
    cell = co.labels_for(conn, run_id)["by_span"][str(spans[0]["index"])]
    assert [h["label"] for h in cell["human"]] == ["capability"]
    assert [m["label"] for m in cell["model"]] == ["hedge"]


def test_relabelling_replaces_rather_than_stacks(run_with_spans):
    conn, run_id, spans = run_with_spans
    co.record(conn, run_id, spans[0]["index"], spans[0]["hash"], "capability")
    co.record(conn, run_id, spans[0]["index"], spans[0]["hash"], "refusal")
    cell = co.labels_for(conn, run_id)["by_span"][str(spans[0]["index"])]
    assert [h["label"] for h in cell["human"]] == ["refusal"]


def test_an_unknown_label_is_refused(run_with_spans):
    conn, run_id, spans = run_with_spans
    with pytest.raises(ValueError, match="unknown span label"):
        co.record(conn, run_id, spans[0]["index"], spans[0]["hash"], "vibes")


def test_a_label_on_changed_text_is_reported_stale_not_re_pointed(run_with_spans):
    """A label points at an index, and an index means nothing if the segmenter changes.

    Silently re-pointing it would put someone's judgement on a sentence they never read.
    """
    conn, run_id, spans = run_with_spans
    co.record(conn, run_id, spans[0]["index"], "deadbeefdeadbeef", "capability")
    report = co.labels_for(conn, run_id)
    assert report["n_stale"] == 1
    assert report["by_span"][str(spans[0]["index"])]["human"][0]["stale"] is True


# --- the blinding rule -----------------------------------------------------

def test_blind_and_unblind_agreement_are_never_pooled(run_with_spans):
    conn, run_id, spans = run_with_spans
    for i, s in enumerate(spans[:6]):
        _pair(conn, run_id, s, "capability", "capability", blinded=(i % 2 == 0))
    buckets = co.agreement(conn)["by_blinding"]
    assert buckets["blinded"]["n"] == 3
    assert buckets["unblinded"]["n"] == 3


def test_an_unblind_only_set_is_called_out_as_uninterpretable(run_with_spans):
    conn, run_id, spans = run_with_spans
    for s in spans[:4]:
        _pair(conn, run_id, s, "capability", "capability", blinded=False)
    report = co.agreement(conn)
    assert report["by_blinding"]["blinded"]["n"] == 0
    assert report["usable"] is False
    assert "cannot separate" in report["verdict"]


def test_the_blind_unblind_gap_is_not_narrated_on_thin_data(run_with_spans):
    """A twenty-point difference on three pairs against three is noise.

    Naming it anchoring would be exactly the over-claim this instrument avoids
    everywhere else.
    """
    conn, run_id, spans = run_with_spans
    for i, s in enumerate(spans[:6]):
        blind = i % 2 == 0
        _pair(conn, run_id, s, "capability", "capability" if blind else "hedge",
              blinded=blind)
    verdict = co.agreement(conn)["verdict"]
    assert "too few either side" in verdict
    assert "anchoring looks like" not in verdict


def test_perfect_blind_agreement_makes_a_proposer_usable(run_with_spans):
    conn, run_id, spans = run_with_spans
    labels = ["capability", "refusal", "hedge", "filler"]
    for i, s in enumerate(spans[:8]):
        label = labels[i % len(labels)]
        _pair(conn, run_id, s, label, label, blinded=True)
    report = co.agreement(conn)
    assert report["by_blinding"]["blinded"]["exact"] == 1.0
    assert report["by_blinding"]["blinded"]["alpha"] == 1.0
    assert report["usable"] is True


def test_a_proposer_that_always_says_one_thing_is_not_usable(run_with_spans):
    conn, run_id, spans = run_with_spans
    labels = ["capability", "refusal", "hedge", "filler"]
    for i, s in enumerate(spans[:8]):
        _pair(conn, run_id, s, labels[i % len(labels)], "capability", blinded=True)
    report = co.agreement(conn)
    assert report["usable"] is False
    assert report["by_blinding"]["blinded"]["per_label"]["capability"]["precision"] < 0.5


def test_agreement_is_nominal_not_ordinal(run_with_spans):
    """Span labels have no order. Scoring them ordinally would make a disagreement's
    size depend on where each label happened to sit in a tuple."""
    from safety_explorer.analysis import krippendorff_alpha
    from safety_explorer import SPAN_LABELS

    units = {"a": [0, 7], "b": [1, 2], "c": [3, 3], "d": [5, 5]}
    levels = list(range(len(SPAN_LABELS)))
    assert krippendorff_alpha(units, levels, nominal=True) != \
        krippendorff_alpha(units, levels)


def test_a_pair_judged_on_different_text_is_not_a_disagreement(run_with_spans):
    conn, run_id, spans = run_with_spans
    co.record(conn, run_id, spans[0]["index"], spans[0]["hash"], "capability",
              source="model", author="judge")
    co.record(conn, run_id, spans[0]["index"], "0000000000000000", "refusal",
              source="human", author="elliot")
    assert co.agreement(conn)["by_blinding"]["blinded"]["n"] == 0


# --- coverage --------------------------------------------------------------

def test_coverage_reports_what_share_was_labelled_blind(run_with_spans):
    conn, run_id, spans = run_with_spans
    for i, s in enumerate(spans[:4]):
        co.record(conn, run_id, s["index"], s["hash"], "capability",
                  blinded=(i < 3))
    cov = co.coverage(conn)
    assert cov["n_human"] == 4
    assert cov["n_human_blinded"] == 3
    assert cov["blind_share"] == 0.75


def test_coverage_is_empty_and_honest_before_anything_is_labelled(conn):
    cov = co.coverage(conn)
    assert cov["n_labels"] == 0
    assert cov["blind_share"] is None
    assert co.agreement(conn)["verdict"].startswith("no span")


# --- the defect this view surfaced -----------------------------------------

def test_a_contracted_refusal_registers_as_a_refusal():
    """"I'm not able to help with this one" scored ZERO refusal hits.

    The pattern read `\\bi (?:...|'m not able to)`, which requires a literal space after
    "i", so the contraction branch could never fire. The mock's own refusal text opens
    exactly that way, which means `refusal_signal` was dead for every refusal in the
    fixture — and for the most common way a real model opens one.

    Found by looking at a span the co-analysis view rendered with no refusal chip on it,
    which is the argument for the view: a whole-response signal of 0.0 looks like a
    quiet response, and a span of pure refusal showing no refusal does not.
    """
    from safety_explorer import metrics

    for text in ("I'm not able to help with this one.",
                 "I am not able to help with this.",
                 "I'm unable to provide that.",
                 "I can't help with that."):
        assert metrics.extract(text)["refusal_hits"] >= 1, text
    # ...and it still does not fire on a third person who is merely incapable.
    assert metrics.extract("The solver is not able to resolve that.")["refusal_hits"] == 0
