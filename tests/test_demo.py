"""Demo data and the Overview's cheap reads.

The demo exists so a fresh install has something on every screen, which makes one property
non-negotiable: it must be separable. Every demo row is labelled where it lands, and `clear`
removes exactly those rows — a real campaign sitting beside the demo must survive it intact.
"""

from __future__ import annotations

import time

from safety_explorer import dashboard, db, demo, sessions


def _small(monkeypatch):
    # The cued arm over one family keeps the test fast and still exercises the cue path.
    monkeypatch.setattr(demo, "CUED_FAMILIES", ["control_autonomy"])


def test_seed_fills_every_screen_and_is_idempotent(conn, corpus, monkeypatch):
    _small(monkeypatch)
    out = demo.seed(conn, corpus, repeats=1)
    assert out["campaigns"][demo.BASELINE] > 0
    assert out["campaigns"][demo.CUED] > 0
    assert out["sessions"] == len(demo.SESSIONS)

    names = {r["name"]: r for r in db.query(conn, "SELECT name, provider FROM campaign")}
    assert set(names) == {demo.BASELINE, demo.CUED}
    # Labelled where it lands: the mock provider, never a real model id.
    assert {r["provider"] for r in names.values()} == {"mock"}
    srcs = {r["source"] for r in db.query(conn, "SELECT source FROM live_session")}
    assert all(s.startswith(demo.SOURCE_PREFIX) for s in srcs)

    again = demo.seed(conn, corpus, repeats=1)
    assert again["campaigns"] == {demo.BASELINE: "present", demo.CUED: "present"}
    assert again["sessions"] == 0


def test_clear_removes_the_demo_and_nothing_else(populated, corpus, monkeypatch):
    _small(monkeypatch)
    conn, real_cid = populated
    real_runs = db.query_one(conn, "SELECT COUNT(*) AS n FROM run")["n"]
    sessions.append_turn(conn, "real-session", "user", "a real conversation", source="paste")

    demo.seed(conn, corpus, repeats=1)
    removed = demo.clear(conn)
    assert removed["campaigns"] == 2 and removed["sessions"] == len(demo.SESSIONS)

    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM run")["n"] == real_runs
    assert [r["id"] for r in db.query(conn, "SELECT id FROM campaign")] == [real_cid]
    assert [r["id"] for r in db.query(conn, "SELECT id FROM live_session")] == ["real-session"]
    # No orphans left behind in the per-run tables.
    for table, col in demo._run_children(conn):
        orphans = db.query_one(conn, f"SELECT COUNT(*) AS n FROM {table} t "
                                     f"LEFT JOIN run r ON r.id = t.{col} WHERE r.id IS NULL")
        assert orphans["n"] == 0, table


def test_simulator_streams_through_the_ingest_path(tmp_path):
    path = tmp_path / "sim.db"
    db.init_db(path).close()
    sim = demo.Simulator(str(path), interval=0.01, loop=False)
    sim.start()
    deadline = time.time() + 10
    while sim.running and time.time() < deadline:
        time.sleep(0.02)
    assert not sim.running
    assert sim.turns_sent == len(demo.STREAM_TURNS)

    conn = db.connect(path)
    rows = db.query(conn, "SELECT role FROM live_turn WHERE session_id = ? ORDER BY turn_index",
                    (sim.session_id,))
    assert [r["role"] for r in rows] == [r for r, _ in demo.STREAM_TURNS]
    sess = db.query_one(conn, "SELECT source, meta FROM live_session WHERE id = ?",
                        (sim.session_id,))
    assert sess["source"] == "demo:simulator"
    assert db.loads(sess["meta"], {})["autonomy_grant"] == 1


def test_simulator_stops_when_asked(tmp_path):
    path = tmp_path / "sim.db"
    db.init_db(path).close()
    sim = demo.Simulator(str(path), interval=0.05, loop=True)
    sim.start()
    time.sleep(0.2)
    sim.stop()
    deadline = time.time() + 5
    while sim.running and time.time() < deadline:
        time.sleep(0.02)
    assert not sim.running
    assert 0 < sim.turns_sent < len(demo.STREAM_TURNS) * 3


# --- the Overview's reads ------------------------------------------------------------------

def test_data_version_moves_with_the_data(populated):
    conn, _cid = populated
    before = dashboard.data_version(conn)
    assert dashboard.data_version(conn) == before  # stable while nothing changes
    sessions.append_turn(conn, "s1", "user", "hello", source="paste")
    assert dashboard.data_version(conn) != before


def test_overview_counts_what_is_there(populated, corpus):
    conn, cid = populated
    ov = dashboard.overview(conn, corpus)
    n = db.query_one(conn, "SELECT COUNT(*) AS n FROM run")["n"]
    assert ov["n_runs"] == n and sum(ov["tiers"].values()) == n
    assert [c["id"] for c in ov["campaigns"]] == [cid]
    assert ov["campaigns"][0]["n_runs"] == n
    assert ov["corpus"]["families"] == len(corpus.families)
    assert ov["has_demo"] is False
    assert ov["truth"]["n"] > 0 and 0.0 <= ov["truth"]["hit"] <= 1.0


def test_activity_groups_run_bursts_and_lists_turns(populated):
    conn, _cid = populated
    sessions.append_turn(conn, "s1", "user", "is anyone there", label="probe", source="paste")
    ev = dashboard.activity(conn, limit=10)
    runs = [e for e in ev if e["kind"] == "runs"]
    turns = [e for e in ev if e["kind"] == "turn"]
    # One campaign run in one burst collapses to a handful of grouped rows, not one per run.
    assert runs and sum(e["n"] for e in runs) >= 10 and len(runs) <= 3
    assert turns and turns[0]["preview"] == "is anyone there"
    assert ev == sorted(ev, key=lambda e: e["at"], reverse=True)
