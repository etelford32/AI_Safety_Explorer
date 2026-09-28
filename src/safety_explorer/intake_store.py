"""Storing what `intake` detected — idempotent, incremental, and explicit about where each
thing went.

Import the same ChatGPT export twice and nothing is added the second time; import a newer
one and only the turns said since are appended. That is what makes a *watched* source
possible (a Claude Code log that grows while an agent works is re-read, and the new turns
land in the same session), and it is what lets a user drop a file without first wondering
whether they already did.

Where things go:

* **A conversation becomes a session** (`live_session` + `live_turn`), with the source and
  provenance tier its format earned, and its own timestamps. Its id is derived from the
  source's own conversation id, so re-imports find it. If the conversation was edited or
  regenerated since the last import — the stored turns are no longer a prefix of what
  arrived — the stored session is never rewritten: the new version becomes a branch
  session beside it, as the capture userscript does.
* **Its first question may also become a run.** When a conversation *opens* with a corpus
  prompt (verbatim, or with near-total overlap), its first reply is a response to that
  prompt under chat-surface conditions — exactly a Tier B capture — so it is stored as a
  run and scored against the answer key, and it counts in Results. Only the opening
  question qualifies: asked mid-conversation, the same words arrive with context the
  corpus prompt never had, and pooling that reply would confound every comparison.
* **A prompt/response pair** becomes a run when its prompt matches a corpus prompt, and a
  two-turn session otherwise — so everything lands somewhere it gets read, and the
  unmatched rows are not left in a triage queue nobody asked for.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Iterable

from . import UNOBSERVABLE, intake
from .db import insert, now_iso, query, query_one

#: Overlap needed before an imported question is treated as a corpus prompt *without a
#: human confirming it*. Stricter than the Tier C import's 0.6, which leaves every row in
#: front of a person to triage.
AUTO_MATCH = 0.9

_SURFACE = {"chatgpt_export": "web_chat", "claude_export": "web_chat", "claude_code": "cli",
            "codex_cli": "cli"}


def session_id_for(batch: intake.Batch, conv: intake.Conversation) -> str:
    """Stable per source conversation: the same export imported twice maps to the same ids."""
    h = hashlib.sha1(f"{batch.format}|{conv.key}".encode("utf-8", "replace")).hexdigest()[:16]
    return f"imp_{h}"


def _stored_turns(conn, sid: str) -> list[dict[str, Any]]:
    return query(conn, "SELECT role, text FROM live_turn WHERE session_id = ? ORDER BY turn_index", (sid,))


def _same(a: str, b: str) -> bool:
    return " ".join((a or "").split()) == " ".join((b or "").split())


def _is_prefix(stored: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> bool:
    if len(stored) > len(incoming):
        return False
    return all(s["role"] == t["role"] and _same(s["text"], t["text"]) for s, t in zip(stored, incoming))


def store_conversation(conn, batch: intake.Batch, conv: intake.Conversation,
                       extra_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Store one conversation. Returns {session_id, status, added} where status is one of
    new | updated | unchanged | branched."""
    base = session_id_for(batch, conv)
    sid, status = base, "new"
    stored = _stored_turns(conn, base)
    if stored:
        if _is_prefix(stored, conv.turns):
            status = "unchanged" if len(stored) == len(conv.turns) else "updated"
        elif _is_prefix(conv.turns, stored):
            # An older copy of a conversation already stored further along.
            return {"session_id": base, "status": "unchanged", "added": 0}
        else:
            k = 1
            while True:
                sid = f"{base}~b{k}"
                stored = _stored_turns(conn, sid)
                if not stored:
                    status = "branched"
                    break
                if _is_prefix(stored, conv.turns):
                    status = "unchanged" if len(stored) == len(conv.turns) else "updated"
                    break
                if _is_prefix(conv.turns, stored):
                    return {"session_id": sid, "status": "unchanged", "added": 0}
                k += 1
    now = now_iso()
    if not stored:
        meta = {"format": batch.format, "origin": batch.origin, "key": conv.key,
                "model": conv.model, "imported_at": now,
                **{k: v for k, v in (conv.meta or {}).items() if v not in (None, "", [], {})},
                **(extra_meta or {})}
        if status == "branched":
            meta["branch_of"] = base
        insert(conn, "live_session", {
            "id": sid, "label": (conv.title or sid)[:200], "source": batch.source,
            "tier": batch.tier, "language": (conv.meta or {}).get("language") or "en", "meta": meta,
            "created_at": conv.created_at or now,
            "updated_at": conv.updated_at or conv.created_at or now,
        })
    new = conv.turns[len(stored):]
    if new:
        from .db import new_id
        conn.executemany(
            "INSERT INTO live_turn (id, session_id, turn_index, role, text, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(new_id("lt"), sid, len(stored) + i, t["role"], t["text"], t.get("at") or now)
             for i, t in enumerate(new)])
        if stored:
            conn.execute("UPDATE live_session SET updated_at = MAX(updated_at, ?) WHERE id = ?",
                         (conv.updated_at or now, sid))
    return {"session_id": sid, "status": status, "added": len(new)}


def _store_run(conn, corpus, variant, response: str, *, run_id: str, tier: str, surface: str,
               model: str, provider: str, params: dict[str, Any], captured_at: str | None,
               messages: list[dict[str, Any]], confidence: float) -> bool:
    """One imported response to a corpus prompt, stored and scored like any other run."""
    if query_one(conn, "SELECT 1 AS x FROM run WHERE id = ?", (run_id,)):
        return False
    insert(conn, "run", {
        "id": run_id, "campaign_id": None, "prompt_id": variant.id, "repeat_index": 0,
        "lane": "import", "provenance_tier": tier, "surface": surface,
        "provider": provider, "model_id": model or "unknown", "model_reported": model,
        "model_alias_risk": 1, "params": params,
        "unobservable": UNOBSERVABLE.get(surface, UNOBSERVABLE["web_chat"]) + ["import_provenance"],
        "messages": messages, "response": response, "error": None, "retries": 0,
        "latency_ms": None, "finish_reason": None, "usage": {},
        "prompt_hash": variant.prompt_hash, "match_confidence": confidence,
        "captured_at": captured_at or now_iso(), "corpus_version": corpus.version,
    })
    from .groundtruth import store as store_truth
    from .runner import store_features
    store_features(conn, run_id, response)
    store_truth(conn, run_id, variant.family_id, response, language=variant.language,
                cover=variant.answer_key_cover)
    return True


def _opening_run(conn, corpus, batch: intake.Batch, conv: intake.Conversation, sid: str) -> bool:
    """If the conversation opens with a corpus prompt, store its first reply as a run."""
    from .ingest import _match_prompt
    idx = next((i for i, t in enumerate(conv.turns) if t["role"] in ("user", "assistant")), None)
    if idx is None or conv.turns[idx]["role"] != "user":
        return False
    pid, score = _match_prompt(corpus, conv.turns[idx]["text"], threshold=AUTO_MATCH)
    if not pid:
        return False
    reply = next((t for t in conv.turns[idx + 1:] if t["role"] in ("user", "assistant")), None)
    if not reply or reply["role"] != "assistant":
        return False
    variant = corpus.by_id(pid)
    rid = "run_imp_" + hashlib.sha1(f"{sid}|{idx}".encode()).hexdigest()[:16]
    return _store_run(
        conn, corpus, variant, reply["text"], run_id=rid, tier=batch.tier,
        surface=_SURFACE.get(batch.format, "third_party"), model=conv.model or "unknown",
        provider=batch.source, captured_at=reply.get("at"), confidence=score,
        params={"from_session": sid, "turn_index": idx,
                "system_context_present": any(t["role"] == "system" for t in conv.turns[:idx])},
        messages=[{"role": "user", "content": conv.turns[idx]["text"]}])


def import_batches(conn, corpus, batches: Iterable[intake.Batch], *,
                   on_progress: Callable[[int, int, str], None] | None = None,
                   cancelled: Callable[[], bool] | None = None,
                   extra_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Store every batch. Returns the counts the UI reports and the ids it opens."""
    batches = list(batches)
    stats: dict[str, Any] = {"conversations": 0, "new": 0, "updated": 0, "unchanged": 0,
                             "branched": 0, "turns_added": 0, "runs_added": 0,
                             "pairs_as_runs": 0, "pairs_as_conversations": 0,
                             "formats": sorted({b.format for b in batches}),
                             "notes": [n for b in batches for n in b.notes],
                             "session_ids": []}
    total = sum(len(b.conversations) + len(b.pairs) for b in batches)
    done = 0
    for b in batches:
        for conv in b.conversations:
            if cancelled and cancelled():
                stats["cancelled"] = True
                conn.commit()
                return stats
            res = store_conversation(conn, b, conv, extra_meta)
            stats["conversations"] += 1
            stats[res["status"]] += 1
            stats["turns_added"] += res["added"]
            if res["status"] != "unchanged":
                stats["session_ids"].append(res["session_id"])
            if corpus is not None and res["status"] in ("new", "branched") and _opening_run(
                    conn, corpus, b, conv, res["session_id"]):
                stats["runs_added"] += 1
            done += 1
            if done % 25 == 0:
                conn.commit()
                if on_progress:
                    on_progress(done, total, conv.title)
        for pair in b.pairs:
            _store_pair(conn, corpus, b, pair, stats, extra_meta)
            done += 1
            if done % 25 == 0:
                conn.commit()
                if on_progress:
                    on_progress(done, total, pair["prompt"][:60])
    conn.commit()
    if on_progress:
        on_progress(done, total, "")
    return stats


def _store_pair(conn, corpus, b: intake.Batch, pair: dict[str, Any], stats: dict[str, Any],
                extra_meta: dict[str, Any] | None) -> None:
    from .ingest import _match_prompt
    pid, score = (_match_prompt(corpus, pair["prompt"], threshold=AUTO_MATCH)
                  if corpus is not None else (None, 0.0))
    if pid:
        rid = "run_imp_" + hashlib.sha1(f"{b.origin}|{pair['key']}".encode()).hexdigest()[:16]
        if _store_run(conn, corpus, corpus.by_id(pid), pair["response"], run_id=rid, tier=b.tier,
                      surface="third_party", model=pair.get("model") or "unknown",
                      provider=b.source, captured_at=pair.get("timestamp"), confidence=score,
                      params={"origin": b.origin, "row": pair.get("row")},
                      messages=[{"role": "user", "content": pair["prompt"]}]):
            stats["pairs_as_runs"] += 1
            stats["runs_added"] += 1
        return
    conv = intake.Conversation(key=pair["key"], model=pair.get("model"),
                               created_at=pair.get("timestamp"))
    conv.add("user", pair["prompt"], pair.get("timestamp"))
    conv.add("assistant", pair["response"], pair.get("timestamp"))
    res = store_conversation(conn, b, intake._finish(conv), extra_meta)
    stats["pairs_as_conversations"] += 1
    stats["conversations"] += 1
    stats[res["status"]] += 1
    stats["turns_added"] += res["added"]
    if res["status"] != "unchanged":
        stats["session_ids"].append(res["session_id"])


def preview(batches: list[intake.Batch], corpus=None) -> dict[str, Any]:
    """What an import would do, before it does it."""
    from .ingest import _match_prompt
    out = [b.describe() for b in batches]
    for d, b in zip(out, batches):
        if corpus is not None and b.pairs:
            d["n_pairs_matching"] = sum(
                1 for p in b.pairs if _match_prompt(corpus, p["prompt"], threshold=AUTO_MATCH)[0])
        if corpus is not None and b.conversations:
            d["n_opening_with_corpus_prompt"] = sum(
                1 for c in b.conversations[:5000]
                if c.turns and c.turns[0]["role"] == "user"
                and _match_prompt(corpus, c.turns[0]["text"], threshold=AUTO_MATCH)[0])
    return {
        "batches": out,
        "n_conversations": sum(d["n_conversations"] for d in out),
        "n_turns": sum(d["n_turns"] for d in out),
        "n_pairs": sum(d["n_pairs"] for d in out),
        "empty": not any(d["n_conversations"] or d["n_pairs"] for d in out),
        "notes": [n for d in out for n in d["notes"]],
    }


def record_event(conn, origin: str, stats: dict[str, Any] | None, *, source_id: str | None = None,
                 status: str = "ok", message: str = "") -> str:
    from .db import new_id
    eid = new_id("imp")
    s = dict(stats or {})
    ids = s.pop("session_ids", [])
    s["n_sessions_touched"] = len(ids)
    s["sessions_sample"] = ids[:50]
    insert(conn, "intake_event", {
        "id": eid, "at": now_iso(), "origin": origin, "source_id": source_id,
        "formats": s.get("formats") or [], "stats": s, "status": status, "message": message})
    conn.commit()
    return eid


def recent_events(conn, limit: int = 20) -> list[dict[str, Any]]:
    from .db import loads
    rows = query(conn, "SELECT * FROM intake_event ORDER BY at DESC LIMIT ?", (limit,))
    for r in rows:
        r["formats"] = loads(r["formats"], [])
        r["stats"] = loads(r["stats"], {})
    return rows
