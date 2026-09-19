"""Blinding and the annotation queue — the design's largest validity safeguard."""

import pytest

from safety_explorer import HUMAN_METRICS, annotate, db


def _first_run(conn):
    return db.query_one(conn, "SELECT id FROM run WHERE response IS NOT NULL")["id"]


def test_blinded_item_leaks_no_metadata(populated):
    conn, _ = populated
    item = annotate.item(conn, _first_run(conn), blinded=True)
    assert "revealed" not in item
    blob = repr(item)
    # The variant letter and family must not be reachable from a blinded item.
    assert "orbital_debris" not in blob


def test_unblinded_item_reveals(populated):
    conn, _ = populated
    item = annotate.item(conn, _first_run(conn), blinded=False)
    assert item["revealed"]["variant"] in list("ABCDEF") or item["revealed"]["arm"] == "control"


def test_queue_is_randomised_not_ladder_ordered(populated, corpus):
    """Walking A,B,C,D,E,F in order primes the expectation of decline."""
    conn, _ = populated
    ids = annotate.queue(conn, "a", limit=100)
    variants = [
        db.query_one(conn, "SELECT p.variant FROM run r JOIN prompt p ON p.id = r.prompt_id "
                           "WHERE r.id = ?", (i,))["variant"]
        for i in ids
    ]
    ladder = ["A", "B", "C", "D", "E", "F"]
    runs_in_order = sum(
        1 for i in range(len(variants) - 1)
        if variants[i] in ladder and variants[i + 1] in ladder
        and ladder.index(variants[i + 1]) == ladder.index(variants[i]) + 1
    )
    assert runs_in_order < len(variants) / 2, "queue looks ladder-ordered"


def test_queue_is_deterministic_for_a_seed(populated):
    conn, _ = populated
    assert annotate.queue(conn, "a", limit=20, seed=7) == annotate.queue(conn, "a", limit=20, seed=7)
    assert annotate.queue(conn, "a", limit=20, seed=7) != annotate.queue(conn, "a", limit=20, seed=8)


def test_queue_excludes_already_annotated(populated):
    conn, _ = populated
    rid = annotate.queue(conn, "a", limit=1)[0]
    annotate.submit(conn, rid, "a", {m: 3 for m in HUMAN_METRICS})
    assert rid not in annotate.queue(conn, "a", limit=100)


def test_reliability_pass_reserves_a_subset(populated):
    conn, _ = populated
    ids = annotate.queue(conn, "a", limit=20)
    for rid in ids:
        annotate.submit(conn, rid, "a", {m: 3 for m in HUMAN_METRICS})
    resserve = annotate.queue(conn, "a", limit=100, pass_index=1)
    assert resserve, "reliability pass must re-serve something"
    assert set(resserve) <= set(ids), "must re-serve from the first pass only"
    assert len(resserve) <= max(1, round(len(ids) * annotate.RELIABILITY_FRACTION)) + 1


def test_submit_validates_range(populated):
    conn, _ = populated
    rid = _first_run(conn)
    with pytest.raises(ValueError):
        annotate.submit(conn, rid, "a", {"capability_retention": 9})
    with pytest.raises(ValueError):
        annotate.submit(conn, rid, "a", {"not_a_metric": 3})
    with pytest.raises(ValueError):
        annotate.submit(conn, rid, "a", {m: 3 for m in HUMAN_METRICS}, refusal_label="nope")


def test_resubmit_replaces_rather_than_duplicates(populated):
    conn, _ = populated
    rid = _first_run(conn)
    annotate.submit(conn, rid, "a", {m: 1 for m in HUMAN_METRICS})
    annotate.submit(conn, rid, "a", {m: 4 for m in HUMAN_METRICS})
    rows = db.query(conn, "SELECT * FROM annotation WHERE run_id = ? AND annotator = 'a'", (rid,))
    assert len(rows) == 1 and rows[0]["capability_retention"] == 4


def test_na_is_allowed_where_the_rubric_says_so(populated):
    conn, _ = populated
    rid = _first_run(conn)
    scores = {m: 3 for m in HUMAN_METRICS}
    scores["boundary_precision"] = None
    annotate.submit(conn, rid, "a", scores)
    row = db.query_one(conn, "SELECT boundary_precision FROM annotation WHERE run_id = ?", (rid,))
    assert row["boundary_precision"] is None
