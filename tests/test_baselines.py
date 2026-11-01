"""Regression tests for conversation baseline integrity and fork metadata."""
import sqlite3

import pytest

from safety_explorer import baselines, sessions


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    sessions.ensure(db)
    yield db
    db.close()


def seed(conn, sid="conversation"):
    sessions.append_turn(conn, sid, "user", "Compute the orbital period", turn_index=0)
    sessions.append_turn(conn, sid, "assistant", "Approximately 90 minutes", turn_index=1)
    return sid


def test_snapshot_is_idempotent(conn):
    sid = seed(conn)
    a = baselines.snapshot(conn, sid)
    assert a == baselines.snapshot(conn, sid)
    assert len(baselines.load(conn, a["id"])["turns"]) == 2


def test_snapshot_is_immutable_when_session_grows(conn):
    sid = seed(conn)
    a = baselines.snapshot(conn, sid)
    sessions.append_turn(conn, sid, "user", "Explain why", turn_index=2)
    assert len(baselines.load(conn, a["id"])["turns"]) == 2
    assert baselines.snapshot(conn, sid)["id"] != a["id"]


def test_snapshot_can_freeze_prefix(conn):
    sid = seed(conn)
    a = baselines.snapshot(conn, sid, through_index=0)
    assert a["n_turns"] == 1


def test_snapshot_rejects_invalid_source(conn):
    with pytest.raises(ValueError, match="unknown session"):
        baselines.snapshot(conn, "missing")


def test_snapshot_rejects_empty_session(conn):
    sid = sessions.open_session(conn)
    with pytest.raises(ValueError, match="empty"):
        baselines.snapshot(conn, sid)


def test_snapshot_detects_corruption(conn):
    sid = seed(conn)
    a = baselines.snapshot(conn, sid)
    conn.execute("UPDATE conversation_baseline SET snapshot='{}' WHERE id=?", (a["id"],))
    with pytest.raises(ValueError, match="integrity"):
        baselines.load(conn, a["id"])


def test_fork_is_idempotent_and_references_baseline(conn):
    sid = seed(conn)
    a = baselines.snapshot(conn, sid)
    one = baselines.fork(conn, a["id"], 0, "risk_framing", {"variant": "neutral"})
    assert one == baselines.fork(conn, a["id"], 0, "risk_framing", {"variant": "neutral"})


def test_fork_rejects_invalid_factor_and_position(conn):
    sid = seed(conn)
    a = baselines.snapshot(conn, sid)
    with pytest.raises(ValueError, match="factor"):
        baselines.fork(conn, a["id"], 0, "unknown", {"x": 1})
    with pytest.raises(ValueError, match="point"):
        baselines.fork(conn, a["id"], 2, "context", {"x": 1})


def test_distinct_sessions_with_identical_text_have_distinct_baselines(conn):
    a = baselines.snapshot(conn, seed(conn, "first"))
    b = baselines.snapshot(conn, seed(conn, "second"))
    assert a["id"] != b["id"]
