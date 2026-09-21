"""CLI plumbing that would otherwise fail silently.

The one property pinned here is campaign resolution: `run --campaign` takes the name a
person typed, but `create_campaign` mints a separate id and the analysis filters on the id.
Without resolution, `analyse --campaign <name>` matches nothing and reports "0 of 0" — an
empty result that reads as "no data" rather than "wrong key". This is exactly the kind of
silent miss a fresh-install smoke test catches and a unit test should keep caught.
"""

from __future__ import annotations

from safety_explorer import cli, db


def _campaign(conn, name: str) -> str:
    cid = db.new_id("camp")
    db.insert(conn, "campaign", {
        "id": cid, "name": name, "created_at": db.now_iso(),
        "provider": "mock", "model_id": "mock-1", "params": {}, "n_repeats": 1,
        "corpus_version": "0", "notes": "",
    })
    conn.commit()
    return cid


def test_a_campaign_name_resolves_to_its_id():
    conn = db.init_db(":memory:")
    cid = _campaign(conn, "v1-local")
    assert cli._resolve_campaign(conn, "v1-local") == cid


def test_a_campaign_id_passes_through():
    conn = db.init_db(":memory:")
    cid = _campaign(conn, "v1-local")
    assert cli._resolve_campaign(conn, cid) == cid


def test_no_campaign_is_all_campaigns():
    conn = db.init_db(":memory:")
    assert cli._resolve_campaign(conn, None) is None
    assert cli._resolve_campaign(conn, "") is None


def test_an_unknown_name_falls_back_to_all_not_to_an_empty_filter(capsys):
    """A typo must not become a filter that silently matches nothing — it falls back to all
    campaigns and says so, so the miss is visible rather than an empty analysis."""
    conn = db.init_db(":memory:")
    _campaign(conn, "real")
    assert cli._resolve_campaign(conn, "typo") is None
    assert "no campaign named 'typo'" in capsys.readouterr().err
