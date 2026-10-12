"""Amendment A1's machinery: the power simulator and the paper pipeline."""

from __future__ import annotations

import json
import re

import pytest

from safety_explorer import paper, power, runner
from safety_explorer.providers import get_provider
from safety_explorer.providers.mock import GROUND_TRUTH


# --------------------------------------------------------------------------- power

def test_mock_effect_sizes_are_per_instance():
    before = dict(GROUND_TRUTH)
    p = get_provider("mock", "mock-1", ground_truth={"depth_risk_interaction": 0.0})
    assert p.g["depth_risk_interaction"] == 0.0
    assert GROUND_TRUTH == before, "an override must never leak into the module fixture"


def test_power_detects_a_planted_effect_and_not_an_absent_one():
    null = power.simulate(("H13b", 0.0, 2, 1001, None, "corpus"))
    full = power.simulate(("H13b", 1.0, 2, 1001, None, "corpus"))
    assert not null["detected_ci"] and not null["detected_holm"]
    assert full["detected_ci"] and full["estimate"] > 0


def test_h4b_detects_its_planted_effect():
    # Only the planted case is asserted for one seed. Whether the null case fires is a
    # RATE, which is what `explorer power` exists to measure — and for H4b at few
    # families it is not negligible (see docs/PAPER.md, "What the simulator found").
    full = power.simulate(("H4b", 1.0, 2, 1001, None, "corpus"))
    assert full["detected_ci"] and full["estimate"] > 0


def test_recommendation_follows_the_amendment_rule():
    def cell(s, r, sc, rate):
        return {"spec": s, "repeats": r, "scale": sc, "rate_ci": rate}
    cells = [cell("H4b", 3, 1.0, 0.6), cell("H4b", 3, 0.0, 0.0),
             cell("H4b", 5, 1.0, 0.85), cell("H4b", 5, 0.0, 0.10),
             cell("H4b", 8, 1.0, 0.95), cell("H4b", 8, 0.0, 0.05)]
    rec = power.recommend(cells, ["H4b"], [3, 5, 8], [0.0, 1.0])
    assert rec["repeats"] == 8, "R=5 has power but its false-positive rate is too high"
    weak = [dict(c, rate_ci=0.5) if c["scale"] == 1.0 else c for c in cells]
    assert power.recommend(weak, ["H4b"], [3, 5, 8], [0.0, 1.0])["underpowered"]


def test_wilson_interval():
    assert power.wilson(0, 20)[0] == 0.0 and power.wilson(20, 20)[1] == 1.0
    lo, hi = power.wilson(10, 20)
    assert lo < 0.5 < hi


def test_observed_noise_needs_repeats(conn, corpus):
    p = get_provider("mock", "mock-1", ground_truth={"noise_sd": 0.2})
    cid = runner.create_campaign(conn, "n", p, corpus, 2)
    runner.execute(conn, cid, corpus, p, 2, only=["orbital_debris.C", "orbital_debris.D"])
    obs = power.observed_noise(conn, cid)
    assert obs["cells"] == 2 and obs["mean_sd"] is not None


# --------------------------------------------------------------------------- paper helpers

def test_macro_names_are_letters_and_distinct():
    names = {paper.macro_name(h, "est") for h in
             ["H1", "H4a", "H4b", "H10a", "H10b", "H12a", "H12b", "H13b"]}
    assert len(names) == 8
    assert all(re.fullmatch(r"[A-Za-z]+", n) for n in names)
    assert paper.macro_name("H4b", "est") == "resHFourBest"


def test_tex_escape_and_bootstrap():
    assert paper.tex_escape("a_b 5% & x≥y") == r"a\_b 5\% \& x$\geq$y"
    units = {"f1": [1.0, 1.0], "f2": [1.0, 0.9], "f3": [0.8, 1.0]}
    pt, ci, dist = paper.family_bootstrap(units, lambda xs: sum(xs) / len(xs), n_resamples=500)
    assert ci[0] <= pt <= ci[1] and paper.p_value(dist) < 0.01
    assert paper.spearman([(1, 1), (2, 2), (3, 3), (4, 4)]) == pytest.approx(1.0)
    assert paper.spearman([(1, 4), (2, 3), (3, 2), (4, 1)]) == pytest.approx(-1.0)


# --------------------------------------------------------------------------- paper build

@pytest.fixture
def campaign(conn, corpus):
    from safety_explorer.cues import load as load_cues
    p = get_provider("mock", "mock-1", context_effect="tightening")
    cid = runner.create_campaign(conn, "paper", p, corpus, 2)
    fams = ["orbital_debris", "impactor_deflection", "network_propagation",
            "persuasion_targeting", "privacy_aggregation", "structural_failure"]
    runner.execute(conn, cid, corpus, p, 2, only=fams + ["alarming_benign"],
                   depths=[0, 64000])
    runner.execute(conn, cid, corpus, p, 2, only=["orbital_debris", "impactor_deflection"],
                   cue_set=load_cues(corpus=corpus), cue_levels=[3])
    return conn, cid


def test_paper_build_writes_every_artefact(campaign, corpus, tmp_path):
    conn, cid = campaign
    res = paper.build(conn, corpus, cid, tmp_path / "gen")
    out = tmp_path / "gen"
    for f in ("results.json", "MANIFEST.json", "numbers.tex", "preamble.tex", "README.md",
              "tables/hypotheses.tex", "tables/context_depth.tex",
              "figures/context_depth.tex", "figures/ladder.tex"):
        assert (out / f).exists(), f
    ids = [r["id"] for r in res["results"]]
    assert ids == ["H1", "H2", "H3", "H4a", "H4b", "H5", "H6", "H7", "H9", "H10a", "H10b",
                   "H11b", "H12a", "H12b", "H13a", "H13b", "H13c", "H13d"]
    by = {r["id"]: r for r in res["results"]}
    # the mock's documented ground truth, recovered through the paper's own tests
    assert by["H13b"]["verdict"].startswith("supported")
    assert by["H4b"]["verdict"].startswith("supported")
    assert by["H11b"]["met"] is True
    assert by["H13c"]["estimate"] == "boundary_tightening"
    # Holm is applied to exactly the confirmatory family
    holm_ids = {r["id"] for r in res["results"] if r.get("holm")}
    assert holm_ids <= set(paper.CONFIRMATORY)
    json.loads((out / "results.json").read_text())   # strict JSON, no NaN
    tex = (out / "numbers.tex").read_text()
    names = re.findall(r"\\newcommand\{\\([^}]*)\}", tex)
    assert len(names) == len(set(names)) and all(n.isalpha() for n in names)
    assert res["validation"]["flag"], "no human ratings, so Layer 0 is not yet validated"
    man = res["manifest"]
    assert man["primary_reading"] == "truth_graded" and not man["warnings"]


def test_pre_amendment_and_stale_scores_are_flagged(campaign, corpus, tmp_path):
    conn, cid = campaign
    conn.execute("UPDATE campaign SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
                 (cid,))
    conn.execute("UPDATE ground_truth SET solver_version = '3'")
    conn.commit()
    res = paper.build(conn, corpus, cid, tmp_path / "gen")
    warnings = " ".join(res["manifest"]["warnings"])
    assert "before Amendment A1" in warnings and "re-score" in warnings
    confirm = [r for r in res["results"] if r["role"] == "confirmatory"]
    assert all("exploratory" in r["verdict"] for r in confirm)


def test_truncation_above_five_percent_is_flagged(campaign, corpus, tmp_path):
    conn, cid = campaign
    conn.execute("UPDATE run SET finish_reason = 'max_tokens' WHERE campaign_id = ? AND "
                 "context_depth_tokens = 64000 AND rowid % 5 = 0", (cid,))
    conn.commit()
    res = paper.build(conn, corpus, cid, tmp_path / "gen")
    assert any("truncated" in w for w in res["manifest"]["warnings"])
