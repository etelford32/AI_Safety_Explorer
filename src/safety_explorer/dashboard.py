"""The cheap reads behind the Overview: what is in the database, and what just happened.

Everything here is a handful of aggregate queries — no stance extraction, no bootstrap — so
the Overview can poll it every few seconds while an agent streams turns in. The expensive
headline numbers (the depth DiD, the power-seeking reach, the sandbagging effect) come from
their own endpoints, which the Overview loads progressively and caches against
`data_version`.
"""

from __future__ import annotations

from typing import Any

from . import db

#: Tables whose rows an analysis reads. The fingerprint moves when any of them gains or loses
#: a row; value-only rewrites (recomputing features) are handled by the client dropping its
#: cache after any action it takes itself.
_VERSIONED = ("run", "ground_truth", "annotation", "span_label", "judgement", "probe",
              "live_turn")


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def data_version(conn) -> str:
    """A fingerprint of the stored data: per table, the row count and the highest rowid.

    Count catches a delete, max rowid catches an insert that a delete elsewhere would
    otherwise balance. It is what lets the UI cache a slow analysis and still never show one
    the data has moved past.
    """
    have = _tables(conn)
    parts = []
    for t in _VERSIONED:
        if t not in have:
            parts.append("-")
            continue
        n, top = conn.execute(f"SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM {t}").fetchone()
        parts.append(f"{n}.{top}")
    return "/".join(parts)


def overview(conn, corpus=None) -> dict[str, Any]:
    """What is in the database, by the cuts a reader needs first."""
    have = _tables(conn)
    q1 = lambda sql, p=(): db.query_one(conn, sql, p)  # noqa: E731

    tiers = {r["provenance_tier"]: r["n"] for r in db.query(
        conn, "SELECT provenance_tier, COUNT(*) AS n FROM run GROUP BY provenance_tier")}
    campaigns = db.query(conn, """
        SELECT c.id, c.name, c.provider, c.model_id, c.created_at, c.n_repeats,
               COUNT(r.id) AS n_runs,
               SUM(CASE WHEN r.error IS NOT NULL AND r.error != '' THEN 1 ELSE 0 END) AS n_errors,
               SUM(CASE WHEN r.cue_id != 'none' THEN 1 ELSE 0 END) AS n_cued,
               MAX(r.captured_at) AS last_run
        FROM campaign c LEFT JOIN run r ON r.campaign_id = c.id
        GROUP BY c.id ORDER BY c.created_at DESC""")
    arms = {r["arm"] or "unmatched": r["n"] for r in db.query(conn, """
        SELECT p.arm AS arm, COUNT(*) AS n FROM run r LEFT JOIN prompt p ON p.id = r.prompt_id
        GROUP BY p.arm""")}

    truth = q1("""SELECT COUNT(*) AS n, AVG(accuracy) AS hit, AVG(graded_accuracy) AS graded
                  FROM ground_truth g JOIN run r ON r.id = g.run_id
                  WHERE r.provenance_tier = 'A' AND r.cue_id = 'none'""") \
        if "ground_truth" in have else None

    human = {
        "annotations": q1("SELECT COUNT(*) AS n FROM annotation")["n"] if "annotation" in have else 0,
        "annotated_runs": q1("SELECT COUNT(DISTINCT run_id) AS n FROM annotation")["n"]
        if "annotation" in have else 0,
        "span_labels": q1("SELECT COUNT(*) AS n FROM span_label WHERE source = 'human'")["n"]
        if "span_label" in have else 0,
    }

    sess = {"n": 0, "turns": 0, "last": None}
    if "live_session" in have:
        row = q1("SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM live_session")
        sess = {"n": row["n"], "last": row["last"],
                "turns": q1("SELECT COUNT(*) AS n FROM live_turn")["n"]}

    total = q1("SELECT COUNT(*) AS n, MAX(captured_at) AS last FROM run")
    out: dict[str, Any] = {
        "data_version": data_version(conn),
        "n_runs": total["n"],
        "last_run": total["last"],
        "tiers": tiers,
        "arms": arms,
        "campaigns": campaigns,
        "truth": {"n": truth["n"], "hit": truth["hit"], "graded": truth["graded"]}
        if truth else None,
        "human": human,
        "sessions": sess,
        "has_demo": any((c["name"] or "").startswith("demo-") for c in campaigns),
    }
    if corpus is not None:
        out["corpus"] = {"version": corpus.version, "families": len(corpus.families),
                         "runnable": len(corpus.runnable), "controls": len(corpus.controls)}
    return out


def activity(conn, limit: int = 40) -> list[dict[str, Any]]:
    """The most recent things that happened, newest first.

    Campaign runs land in bursts of hundreds within a minute, so they are grouped per
    campaign per minute — a feed of four hundred identical "run stored" lines would bury the
    one live-session turn worth seeing. Session turns are listed one by one, since each is a
    step in a conversation someone may be watching.
    """
    have = _tables(conn)
    events: list[dict[str, Any]] = []
    for r in db.query(conn, """
            SELECT COALESCE(c.name, '(no campaign)') AS campaign, r.provenance_tier AS tier,
                   r.model_id AS model, substr(r.captured_at, 1, 16) AS minute,
                   COUNT(*) AS n, MAX(r.captured_at) AS at,
                   SUM(CASE WHEN r.error IS NOT NULL AND r.error != '' THEN 1 ELSE 0 END) AS n_errors,
                   SUM(CASE WHEN r.cue_id != 'none' THEN 1 ELSE 0 END) AS n_cued
            FROM run r LEFT JOIN campaign c ON c.id = r.campaign_id
            GROUP BY r.campaign_id, minute ORDER BY at DESC LIMIT ?""", (limit,)):
        events.append({"kind": "runs", **r})
    if "live_turn" in have:
        for t in db.query(conn, """
                SELECT t.captured_at AS at, t.role, t.turn_index, s.id AS session_id,
                       s.label, s.source, substr(t.text, 1, 160) AS preview
                FROM live_turn t JOIN live_session s ON s.id = t.session_id
                ORDER BY t.captured_at DESC LIMIT ?""", (limit,)):
            events.append({"kind": "turn", **t})
    if "annotation" in have:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(annotation)")}
        if "created_at" in cols:
            for a in db.query(conn, """
                    SELECT substr(created_at, 1, 16) AS minute, MAX(created_at) AS at,
                           annotator, COUNT(*) AS n
                    FROM annotation GROUP BY annotator, minute ORDER BY at DESC LIMIT ?""",
                              (limit,)):
                events.append({"kind": "annotations", **a})
    # Turns captured in the same second (a seeded or batched session) still read newest-first.
    events.sort(key=lambda e: (e.get("at") or "", e.get("turn_index") or 0), reverse=True)
    return events[:limit]
