"""Live sessions: a conversation an agent or app is having right now, accumulated turn
by turn.

This is the surface the instrument sits *alongside* a running agent on, and its whole
character is set by one rule carried over from the Live view: **push, never pull.** The
tool never reaches into another process's memory or scrapes a screen. A source — an agent
framework's callback, a wrapped provider, a person pasting — opts in by POSTing each turn
as it happens. That is the paste boundary generalised: emitting is a choice the source
makes, not something the tool takes.

What that buys, and what it costs, is the same as everywhere else here and is stated per
session rather than assumed:

* **Provenance is declared.** A session carries a `tier` and a `source`. An agent hook is
  Tier B unless the sampling parameters and system prompt are known; the tool does not
  silently pool a session with campaign data.
* **A session is not a run.** It carries no answer key, so Layer 0 runs only on the turns
  whose question happens to match a corpus prompt — reported per turn, never assumed.
* **The ingested text is data, not instructions.** The register estimators, the lexicon
  and the embedding model are pure functions over the text; nothing an agent emits is
  executed or fed to another model as a command. The observation surface adds no
  injection surface, by construction.

Sessions live in SQLite like everything else, so an agent watched overnight is still there
in the morning and the analysis outlives the process that produced it.
"""

from __future__ import annotations

from typing import Any

from .db import insert, new_id, now_iso, query, query_one

TIERS = ("A", "B", "C")


def ensure(conn) -> None:
    """Create the session tables if a bare connection has not seen the schema.

    The server creates them through `init_db`; this lets the module stand on its own in a
    test or a script that opened the file directly.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS live_session (
        id TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'unknown', tier TEXT NOT NULL DEFAULT 'B',
        language TEXT NOT NULL DEFAULT 'en', meta TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS live_turn (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES live_session(id) ON DELETE CASCADE,
        turn_index INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
        captured_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_turn_session "
                 "ON live_turn(session_id, turn_index)")
    conn.commit()


def open_session(conn, label: str = "", source: str = "unknown", tier: str = "B",
                 language: str = "en", meta: dict[str, Any] | None = None) -> str:
    ensure(conn)
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}, got {tier!r}")
    sid = new_id("sess")
    ts = now_iso()
    insert(conn, "live_session", {
        "id": sid, "label": label or sid, "source": source, "tier": tier,
        "language": language, "meta": meta or {}, "created_at": ts, "updated_at": ts,
    })
    conn.commit()
    return sid


class TurnConflict(ValueError):
    """A turn was posted at an index that already holds different text.

    In a chat window that is an edit or a regeneration — the conversation branched. The
    stored turn is never overwritten: a trajectory that silently changes under the reader is
    worse than none. The source is told, and starts a new session for the branch.
    """

    def __init__(self, session_id: str, turn_index: int, expected: int):
        super().__init__(f"turn {turn_index} of {session_id!r} differs from the stored turn — "
                         f"the conversation was edited or regenerated; post it as a new session")
        self.turn_index = turn_index
        self.expected = expected


class TurnGap(ValueError):
    """A turn was posted past the end of the session; the earlier turns are missing."""

    def __init__(self, session_id: str, turn_index: int, expected: int):
        super().__init__(f"turn {turn_index} posted to {session_id!r}, which has {expected} "
                         f"turn(s); send turn {expected} first")
        self.turn_index = turn_index
        self.expected = expected


def _same_text(a: str, b: str) -> bool:
    """Equal up to whitespace — a re-read of the same message from a re-rendered page must
    count as the same message, or every reload would look like an edit."""
    return " ".join((a or "").split()) == " ".join((b or "").split())


def append_turn(conn, session_id: str, role: str, text: str,
                open_if_missing: bool = True, turn_index: int | None = None,
                **open_kwargs: Any) -> dict[str, Any]:
    """Append one turn to a session, opening the session first if it does not exist.

    Opening-on-first-turn is deliberate: an agent hook should be able to post its very
    first turn with a session id it chose, without a separate handshake. The role is taken
    as given — the source stated it — so a turn is never mis-attributed by a splitter.

    `turn_index` makes the post idempotent, for a source that re-reads a whole conversation
    (the capture userscript does, on every reload): the same text at an index already
    stored is acknowledged and not duplicated, different text there raises `TurnConflict`,
    and an index past the end raises `TurnGap`. Without it, turns append in arrival order.
    """
    ensure(conn)
    if role not in ("user", "assistant", "system", "tool"):
        raise ValueError(f"unexpected role {role!r}")
    if turn_index is not None:
        turn_index = int(turn_index)
        if turn_index < 0:
            raise ValueError("turn_index must be >= 0")
    sess = query_one(conn, "SELECT * FROM live_session WHERE id = ?", (session_id,))
    if sess is None:
        if not open_if_missing:
            raise KeyError(f"no session {session_id}")
        if turn_index:
            # Never open an empty session for a post that cannot be its first turn.
            raise TurnGap(session_id, turn_index, 0)
        # Honour a caller-chosen id rather than minting a new one.
        ts = now_iso()
        insert(conn, "live_session", {
            "id": session_id, "label": open_kwargs.get("label") or session_id,
            "source": open_kwargs.get("source", "unknown"),
            "tier": open_kwargs.get("tier", "B"),
            "language": open_kwargs.get("language", "en"),
            "meta": open_kwargs.get("meta") or {}, "created_at": ts, "updated_at": ts,
        })
    n = query_one(conn, "SELECT COUNT(*) AS n FROM live_turn WHERE session_id = ?",
                  (session_id,))["n"]
    if turn_index is not None and turn_index != n:
        if turn_index > n:
            conn.commit()
            raise TurnGap(session_id, turn_index, n)
        stored = query_one(conn, "SELECT role, text FROM live_turn "
                                 "WHERE session_id = ? AND turn_index = ?",
                           (session_id, turn_index))
        if stored and stored["role"] == role and _same_text(stored["text"], text):
            conn.commit()
            return {"session_id": session_id, "turn_index": turn_index, "duplicate": True}
        conn.commit()
        raise TurnConflict(session_id, turn_index, n)
    ts = now_iso()
    insert(conn, "live_turn", {
        "id": new_id("lt"), "session_id": session_id, "turn_index": n,
        "role": role, "text": text or "", "captured_at": ts,
    })
    conn.execute("UPDATE live_session SET updated_at = ? WHERE id = ?", (ts, session_id))
    conn.commit()
    return {"session_id": session_id, "turn_index": n}


def append_paste(conn, text: str, session_id: str | None = None,
                 **open_kwargs: Any) -> dict[str, Any]:
    """Split pasted or selected text into turns and store them as a session.

    For a source that has text but no roles — a selection from a page whose markup the
    capture script does not recognise. The split is the Live view's, and so is its honesty:
    the convention that produced the turns, and whether it was confident, come back with
    the result, so a single-block "assistant" reading of an unmarked selection is stated
    rather than passed off as a conversation.
    """
    from . import live

    split = live.split_turns(text)
    if not split["turns"]:
        raise ValueError("nothing to store: the text was empty")
    sid = session_id or new_id("sel")
    meta = dict(open_kwargs.pop("meta", None) or {})
    meta.update({"split_convention": split["convention"], "split_confident": split["confident"]})
    for i, t in enumerate(split["turns"]):
        role = t["role"] if t["role"] in ("user", "assistant", "system", "tool") else "user"
        append_turn(conn, sid, role, t["text"], turn_index=i, meta=meta, **open_kwargs)
    return {"session_id": sid, "n_turns": len(split["turns"]),
            "convention": split["convention"], "confident": split["confident"],
            "note": split["note"]}


def session_drift(conn, session_id: str) -> str | None:
    """The list-level drift status of one session (quiet / watch / alert), or None."""
    sess = query_one(conn, "SELECT language FROM live_session WHERE id = ?", (session_id,))
    if sess is None:
        return None
    from . import live, stance as st
    turns = [{"role": t["role"], "text": t["text"]} for t in session_turns(conn, session_id)
             if t["role"] in ("user", "assistant")]
    report = live.analyse_turns(turns, corpus=None, cuts=None, language=sess["language"])
    return st.register_drift(report["trajectory"], report["posture_sequence"])["status"]


def list_sessions(conn, limit: int = 50, with_drift: bool = True) -> list[dict[str, Any]]:
    """Sessions newest first, each carrying a drift status so the list is a monitor.

    The drift is computed on the cheap, pure-text path — no corpus match, no posture cuts —
    because a scanning overseer needs to see WHICH session is drifting before opening it,
    and the full reading (with posture and Layer 0 where available) is a click away. The
    list-level status uses only the register channels, which need nothing but the turns.
    """
    ensure(conn)
    rows = query(conn, """
        SELECT s.*, COUNT(t.id) AS n_turns,
               SUM(CASE WHEN t.role = 'assistant' THEN 1 ELSE 0 END) AS n_assistant
        FROM live_session s LEFT JOIN live_turn t ON t.session_id = s.id
        GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?""", (limit,))
    if with_drift:
        from . import live, stance as st
        for r in rows:
            turns = [{"role": t["role"], "text": t["text"]}
                     for t in session_turns(conn, r["id"])
                     if t["role"] in ("user", "assistant")]
            report = live.analyse_turns(turns, corpus=None, cuts=None,
                                        language=r["language"])
            r["drift"] = st.register_drift(report["trajectory"],
                                           report["posture_sequence"])["status"]
    return rows


def session_turns(conn, session_id: str) -> list[dict[str, Any]]:
    ensure(conn)
    rows = query(conn, "SELECT role, text, captured_at FROM live_turn "
                       "WHERE session_id = ? ORDER BY turn_index", (session_id,))
    return rows


def analyse_session(conn, session_id: str, corpus=None,
                    cuts=None) -> dict[str, Any] | None:
    """Run the live analysis over a session's accumulated turns.

    Reuses `live.analyse_turns`, so a session and a pasted transcript get an identical
    reading — the register trajectory, the per-turn under-read flag, the embedding reading
    with its trust status, and the standing limits. The only difference is the source, and
    that difference is carried in the session's declared provenance rather than hidden.
    """
    from . import live

    sess = query_one(conn, "SELECT * FROM live_session WHERE id = ?", (session_id,))
    if sess is None:
        return None
    turns = [{"role": t["role"], "text": t["text"]}
             for t in session_turns(conn, session_id)
             if t["role"] in ("user", "assistant")]
    # A session may declare the autonomy it granted the agent (meta.autonomy_grant, 0-4 on
    # the corpus ladder). When it does, the power-seeking reading flags turns that reach
    # past it; when it does not, the reach is reported without a mandate comparison.
    from . import db
    grant = db.loads(sess["meta"], {}).get("autonomy_grant")
    report = live.analyse_turns(turns, corpus, cuts, sess["language"],
                                autonomy_grant=grant)
    report["session"] = {
        "id": sess["id"], "label": sess["label"], "source": sess["source"],
        "tier": sess["tier"], "created_at": sess["created_at"],
        "updated_at": sess["updated_at"],
    }
    # Provenance is a limit, stated with the reading, not a footnote.
    report["limits"].insert(0, (
        f"This is a live session from {sess['source']!r}, provenance Tier {sess['tier']}. "
        "It is observed, not run: the tool did not set the sampling parameters or the "
        "system prompt, and it is not pooled with campaign data."))
    return report
