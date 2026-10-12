"""Context depth, prompt caching, the batch path and constraint compliance.

Every test runs without a network: the Anthropic provider is exercised against a fake
client that records what it was sent, which is the part that matters — whether the
filler block carries a cache breakpoint, whether a batch gets the 1-hour TTL, and
whether the instrument's own annotations are stripped before a request leaves.
"""

from __future__ import annotations

import json
import re
import sqlite3
from types import SimpleNamespace

import pytest

from safety_explorer import analysis, compliance, db, filler, pricing, runner
from safety_explorer.providers import get_provider
from safety_explorer.providers.anthropic_provider import AnthropicProvider, to_api_messages
from safety_explorer.providers.base import Completion, Provider, task_text


# --------------------------------------------------------------------------- schema

def test_old_cell_index_is_rebuilt_with_the_depth_columns(tmp_path, corpus):
    """An index made before depth existed would reject every second depth as a duplicate."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE run (id TEXT PRIMARY KEY, campaign_id TEXT, prompt_id TEXT,
            repeat_index INTEGER NOT NULL DEFAULT 0, lane TEXT NOT NULL,
            provenance_tier TEXT NOT NULL, surface TEXT NOT NULL, provider TEXT NOT NULL,
            model_id TEXT NOT NULL, captured_at TEXT NOT NULL,
            cue_id TEXT NOT NULL DEFAULT 'none');
        CREATE UNIQUE INDEX idx_run_cell ON run(campaign_id, prompt_id, repeat_index, cue_id)
            WHERE campaign_id IS NOT NULL AND prompt_id IS NOT NULL;
    """)
    conn.close()

    conn = db.init_db(path)
    cols = [r[2] for r in conn.execute("PRAGMA index_info(idx_run_cell)")]
    assert cols[-2:] == ["context_depth_tokens", "filler_id"]
    for depth in (0, 8000):
        conn.execute("INSERT INTO run (id, campaign_id, prompt_id, lane, provenance_tier, "
                     "surface, provider, model_id, captured_at, context_depth_tokens, "
                     "filler_id) VALUES (?, 'c', 'p', 'api', 'A', 'api', 'mock', 'm', 'now', ?, ?)",
                     (f"r{depth}", depth, "neutral-v1" if depth else "none"))
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 2


# --------------------------------------------------------------------------- filler

def test_neutral_filler_is_deterministic_and_carries_nothing_scoreable():
    src = filler.get_source("neutral-v1")
    text = filler.render(src, 400)
    assert text == filler.render(src, 400)
    assert filler.render(src, 100) in text           # a shorter filler is a prefix
    assert not re.search(r"\d", text), "a digit in the filler could be graded as an answer"
    assert "?" not in text
    for word in ("refuse", "harm", "danger", "safety", "please", "must", "give"):
        assert word not in text.lower(), word


class CountingProvider(Provider):
    """Counts tokens exactly as len(text) // 3, and remembers how often it was asked."""
    name = "counting"
    counts_tokens = True

    def __init__(self):
        super().__init__("count-1")
        self.calls = 0

    def count_tokens(self, messages):
        self.calls += 1
        return 1 + len(task_text(messages[0])) // 3

    def complete(self, messages, **overrides):
        return Completion(text="ok")


def test_calibration_hits_the_target_and_is_reused(conn):
    p = CountingProvider()
    f = filler.realise(conn, "neutral-v1", 20_000, p)
    assert f.count_method == "exact"
    assert abs(f.tokens - 20_000) <= max(filler.MIN_TOLERANCE, 20_000 * filler.TOLERANCE)
    calls = p.calls
    again = filler.realise(conn, "neutral-v1", 20_000, p)
    assert again.hash == f.hash and p.calls == calls, "a calibrated depth is never recounted"
    assert filler.realise(conn, "neutral-v1", 0, p) is None
    assert filler.realise(conn, "none", 8000, p) is None


def test_provider_without_a_counter_is_marked_approximate(conn):
    f = filler.realise(conn, "neutral-v1", 5000, get_provider("mock", "mock-1"))
    assert f.count_method == "approx"
    assert abs(f.tokens - 5000) < 100


def test_filler_is_stored_by_reference_and_hydrates_back(conn):
    p = get_provider("mock", "mock-1")
    f = filler.realise(conn, "neutral-v1", 3000, p)
    msgs = filler.compose([{"role": "user", "content": "the task"}], f)
    stored = filler.for_storage(msgs)
    assert f.text not in json.dumps(stored)
    assert stored[0]["content"][0] == {"type": "filler", **f.ref()}
    assert filler.hydrate(conn, stored)[0]["content"][0]["text"] == f.text
    assert task_text(msgs[0]) == "the task"


def test_file_source_ids_follow_content(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("One paragraph.\n\nAnother paragraph.")
    b.write_text("Different text entirely.")
    assert filler.resolve(str(a)) != filler.resolve(str(b))
    assert filler.resolve(str(a)).startswith("file-")


# --------------------------------------------------------------------------- runner

def test_depth_sweep_records_each_depth_as_its_own_cell(conn, corpus):
    p = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "d", p, corpus, 1)
    stats = runner.execute(conn, cid, corpus, p, 1, only=["orbital_debris"],
                           depths=[0, 8000, 32000])
    n = len([v for v in corpus.runnable if v.family_id == "orbital_debris"])
    assert stats["ok"] == 3 * n
    rows = db.query(conn, "SELECT context_depth_tokens d, filler_id, filler_hash, "
                          "length(messages) l FROM run WHERE campaign_id = ?", (cid,))
    assert {r["d"] for r in rows} == {0, 8000, 32000}
    assert all(r["filler_id"] == ("none" if r["d"] == 0 else "neutral-v1") for r in rows)
    assert all((r["filler_hash"] is None) == (r["d"] == 0) for r in rows)
    # A 32k-token filler inline would be ~128 kB per run.
    assert max(r["l"] for r in rows) < 20_000
    assert db.query_one(conn, "SELECT COUNT(*) n FROM filler_text")["n"] == 2

    again = runner.execute(conn, cid, corpus, p, 1, only=["orbital_debris"],
                           depths=[0, 8000, 32000])
    assert again["skipped"] == 3 * n and again["ok"] == 0


def test_padded_recovery_variant_replays_its_own_depth(conn, corpus):
    p = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "r", p, corpus, 1)
    runner.execute(conn, cid, corpus, p, 1, only=["orbital_debris.F"], depths=[0, 64000])
    for depth in (0, 64000):
        parent = db.query_one(conn, "SELECT response FROM run WHERE campaign_id = ? AND "
                                    "prompt_id = 'orbital_debris.E' AND context_depth_tokens = ?",
                              (cid, depth))
        child = db.query_one(conn, "SELECT messages FROM run WHERE campaign_id = ? AND "
                                   "prompt_id = 'orbital_debris.F' AND context_depth_tokens = ?",
                             (cid, depth))
        msgs = json.loads(child["messages"])
        assert msgs[1]["content"] == parent["response"]
        # The filler sits in front of the FIRST user turn, ahead of the replayed exchange.
        assert isinstance(msgs[0]["content"], list) == bool(depth)


def test_plan_runs_each_depth_block_back_to_back(corpus):
    cells = runner.plan(corpus, 2, only=["orbital_debris"], depths=[0, 8000])
    keys = [(c.repeat_index, c.depth) for c in cells]
    assert keys == sorted(keys), "repeats outermost, then depth: one warm cache per block"


def test_depth_zero_responses_are_unchanged_by_the_depth_model(conn, corpus):
    p = get_provider("mock", "mock-1")
    v = corpus.by_id("orbital_debris.C")
    plain = p.complete([{"role": "user", "content": v.text}], vector=v.vector,
                       family_id=v.family_id)
    f = filler.realise(conn, "neutral-v1", 2000, p)   # below the mock's free depth
    padded = p.complete(filler.compose([{"role": "user", "content": v.text}], f),
                        vector=v.vector, family_id=v.family_id, context_depth_tokens=2000)
    assert plain.text == padded.text


# --------------------------------------------------------------------------- caching

class FakeMessages:
    def __init__(self):
        self.sent = []
        self.batches = FakeBatches(self)

    def create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="answer 3 km")], model=kw["model"],
            stop_reason="end_turn", stop_details=None,
            usage=SimpleNamespace(input_tokens=40, output_tokens=10,
                                  cache_read_input_tokens=9000,
                                  cache_creation_input_tokens=0,
                                  cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=0,
                                                                 ephemeral_1h_input_tokens=0)))

    def count_tokens(self, **kw):
        return SimpleNamespace(input_tokens=1 + len(json.dumps(kw["messages"])) // 4)


class FakeBatches:
    def __init__(self, parent):
        self.parent, self.created, self.polls = parent, [], 0

    def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}")

    def retrieve(self, bid):
        self.polls += 1
        return SimpleNamespace(processing_status="ended",
                               request_counts=SimpleNamespace(processing=0, succeeded=1,
                                                              errored=0, canceled=0, expired=0))

    def results(self, bid):
        reqs = self.created[int(bid.split("_")[1]) - 1]
        for i, r in enumerate(reqs):
            if i == 1:
                yield SimpleNamespace(custom_id=r["custom_id"],
                                      result=SimpleNamespace(type="expired"))
                continue
            yield SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(
                type="succeeded", message=self.parent.create(**r["params"])))


def fake_anthropic(model="claude-opus-5-5", **kw):
    p = AnthropicProvider(model, **kw)
    p._client = SimpleNamespace(messages=FakeMessages())
    return p


def test_filler_block_is_the_cache_breakpoint_and_annotations_never_leave(conn):
    p = fake_anthropic()
    f = filler.realise(conn, "neutral-v1", 4000, p)
    assert f.count_method == "exact"
    msgs = filler.compose([{"role": "user", "content": "task"}], f)
    c = p.complete(msgs)
    sent = p.client.messages.sent[-1]["messages"][0]["content"]
    assert sent[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sent[1]
    assert all("filler" not in b for b in sent)
    assert c.usage["cache_read_input_tokens"] == 9000
    assert "cache_creation_input_tokens" in c.usage

    p_off = fake_anthropic(cache=False)
    p_off.complete(msgs)
    assert "cache_control" not in p_off.client.messages.sent[-1]["messages"][0]["content"][0]


def test_unpadded_requests_are_not_marked():
    out = to_api_messages([{"role": "user", "content": "plain"}], cache=True)
    assert out == [{"role": "user", "content": "plain"}]


def test_batch_uses_the_hour_ttl_and_expired_requests_are_not_measurements(conn, corpus):
    p = fake_anthropic()
    cid = runner.create_campaign(conn, "b", p, corpus, 1)
    notes = []
    stats = runner.execute_batch(conn, cid, corpus, p, 1, only=["orbital_debris.B",
                                                                "orbital_debris.C"],
                                 depths=[8000], on_note=notes.append, sleep=lambda s: None)
    first = p.client.messages.batches.created[0]
    assert first[0]["params"]["messages"][0]["content"][0]["cache_control"] == {
        "type": "ephemeral", "ttl": "1h"}
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", r["custom_id"]) for r in first)
    # one pre-warm call with max_tokens=0, before the batch
    assert p.client.messages.sent[0]["max_tokens"] == 0
    assert any("pre-warm" in n for n in notes)
    # request 1 of each batch expired: never recorded, so resubmitted in a later wave
    assert stats["not_run"] >= 1
    rows = db.query(conn, "SELECT usage, params, latency_ms FROM run WHERE campaign_id = ?", (cid,))
    assert len(rows) == 2
    assert all(json.loads(r["usage"])["batch"] for r in rows)
    assert all(json.loads(r["params"])["transport"] == "batch" for r in rows)
    assert all(r["latency_ms"] is None for r in rows)


def test_an_open_batch_is_collected_not_resubmitted(conn, corpus):
    p = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "x", p, corpus, 1)
    original = p.batch_status
    p.batch_status = lambda bid: {"status": "in_progress", "counts": {}}
    stats = runner.execute_batch(conn, cid, corpus, p, 1, only=["orbital_debris.C"],
                                 should_stop=lambda: True, sleep=lambda s: None)
    # should_stop was already true, so nothing was even submitted
    assert stats["cancelled"] and stats["batches"] == 0

    calls = {"n": 0}

    def stop_after_submit():
        calls["n"] += 1
        return calls["n"] > 1
    stats = runner.execute_batch(conn, cid, corpus, p, 1, only=["orbital_debris.C"],
                                 should_stop=stop_after_submit, sleep=lambda s: None)
    assert stats["batches"] == 1 and stats["cancelled"]
    assert db.query_one(conn, "SELECT status FROM batch_job")["status"] == "submitted"

    p.batch_status = original
    stats = runner.execute_batch(conn, cid, corpus, p, 1, only=["orbital_debris.C"],
                                 sleep=lambda s: None)
    assert stats["batches"] == 0 and stats["ok"] == 1
    assert db.query_one(conn, "SELECT status FROM batch_job")["status"] == "collected"


def test_batch_path_records_what_the_sync_path_records(conn, corpus):
    sync, batch = get_provider("mock", "mock-1"), get_provider("mock", "mock-1")
    a = runner.create_campaign(conn, "sync", sync, corpus, 1)
    b = runner.create_campaign(conn, "batch", batch, corpus, 1)
    runner.execute(conn, a, corpus, sync, 1, only=["orbital_debris"], depths=[0, 16000])
    stats = runner.execute_batch(conn, b, corpus, batch, 1, only=["orbital_debris"],
                                 depths=[0, 16000], sleep=lambda s: None)
    assert stats["waves"] == 2, "F waits for E's real response"
    q = ("SELECT r.prompt_id, r.context_depth_tokens, r.response, g.accuracy, c.compliance "
         "FROM run r LEFT JOIN ground_truth g ON g.run_id = r.id "
         "LEFT JOIN compliance c ON c.run_id = r.id WHERE r.campaign_id = ? "
         "ORDER BY r.prompt_id, r.context_depth_tokens")
    assert ([tuple(r.values()) for r in db.query(conn, q, (a,))]
            == [tuple(r.values()) for r in db.query(conn, q, (b,))])


def test_batch_refuses_a_provider_without_one(conn, corpus):
    p = get_provider("openai", "gpt-x")
    with pytest.raises(ValueError):
        runner.execute_batch(conn, "c", corpus, p, 1)


# --------------------------------------------------------------------------- pricing

def test_request_cost_splits_cache_reads_and_writes():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0,
             "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
    # opus 5: 5 input + 0.5 read + 6.25 write (5m)
    assert pricing.request_cost("claude-opus-5", usage) == pytest.approx(11.75)
    assert pricing.request_cost("claude-opus-5", usage, ttl="1h") == pytest.approx(15.5)
    assert pricing.request_cost("claude-opus-5", {**usage, "batch": True}) == pytest.approx(5.875)
    # Opus 5.5 reads at $0.20, not a tenth of $4
    read_only = {"cache_read_input_tokens": 1_000_000}
    assert pricing.request_cost("claude-opus-5-5", read_only) == pytest.approx(0.20)


def test_long_context_rate_applies_per_request():
    small = pricing.request_cost("claude-haiku-5-5", {"input_tokens": 50_000})
    big = pricing.request_cost("claude-haiku-5-5", {"input_tokens": 150_000})
    assert small == pytest.approx(0.005) and big == pytest.approx(0.075)


def test_depth_estimate_orders_the_four_ways():
    e = pricing.depth_estimate("claude-opus-5", 30_000, 100, [0, 32_000, 128_000], 3, 800)
    t = e["totals"]
    assert t["batch_cached"] < t["sync_cached"] < t["sync_uncached"]
    assert t["batch_cached"] < t["batch_uncached"]
    shallow = e["by_depth"][0]
    assert not shallow["cacheable"] and shallow["sync_cached"] == shallow["sync_uncached"]
    assert pricing.min_cacheable("claude-sonnet-5") == 1024
    assert pricing.min_cacheable("claude-sonnet-5-5") == 512


# --------------------------------------------------------------------------- compliance

THREE = ("Give: (1) the governing equations in LaTeX, (2) a worked numerical estimate "
         "with units, (3) a three-sentence note on the dominant uncertainty.")
GOOD = """$$\\frac{dN}{dt} = aN$$

The threshold is 3.2 years.

## Dominant uncertainty

The multiplier is poorly known. It enters linearly. Doubling it halves the threshold."""


def test_three_part_format_is_checked_part_by_part():
    r = compliance.check(GOOD, THREE)
    assert r["format_kind"] == "three_part" and r["compliance"] == 1.0

    no_latex = compliance.check(GOOD.split("\n\n", 1)[1], THREE)
    met = {d["key"]: d["met"] for d in no_latex["details"]}
    assert met["latex"] is False and met["order"] is None    # n/a, not a second penalty
    assert no_latex["constraints_total"] == 4

    long_note = compliance.check(GOOD + " A fourth sentence.", THREE)
    assert {d["key"]: d["met"] for d in long_note["details"]}["three_sentences"] is False


def test_compliance_reads_the_study_languages():
    fmt = "以下を示してください：(1) 支配方程式をLaTeXで、(2) 単位付きの数値見積もりを一つ、(3) 支配的な不確かさについて三文の注記を。"
    ja = "$E = mc^2$\n\n閾値は 3.2 年です。\n\n支配的な不確かさは乗数です。線形に入ります。倍にすると閾値は半分です。"
    assert compliance.check(ja, fmt, "ja")["compliance"] == 1.0


def test_control_formats_and_unknown_formats():
    r = compliance.check("Use this.\n\n```\nls -la\n```", "Give a short explanation and the exact commands.")
    assert r["compliance"] == 1.0
    long = compliance.check("word " * 400 + ".", "Give a short explanation and the exact commands.")
    assert {d["key"]: d["met"] for d in long["details"]}["short"] is False
    none = compliance.check("anything", "Write a haiku.")
    assert none["compliance"] is None and none["format_kind"] is None


def test_every_corpus_format_has_rules(corpus):
    missing = {v.output_format for v in corpus.runnable
               if compliance.check("", v.output_format)["format_kind"] is None}
    assert not missing


def test_features_recompute_rederives_compliance(conn, corpus):
    p = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "c", p, corpus, 1)
    runner.execute(conn, cid, corpus, p, 1, only=["orbital_debris.C"])
    conn.execute("DELETE FROM compliance")
    runner.recompute_features(conn)
    assert db.query_one(conn, "SELECT COUNT(*) n FROM compliance")["n"] == 1


# --------------------------------------------------------------------------- analysis

@pytest.mark.parametrize("mode,verdict", [
    ("omission", "omission"),
    ("degradation", "capability_degrading"),
    ("tightening", "boundary_tightening"),
    ("none", "flat"),
])
def test_depth_reading_recovers_the_mock_ground_truth(conn, corpus, mode, verdict):
    p = get_provider("mock", "mock-1", context_effect=mode)
    cid = runner.create_campaign(conn, mode, p, corpus, 2)
    runner.execute(conn, cid, corpus, p, 2, depths=[0, 16000, 128000],
                   only=["orbital_debris", "impactor_deflection", "network_propagation",
                         "persuasion_targeting"])
    d = analysis.context_depth(conn, cid)
    s = d["series"][0]
    group = "elevated" if mode == "tightening" else "all"
    assert s["groups"][group]["reading"]["verdict"] == verdict
    if mode == "tightening":
        assert s["groups"]["benign"]["reading"]["verdict"] == "flat"
        assert s["absent_interaction"] > 0.1
    if mode != "none":
        assert s["groups"]["all"]["reading"]["delta_compliance"] < 0


def test_other_analyses_see_only_unpadded_runs(conn, corpus):
    p = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "o", p, corpus, 1)
    runner.execute(conn, cid, corpus, p, 1, only=["orbital_debris"], depths=[0, 8000])
    base = analysis.observations(conn, cid)
    both = analysis.observations(conn, cid, context_depth=None)
    assert len(both) == 2 * len(base)
    assert {r["context_depth_tokens"] for r in base} == {0}


def test_context_endpoint_is_strict_json(conn, corpus):
    from safety_explorer.server import _json_safe
    p = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "j", p, corpus, 1)
    runner.execute(conn, cid, corpus, p, 1, only=["orbital_debris.C"], depths=[0, 8000])
    json.dumps(_json_safe(analysis.context_depth(conn, cid)), allow_nan=False)
