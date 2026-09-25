"""The posture cut points are cached against the run table, not recomputed per request.

Opening a session or pasting a conversation used to re-extract stance from every stored run
to derive the cut points — ten seconds at a few thousand runs, on every request. The cuts
are a pure function of the stored responses, so they are cached until a run is added. These
pin both halves: a repeat call does not recompute, and a new run does.
"""

from __future__ import annotations

from safety_explorer import runner, server, stance as st
from safety_explorer.providers import get_provider


def test_cuts_are_computed_once_and_reused(populated, monkeypatch):
    conn, _ = populated
    server._CUTS.update(key=None, cuts=None)
    calls = []
    real = st.calibrate
    monkeypatch.setattr(st, "calibrate", lambda rows: calls.append(1) or real(rows))

    first = server.posture_cuts(conn)
    second = server.posture_cuts(conn)
    assert first is second
    assert len(calls) == 1
    assert first is not None and first.n >= 8


def test_a_new_run_invalidates_the_cache(populated, corpus, monkeypatch):
    conn, _ = populated
    server._CUTS.update(key=None, cuts=None)
    calls = []
    real = st.calibrate
    monkeypatch.setattr(st, "calibrate", lambda rows: calls.append(1) or real(rows))

    server.posture_cuts(conn)
    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "more", provider, corpus, 1)
    runner.execute(conn, cid, corpus, provider, 1, only=["physiological_limits"])
    server.posture_cuts(conn)
    assert len(calls) == 2


def test_the_favicon_is_the_app_icon():
    """The browser tab and the .app bundle carry one icon. The web copy exists only because
    the server serves from inside the package; it must never drift from the source."""
    from pathlib import Path
    root = Path(server.__file__).resolve().parents[2]
    assert (server.WEB_ROOT / "favicon.svg").read_bytes() == \
        (root / "packaging" / "icon.svg").read_bytes()
