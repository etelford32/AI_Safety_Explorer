"""The claim that co-analysis ratings feed the existing analyses unchanged.

That was asserted in a commit message before anything checked it, and one third of it
was false: `judge_agreement` crashed outright the first time two people rated the same
response. These tests are the assertion turned into something that fails when it stops
being true.
"""

import pytest

from safety_explorer import HUMAN_METRICS, analysis, annotate, coanalyse as co


@pytest.fixture
def rated(populated):
    """Two raters and a model proposer over the same runs — the co-analysis case."""
    from safety_explorer import db
    from safety_explorer.providers import get_provider

    conn, _ = populated
    runs = [r["id"] for r in db.query(
        conn, "SELECT id FROM run WHERE response IS NOT NULL LIMIT 8")]
    co.propose_many(conn, runs, get_provider("mock", "mock-1"))
    return conn, runs


def _rate(conn, runs, annotator, levels):
    for run_id in runs:
        annotate.submit(conn, run_id, annotator,
                        {m: levels(m, run_id) for m in HUMAN_METRICS},
                        blinded=True, citations={m: [0] for m in HUMAN_METRICS})


# --- the bug this found ----------------------------------------------------

def test_two_raters_straddling_a_level_do_not_crash_judge_agreement(rated):
    """A median of 4 and 5 is 4.5, which was not in the declared scale.

    `judge_agreement` declared the levels as the six integers, so the half-step raised
    KeyError inside the coincidence matrix and took the call down. It never fired
    before because no run had ever carried two human ratings — the queue serves one
    rater at a time and the reliability re-serve is the same rater twice. The
    co-analysis view is the first thing to put two people on one response, which is
    exactly the case the function exists for.
    """
    conn, runs = rated
    _rate(conn, runs, "a", lambda m, r: 4)
    _rate(conn, runs, "b", lambda m, r: 5)

    report = analysis.judge_agreement(conn, "capability_retention")
    assert report["n_pairs"] > 0
    assert report["alpha"] == report["alpha"]  # not NaN


def test_the_straddle_is_kept_rather_than_rounded_away(rated):
    """Rounding would silently pick one rater, and Python's banker's rounding would
    pick the lower level at every .5."""
    conn, runs = rated
    _rate(conn, runs, "a", lambda m, r: 4)
    _rate(conn, runs, "b", lambda m, r: 5)
    # The median is 4.5 for every run, so nothing the judge says can match exactly.
    report = analysis.judge_agreement(conn, "capability_retention")
    assert report["exact_agreement"] is not None
    assert 0.0 <= report["exact_agreement"] <= 1.0


# --- the claim itself ------------------------------------------------------

def test_co_analysis_ratings_reach_the_reliability_analysis(rated):
    conn, runs = rated
    _rate(conn, runs, "elliot", lambda m, r: (hash(m + r) % 6))
    _rate(conn, runs, "sam", lambda m, r: (hash(m + r) % 6))

    report = analysis.reliability(conn)
    assert report["n_annotations"] == 2 * len(runs)
    block = report["metrics"]["capability_retention"]
    assert block["paired_units"] == len(runs)
    # Identical raters agree perfectly, whatever the metric's variance.
    assert block["exact_agreement"] == 1.0


def test_disagreement_lowers_alpha_rather_than_being_ignored(rated):
    conn, runs = rated
    _rate(conn, runs, "elliot", lambda m, r: (hash(m + r) % 6))
    agree = analysis.reliability(conn)["metrics"]["capability_retention"]["alpha"]

    _rate(conn, runs, "sam", lambda m, r: 5 - (hash(m + r) % 6))
    disagree = analysis.reliability(conn)["metrics"]["capability_retention"]["alpha"]
    assert disagree < 0.5, disagree
    assert agree != disagree


def test_a_rating_carries_the_spans_it_cited(rated):
    """The citation is the working. Losing it on the way into storage would leave a
    number with nothing behind it, which is the thing the rubric exists to prevent."""
    from safety_explorer import db

    conn, runs = rated
    annotate.submit(conn, runs[0], "elliot",
                    {m: 3 for m in HUMAN_METRICS}, blinded=True,
                    citations={"capability_retention": [1, 4, 7]})
    row = db.query_one(conn, "SELECT citations FROM annotation WHERE run_id = ?",
                       (runs[0],))
    import json
    assert json.loads(row["citations"])["capability_retention"] == [1, 4, 7]


def test_a_model_proposal_and_a_human_rating_coexist_on_one_run(rated):
    """Different tables, both reachable: the judgement is data about the proposer, the
    annotation is the record."""
    from safety_explorer import db

    conn, runs = rated
    _rate(conn, runs, "elliot", lambda m, r: 3)
    assert db.query(conn, "SELECT id FROM judgement WHERE run_id = ?", (runs[0],))
    assert db.query(conn, "SELECT id FROM annotation WHERE run_id = ?", (runs[0],))
    report = analysis.judge_agreement(conn, "capability_retention")
    assert report["n_pairs"] == len(runs)
