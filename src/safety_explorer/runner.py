"""Campaign execution.

Design commitments worth stating because they are easy to get wrong:

* Each response is written to the database the moment it arrives. A three-hour
  campaign that dies at two hours fifty should lose nothing.
* Campaigns are resumable: a cell already present is skipped, not re-run.
* A refusal is a measurement, never an error. Only transport failures retry, and the
  retry count is stored.
* Multi-turn variants replay their predecessor's real exchange, so variant F measures
  recovery from an actual boundary rather than from a hypothetical one.
* Context depth is a factor of the cell, like the cue. A padded cell replays the
  predecessor from the SAME depth, so recovery is measured from a boundary met under
  the same amount of context.
* The batch path (`execute_batch`) records exactly what the synchronous path records,
  through the same function. It differs only in transport, and never resubmits a cell
  while a batch holding it is still open.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import UNOBSERVABLE
from . import filler as filler_mod
from .cues import Cue, CueSet
from .corpus import Corpus, Variant
from .db import insert, loads, new_id, now_iso, query, query_one, upsert
from .metrics import extract
from .providers import Provider
from .providers.base import BatchRequest, Completion, task_text


@dataclass
class RunPlan:
    variant: Variant
    repeat_index: int
    cue: Cue | None = None
    #: Tokens of filler in front of the task; 0 is the unpadded cell.
    depth: int = 0
    filler_id: str = filler_mod.NONE

    @property
    def cue_id(self) -> str:
        return self.cue.id if self.cue else "none"

    @property
    def key(self) -> tuple[str, int, str, int, str]:
        """The cell identity — the columns of `idx_run_cell` below the campaign."""
        return (self.variant.id, self.repeat_index, self.cue_id, self.depth, self.filler_id)


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
         cue_arms: list[str] | None = None, depths: list[int] | None = None,
         filler_id: str = "neutral-v1") -> list[RunPlan]:
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

    # Repeats outermost, then depth, then cues, then prompts: a full sweep across every
    # depth and cue completes before the second repeat starts. An interrupted campaign
    # then yields a balanced design rather than every repeat of the baseline and none of
    # the treatment, which would make the central contrast unavailable. Depth sits
    # outside the prompts so every cell sharing a filler runs back to back, which is
    # what keeps that filler's prompt-cache entry warm.
    depth_levels = sorted(set(depths)) if depths else [0]
    return [
        RunPlan(v, r, cue, d, filler_id if d else filler_mod.NONE)
        for r in range(n_repeats)
        for d in depth_levels
        for cue in selected_cues
        for v in ordered
    ]


def existing_cells(conn: sqlite3.Connection,
                   campaign_id: str) -> set[tuple[str, int, str, int, str]]:
    rows = query(conn, "SELECT prompt_id, repeat_index, cue_id, context_depth_tokens, "
                       "filler_id FROM run WHERE campaign_id = ?", (campaign_id,))
    return {(r["prompt_id"], r["repeat_index"], r["cue_id"] or "none",
             r["context_depth_tokens"] or 0, r["filler_id"] or filler_mod.NONE)
            for r in rows}


def build_messages(conn: sqlite3.Connection, campaign_id: str, corpus: Corpus,
                   variant: Variant, repeat_index: int,
                   cue: Cue | None = None, depth: int = 0,
                   filler_id: str = filler_mod.NONE) -> list[dict[str, str]]:
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
                "AND repeat_index = ? AND context_depth_tokens = ? AND filler_id = ? "
                "AND response IS NOT NULL",
                (campaign_id, parent.id, repeat_index, depth, filler_id),
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


def _overrides(cell: RunPlan) -> dict[str, Any]:
    """What the provider is told about a cell beyond its messages."""
    v, cue = cell.variant, cell.cue
    return {
        "vector": v.vector, "repeat_index": cell.repeat_index,
        "family_id": v.family_id, "language": v.language,
        # The mock uses this to answer only what the prompt asked for. A live provider
        # ignores it - it reads the prompt, which is where the constraint really lives.
        "answer_key_cover": v.answer_key_cover,
        "cue_level": (cue.level if cue else 0),
        "cue_arm": (cue.arm if cue else "none"),
        # Likewise read only by the mock, whose depth effect is documented ground truth.
        "context_depth_tokens": cell.depth,
    }


class Fillers:
    """The filler for each depth, calibrated once per campaign execution."""

    def __init__(self, conn: sqlite3.Connection, provider: Provider) -> None:
        self.conn, self.provider = conn, provider
        self._by_key: dict[tuple[str, int], filler_mod.Filler | None] = {}

    def get(self, cell: RunPlan) -> filler_mod.Filler | None:
        k = (cell.filler_id, cell.depth)
        if k not in self._by_key:
            self._by_key[k] = filler_mod.realise(self.conn, cell.filler_id, cell.depth,
                                                 self.provider)
        return self._by_key[k]


def compose_cell(conn: sqlite3.Connection, campaign_id: str, corpus: Corpus,
                 cell: RunPlan, fillers: Fillers
                 ) -> tuple[list[dict[str, Any]], filler_mod.Filler | None]:
    messages = build_messages(conn, campaign_id, corpus, cell.variant, cell.repeat_index,
                              cell.cue, cell.depth, cell.filler_id)
    f = fillers.get(cell)
    return filler_mod.compose(messages, f), f


def record_run(conn: sqlite3.Connection, campaign_id: str, corpus: Corpus,
               provider: Provider, cell: RunPlan, messages: list[dict[str, Any]],
               completion: Completion, surface: str = "api",
               params: dict[str, Any] | None = None,
               filler: filler_mod.Filler | None = None) -> str:
    """Write one cell's run and every deterministic layer over it. Not committed."""
    v, cue = cell.variant, cell.cue
    batched = bool((completion.usage or {}).get("batch"))
    run_id = new_id("run")
    insert(conn, "run", {
        "id": run_id, "campaign_id": campaign_id, "prompt_id": v.id,
        "repeat_index": cell.repeat_index, "lane": "api", "provenance_tier": "A",
        "surface": surface, "provider": provider.name, "model_id": provider.model,
        "model_reported": completion.model_reported,
        "model_alias_risk": int(provider.alias_risk()),
        "params": params if params is not None else provider.params,
        "unobservable": UNOBSERVABLE.get(surface, []),
        # The filler is stored by reference: its text lives once in `filler_text`.
        "messages": filler_mod.for_storage(messages), "response": completion.text or None,
        "error": completion.error, "retries": completion.retries,
        # A batch request has no latency of its own worth recording; the queue's is not
        # the model's.
        "latency_ms": None if batched else completion.latency_ms,
        "finish_reason": completion.finish_reason,
        "stop_details": (completion.raw or {}).get("stop_details", {}),
        "usage": completion.usage,
        "cue_id": cell.cue_id,
        "cue_level": cue.level if cue else 0,
        "cue_arm": cue.arm if cue else "none",
        "context_depth_tokens": cell.depth,
        "filler_id": cell.filler_id,
        "filler_hash": filler.hash if filler else None,
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
    from .compliance import store as store_compliance
    store_compliance(conn, run_id, completion.text, v.output_format, v.language)
    return run_id


def execute(conn: sqlite3.Connection, campaign_id: str, corpus: Corpus, provider: Provider,
            n_repeats: int, only: list[str] | None = None, surface: str = "api",
            resume: bool = True,
            on_progress: Callable[[int, int, Variant, str], None] | None = None,
            should_stop: Callable[[], bool] | None = None,
            cue_set: "CueSet | None" = None, cue_levels: list[int] | None = None,
            cue_arms: list[str] | None = None,
            probe_kinds: list[str] | None = None,
            depths: list[int] | None = None,
            filler_id: str = "neutral-v1") -> dict[str, Any]:
    cells = plan(corpus, n_repeats, only, cue_set, cue_levels, cue_arms, depths, filler_id)
    done = existing_cells(conn, campaign_id) if resume else set()
    todo = [c for c in cells if c.key not in done]
    fillers = Fillers(conn, provider)

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
        messages, f = compose_cell(conn, campaign_id, corpus, cell, fillers)
        completion = provider.complete(messages, **_overrides(cell))
        run_id = record_run(conn, campaign_id, corpus, provider, cell, messages,
                            completion, surface, filler=f)

        if probe_kinds and not completion.error:
            try:
                run_probes(conn, provider, run_id, task_text(messages[-1]),
                           messages, completion.text, probe_kinds,
                           context=_overrides(cell))
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


# ---------------------------------------------------------------------------
# Batch transport
# ---------------------------------------------------------------------------

#: A batch may hold 256 MB of requests. Deep cells carry their filler inline in every
#: request (the cache saves the processing, not the upload), so batches are split well
#: under that by measured size rather than by request count.
BATCH_MAX_BYTES = 180_000_000
BATCH_MAX_REQUESTS = 50_000


def _cell_from_map(corpus: Corpus, cue_set: CueSet | None,
                   entry: dict[str, Any]) -> RunPlan | None:
    v = corpus.by_id(entry["prompt_id"])
    if v is None:
        return None
    cue = None
    if entry.get("cue_id", "none") != "none":
        cue = next((c for c in (cue_set.cues if cue_set else []) if c.id == entry["cue_id"]),
                   None)
        if cue is None:
            return None
    return RunPlan(v, entry["repeat_index"], cue, entry.get("depth", 0),
                   entry.get("filler_id", filler_mod.NONE))


def collect_batch(conn: sqlite3.Connection, job: dict[str, Any], corpus: Corpus,
                  provider: Provider, cue_set: CueSet | None, surface: str,
                  stats: dict[str, Any],
                  on_progress: Callable[[int, int, Variant, str], None] | None = None,
                  should_stop: Callable[[], bool] | None = None,
                  poll_seconds: float = 30.0,
                  sleep: Callable[[float], None] = time.sleep) -> bool:
    """Wait for one submitted batch and record its results. False if cancelled first."""
    bid = job["provider_batch_id"]
    while True:
        status = provider.batch_status(bid)
        if status["status"] == "ended":
            break
        if should_stop is not None and should_stop():
            return False
        stats["polls"] = stats.get("polls", 0) + 1
        sleep(poll_seconds)

    request_map = loads(job["request_map"], {})
    done = existing_cells(conn, job["campaign_id"])
    params = {**provider.params, "cache_ttl": "1h", "transport": "batch"}
    seen = 0
    for i, (cid, completion) in enumerate(provider.batch_results(bid), start=1):
        entry = request_map.get(cid)
        seen += 1
        if entry is None:
            continue
        cell = _cell_from_map(corpus, cue_set, entry)
        if cell is None or cell.key in done:
            continue
        if completion is None:
            # Expired or cancelled: never ran, so no row. The cell stays to-do and the
            # next wave resubmits it.
            stats["not_run"] = stats.get("not_run", 0) + 1
            continue
        f = None
        if entry.get("filler_hash"):
            row = query_one(conn, "SELECT text FROM filler_text WHERE hash = ?",
                            (entry["filler_hash"],))
            if row:
                f = filler_mod.Filler(cell.filler_id, cell.depth, row["text"],
                                      entry["filler_hash"], entry.get("filler_tokens", 0),
                                      entry.get("count_method", "exact"))
        messages = filler_mod.hydrate(conn, entry["messages"])
        record_run(conn, job["campaign_id"], corpus, provider, cell, messages, completion,
                   surface, params=params, filler=f)
        done.add(cell.key)
        if completion.error:
            stats["errors"] += 1
        else:
            stats["ok"] += 1
        if on_progress:
            on_progress(stats["ok"] + stats["errors"], stats["total"] - stats["skipped"],
                        cell.variant, completion.error or "ok")
    conn.execute("UPDATE batch_job SET status = ?, collected_at = ?, counts = ? WHERE id = ?",
                 ("collected" if seen or not status.get("unknown") else "lost", now_iso(),
                  json.dumps(status.get("counts", {})), job["id"]))
    conn.commit()
    return True


def _chunks(requests: list[tuple[BatchRequest, dict[str, Any]]]):
    chunk: list[tuple[BatchRequest, dict[str, Any]]] = []
    size = 0
    for item in requests:
        n = len(json.dumps(item[0].messages))
        if chunk and (size + n > BATCH_MAX_BYTES or len(chunk) >= BATCH_MAX_REQUESTS):
            yield chunk
            chunk, size = [], 0
        chunk.append(item)
        size += n
    if chunk:
        yield chunk


def execute_batch(conn: sqlite3.Connection, campaign_id: str, corpus: Corpus,
                  provider: Provider, n_repeats: int, only: list[str] | None = None,
                  surface: str = "api",
                  on_progress: Callable[[int, int, Variant, str], None] | None = None,
                  should_stop: Callable[[], bool] | None = None,
                  cue_set: "CueSet | None" = None, cue_levels: list[int] | None = None,
                  cue_arms: list[str] | None = None,
                  depths: list[int] | None = None, filler_id: str = "neutral-v1",
                  prewarm: bool = True, poll_seconds: float = 30.0,
                  on_note: Callable[[str], None] | None = None,
                  sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Run a campaign through the provider's batch API.

    Multi-turn variants need their predecessor's real response, which a batch cannot
    supply mid-flight, so cells go in waves: each wave holds every cell whose
    predecessor (if any) is already recorded, and waits for its results before the next
    is built. For the corpus as it stands that is two waves.

    Batches already open for this campaign are collected before anything is submitted,
    so a process that died while waiting picks up where it left off rather than paying
    for the same cells again.
    """
    if not getattr(provider, "supports_batch", False):
        raise ValueError(f"provider '{provider.name}' has no batch API; run without --batch")
    note = on_note or (lambda _m: None)
    cells = plan(corpus, n_repeats, only, cue_set, cue_levels, cue_arms, depths, filler_id)
    stats: dict[str, Any] = {"total": len(cells), "skipped": 0, "ok": 0, "errors": 0,
                             "cancelled": False, "batches": 0, "waves": 0}
    fillers = Fillers(conn, provider)

    def collect_open() -> bool:
        for job in query(conn, "SELECT * FROM batch_job WHERE campaign_id = ? "
                               "AND status = 'submitted' ORDER BY submitted_at", (campaign_id,)):
            note(f"collecting batch {job['provider_batch_id']} ({job['n_requests']} requests)")
            if not collect_batch(conn, job, corpus, provider, cue_set, surface, stats,
                                 on_progress, should_stop, poll_seconds, sleep):
                return False
        return True

    initial = existing_cells(conn, campaign_id)
    stats["skipped"] = sum(1 for c in cells if c.key in initial)
    if not collect_open():
        stats["cancelled"] = True
        return stats

    wave = 0
    while True:
        done = existing_cells(conn, campaign_id)
        todo = [c for c in cells if c.key not in done]
        if not todo:
            break
        if should_stop is not None and should_stop():
            stats["cancelled"] = True
            break
        ready = []
        for c in todo:
            parent = c.variant.conversation_with
            if parent and not any(k[0] == parent and k[1] == c.repeat_index
                                  and k[3] == c.depth and k[4] == c.filler_id for k in done):
                continue
            ready.append(c)
        if not ready:
            stats["stalled"] = len(todo)
            note(f"{len(todo)} cell(s) wait on a predecessor that has no recorded run; "
                 f"resume the campaign to retry them")
            break
        if wave >= 2 * len(cells) + 2:   # belt and braces against a non-advancing loop
            stats["stalled"] = len(todo)
            break

        requests: list[tuple[BatchRequest, dict[str, Any]]] = []
        prefixes: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for i, cell in enumerate(ready):
            messages, f = compose_cell(conn, campaign_id, corpus, cell, fillers)
            cid = f"w{wave}-{i}"
            requests.append((BatchRequest(cid, messages, _overrides(cell)), {
                "prompt_id": cell.variant.id, "repeat_index": cell.repeat_index,
                "cue_id": cell.cue_id, "depth": cell.depth, "filler_id": cell.filler_id,
                "filler_hash": f.hash if f else None,
                "filler_tokens": f.tokens if f else 0,
                "count_method": f.count_method if f else None,
                "messages": filler_mod.for_storage(messages),
            }))
            if f is not None:
                prefixes.setdefault((cell.filler_id, cell.depth), messages)

        if prewarm and hasattr(provider, "prewarm"):
            for (fid, depth), messages in prefixes.items():
                res = provider.prewarm(messages)
                note(f"cache pre-warm {fid} @ {depth:,} tokens: "
                     + ("ok" if res.get("ok") else f"skipped ({res.get('error')})"))

        for chunk in _chunks(requests):
            bid = provider.batch_submit([r for r, _ in chunk])
            job_id = new_id("bat")
            insert(conn, "batch_job", {
                "id": job_id, "campaign_id": campaign_id, "provider": provider.name,
                "model_id": provider.model, "provider_batch_id": bid, "wave": wave,
                "n_requests": len(chunk),
                "request_map": {r.custom_id: m for r, m in chunk},
                "status": "submitted", "submitted_at": now_iso(),
            })
            conn.commit()
            stats["batches"] += 1
            note(f"wave {wave}: submitted batch {bid} with {len(chunk)} request(s)")
        stats["waves"] += 1
        wave += 1
        if not collect_open():
            stats["cancelled"] = True
            note("stopped while a batch was processing; it keeps running and is "
                 "collected the next time this campaign is resumed")
            break
    return stats


def store_features(conn: sqlite3.Connection, run_id: str, response: str | None) -> None:
    f = extract(response)
    f["run_id"] = run_id
    f["computed_at"] = now_iso()
    upsert(conn, "feature", f, key="run_id")


def recompute_features(conn: sqlite3.Connection) -> int:
    """Re-derive every feature row, and every compliance row, from stored responses.

    Free by design: improving the extractor should never cost an API call.
    """
    from .compliance import recompute_all as recompute_compliance

    rows = query(conn, "SELECT id, response FROM run")
    for r in rows:
        store_features(conn, r["id"], r["response"])
    conn.commit()
    recompute_compliance(conn)
    return len(rows)
