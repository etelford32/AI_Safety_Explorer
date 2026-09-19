"""Lanes 2 and 3 — manual capture and bulk import.

The models people actually complain about are usually reached through a chat window,
not an API. Excluding that surface because it is inconvenient would mean measuring the
wrong thing, so it gets a first-class lane with an honest provenance tier rather than
being quietly dressed up as API data.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from . import UNOBSERVABLE
from .corpus import Corpus, normalise, text_hash
from .db import insert, new_id, now_iso, query
from .runner import store_features

# What a human genuinely cannot tell you about a chat-surface response. Attached to
# every Tier B run so the limitation travels with the data.
MANUAL_UNOBSERVABLE = UNOBSERVABLE["web_chat"]


def capture(conn: sqlite3.Connection, corpus: Corpus, prompt_id: str, response: str,
            model_label: str, surface: str = "web_chat", provider: str = "manual",
            campaign_id: str | None = None, repeat_index: int = 0,
            notes: str = "", captured_at: str | None = None) -> str:
    """Record a response pasted in from a chat surface as a Tier B run."""
    variant = corpus.by_id(prompt_id)
    if variant is None:
        raise ValueError(f"unknown prompt id '{prompt_id}'")

    run_id = new_id("run")
    insert(conn, "run", {
        "id": run_id,
        "campaign_id": campaign_id,
        "prompt_id": variant.id,
        "repeat_index": repeat_index,
        "lane": "manual",
        "provenance_tier": "B",
        "surface": surface,
        "provider": provider,
        "model_id": model_label,
        "model_reported": None,
        "model_alias_risk": 1,   # a chat surface's model label is always an alias
        "params": {"note": notes} if notes else {},
        "unobservable": UNOBSERVABLE.get(surface, MANUAL_UNOBSERVABLE),
        "messages": [{"role": "user", "content": variant.text}],
        "response": response,
        "error": None,
        "retries": 0,
        "latency_ms": None,
        "finish_reason": None,
        "usage": {},
        "prompt_hash": variant.prompt_hash,
        "match_confidence": 1.0,
        "captured_at": captured_at or now_iso(),
        "corpus_version": corpus.version,
    })
    store_features(conn, run_id, response)
    conn.commit()
    return run_id


def _match_prompt(corpus: Corpus, text: str) -> tuple[str | None, float]:
    """Match imported text to a corpus prompt: exact hash first, then token overlap."""
    h = text_hash(text)
    for v in corpus.all_variants:
        if v.prompt_hash == h:
            return v.id, 1.0

    target = set(normalise(text).lower().split())
    if not target:
        return None, 0.0
    best_id, best_score = None, 0.0
    for v in corpus.all_variants:
        other = set(normalise(v.text).lower().split())
        union = target | other
        score = len(target & other) / len(union) if union else 0.0
        if score > best_score:
            best_id, best_score = v.id, score
    # Below this, "matched" would be a guess dressed up as a fact.
    return (best_id, round(best_score, 3)) if best_score >= 0.6 else (None, round(best_score, 3))


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with Path(path).open() as fh:
        for n, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{n}: invalid JSON — {exc}") from exc


def _extract_chatml(row: dict[str, Any]) -> tuple[str, str]:
    """Pull the final user prompt and assistant reply out of a message-list transcript."""
    messages = row.get("messages") or []
    prompt = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
    )
    response = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "assistant"), ""
    )
    return prompt, response


def import_file(conn: sqlite3.Connection, corpus: Corpus, path: Path,
                fmt: str = "jsonl", surface: str = "api", tier: str = "C",
                campaign_id: str | None = None,
                default_model: str = "unknown") -> dict[str, Any]:
    """Bulk-import transcripts produced elsewhere.

    Unmatched rows are stored with `prompt_id = NULL` and their best match confidence
    rather than being dropped, so they surface in the UI as unmatched and can be
    triaged. Silently discarding them would hide exactly the cases worth looking at.
    """
    stats = {"rows": 0, "matched": 0, "unmatched": 0}

    for row in iter_jsonl(path):
        stats["rows"] += 1
        if fmt == "chatml":
            prompt_text, response = _extract_chatml(row)
        else:
            prompt_text = row.get("prompt") or ""
            response = row.get("response") or row.get("completion") or ""

        prompt_id, confidence = _match_prompt(corpus, prompt_text)
        if prompt_id:
            stats["matched"] += 1
        else:
            stats["unmatched"] += 1

        run_id = new_id("run")
        insert(conn, "run", {
            "id": run_id,
            "campaign_id": campaign_id,
            "prompt_id": prompt_id,
            "repeat_index": int(row.get("repeat_index") or 0),
            "lane": "import",
            "provenance_tier": tier,
            "surface": row.get("surface") or surface,
            "provider": row.get("provider") or "import",
            "model_id": row.get("model") or default_model,
            "model_reported": row.get("model_reported"),
            "model_alias_risk": 1,
            "params": row.get("params") or {},
            "unobservable": UNOBSERVABLE.get(surface, []) + ["import_provenance"],
            "messages": row.get("messages") or [{"role": "user", "content": prompt_text}],
            "response": response or None,
            "error": row.get("error"),
            "retries": 0,
            "latency_ms": row.get("latency_ms"),
            "finish_reason": row.get("finish_reason"),
            "usage": row.get("usage") or {},
            "prompt_hash": text_hash(prompt_text) if prompt_text else None,
            "match_confidence": confidence,
            "captured_at": row.get("timestamp") or now_iso(),
            "corpus_version": corpus.version,
        })
        store_features(conn, run_id, response)

    conn.commit()
    return stats


def unmatched(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return query(
        conn,
        "SELECT id, model_id, match_confidence, captured_at, substr(response, 1, 200) AS preview "
        "FROM run WHERE prompt_id IS NULL ORDER BY captured_at DESC",
    )
