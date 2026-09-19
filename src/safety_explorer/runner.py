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
from .cues import Cue, CueSet
from .corpus import Corpus, Variant
from .db import insert, new_id, now_iso, query, query_one, upsert
from .metrics import extract
from .providers import Provider


@dataclass
class RunPlan:
    variant: Variant
    repeat_index: int
    cue: Cue | None = None


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
            "arm": v.arm, "sub_arm": v.sub_arm, "language": v.language,
            "control_arm": v.control_arm,
            "variant": v.variant,
            "title": v.title, "text": v.text, "output_format": v.output_format,
            "intent": v.intent, "operationality": v.operationality,
            "specificity": v.specificity, "autonomy": v.autonomy, "depth": v.depth,
            "hazard_review": v.hazard_review, "hazard_rationale": v.hazard_rationale,
            "conversation_with": v.conversation_with,
            "expected_benign": int(v.expected_benign),
            "answer_key": (v.answer_key if isinstance(v.answer_key, str)
                           else ",".join(sorted(v.answer_key))),
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


def plan(corpus: Corpus, n_repeats: int, only: list[str] | None = None,
         cue_set: CueSet | None = None, cue_levels: list[int] | None = None,
         cue_arms: list[str] | None = None) -> list[RunPlan]:
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
    selected_cues = (cue_set.select(cue_levels, cue_arms) if cue_set else [None])

    # Repeats outermost, then cues, then prompts: a full sweep across every cue
    # completes before the second repeat starts. An interrupted campaign then yields a
    # balanced design rather than every repeat of the baseline and none of the
    # treatment, which would make the central contrast unavailable.
    return [
        RunPlan(v, r, cue)
        for r in range(n_repeats)
        for cue in selected_cues
        for v in ordered
    ]


def existing_cells(conn: sqlite3.Connection, campaign_id: str) -> set[tuple[str, int, str]]:
    rows = query(conn, "SELECT prompt_id, repeat_index, cue_id FROM run WHERE campaign_id = ?",
                 (campaign_id,))
    return {(r["prompt_id"], r["repeat_index"], r["cue_id"] or "none") for r in rows}


def build_messages(conn: sqlite3.Connection, campaign_id: str, corpus: Corpus,
                   variant: Variant, repeat_index: int,
                   cue: Cue | None = None) -> list[dict[str, str]]:
    """Build the message list, replaying a real predecessor exchange for multi-turn variants.

    An observation cue is composed onto the final user turn only. Putting it on the
    replayed predecessor as well would change the exchange the recovery variant is
    recovering from, so the boundary being recovered from would differ by cue.
    """
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
    final = cue.apply(variant.text) if cue is not None else variant.text
    messages.append({"role": "user", "content": final})
    return messages


def run_probes(conn: sqlite3.Connection, provider: Provider, run_id: str,
               composed_prompt: str, messages: list[dict[str, str]],
               answer: str | None, kinds: list[str],
               context: dict[str, Any] | None = None) -> int:
    """Attach awareness probes to a performance run.

    Parallel probes are independent calls carrying the composed prompt as quoted data,
    so they cannot contaminate the performance rollout — that independence is the whole
    reason the two channels can be measured at once. Follow-up probes continue the
    performance conversation, which is safe only because the answer is already committed.
    """
    from . import probes as probe_mod

    written = 0
    for kind in kinds:
        spec = probe_mod.PROBES.get(kind)
        if spec is None:
            continue
        if spec.mode == "followup" and not answer:
            continue   # nothing was said, so there is nothing to ask about

        if spec.mode == "parallel":
            probe_messages = [{"role": "user", "content": probe_mod.build(kind, composed_prompt)}]
        else:
            probe_messages = [
                *messages,
                {"role": "assistant", "content": answer or ""},
                {"role": "user", "content": spec.template},
            ]

        # The cell's context travels with the probe. A follow-up probe asks about the
        # response just given, so a provider that cannot see which cue produced it
        # cannot answer honestly about it — and the self-report channel silently
        # reports full effort for every cell, which looks like perfect calibration.
        completion = provider.complete(probe_messages, probe_kind=kind, **(context or {}))
        insert(conn, "probe", {
            "id": new_id("prb"), "run_id": run_id, "kind": kind, "mode": spec.mode,
            "provider": provider.name, "model_id": provider.model,
            "prompt": probe_messages[-1]["content"],
            "response": completion.text or None,
            "parsed": probe_mod.parse(kind, completion.text),
            "error": completion.error, "latency_ms": completion.latency_ms,
            "captured_at": now_iso(),
        })
        written += 1
    return written


def execute(conn: sqlite3.Connection, campaign_id: str, corpus: Corpus, provider: Provider,
            n_repeats: int, only: list[str] | None = None, surface: str = "api",
            resume: bool = True,
            on_progress: Callable[[int, int, Variant, str], None] | None = None,
            should_stop: Callable[[], bool] | None = None,
            cue_set: "CueSet | None" = None, cue_levels: list[int] | None = None,
            cue_arms: list[str] | None = None,
            probe_kinds: list[str] | None = None) -> dict[str, Any]:
    cells = plan(corpus, n_repeats, only, cue_set, cue_levels, cue_arms)
    done = existing_cells(conn, campaign_id) if resume else set()
    todo = [c for c in cells
            if (c.variant.id, c.repeat_index,
                c.cue.id if c.cue else "none") not in done]

    stats: dict[str, Any] = {
        "total": len(cells), "skipped": len(cells) - len(todo),
        "ok": 0, "errors": 0, "cancelled": False,
    }

    for i, cell in enumerate(todo, start=1):
        # Cancellation is checked between cells, never mid-call: a response already
        # paid for is always written. Campaigns resume, so stopping loses nothing.
        if should_stop is not None and should_stop():
            stats["cancelled"] = True
            break
        v = cell.variant
        cue = cell.cue
        messages = build_messages(conn, campaign_id, corpus, v, cell.repeat_index, cue)
        completion = provider.complete(
            messages, vector=v.vector, repeat_index=cell.repeat_index,
            family_id=v.family_id, language=v.language,
            # The mock uses this to answer only what the prompt asked for. A live
            # provider ignores it - it reads the prompt, which is where the constraint
            # really lives.
            answer_key_cover=v.answer_key_cover,
            cue_level=(cue.level if cue else 0),
            cue_arm=(cue.arm if cue else "none"),
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
            "usage": completion.usage,
            "cue_id": cue.id if cue else "none",
            "cue_level": cue.level if cue else 0,
            "cue_arm": cue.arm if cue else "none",
            "prompt_hash": v.prompt_hash,
            "match_confidence": 1.0, "captured_at": now_iso(),
            "corpus_version": corpus.version,
        })
        store_features(conn, run_id, completion.text)
        # Objective correctness, computed inline — it is pure arithmetic over the
        # response and costs nothing.
        from .groundtruth import store as store_truth
        # The language MUST be passed. Without it a French response is parsed with
        # English conventions, "1,292e+20" reads as 1.292e23, and the arm reports a
        # dramatic cross-lingual capability collapse that is entirely a parser artefact.
        # The cover MUST be passed too. Variant A states no parameters and six of the
        # eight F variants ask an adjacent question, so scoring them against the whole
        # key records a capability loss where the question simply differed.
        store_truth(conn, run_id, v.family_id, completion.text, language=v.language,
                    cover=v.answer_key_cover)

        if probe_kinds and not completion.error:
            try:
                run_probes(conn, provider, run_id, messages[-1]["content"],
                           messages, completion.text, probe_kinds,
                           context={"vector": v.vector,
                                    "repeat_index": cell.repeat_index,
                                    "family_id": v.family_id, "language": v.language,
                                    "cue_level": cue.level if cue else 0,
                                    "cue_arm": cue.arm if cue else "none"})
            except Exception as exc:  # noqa: BLE001 — a probe failure must not lose the run
                stats.setdefault("probe_errors", 0)
                stats["probe_errors"] += 1
                if on_progress:
                    on_progress(i, len(todo), v, f"probe failed: {exc}")
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
