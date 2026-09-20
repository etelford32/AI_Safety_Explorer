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


# --- proposals grounded in the rubric --------------------------------------

@pytest.fixture
def proposer(populated):
    from safety_explorer.providers import get_provider
    return populated[0], get_provider("mock", "mock-1")


def _any_run(conn):
    from safety_explorer import db
    return db.query(conn, "SELECT r.id FROM run r JOIN prompt p ON p.id = r.prompt_id "
                          "WHERE r.response IS NOT NULL AND p.variant = 'C' LIMIT 1")[0]["id"]


def test_a_proposal_rates_every_metric_and_labels_every_span(proposer):
    conn, provider = proposer
    out = co.propose(conn, _any_run(conn), provider)
    assert out["parsed"] is True
    assert out["problems"] == [], out["problems"]
    assert out["n_ratings"] == len(__import__("safety_explorer").rubric.load().metrics)


def test_every_rating_cites_the_spans_behind_it(proposer):
    """A level with nothing behind it is an impression. The rubric exists to make
    impressions visible, not to pretend they did not happen."""
    conn, provider = proposer
    out = co.propose(conn, _any_run(conn), provider)
    assert out["ungrounded"] == [], out["ungrounded"]
    for rating in out["ratings"]:
        if rating["level"] is not None:
            assert rating["cites"], rating


def test_a_proposal_writes_span_labels_a_human_can_argue_with(proposer):
    conn, provider = proposer
    run_id = _any_run(conn)
    co.propose(conn, run_id, provider)
    labels = co.labels_for(conn, run_id)["by_span"]
    assert labels
    assert all(cell["model"] for cell in labels.values())


def test_the_configuration_is_written_into_the_author(proposer):
    """Showing the proposer the computed evidence plainly helps, and also means part of
    what is measured is the extractor. Rather than guess, the two configurations are two
    authors and `agreement()` compares them."""
    conn, provider = proposer
    run_id = _any_run(conn)
    assert co.propose(conn, run_id, provider, show_evidence=True)["author"].endswith("+ev")
    assert co.propose(conn, run_id, provider, show_evidence=False)["author"].endswith("-ev")


# --- the coherence check and its controls ----------------------------------

def test_the_mock_proposer_is_coherent_by_construction(proposer):
    """Its ratings are computed FROM its labels, so they cannot contradict them.

    A coherence check that flags the mock is a bug in the check, not a finding.
    """
    conn, provider = proposer
    from safety_explorer import db
    for row in db.query(conn, "SELECT id FROM run WHERE response IS NOT NULL LIMIT 6"):
        out = co.propose(conn, row["id"], provider)
        if out.get("error"):
            continue
        assert out["coherence"]["coherent"] == 1.0, out["coherence"]["contradictions"]


def test_a_proposal_that_contradicts_its_own_labels_is_caught(run_with_spans):
    """The falsification test. Coherence of 1.0 everywhere means nothing until the
    check is shown to fire — the same discipline as the null control and the
    consistency floor.
    """
    import json

    from safety_explorer import rubric as rb

    conn, run_id, spans = run_with_spans
    rubric = rb.load()
    labelled = [{"index": s["index"], "label": "refusal", "confidence": 0.9,
                 "because": "declines"} for s in spans]

    def proposal(levels):
        return json.dumps({"spans": labelled, "ratings": [
            {"metric": m, "level": lv, "cites": [spans[0]["index"]], "because": "x"}
            for m, lv in levels.items()]})

    # What is honest about quantitative content depends on this run: the rule compares
    # the rating against figures the spans actually carry, so the test has to look
    # rather than assume. Getting that wrong is how a sound rule gets "fixed".
    figures = any(s["evidence"]["quantities"] for s in spans)
    honest = co.check_coherence(
        co.parse_proposal(proposal({"capability_retention": 0, "over_refusal": 5,
                                    "quantitative_content": 5 if figures else 0}),
                          spans, rubric), spans)
    lying = co.check_coherence(
        co.parse_proposal(proposal({"capability_retention": 5, "over_refusal": 0,
                                    "quantitative_content": 0 if figures else 5}),
                          spans, rubric), spans)

    assert honest["coherent"] == 1.0, honest["contradictions"]
    assert lying["coherent"] == 0.0
    assert len(lying["contradictions"]) == 3


def test_a_rule_only_fires_where_it_can_decide(run_with_spans):
    """A middling rating constrains the labels very little, and pretending otherwise
    would manufacture disagreements."""
    stats = co.SpanStats(n=10, counts={"filler": 10}, with_quantities=0,
                         recovery_after_boundary=False)
    rule = next(r for r in co.COHERENCE if r.key == "capability_high_needs_capability")
    assert rule.evaluate(3, stats)["checked"] is False
    assert rule.evaluate(5, stats)["checked"] is True
    assert rule.evaluate(None, stats)["checked"] is False


# --- malformed proposals are kept, not dropped -----------------------------

def test_an_unparseable_proposal_is_counted_not_discarded(run_with_spans):
    """A proposer that will not follow the format is a finding about the proposer.
    Dropping those rows would make every proposer look equally well-behaved."""
    from safety_explorer import rubric as rb

    _conn, _run_id, spans = run_with_spans
    out = co.parse_proposal("I'd rather not answer that.", spans, rb.load())
    assert out["parsed"] is False
    assert out["problems"] == ["response was not JSON"]


def test_a_fenced_proposal_is_still_read(run_with_spans):
    import json

    from safety_explorer import rubric as rb

    _conn, _run_id, spans = run_with_spans
    body = json.dumps({"spans": [{"index": spans[0]["index"], "label": "capability"}],
                       "ratings": []})
    out = co.parse_proposal(f"```json\n{body}\n```", spans, rb.load())
    assert out["parsed"] is True
    assert out["spans"][0]["label"] == "capability"


def test_invented_spans_and_labels_are_named_and_refused(run_with_spans):
    import json

    from safety_explorer import rubric as rb

    _conn, _run_id, spans = run_with_spans
    body = json.dumps({
        "spans": [{"index": 9999, "label": "capability"},
                  {"index": spans[0]["index"], "label": "vibes"}],
        "ratings": [{"metric": "not_a_metric", "level": 3, "cites": []},
                    {"metric": "over_refusal", "level": 11, "cites": []}]})
    out = co.parse_proposal(body, spans, rb.load())
    assert out["spans"] == []
    assert any("does not exist" in p for p in out["problems"])
    assert any("unknown label" in p for p in out["problems"])
    assert any("unknown metric" in p for p in out["problems"])
    assert any("outside 0-5" in p for p in out["problems"])


def test_an_uncited_rating_is_stored_and_flagged_not_dropped(run_with_spans):
    import json

    from safety_explorer import rubric as rb

    _conn, _run_id, spans = run_with_spans
    body = json.dumps({"spans": [], "ratings": [
        {"metric": "capability_retention", "level": 4, "cites": [], "because": "felt full"}]})
    out = co.parse_proposal(body, spans, rb.load())
    rating = out["ratings"][0]
    assert rating["level"] == 4
    assert rating["grounded"] is False
    assert any("no span cited" in p for p in out["problems"])


def test_the_mock_proposer_satisfies_every_rule_across_the_whole_range(run_with_spans):
    """Exhaustive, because sampling missed the band where it did not.

    "Coherent by construction" was asserted and tested by proposing on a handful of
    stored runs. That passed while a narrow band of capability share — between a quarter
    and three tenths, where the fixture's rounding and the rule's threshold disagree —
    produced a self-contradicting proposal. The instrument self-check found it on the
    25th conversation. Walking the whole range is what the claim actually requires.
    """
    import json
    import random

    from safety_explorer import rubric as rb
    from safety_explorer.providers.mock import MockProvider

    _conn, _run_id, spans = run_with_spans
    provider = MockProvider()
    rubric = rb.load()

    worst = 1.0
    for n_capability in range(len(spans) + 1):
        # Build a span set whose capability share sweeps 0 to 1 in real steps.
        doctored = []
        for i, s in enumerate(spans):
            evidence = dict(s["evidence"])
            hot = i < n_capability
            evidence["quantities"] = [{"value": 1.0, "target": "t", "class": "correct"}] \
                if hot else []
            evidence["refusal"] = [] if hot else (["I'm not able to"] if i % 3 else [])
            evidence["hedge"] = [] if hot else ["roughly"]
            evidence["safety_framing"] = []
            evidence["evaluation_aware"] = False
            doctored.append({**s, "evidence": evidence})

        completion = provider._propose(doctored, list(rubric.metrics),
                                       random.Random(n_capability), 0.0)
        parsed = co.parse_proposal(completion.text, doctored, rubric)
        assert parsed["parsed"], parsed["problems"]
        coherence = co.check_coherence(parsed, doctored)
        if coherence["coherent"] is not None:
            worst = min(worst, coherence["coherent"])
            assert coherence["coherent"] == 1.0, (
                n_capability, coherence["contradictions"], coherence["span_mix"],
                json.loads(completion.text)["ratings"][:2])
    assert worst == 1.0
