"""Span-level co-analysis: a model proposes, a human decides, and the gap is measured.

Layer 2 — blinded human annotation — is the binding constraint on everything in this
instrument. Sixty responses is a long evening. Layer 3 has existed since v0.1 as a table
and an agreement statistic with nothing writing to it, because an LLM judge that scores
whole responses is hard to trust and harder to check: it returns a number, and the only
way to audit it is to re-read the response yourself, which is the work it was supposed
to save.

Span-level changes that. A proposal here is attached to a specific block of text, with a
quote and a reason, so checking it costs a glance rather than a re-read. A human who
disagrees says so on that span, and both rows survive.

**The blinding rule is the whole design.** A human shown a model's proposal before
judging will agree with it more often, and that agreement is not evidence the model was
right — it is evidence the human was anchored. So the default flow labels blind: the
human sees the span and its computed evidence, assigns a label, and only then is the
proposal revealed. `blinded` is stored per label, and `agreement()` reports the two
populations separately and refuses to pool them.

Nothing here is authoritative. A model proposal is data about the model; the human row
is the record. `promote()` exists to turn adjudicated spans into the run-level judgement
the older analyses read, and it will not run on unblinded labels.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from . import LABEL_SOURCES, SPAN_LABELS
from .conversation import SEGMENTER_VERSION, assemble
from .db import new_id, now_iso, query, query_one, upsert

#: Below this, a model's proposals are reported but never promoted to a run-level
#: judgement. Matches the Layer 3 threshold already in PLAN.md, deliberately: a span
#: judge is held to the same bar as the response judge it replaces.
USABLE_ALPHA = 0.67

#: Pairs needed in BOTH buckets before the blind/unblind difference is read as anything.
#: Below this the two are still reported — they are the data — but the gap between them
#: is not narrated, because a twenty-point difference on eight pairs against nine is
#: noise, and naming it "anchoring" would be the same over-claim this instrument spends
#: its effort avoiding everywhere else.
MIN_GAP_PAIRS = 20


def record(conn, run_id: str, span_index: int, span_hash: str, label: str,
           source: str = "human", author: str = "local", confidence: float | None = None,
           rationale: str = "", quote: str = "", blinded: bool = True) -> str:
    """Store one label. Re-labelling the same cell replaces it; it does not stack."""
    if label not in SPAN_LABELS:
        raise ValueError(f"unknown span label {label!r}; expected one of {SPAN_LABELS}")
    if source not in LABEL_SOURCES:
        raise ValueError(f"unknown label source {source!r}")
    row_id = new_id("spl")
    upsert(conn, "span_label", {
        "id": row_id, "run_id": run_id, "span_index": span_index,
        "span_hash": span_hash, "segmenter_version": SEGMENTER_VERSION,
        "source": source, "author": author, "label": label,
        "confidence": confidence, "rationale": rationale, "quote": quote,
        "blinded": int(bool(blinded)), "created_at": now_iso(),
    }, key="id", conflict=("run_id", "span_index", "source", "author"))
    conn.commit()
    return row_id


def labels_for(conn, run_id: str) -> dict[str, Any]:
    """Every label on a run, with each one checked against the span it points at.

    A label is stored against an index. If the segmenter changes, that index may now
    cover different words, so each row's stored hash is compared with the live span's.
    A mismatch is reported as `stale` and excluded from agreement — re-pointing it at
    whatever now sits at that index would silently put someone's judgement on a sentence
    they never read.
    """
    convo = assemble(conn, run_id)
    live = {}
    if convo:
        for turn in convo["turns"]:
            for span in turn.get("spans", []):
                live[span["index"]] = span["hash"]

    out: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"human": [], "model": []})
    stale = 0
    for row in query(conn, "SELECT * FROM span_label WHERE run_id = ? "
                           "ORDER BY span_index, source", (run_id,)):
        fresh = live.get(row["span_index"]) == row["span_hash"]
        stale += int(not fresh)
        out[row["span_index"]][row["source"]].append({
            "label": row["label"], "author": row["author"],
            "confidence": row["confidence"], "rationale": row["rationale"],
            "quote": row["quote"], "blinded": bool(row["blinded"]),
            "stale": not fresh, "created_at": row["created_at"],
        })
    return {"run_id": run_id, "by_span": {str(k): v for k, v in out.items()},
            "n_stale": stale, "segmenter_version": SEGMENTER_VERSION}


def coverage(conn, run_id: str | None = None) -> dict[str, Any]:
    """How much of the corpus has been co-analysed, and how much of it blind."""
    where, params = ("WHERE run_id = ?", (run_id,)) if run_id else ("", ())
    rows = query(conn, f"SELECT * FROM span_label {where}", params)
    human = [r for r in rows if r["source"] == "human"]
    model = [r for r in rows if r["source"] == "model"]
    blind = [r for r in human if r["blinded"]]
    return {
        "n_labels": len(rows),
        "n_human": len(human),
        "n_model": len(model),
        "n_human_blinded": len(blind),
        "blind_share": round(len(blind) / len(human), 3) if human else None,
        "runs_touched": len({r["run_id"] for r in rows}),
        "label_mix": dict(Counter(r["label"] for r in human)),
    }


def agreement(conn, author: str | None = None) -> dict[str, Any]:
    """Where a model's proposals and a human's adjudications meet, and where they do not.

    Reported **separately for blinded and unblinded human labels**, and never pooled.
    Pooling them would mix a measurement of the model with a measurement of anchoring,
    and the blinded figure is the only one that says anything about the model.

    Per-label agreement matters more than the headline here. A proposer that is right
    about `capability` and wrong about `boundary` is useful with a caveat; one that is
    uniformly mediocre is not, and a single percentage cannot tell them apart.
    """
    from .analysis import krippendorff_alpha

    rows = query(conn, "SELECT * FROM span_label"
                       + (" WHERE author = ? OR source = 'human'" if author else ""),
                 (author,) if author else ())
    cells: dict[tuple[str, int], dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"human": [], "model": []})
    for r in rows:
        cells[(r["run_id"], r["span_index"])][r["source"]].append(r)

    buckets: dict[str, dict[str, Any]] = {
        "blinded": {"pairs": [], "confusion": defaultdict(Counter)},
        "unblinded": {"pairs": [], "confusion": defaultdict(Counter)},
    }
    for (_run, _idx), cell in cells.items():
        if not cell["human"] or not cell["model"]:
            continue
        h, m = cell["human"][0], cell["model"][0]
        if h["span_hash"] != m["span_hash"]:
            continue     # the two judged different text; not a disagreement
        bucket = buckets["blinded" if h["blinded"] else "unblinded"]
        bucket["pairs"].append((h["label"], m["label"]))
        bucket["confusion"][h["label"]][m["label"]] += 1

    out: dict[str, Any] = {"labels": list(SPAN_LABELS), "by_blinding": {}}
    for name, bucket in buckets.items():
        pairs = bucket["pairs"]
        if not pairs:
            out["by_blinding"][name] = {"n": 0, "exact": None, "alpha": None,
                                        "per_label": {}, "confusion": {}}
            continue
        exact = sum(1 for a, b in pairs if a == b) / len(pairs)
        units = {str(i): [SPAN_LABELS.index(a), SPAN_LABELS.index(b)]
                 for i, (a, b) in enumerate(pairs)}
        alpha = krippendorff_alpha(units, levels=list(range(len(SPAN_LABELS))),
                                   nominal=True)
        per_label = {}
        for label in SPAN_LABELS:
            human_says = [p for p in pairs if p[0] == label]
            model_says = [p for p in pairs if p[1] == label]
            hit = sum(1 for a, b in human_says if b == label)
            per_label[label] = {
                "n_human": len(human_says),
                "n_model": len(model_says),
                "recall": round(hit / len(human_says), 3) if human_says else None,
                "precision": round(hit / len(model_says), 3) if model_says else None,
            }
        out["by_blinding"][name] = {
            "n": len(pairs),
            "exact": round(exact, 3),
            "alpha": None if alpha != alpha else round(alpha, 3),   # NaN-safe
            "per_label": per_label,
            "confusion": {h: dict(c) for h, c in bucket["confusion"].items()},
        }

    blind = out["by_blinding"]["blinded"]
    out["usable"] = bool(blind["n"] and blind["alpha"] is not None
                         and blind["alpha"] >= USABLE_ALPHA)
    out["verdict"] = _verdict(out, blind)
    return out


def _verdict(out: dict[str, Any], blind: dict[str, Any]) -> str:
    unblind = out["by_blinding"]["unblinded"]
    if not blind["n"]:
        if unblind["n"]:
            return (f"{unblind['n']} pair(s), none of them blind. Agreement measured "
                    f"this way cannot separate how often the proposal was right from "
                    f"how much it anchored the analyst.")
        return "no span has both a proposal and an adjudication yet"

    parts = [f"blind: {blind['n']} pair(s), {blind['exact']:.0%} exact, "
             f"alpha {blind['alpha']}"]
    if unblind["n"]:
        gap = (unblind["exact"] or 0) - (blind["exact"] or 0)
        powered = blind["n"] >= MIN_GAP_PAIRS and unblind["n"] >= MIN_GAP_PAIRS
        if not powered:
            parts.append(f"unblind {unblind['exact']:.0%} on {unblind['n']} pair(s) — "
                         f"too few either side to read the difference")
        elif gap > 0:
            parts.append(f"unblind runs {gap:+.0%}, which is what anchoring looks "
                         f"like; it is not evidence the proposals were better")
        else:
            parts.append(f"unblind runs {gap:+.0%}, so there is no sign of anchoring "
                         f"in this set")
    parts.append("usable as a proposer" if out["usable"]
                 else f"below alpha {USABLE_ALPHA}: proposals stay suggestions")
    return "; ".join(parts)
