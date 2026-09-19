"""Campaign execution.

Design commitments worth stating because they are easy to get wrong:

* Each response is written to the database the moment it arrives. A three-hour
  campaign that dies at two hours fifty should lose nothing.
* Campaigns are resumable: a cell already present is skipped, not re-run.
* A refusal is a measurement, never an error. Only transport failures retry, and the
  retry count is stored.
* Multi-turn variants replay their predecessor's real exchange, so variant F measures
  recovery from an actual boundary rather than from a hypothetical one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from . import UNOBSERVABLE
from .corpus import Corpus, Variant
from .db import insert, new_id, now_iso, query, query_one, upsert
from .metrics import extract
from .providers import Provider


@dataclass
class RunPlan:
    variant: Variant
    repeat_index: int


def snapshot_corpus(conn: sqlite3.Connection, corpus: Corpus, lint_clean: bool) -> None:
    """Write the corpus into the database, content-addressed.

    Prompts live in TOML; this stores the snapshot a run is attributed to, so editing
    a prompt later can never silently reattribute old data to new text.
    """
    upsert(conn, "corpus_version", {
        "version": corpus.version,
        "content_hash": corpus.content_hash,
        "loaded_at": now_iso(),
        "n_prompts": len(corpus.all_variants),
        "lint_clean": int(lint_clean),
    }, key="version")

    for fam in corpus.families:
        upsert(conn, "family", {
            "id": fam.id, "name": fam.name, "domain": fam.domain,
            "focal_dimension": fam.focal_dimension, "reasoning_core": fam.reasoning_core,
            "status": fam.status, "substrate_rule": fam.substrate_rule,
            "corpus_version": corpus.version,
        }, key="id")
        for tg in fam.twin_groups:
            upsert(conn, "twin_group", {
                "id": tg.id, "family_id": fam.id, "reasoning_core": tg.reasoning_core,
                "output_format": tg.output_format, "corpus_version": corpus.version,
            }, key="id")

    for v in corpus.all_variants:
        upsert(conn, "prompt", {
            "id": v.id, "family_id": v.family_id, "twin_group_id": v.twin_group_id,
            "arm": v.arm, "sub_arm": v.sub_arm, "control_arm": v.control_arm,
            "variant": v.variant,
            "title": v.title, "text": v.text, "output_format": v.output_format,
            "intent": v.intent, "operationality": v.operationality,
            "specificity": v.specificity, "autonomy": v.autonomy, "depth": v.depth,
            "hazard_review": v.hazard_review, "hazard_rationale": v.hazard_rationale,
            "conversation_with": v.conversation_with,
            "expected_benign": int(v.expected_benign),
            "prompt_hash": v.prompt_hash, "corpus_version": corpus.version,
            "status": v.status,
        }, key="id")
    conn.commit()


def create_campaign(conn: sqlite3.Connection, name: str, provider: Provider,
                    corpus: Corpus, n_repeats: int, notes: str = "") -> str:
    cid = new_id("camp")
    insert(conn, "campaign", {
        "id": cid, "name": name, "created_at": now_iso(),
        "provider": provider.name, "model_id": provider.model,
        "params": provider.params, "n_repeats": n_repeats,
        "corpus_version": corpus.version, "notes": notes,
    })
    conn.commit()
    return cid


def plan(corpus: Corpus, n_repeats: int, only: list[str] | None = None) -> list[RunPlan]:
    """Order the cells so that a conversational predecessor always runs first."""
    variants = corpus.runnable
    if only:
        wanted = set(only)
        variants = [
            v for v in variants
            if v.id in wanted or v.family_id in wanted or v.arm in wanted
            or (v.control_arm or "") in wanted
        ]

    ordered: list[Variant] = []
    seen: set[str] = set()

    def visit(v: Variant) -> None:
        if v.id in seen:
            return
        if v.conversation_with:
            parent = corpus.by_id(v.conversation_with)
            if parent and parent.id not in seen:
                visit(parent)
        seen.add(v.id)
        ordered.append(v)

    for v in variants:
        visit(v)

    # `ordered` may contain variants outside the `only` selection: a multi-turn
    # variant's predecessor is pulled in regardless, because measuring recovery from a
    # boundary requires that the boundary actually happened.
    #
    # Repeats outermost: a full sweep completes before the second sweep starts, so an
    # interrupted campaign yields a balanced design rather than three copies of family 1.
    return [RunPlan(v, r) for r in range(n_repeats) for v in ordered]


def existing_cells(conn: sqlite3.Connection, campaign_id: str) -> set[tuple[str, int]]:
    rows = query(conn, "SELECT prompt_id, repeat_index FROM run WHERE campaign_id = ?", (campaign_id,))
    return {(r["prompt_id"], r["repeat_index"]) for r in rows}


def build_messages(conn: sqlite3.Connection, campaign_id: str, corpus: Corpus,
                   variant: Variant, repeat_index: int) -> list[dict[str, str]]:
    """Build the message list, replaying a real predecessor exchange for multi-turn variants."""
    messages: list[dict[str, str]] = []
    if variant.conversation_with:
        parent = corpus.by_id(variant.conversation_with)
        if parent:
            prior = query_one(
                conn,
                "SELECT response FROM run WHERE campaign_id = ? AND prompt_id = ? "
                "AND repeat_index = ? AND response IS NOT NULL",
                (campaign_id, parent.id, repeat_index),
            )
            messages.append({"role": "user", "content": parent.text})
            messages.append({
                "role": "assistant",
                # If the predecessor errored we still need a turn here; the placeholder
                # is recorded in the stored messages so the gap is visible in analysis.
                "content": (prior or {}).get("response") or "[predecessor response unavailable]",
            })
    messages.append({"role": "user", "content": variant.text})
    return messages


def execute(conn: sqlite3.Connection, campaign_id: str, corpus: Corpus, provider: Provider,
            n_repeats: int, only: list[str] | None = None, surface: str = "api",
            resume: bool = True,
            on_progress: Callable[[int, int, Variant, str], None] | None = None) -> dict[str, Any]:
    cells = plan(corpus, n_repeats, only)
    done = existing_cells(conn, campaign_id) if resume else set()
    todo = [c for c in cells if (c.variant.id, c.repeat_index) not in done]

    stats = {"total": len(cells), "skipped": len(cells) - len(todo), "ok": 0, "errors": 0}

    for i, cell in enumerate(todo, start=1):
        v = cell.variant
        messages = build_messages(conn, campaign_id, corpus, v, cell.repeat_index)
        completion = provider.complete(
            messages, vector=v.vector, repeat_index=cell.repeat_index
        )

        run_id = new_id("run")
        insert(conn, "run", {
            "id": run_id, "campaign_id": campaign_id, "prompt_id": v.id,
            "repeat_index": cell.repeat_index, "lane": "api", "provenance_tier": "A",
            "surface": surface, "provider": provider.name, "model_id": provider.model,
            "model_reported": completion.model_reported,
            "model_alias_risk": int(provider.alias_risk()),
            "params": provider.params,
            "unobservable": UNOBSERVABLE.get(surface, []),
            "messages": messages, "response": completion.text or None,
            "error": completion.error, "retries": completion.retries,
            "latency_ms": completion.latency_ms, "finish_reason": completion.finish_reason,
            "stop_details": (completion.raw or {}).get("stop_details", {}),
            "usage": completion.usage, "prompt_hash": v.prompt_hash,
            "match_confidence": 1.0, "captured_at": now_iso(),
            "corpus_version": corpus.version,
        })
        store_features(conn, run_id, completion.text)
        conn.commit()

        if completion.error:
            stats["errors"] += 1
        else:
            stats["ok"] += 1
        if on_progress:
            on_progress(i, len(todo), v, completion.error or "ok")

    return stats


def store_features(conn: sqlite3.Connection, run_id: str, response: str | None) -> None:
    f = extract(response)
    f["run_id"] = run_id
    f["computed_at"] = now_iso()
    upsert(conn, "feature", f, key="run_id")


def recompute_features(conn: sqlite3.Connection) -> int:
    """Re-derive every feature row from stored responses.

    Free by design: improving the extractor should never cost an API call.
    """
    rows = query(conn, "SELECT id, response FROM run")
    for r in rows:
        store_features(conn, r["id"], r["response"])
    conn.commit()
    return len(rows)
