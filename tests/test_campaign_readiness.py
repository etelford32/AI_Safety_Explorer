"""Guards on the things that would waste a real campaign or a real annotation session.

Every test here corresponds to a defect that costs money or hours rather than
correctness in the abstract.
"""

import pytest

from safety_explorer import annotate, db
from safety_explorer.providers.anthropic_provider import AnthropicProvider, rejects_sampling


# --- provider: parameter legality -----------------------------------------

def test_sampling_params_rejected_on_current_frontier_models():
    """Sending temperature to Opus 5 is a 400 on EVERY call, not a degraded call.

    The provider shipped with temperature=0.0 hardcoded, which would have failed all
    246 cells of a campaign before a single response was stored.
    """
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5-1",
                  "claude-opus-4-8", "claude-opus-4-7"):
        assert rejects_sampling(model), model
        with pytest.raises(ValueError, match="rejects sampling"):
            AnthropicProvider(model, temperature=0.0)._build_kwargs(
                [{"role": "user", "content": "x"}], {})


def test_sampling_params_allowed_on_older_models():
    for model in ("claude-haiku-4-5", "claude-opus-4-6", "claude-sonnet-4-6"):
        assert not rejects_sampling(model), model
        kw = AnthropicProvider(model, temperature=0.0)._build_kwargs(
            [{"role": "user", "content": "x"}], {})
        assert kw["temperature"] == 0.0


def test_no_sampling_sent_by_default():
    """Omitting temperature is legal everywhere and records the model's own behaviour."""
    kw = AnthropicProvider("claude-opus-5")._build_kwargs([{"role": "user", "content": "x"}], {})
    assert "temperature" not in kw and "top_p" not in kw


def test_thinking_is_explicit_when_set():
    off = AnthropicProvider("claude-opus-5", thinking="off")._build_kwargs(
        [{"role": "user", "content": "x"}], {})
    assert off["thinking"] == {"type": "disabled"}
    on = AnthropicProvider("claude-opus-5", thinking="adaptive")._build_kwargs(
        [{"role": "user", "content": "x"}], {})
    assert on["thinking"] == {"type": "adaptive"}
    # Unset means "whatever the model does by default" — not silently disabled.
    assert "thinking" not in AnthropicProvider("claude-opus-5")._build_kwargs(
        [{"role": "user", "content": "x"}], {})


# --- annotation budget ----------------------------------------------------

def test_coverage_plan_beats_random_on_twin_pairs(populated, corpus):
    """A twin delta needs BOTH members rated, so a uniform sample wastes the budget."""
    conn, cid = populated
    plan = annotate.plan_set(conn, corpus, budget=30, campaign_id=cid)
    # 30 ratings selected for coverage should complete far more pairs than the
    # ~(30/N)^2 * pairs a uniform sample would.
    assert plan["complete_twin_pairs"] >= plan["n_family"] * 0.5, plan


def test_coverage_plan_covers_every_family(populated, corpus):
    """Bootstrap intervals resample families; a family with zero ratings contributes nothing.

    An earlier greedy version exhausted one family at a time and dropped two families
    entirely — including the only specificity-focal one, which would have left RQ3
    with no data at all.
    """
    conn, cid = populated
    plan = annotate.plan_set(conn, corpus, budget=40, campaign_id=cid)
    families_in_campaign = {
        r["family_id"] for r in db.query(
            conn,
            "SELECT DISTINCT p.family_id FROM run r JOIN prompt p ON p.id = r.prompt_id "
            "WHERE r.campaign_id = ? AND p.arm = 'family'", (cid,))
    }
    assert set(plan["families_covered"]) == families_in_campaign

    # Fairness is "no family starved relative to what it could supply", not "equal
    # counts". Families differ in how many variants they carry — only some have a
    # language arm — so equal counts would mean deliberately under-using the richer
    # ones. What matters for the bootstrap is that every family is represented.
    available = {
        r["family_id"]: r["n"] for r in db.query(
            conn,
            "SELECT p.family_id, COUNT(DISTINCT p.id) AS n FROM run r "
            "JOIN prompt p ON p.id = r.prompt_id "
            "WHERE r.campaign_id = ? AND p.arm = 'family' GROUP BY p.family_id", (cid,))
    }
    fair_share = plan["n_family"] // len(families_in_campaign)
    for family, got in plan["families_covered"].items():
        assert got >= min(fair_share, available[family]), (
            f"{family} starved: got {got}, could supply {available[family]}, "
            f"fair share {fair_share}")


def test_coverage_plan_reserves_budget_for_controls(conn, corpus):
    """Controls have no twin, so a pure pair-maximiser would never pick one.

    Uses a campaign that actually includes controls — the shared fixture deliberately
    runs family prompts only.
    """
    from safety_explorer import runner
    from safety_explorer.providers import get_provider

    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "ctrl", provider, corpus, 1)
    runner.execute(conn, cid, corpus, provider, 1, only=["orbital_debris", "control"])

    plan = annotate.plan_set(conn, corpus, budget=40, campaign_id=cid)
    assert plan["n_controls"] > 0, plan
    assert plan["n_family"] > 0, plan


def test_coverage_queue_still_blinds_and_shuffles(populated, corpus):
    conn, cid = populated
    ids = annotate.queue(conn, "a", limit=20, campaign_id=cid,
                         strategy="coverage", corpus=corpus)
    assert ids
    item = annotate.item(conn, ids[0], blinded=True)
    assert "revealed" not in item
    assert annotate.queue(conn, "a", limit=20, campaign_id=cid, strategy="coverage",
                          corpus=corpus, seed=1) != \
           annotate.queue(conn, "a", limit=20, campaign_id=cid, strategy="coverage",
                          corpus=corpus, seed=2)


# --- truncation -----------------------------------------------------------

def test_truncated_responses_are_excluded_by_default(populated):
    """A response cut off at max_tokens is a corrupted measurement, not a degraded one.

    It is short, light on equations and missing its conclusion — indistinguishable from
    capability loss to every metric here.
    """
    from safety_explorer import analysis

    conn, cid = populated
    before = len(analysis.observations(conn, cid, tiers="A"))
    victim = db.query_one(conn, "SELECT id FROM run WHERE campaign_id = ? LIMIT 1", (cid,))
    conn.execute("UPDATE run SET finish_reason = 'max_tokens' WHERE id = ?", (victim["id"],))
    conn.commit()

    after = analysis.observations(conn, cid, tiers="A")
    assert len(after) == before - 1
    assert victim["id"] not in {o["run_id"] for o in after}

    kept = analysis.observations(conn, cid, tiers="A", include_truncated=True)
    assert len(kept) == before
    assert any(o["truncated"] for o in kept)


# --- migration ------------------------------------------------------------

def test_migration_adds_columns_to_an_existing_database(tmp_path):
    """A longitudinal instrument must not ask the user to start over for a new column."""
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.executescript(
        "CREATE TABLE run (id TEXT PRIMARY KEY, finish_reason TEXT);"
        "CREATE TABLE prompt (id TEXT PRIMARY KEY);"
    )
    conn.execute("INSERT INTO run (id, finish_reason) VALUES ('r1', 'end_turn')")
    conn.commit()

    applied = db.migrate(conn)
    assert "run.stop_details" in applied and "prompt.sub_arm" in applied
    assert db.query_one(conn, "SELECT id FROM run WHERE id = 'r1'"), "existing row lost"
    assert db.migrate(conn) == [], "migration must be idempotent"


# --- pricing --------------------------------------------------------------

def test_pricing_flags_unknown_models_rather_than_guessing():
    from safety_explorer import pricing

    assert pricing.estimate("claude-opus-5", 1_000_000, 1_000_000)["cost_total"] == 30.00
    assert pricing.estimate("some-other-model", 100, 100)["known"] is False


def test_batch_is_half_price():
    from safety_explorer import pricing

    full = pricing.estimate("claude-opus-5", 1_000_000, 1_000_000)["cost_total"]
    batch = pricing.estimate("claude-opus-5", 1_000_000, 1_000_000, batch=True)["cost_total"]
    assert batch == full / 2
