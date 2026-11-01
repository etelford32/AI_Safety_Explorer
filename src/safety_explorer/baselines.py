"""Immutable, content-addressed experimental snapshots of existing sessions.

The source session remains authoritative and may continue growing. A fork is an
experiment specification, not a claim that a model replay has occurred.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from .db import now_iso

FACTORS = frozenset({"risk_framing", "context", "model", "sampling", "other"})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS conversation_baseline (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES live_session(id) ON DELETE RESTRICT,
        through_index INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        source_tier TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS conversation_fork (
        id TEXT PRIMARY KEY,
        baseline_id TEXT NOT NULL REFERENCES conversation_baseline(id) ON DELETE RESTRICT,
        fork_after_index INTEGER NOT NULL,
        factor TEXT NOT NULL,
        specification TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversation_fork_baseline ON conversation_fork(baseline_id)")


def snapshot(conn: sqlite3.Connection, session_id: str, through_index: int | None = None) -> dict:
    """Freeze an ordered prefix, including source metadata and turn timestamps."""
    ensure(conn)
    sess = conn.execute(
        "SELECT id,source,tier,language,meta FROM live_session WHERE id=?", (session_id,)
    ).fetchone()
    if sess is None:
        raise ValueError("unknown session")
    rows = conn.execute(
        "SELECT turn_index,role,text,captured_at FROM live_turn WHERE session_id=? ORDER BY turn_index",
        (session_id,),
    ).fetchall()
    if not rows:
        raise ValueError("cannot baseline an empty session")
    if through_index is None:
        end = len(rows) - 1
    elif type(through_index) is int and 0 <= through_index < len(rows):
        end = through_index
    else:
        raise ValueError("invalid through_index")
    prefix = rows[:end + 1]
    if any(row[0] != i for i, row in enumerate(prefix)):
        raise ValueError("noncontiguous session turns")
    payload = {
        "session_id": sess[0], "source": sess[1], "tier": sess[2],
        "language": sess[3], "meta": json.loads(sess[4]),
        "turns": [
            {"index": r[0], "role": r[1], "text": r[2], "at": r[3]}
            for r in prefix
        ],
    }
    encoded = _json(payload)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    ident = "base_" + digest
    conn.execute(
        """INSERT OR IGNORE INTO conversation_baseline
           (id,session_id,through_index,content_hash,snapshot,source_tier,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (ident, session_id, end, digest, encoded, sess[2], now_iso()),
    )
    conn.commit()
    return {"id": ident, "hash": digest, "through_index": end, "n_turns": len(prefix)}


def load(conn: sqlite3.Connection, baseline_id: str) -> dict:
    row = conn.execute(
        "SELECT snapshot,content_hash FROM conversation_baseline WHERE id=?", (baseline_id,)
    ).fetchone()
    if row is None:
        raise ValueError("unknown baseline")
    if hashlib.sha256(row[0].encode("utf-8")).hexdigest() != row[1]:
        raise ValueError("baseline integrity failure")
    return json.loads(row[0])


def fork(conn: sqlite3.Connection, baseline_id: str, fork_after_index: int,
         factor: str, specification: dict) -> dict:
    """Register an experiment; execution and results belong to later phases."""
    baseline = load(conn, baseline_id)
    if type(fork_after_index) is not int or not -1 <= fork_after_index < len(baseline["turns"]):
        raise ValueError("fork point outside baseline")
    if factor not in FACTORS:
        raise ValueError("unknown experimental factor")
    if not isinstance(specification, dict) or not specification:
        raise ValueError("experiment specification required")
    encoded = _json({
        "baseline": baseline_id, "after": fork_after_index,
        "factor": factor, "specification": specification,
    })
    ident = "fork_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT OR IGNORE INTO conversation_fork
           (id,baseline_id,fork_after_index,factor,specification,created_at)
           VALUES (?,?,?,?,?,?)""",
        (ident, baseline_id, fork_after_index, factor, _json(specification), now_iso()),
    )
    conn.commit()
    return {"id": ident, "baseline_id": baseline_id, "fork_after_index": fork_after_index}
