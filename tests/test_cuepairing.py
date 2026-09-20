"""A twin pair must carry the same cue on both sides.

Found by running the full instrument self-check on a campaign with a cue arm, which no
test had ever done. The cell key was (prompt, repeat), which was correct until the
sandbagging arm gave one prompt seven runs per repeat — one uncued, six across three
severities and two arms. All seven collapsed onto one key, the last written won, and
every cued test was differenced against whichever baseline landed last: a run at no cue
against a baseline at severity 5.

On a full campaign that collapsed 1386 observations onto 198 keys, and it silently mixed
the ladder with the cue manipulation the placebo design exists to isolate — in the
analysis this module calls its primary unit. The unit suite never saw it because its
fixture runs no cues.
"""

from pathlib import Path

import pytest

from safety_explorer import analysis, cues as cue_mod, db, runner
from safety_explorer.providers import get_provider


@pytest.fixture(scope="module")
def cued(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    from safety_explorer import corpus as corpus_mod

    corpus = corpus_mod.load(root / "corpus")
    conn = db.init_db(tmp_path_factory.mktemp("cued") / "x.db")
    runner.snapshot_corpus(conn=conn, corpus=corpus, lint_clean=True)
    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "cued", provider, corpus, 1)
    runner.execute(conn, cid, corpus, provider, 1,
                   only=["orbital_debris", "control_autonomy"],
                   cue_set=cue_mod.load(root / "corpus" / "cues.toml"),
                   cue_levels=[0, 3, 5])
    return conn, corpus, cid


def test_the_fixture_actually_carries_several_cues_per_cell(cued):
    """Without this the rest of the file would pass vacuously."""
    conn, _corpus, cid = cued
    rows = db.query(conn, """
        SELECT prompt_id, repeat_index, COUNT(DISTINCT cue_id) AS n
        FROM run WHERE campaign_id = ? GROUP BY prompt_id, repeat_index
        ORDER BY n DESC LIMIT 1""", (cid,))
    assert rows[0]["n"] >= 3, rows


def test_a_twin_delta_never_crosses_the_cue_manipulation(cued):
    conn, corpus, cid = cued
    for cue in ("none", None):
        deltas = analysis.twin_deltas(conn, corpus, cid, cue=cue)
        assert deltas, f"no pairs for cue={cue!r}"
        for d in deltas:
            base = db.query_one(conn, "SELECT cue_id FROM run WHERE id = ?",
                                (d["baseline_run_id"],))
            test = db.query_one(conn, "SELECT cue_id FROM run WHERE id = ?",
                                (d["test_run_id"],))
            assert base["cue_id"] == test["cue_id"], (d["variant"], base, test)


def test_the_ladder_reads_the_uncued_runs_by_default(cued):
    """What the analysis meant before the sandbagging arm existed."""
    conn, corpus, cid = cued
    deltas = analysis.twin_deltas(conn, corpus, cid)
    assert deltas
    assert {d["cue_id"] for d in deltas} == {"none"}


def test_asking_for_every_cue_returns_more_pairs_not_mixed_ones(cued):
    conn, corpus, cid = cued
    uncued = analysis.twin_deltas(conn, corpus, cid, cue="none")
    every = analysis.twin_deltas(conn, corpus, cid, cue=None)
    assert len(every) > len(uncued)
    assert len({d["cue_id"] for d in every}) > 1


def test_a_depth_pair_never_crosses_the_cue_manipulation(cued):
    """Otherwise the gap measured is the cue rather than the register."""
    conn, corpus, cid = cued
    report = analysis.depth_interaction(conn, corpus, cid, source="auto", cue=None)
    seen = set()
    for block in report["by_focal_dimension"].values():
        for level in block["levels"]:
            for row in level.get("rows", []) or []:
                seen.add(row.get("cue_id"))
    # Every recorded gap names the single cue it was measured under.
    assert None not in seen, "a depth gap was recorded without its cue"


def test_the_depth_interaction_survives_a_cued_campaign(cued):
    """The regression, end to end.

    With the pairing broken this returned a gap of 0.0 at every level, so the mock's
    documented interaction read as absent — a false negative on RQ4 produced by the
    presence of an unrelated arm.
    """
    conn, corpus, cid = cued
    report = analysis.depth_interaction(conn, corpus, cid, source="auto")
    levels = {lv["level"]: lv["median_gap"]
              for lv in report["by_focal_dimension"]["intent"]["levels"]}
    assert levels.get("C") == 0.0, levels
    assert levels.get("D", 0) > 0, levels
