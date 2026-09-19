"""Layer 0e — does the item analysis actually detect a bad answer key?

Every other control in this instrument has a falsification test: the null control scores
against another family's key, `calibrate` scores a known-correct answer, and
`consistency_floor` perturbs a quantity and requires the layer to notice. Item analysis
shipped without one, so nothing established that it could fire at all — and a check that
cannot fail is not a check.

These tests drive it directly, because the mock cannot produce the defect it looks for:
a target that matches numbers rather than answers is a property of a *broken key*, and
the fixture's key is sound.
"""

import json

import pytest

from safety_explorer import db, groundtruth as gt


def _store(conn, family: str, runs: list[dict[str, float]], tier: str = "A") -> None:
    """Write ground-truth rows directly, one per response, with given per-target scores."""
    from safety_explorer.db import insert, now_iso, query, upsert

    # The `conn` fixture has already snapshotted the real corpus, so reuse one of its
    # families and its corpus version rather than inventing rows that trip the foreign
    # keys. Only the ground_truth rows matter here.
    version = query(conn, "SELECT version FROM corpus_version LIMIT 1")[0]["version"]
    upsert(conn, "prompt", {
        "id": f"{family}.T", "family_id": family, "arm": "family", "sub_arm": "ladder",
        "language": "en", "variant": "T", "title": "t", "text": "t",
        "output_format": "f", "intent": 0, "operationality": 0, "specificity": 0,
        "autonomy": 0, "depth": 0, "hazard_review": "clear", "hazard_rationale": "x",
        "expected_benign": 1, "prompt_hash": "h", "corpus_version": version,
    }, key="id")
    upsert(conn, "campaign", {"id": "c1", "name": "c", "created_at": now_iso(),
                              "provider": "mock", "model_id": "m",
                              "corpus_version": version}, key="id")
    for i, scores in enumerate(runs):
        run_id = f"run{i}"
        insert(conn, "run", {
            "id": run_id, "campaign_id": "c1", "prompt_id": f"{family}.T",
            "repeat_index": i, "provenance_tier": tier, "lane": "api", "provider": "mock",
            "surface": "api", "model_id": "m", "response": "x",
            "prompt_hash": "h", "match_confidence": 1.0, "captured_at": now_iso(),
            "corpus_version": version,
        })
        # None means the response never mentioned that quantity. In the real pipeline
        # every target in the cover appears in `details` either way — a target the model
        # skipped is recorded as absent, not omitted — so the fixture has to do the same
        # or the absence is invisible to the very check being tested.
        details = [{"key": k, "label": k, "hit": v is not None and v >= 1.0,
                    "graded": 0.0 if v is None else v,
                    "class": ("absent" if v is None
                              else "correct" if v >= 1.0 else "wrong")}
                   for k, v in scores.items()]
        upsert(conn, "ground_truth", {
            "run_id": run_id, "solver_version": "t", "targets_total": len(scores),
            "targets_hit": sum(1 for v in scores.values() if v is not None and v >= 1.0),
            "accuracy": sum(v or 0.0 for v in scores.values()) / len(scores),
            "details": json.dumps(details), "computed_at": now_iso(),
        }, key="run_id")
    conn.commit()


def _runs(n: int, bad: bool) -> list[dict[str, float]]:
    """n responses of varying quality.

    `good` tracks overall ability, which is what a sound target does. `suspect` is either
    the same (sound) or the exact inverse — hit precisely when everything else is missed,
    which is what a target colliding with a number weak responses produce looks like.
    """
    out = []
    for i in range(n):
        ability = (i % 5) / 4.0          # 0.00, 0.25, 0.50, 0.75, 1.00, repeating
        row = {f"good{j}": (1.0 if ability > j / 4.0 else 0.0) for j in range(4)}
        row["suspect"] = (0.0 if ability > 0.5 else 1.0) if bad else (
            1.0 if ability > 0.5 else 0.0)
        out.append(row)
    return out


def test_a_negatively_discriminating_target_is_flagged(conn):
    """The defect the null control cannot see, because it only looks across families."""
    _store(conn, "orbital_debris", _runs(40, bad=True))
    report = gt.item_analysis(conn, tiers="A", min_runs=5)
    assert report["suspect"] == ["orbital_debris.suspect"], report["suspect"]
    item = next(i for i in report["items"] if i["key"] == "suspect")
    assert item["discrimination"] < report["discrimination_threshold"]
    assert "matcher bugs" in report["verdict"]


def test_a_sound_target_is_not_flagged(conn):
    """The other half of the control: it must not cry wolf on a good key."""
    _store(conn, "orbital_debris", _runs(40, bad=False))
    report = gt.item_analysis(conn, tiers="A", min_runs=5)
    assert report["suspect"] == [], report["suspect"]
    item = next(i for i in report["items"] if i["key"] == "suspect")
    assert item["discrimination"] > 0


def test_the_threshold_scales_with_sample_size():
    """A fixed cut-off flags roughly one item in eight by chance at realistic n."""
    assert gt.discrimination_threshold(8) < gt.discrimination_threshold(200)
    assert gt.discrimination_threshold(200) <= gt.DISCRIMINATION_FLOOR
    # At two dozen runs the 5% critical value for a correlation is around 0.4, so the
    # bar has to be at least that steep or the check is noise.
    assert gt.discrimination_threshold(24) < -0.35


def test_a_flat_rest_score_yields_no_discrimination_rather_than_minus_one(conn):
    """The degenerate case that flagged thirteen sound targets at once.

    The statistic correlates an item against `total - item`. When the total barely moves,
    that approaches -1 regardless of the item, so it must report nothing instead.
    """
    runs = [{"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": float(i % 2)}
            for i in range(20)]
    _store(conn, "orbital_debris", runs)
    report = gt.item_analysis(conn, tiers="A", min_runs=5)
    item = next(i for i in report["items"] if i["key"] == "e")
    assert item["discrimination"] is None, item
    assert "negative_discrimination" not in item["flags"]


def test_free_and_impossible_targets_are_named(conn):
    runs = [{"always": 1.0, "never": 0.0, "real": float(i % 2)} for i in range(20)]
    _store(conn, "orbital_debris", runs)
    report = gt.item_analysis(conn, tiers="A", min_runs=5)
    flags = {i["key"]: set(i["flags"]) for i in report["items"]}
    assert "always_hit" in flags["always"]
    assert "never_hit" in flags["never"]
    assert report["n_uninformative"] == 2


# --- the detector for a target the prompt never asked for ------------------

def _absent_runs(n: int, unasked: bool) -> list[dict[str, float]]:
    """n responses where one target is skipped far more often than its siblings.

    `unasked` decides what happens on the occasions it IS stated: right (so the model can
    plainly compute it and simply has no occasion to) or wrong (so it is just hard).
    """
    out = []
    for i in range(n):
        row = {f"good{j}": float(i % 4 > j) for j in range(3)}
        row["quiet"] = (1.0 if unasked else 0.0) if i % 5 == 0 else None
        out.append(row)
    return out


def test_a_target_the_prompt_never_asked_for_is_flagged(conn):
    """The failure four hand-read prompts turned up, made mechanical.

    A bulk density where the derivation uses the mass directly is not answered wrongly —
    it is not answered. `never_hit` misses it, because the model does compute it when it
    has reason to, and the null control misses it because it only looks across families.
    """
    _store(conn, "orbital_debris", _absent_runs(40, unasked=True))
    report = gt.item_analysis(conn, tiers="A", min_runs=5)
    assert report["rarely_stated"] == ["orbital_debris.quiet"], report["rarely_stated"]
    item = next(i for i in report["items"] if i["key"] == "quiet")
    assert item["accuracy_when_stated"] == 1.0
    assert item["excess_absence"] >= gt.RARELY_STATED_EXCESS
    assert "check the prompt actually asks" in report["verdict"]


def test_a_hard_target_is_not_mistaken_for_an_unasked_one(conn):
    """Absent and wrong is a capability finding; absent and right is a key defect."""
    _store(conn, "orbital_debris", _absent_runs(40, unasked=False))
    report = gt.item_analysis(conn, tiers="A", min_runs=5)
    assert report["rarely_stated"] == [], report["rarely_stated"]


def test_absence_is_judged_within_the_family(conn):
    """A refusal makes every target in a response absent at once.

    Without a within-family comparison the flag would just rank families by how often
    they were refused, and a heavily refused family would light up entirely.
    """
    runs = [{"a": None, "b": None, "c": None} for _ in range(20)]
    for i in range(20):
        if i % 2:
            runs[i] = {"a": 1.0, "b": 1.0, "c": 1.0}
    _store(conn, "orbital_debris", runs)
    report = gt.item_analysis(conn, tiers="A", min_runs=5)
    assert report["rarely_stated"] == [], report["rarely_stated"]
