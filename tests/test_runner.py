"""Runner behaviour: resumability, conversation replay, provenance."""

from safety_explorer import db, runner
from safety_explorer.providers import get_provider


def test_multi_turn_predecessor_runs_first(corpus):
    plan = runner.plan(corpus, 1, only=["orbital_debris.F"])
    ids = [p.variant.id for p in plan]
    assert ids.index("orbital_debris.E") < ids.index("orbital_debris.F"), (
        "measuring recovery from a boundary requires the boundary to have happened"
    )


def test_repeats_are_outermost(corpus):
    """An interrupted campaign should yield a balanced design, not three copies of family 1."""
    plan = runner.plan(corpus, 3, only=["orbital_debris"])
    first_sweep = [p for p in plan[:6]]
    assert {p.repeat_index for p in first_sweep} == {0}
    assert len({p.variant.id for p in first_sweep}) == 6


def test_conversation_replays_the_real_exchange(populated, corpus):
    conn, cid = populated
    row = db.query_one(
        conn,
        "SELECT messages FROM run WHERE campaign_id = ? AND prompt_id = ? AND repeat_index = 0",
        (cid, "orbital_debris.F"),
    )
    messages = db.loads(row["messages"], [])
    assert len(messages) == 3, "F must carry the E exchange as context"
    assert messages[0]["role"] == "user" and messages[1]["role"] == "assistant"
    e_run = db.query_one(
        conn,
        "SELECT response FROM run WHERE campaign_id = ? AND prompt_id = ? AND repeat_index = 0",
        (cid, "orbital_debris.E"),
    )
    assert messages[1]["content"] == e_run["response"], "must replay the real response"


def test_resume_skips_existing_cells(conn, corpus):
    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "r", provider, corpus, 1)
    first = runner.execute(conn, cid, corpus, provider, 1, only=["orbital_debris"])
    assert first["ok"] == 6 and first["skipped"] == 0
    second = runner.execute(conn, cid, corpus, provider, 1, only=["orbital_debris"])
    assert second["ok"] == 0 and second["skipped"] == 6


def test_api_runs_are_tier_a(populated):
    conn, cid = populated
    rows = db.query(conn, "SELECT DISTINCT provenance_tier, lane FROM run WHERE campaign_id = ?", (cid,))
    assert rows == [{"provenance_tier": "A", "lane": "api"}]


def test_features_recompute_without_api_calls(populated):
    conn, cid = populated
    before = db.query_one(conn, "SELECT COUNT(*) AS n FROM run")["n"]
    n = runner.recompute_features(conn)
    assert n == before
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM feature")["n"] == before


def test_corpus_snapshot_is_content_addressed(conn, corpus):
    row = db.query_one(conn, "SELECT * FROM corpus_version WHERE version = ?", (corpus.version,))
    assert row["content_hash"] == corpus.content_hash
    prompt = db.query_one(conn, "SELECT prompt_hash FROM prompt WHERE id = ?",
                          (corpus.runnable[0].id,))
    assert prompt["prompt_hash"] == corpus.runnable[0].prompt_hash
