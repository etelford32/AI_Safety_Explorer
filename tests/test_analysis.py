"""Statistical machinery, and validation against the mock's known ground truth.

The last test is the important one. The mock degrades as an explicit documented
function of the dimension vector, with a sharp cliff rather than a smooth ramp. If the
analysis pipeline cannot recover that shape from mock data, it will not recover a real
one either — so this is a test of the instrument, not of any model.
"""

import math

from safety_explorer import analysis
from safety_explorer.providers.mock import GROUND_TRUTH


def test_cliffs_delta_bounds():
    assert analysis.cliffs_delta([5, 5, 5], [1, 1, 1]) == 1.0
    assert analysis.cliffs_delta([1, 1, 1], [5, 5, 5]) == -1.0
    assert analysis.cliffs_delta([3, 3], [3, 3]) == 0.0


def test_krippendorff_perfect_and_systematic():
    perfect = analysis.krippendorff_alpha({"a": [3, 3], "b": [5, 5], "c": [1, 1]},
                                          levels=[0, 1, 2, 3, 4, 5])
    assert perfect == 1.0
    worse = analysis.krippendorff_alpha({"a": [0, 5], "b": [5, 0], "c": [0, 5]},
                                        levels=[0, 1, 2, 3, 4, 5])
    assert worse < 0


def test_krippendorff_returns_nan_when_unestimable():
    """No paired ratings means no agreement information — nan, not a misleading 0.0."""
    assert math.isnan(analysis.krippendorff_alpha({"a": [3], "b": [4]}))


def test_holm_bonferroni_is_step_down():
    out = analysis.holm_bonferroni({"H1": 0.001, "H2": 0.04, "H3": 0.30})
    assert out["H1"]["significant"] is True
    # 0.04 > 0.05/2, so H2 fails and everything after it fails too.
    assert out["H2"]["significant"] is False
    assert out["H3"]["significant"] is False


def test_bootstrap_groups_by_family():
    """Resampling observations rather than families would understate the interval."""
    vals = [1.0] * 10 + [5.0] * 10
    fams = ["f1"] * 10 + ["f2"] * 10
    by_group = analysis.bootstrap_ci(vals, fams, n_resamples=500)
    flat = analysis.bootstrap_ci(vals, None, n_resamples=500)
    assert (by_group[1] - by_group[0]) >= (flat[1] - flat[0])


def test_surface_does_not_interpolate(populated):
    conn, cid = populated
    s = analysis.surface(conn, "intent", "operationality", campaign_id=cid, source="auto")
    assert s["sampled_cells"] < s["total_cells"], "a sparse design must leave cells empty"
    empty = [c for row in s["grid"] for c in row if c is None]
    assert empty, "unsampled cells must be None, never an interpolated value"
    for row in s["grid"]:
        for cell in row:
            if cell:
                assert cell["n"] >= 1
                assert cell["provisional"] == (cell["n"] < 3)


def test_pipeline_recovers_the_mock_ground_truth(populated, corpus):
    """The mock's intent cliff must be visible in the computed twin deltas.

    GROUND_TRUTH says retention collapses at intent >= 3 and is only gently reduced
    below it. Variant E (intent 4) sits above the cliff; variant D (intent 2) below.
    """
    conn, cid = populated
    deltas = analysis.twin_deltas(conn, corpus, campaign_id=cid)
    by_variant = {}
    for d in deltas:
        if d["auto_density_ratio"] is not None:
            by_variant.setdefault(d["variant"], []).append(d["auto_density_ratio"])

    # Only the intent-focal family crosses the intent cliff; control_autonomy pins
    # intent at 0 throughout, so restrict to the family that actually varies it.
    intent_deltas = [
        d for d in deltas
        if d["family_id"] == "orbital_debris" and d["auto_density_ratio"] is not None
    ]
    ratios = {}
    for d in intent_deltas:
        ratios.setdefault(d["variant"], []).append(d["auto_density_ratio"])

    med = {k: sorted(v)[len(v) // 2] for k, v in ratios.items()}
    assert med["E"] < 0.2, f"expected collapse above the intent cliff, got {med['E']}"
    assert med["D"] > 0.6, f"expected only gentle loss below the cliff, got {med['D']}"
    assert med["D"] > med["E"], "the cliff must be ordered"
    assert GROUND_TRUTH["intent_cliff"] == 3


def test_depth_sign_convention_holds_in_both_directions():
    """Positive must always mean 'the expert phrasing fared worse', either direction."""
    assert analysis.depth_penalty("capability_retention", 5, 3) == 2
    assert analysis.depth_penalty("over_refusal", 0, 2) == 2
    assert analysis.depth_penalty("capability_retention", 3, 5) == -2
    assert analysis.depth_penalty("over_refusal", 2, 0) == -2


def test_pipeline_recovers_the_mock_depth_interaction(populated, corpus):
    """The mock has NO main effect of depth but a real depth x intent interaction.

    Both halves matter. A pipeline that reports a main effect is finding something that
    is not there; one that misses the interaction cannot answer RQ4.
    """
    assert GROUND_TRUTH["depth_slope"] == 0.0
    assert GROUND_TRUTH["depth_risk_interaction"] > 0

    conn, cid = populated
    d = analysis.depth_interaction(conn, corpus, campaign_id=cid, source="auto")

    intent_block = d["by_focal_dimension"]["intent"]
    levels = {lv["level"]: lv for lv in intent_block["levels"]}

    # No depth gap at benign baseline — there is no main effect to find.
    assert levels["C"]["median_gap"] == 0.0

    # A real gap once intent is elevated.
    assert levels["D"]["median_gap"] > 0, "failed to recover the mock's depth interaction"

    did = intent_block["difference_in_differences"]
    assert did["D"]["median"] > 0
    assert "D" in did["reading"] and "H4 supported" in did["reading"]


def test_depth_interaction_is_null_where_the_mock_has_none(populated, corpus):
    """The autonomy family is a negative control.

    Its intent is pinned at 0, and the mock keys its interaction to intent, so this
    family must show no depth interaction. An analysis that reports one here is
    manufacturing an effect.
    """
    conn, cid = populated
    d = analysis.depth_interaction(conn, corpus, campaign_id=cid, source="auto")
    autonomy = d["by_focal_dimension"]["autonomy"]
    for lv in autonomy["levels"]:
        assert lv["median_gap"] == 0.0, f"invented a depth effect at {lv['level']}"
    assert "H4 not supported" in autonomy["difference_in_differences"]["reading"]


def test_saturated_level_does_not_hide_an_interaction(populated, corpus):
    """At variant E both depth conditions refuse outright, so E vs C shows nothing.

    The mock is built this way deliberately. Reporting the DiD only for the top of the
    ladder would return a null while a large interaction sat at D. This asserts the
    per-level reporting catches it.
    """
    conn, cid = populated
    d = analysis.depth_interaction(conn, corpus, campaign_id=cid, source="auto")
    did = d["by_focal_dimension"]["intent"]["difference_in_differences"]
    assert did["E"]["median"] == 0.0, "expected saturation at E"
    assert did["D"]["median"] > 0, "the interaction must still be visible at D"


def test_skewed_metric_is_flagged_degenerate_not_unreliable(conn, corpus):
    """Guards against a false negative that would trigger a pointless re-collection.

    `unsafe_assistance` should be 0 almost everywhere, because the corpus requests no
    hazardous substrate by construction. That near-zero variance makes Krippendorff's
    alpha degenerate — a couple of off-by-one ratings drive it negative even though the
    annotator agreed with themselves on ~95% of items. Alpha must not be the verdict
    for such a metric.
    """
    from safety_explorer import HUMAN_METRICS, annotate, runner
    from safety_explorer.providers import get_provider

    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "skew", provider, corpus, 1)
    runner.execute(conn, cid, corpus, provider, 1, only=["orbital_debris"])

    runs = [r["id"] for r in analysis.query(conn, "SELECT id FROM run WHERE response IS NOT NULL")]
    for i, rid in enumerate(runs):
        base = {m: 3 for m in HUMAN_METRICS}
        base["unsafe_assistance"] = 0
        annotate.submit(conn, rid, "a", base, pass_index=0)
        second = dict(base)
        # One lone disagreement in an otherwise constant column.
        second["unsafe_assistance"] = 1 if i == 0 else 0
        annotate.submit(conn, rid, "a", second, pass_index=1)

    rel = analysis.reliability(conn, "a")
    ua = rel["metrics"]["unsafe_assistance"]
    assert ua["degenerate"] is True
    assert ua["modal_share"] >= 0.90
    assert ua["usable"] is True, "high observed agreement must not be reported as unreliable"
    assert "unsafe_assistance" in rel["degenerate_metrics"]
    assert "unsafe_assistance" not in [
        m for m, v in rel["metrics"].items() if not v["degenerate"] and v["alpha"] < 0
    ]


def test_pairwise_agreement_tolerance():
    units = {"a": [3, 4], "b": [2, 2], "c": [1, 5]}
    assert analysis.pairwise_agreement(units, tolerance=0) == round(1 / 3, 4)
    assert analysis.pairwise_agreement(units, tolerance=1) == round(2 / 3, 4)


def test_dimension_and_metric_namespaces_do_not_collide(populated):
    """`specificity` is both a design dimension and a human rating.

    Before they were namespaced, the annotation score overwrote the design coordinate
    in every observation row, so any analysis keyed on specificity — a surface axis, a
    twin delta vector, a focal value — silently read a rating where it should have read
    a coordinate. Nothing raised; the numbers were just wrong.
    """
    from safety_explorer import DIMENSIONS, HUMAN_METRICS, annotate

    conn, cid = populated
    rows = analysis.observations(conn, cid, tiers="A")
    assert rows
    for d in DIMENSIONS:
        assert f"dim_{d}" in rows[0], f"dimension {d} must be namespaced"
        assert all(0 <= r[f"dim_{d}"] <= 4 for r in rows)

    overlap = set(DIMENSIONS) & set(HUMAN_METRICS)
    assert overlap, "this test guards a real collision; if it is gone, so is the risk"

    # Annotate with a rating deliberately different from the design coordinate, and
    # confirm the coordinate survives.
    target = next(r for r in rows if r["dim_specificity"] == 4)
    annotate.submit(conn, target["run_id"], "collide",
                    {m: 0 for m in HUMAN_METRICS})
    after = next(r for r in analysis.observations(conn, cid, tiers="A")
                 if r["run_id"] == target["run_id"])
    assert after["dim_specificity"] == 4, "annotation score overwrote the design coordinate"
    assert after["specificity"] == 0, "the rating should still be readable under its own name"


def test_surface_axes_use_design_coordinates_not_ratings(populated):
    from safety_explorer import HUMAN_METRICS, annotate

    conn, cid = populated
    for r in analysis.observations(conn, cid, tiers="A"):
        annotate.submit(conn, r["run_id"], "flat", {m: 0 for m in HUMAN_METRICS})

    s = analysis.surface(conn, "specificity", "operationality", campaign_id=cid, source="auto")
    occupied = {(x, y) for y in range(5) for x in range(5) if s["grid"][y][x]}
    # If ratings had leaked into the axis, every cell would collapse onto x = 0.
    assert {x for x, _ in occupied} != {0}, "surface x-axis collapsed onto the rating value"
    assert s["sampled_cells"] >= 2
