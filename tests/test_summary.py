"""What a finished run reports: exports by name, the twin table's arms, truncation, the summary.

Three things the first live baseline got wrong in its read-out (2026-10-13):

* `explorer export --campaign <name>` compared the name with the campaign id and wrote an
  empty file for every model;
* `analyse twins` grouped by variant letter, so the C, D and E rows pooled the English
  ladder step with the translated prompts against their English twins;
* nothing said how many responses were cut off at max_tokens, although every analysis
  drops them, and one model lost about two thirds of its pairs that way.

Each is pinned here against a mock campaign run through the real CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from safety_explorer import analysis, cli, db, summary
from safety_explorer.providers import mock as mock_mod

ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str], db_path: Path) -> int:
    return cli.main(["--db", str(db_path), "--corpus", str(ROOT / "corpus"), *args])


@pytest.fixture
def campaigns(tmp_path):
    """Two mock campaigns, run through the CLI as the baseline script runs them."""
    path = tmp_path / "explorer.db"
    assert run(["init"], path) == 0
    assert run(["run", "--campaign", "baseline-mock-a", "--provider", "mock", "--model",
                "mock-a", "--repeats", "2"], path) == 0
    assert run(["run", "--campaign", "baseline-mock-b", "--provider", "mock", "--model",
                "mock-b", "--repeats", "2", "--max-tokens", "4000"], path) == 0
    return path


def ids(path: Path) -> dict[str, str]:
    conn = db.connect(str(path))
    return {r["name"]: r["id"] for r in db.query(conn, "SELECT name, id FROM campaign")}


# -- export ---------------------------------------------------------------------------------------

def test_export_by_name_writes_the_campaigns_rows(campaigns, tmp_path, capsys):
    out = tmp_path / "a.jsonl"
    assert run(["export", "--campaign", "baseline-mock-a", "--out", str(out)], campaigns) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 218
    assert {r["campaign_id"] for r in rows} == {ids(campaigns)["baseline-mock-a"]}
    by_id = tmp_path / "b.jsonl"
    assert run(["export", "--campaign", ids(campaigns)["baseline-mock-a"], "--out",
                str(by_id)], campaigns) == 0
    assert by_id.read_text() == out.read_text()


def test_export_of_an_unknown_campaign_is_an_error_not_an_empty_file(campaigns, tmp_path, capsys):
    out = tmp_path / "x.jsonl"
    assert run(["export", "--campaign", "baseline-typo", "--out", str(out)], campaigns) == 2
    assert "no campaign named 'baseline-typo'" in capsys.readouterr().err
    assert not out.exists()


# -- the twin table -------------------------------------------------------------------------------

def test_the_twin_table_keeps_ladder_depth_and_language_apart(campaigns, capsys):
    capsys.readouterr()
    assert run(["analyse", "twins", "--campaign", "baseline-mock-a", "--source",
                "truth_graded"], campaigns) == 0
    out = capsys.readouterr().out
    ladder = out[out.index("ladder:"):out.index("depth arm:")]
    language = out[out.index("language arm:"):]
    assert "C.ja" in language and "C.ja" not in ladder
    c_row = next(line for line in ladder.splitlines() if line.strip().startswith("C "))
    assert c_row.split()[1] == "16"                 # 8 families x 2 repeats, English only
    assert "not comparable" in ladder               # B against A
    assert "Variant A states no parameters" in out


def test_summarise_deltas_labels_translations_and_orders_by_arm():
    def d(variant, sub_arm, lang="en", v=0.1, fam="f1", comparable=True):
        return {"variant": variant, "sub_arm": sub_arm, "language": lang, "family_id": fam,
                "gt_graded_delta": v if comparable else None, "gt_comparable": comparable}
    rows = analysis.summarise_deltas([
        d("C", "language", "ja", -0.2), d("C", "ladder", v=0.0), d("C", "ladder", fam="f2"),
        d("C_intro", "depth"), d("B", "ladder", comparable=False)], value="gt_graded_delta")
    assert [(r["arm"], r["variant"]) for r in rows] == [
        ("ladder", "B"), ("ladder", "C"), ("depth", "C_intro"), ("language", "C.ja")]
    assert rows[0]["effect"] == "not comparable" and rows[0]["provisional"] is False
    assert rows[1]["n"] == 2 and rows[3]["median"] == -0.2
    pooled = analysis.summarise_deltas([d("C", "language", "ja"), d("C", "ladder")],
                                       value="gt_graded_delta", split_language=False)
    assert [r["variant"] for r in pooled] == ["C"]


# -- truncation -----------------------------------------------------------------------------------

def test_run_reports_responses_cut_off_at_max_tokens(tmp_path, capsys, monkeypatch):
    real = mock_mod.MockProvider.complete

    def complete(self, messages, **kw):
        c = real(self, messages, **kw)
        if "harm" in (messages[-1].get("content") or "").lower():
            c.finish_reason = "max_tokens"
        return c
    monkeypatch.setattr(mock_mod.MockProvider, "complete", complete)
    path = tmp_path / "t.db"
    assert run(["run", "--campaign", "trunc", "--provider", "mock", "--model", "mock-1",
                "--repeats", "1"], path) == 0
    out = capsys.readouterr().out
    conn = db.connect(str(path))
    h = analysis.run_health(conn, ids(path)["trunc"])
    assert h["truncated"] > 0 and h["usable"] == h["runs"] - h["truncated"]
    assert f"{h['truncated']} of {h['runs']} response(s) were cut off at max_tokens" in out
    assert "raise it and run the model again as a new campaign" in out


def test_every_providers_word_for_cut_off_is_excluded(campaigns):
    conn = db.connect(str(campaigns))
    cid = ids(campaigns)["baseline-mock-a"]
    runs = db.query(conn, "SELECT id FROM run WHERE campaign_id = ? ORDER BY id LIMIT 3", (cid,))
    for r, reason in zip(runs, ("max_tokens", "length", "model_context_window_exceeded")):
        conn.execute("UPDATE run SET finish_reason = ? WHERE id = ?", (reason, r["id"]))
    conn.execute("UPDATE run SET finish_reason = 'refusal' WHERE id = (SELECT id FROM run "
                 "WHERE campaign_id = ? ORDER BY id DESC LIMIT 1)", (cid,))
    conn.commit()
    h = analysis.run_health(conn, cid)
    assert h["truncated"] == 3 and h["refusal_stops"] == 1 and h["usable"] == 215
    kept = {o["run_id"] for o in analysis.observations(conn, cid)}
    assert not kept & {r["id"] for r in runs}


# -- the summary ----------------------------------------------------------------------------------

def test_summary_puts_the_campaigns_side_by_side_and_says_what_differs(campaigns, tmp_path, capsys):
    capsys.readouterr()
    out_dir = tmp_path / "summary"
    assert run(["summary", "--prefix", "baseline-", "--out", str(out_dir)], campaigns) == 0
    text = capsys.readouterr().out
    for heading in ("run health", "API refusal stops by family", "part 1 · the API stop",
                    "part 1 · change in the stop", "part 2 · where answered", "all pairs",
                    "controls:"):
        assert heading in text
    assert "baseline-mock-a" in text and "baseline-mock-b" in text
    assert "F vs C  follow-up after E (recovery)" in text
    assert "differ in settings" in text and "max_tokens 4000" in text
    s = json.loads((out_dir / "summary.json").read_text())
    assert [c["name"] for c in s["campaigns"]] == ["baseline-mock-a", "baseline-mock-b"]
    a = s["campaigns"][0]
    assert a["health"]["usable"] == 218 and a["part2"]["ladder"]["C"]["n"] == 16
    assert a["part2"]["ladder"]["F"]["n"] and set(a["part2"]["language"]) == {"es", "fr", "ja"}
    assert a["part1"]["changes"]["E"] == {"n": 16, "n_families": 8, "mean": 0.0,
                                          "ci95": [0.0, 0.0]}      # the mock never stops
    assert a["blocked_families"] == {}
    assert (out_dir / "SUMMARY.md").read_text().startswith("# Summary: baseline-mock-a")


def test_the_summary_files_carry_no_prompt_or_response_text(campaigns, tmp_path):
    out_dir = tmp_path / "s"
    assert run(["summary", "--out", str(out_dir)], campaigns) == 0
    shared = (out_dir / "summary.json").read_text() + (out_dir / "SUMMARY.md").read_text()
    conn = db.connect(str(campaigns))
    for r in db.query(conn, "SELECT r.response, p.text FROM run r JOIN prompt p "
                            "ON p.id = r.prompt_id LIMIT 40"):
        for text in (r["response"], r["text"]):
            snippet = " ".join((text or "").split())[:60]
            if len(snippet) > 30:
                assert snippet not in shared


def test_summary_flags_heavy_truncation_and_names_the_campaign(campaigns):
    conn = db.connect(str(campaigns))
    cid = ids(campaigns)["baseline-mock-b"]
    conn.execute("UPDATE run SET finish_reason = 'max_tokens' WHERE id IN (SELECT id FROM run "
                 "WHERE campaign_id = ? LIMIT 30)", (cid,))
    conn.commit()
    from safety_explorer import corpus as corpus_mod
    s = summary.build(conn, corpus_mod.load(ROOT / "corpus"),
                      summary.select(conn, ["baseline-mock-a", "baseline-mock-b"]))
    warn = " ".join(s["warnings"])
    assert "baseline-mock-b: 30 of 218 responses (14%) were cut off" in warn
    assert "baseline-mock-a:" not in warn.split("differ")[0]
    with pytest.raises(LookupError):
        summary.select(conn, ["nope"])


def test_the_summary_shows_what_a_zero_delta_is_made_of(campaigns):
    """A pair where the API stopped both sides scores 0 against 0 and reads as "no change".
    The summary must show that, rather than leave a median of zero to be read as a finding."""
    conn = db.connect(str(campaigns))
    cid = ids(campaigns)["baseline-mock-a"]
    fams = ("orbital_debris", "impactor_deflection", "network_propagation")   # those with ja
    stop = [r["id"] for r in db.query(conn, f"""
        SELECT r.id FROM run r JOIN prompt p ON p.id = r.prompt_id
        WHERE r.campaign_id = ? AND p.variant = 'C' AND p.family_id IN ({",".join("?" * 3)})
          AND (p.sub_arm = 'ladder' OR (p.sub_arm = 'language' AND p.language = 'ja'))""",
        (cid, *fams))]
    assert len(stop) == 12                       # 3 families x 2 repeats, English and Japanese
    for rid in stop:
        conn.execute("UPDATE run SET finish_reason = 'refusal', stop_details = ? WHERE id = ?",
                     (json.dumps({"type": "refusal", "category": "cyber"}), rid))
        conn.execute("UPDATE ground_truth SET graded_accuracy = 0 WHERE run_id = ?", (rid,))
    conn.commit()
    from safety_explorer import corpus as corpus_mod
    s = summary.build(conn, corpus_mod.load(ROOT / "corpus"),
                      summary.select(conn, ["baseline-mock-a"]))
    c = s["campaigns"][0]
    assert c["refusal_categories"] == {"cyber": 12}
    assert c["stops_by_family"]["orbital_debris"] == {"calls": 36, "stopped": 4}
    assert c["stops_by_family"]["structural_failure"]["stopped"] == 0
    assert list(c["stops_by_family"])[-1] == "controls"
    rates = c["part1"]["rates"]
    assert rates["C"] == {"runs": 16, "api_refusal_stops": 6}
    # Part 1: C was stopped and D was not, so the paired change in the stop at D is negative.
    assert c["part1"]["changes"]["D"]["mean"] < 0
    # Part 2: the stopped C runs and every pair they are in are gone; the rest remain.
    assert c["part2"]["levels"]["C"]["scored"] == 10
    assert c["part2"]["levels"]["A"]["mean_graded"] is None       # A has no answer key
    assert c["part2"]["ladder"]["D"]["n"] == 10
    assert c["part2"]["language"]["ja"]["n"] == 12                # D.ja and E.ja pairs only
    # All pairs (robustness): the Japanese C pairs tie at zero, both sides stopped.
    ja = c["all_pairs"]["ties"]["lang:ja"]
    assert ja["both_stopped"] == 6 and ja["both_zero"] >= 6
    text = summary.render_text(s)
    assert "pairs tied at 0" in text and "6/16 (38%)" in text
    assert "cyber 12" in text
    md = summary.render_markdown(s)
    assert "part 2 · where answered" in md
    s["campaigns"][0]["health"]["refusal_stops"] = 120          # as in the first live baseline
    assert any("the API stopped 120 of 218 calls" in w for w in summary.warnings(s["campaigns"]))


def test_a_family_the_api_blocks_wholesale_is_named(campaigns):
    conn = db.connect(str(campaigns))
    cid = ids(campaigns)["baseline-mock-a"]
    conn.execute("UPDATE run SET finish_reason = 'refusal' WHERE campaign_id = ? AND prompt_id IN "
                 "(SELECT id FROM prompt WHERE family_id = 'network_propagation')", (cid,))
    conn.commit()
    from safety_explorer import corpus as corpus_mod
    s = summary.build(conn, corpus_mod.load(ROOT / "corpus"), summary.select(conn, ["baseline-mock-a"]))
    c = s["campaigns"][0]
    assert c["blocked_families"] == {"network_propagation": {"calls": 6, "stopped": 6}}
    assert any("network_propagation is blocked: the API stopped 6 of 6 of its benign calls"
               in w for w in s["warnings"])
    text = summary.render_text(s)
    assert "36/36 (100%) blocked" in text
    # Part 1 keeps it (the topic is part of the boundary); part 2 leaves it out.
    assert c["part1"]["rates"]["A"]["api_refusal_stops"] == 2
    assert c["part2"]["ladder"]["C"]["n_families"] == 7
