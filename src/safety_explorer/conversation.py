"""Assemble a stored run into a conversation, and cut it into spans worth arguing about.

Every measure in this instrument so far scores a response as a *whole*: one accuracy,
one technical density, one set of ratings. That is the right unit for the ladder, and
the wrong unit for the question this module serves — *where* in the response did the
behaviour change? A reply that answers three sub-questions and declines the fourth has
one refusal signal and one capability score, and neither says which part was which.

So a run is re-presented here as what it actually is: a short conversation, with the
assistant's turn cut into spans that carry the evidence already computed about them.
Nothing here is a judgement. A span knows that it contains a refusal phrase, a figure
that matched `critical_population` with class `near`, and two hedges; it does not know
whether any of that was appropriate. That call belongs to a human, with a model allowed
to propose — which is what `coanalyse` is for, and why it is a separate module.

Three properties this module has to keep:

* **Free and recomputable.** Pure functions over stored text, like Layer 1 and Layer 0.
  Improving the segmentation never costs an API call.
* **Stable identity.** Labels are stored against a span, so a span needs an identity
  that survives a change to the segmenter. Each carries a hash of its own text; when the
  segmenter changes, labels whose hash no longer matches are reported as stale rather
  than silently re-pointed at different words.
* **Honest about what a turn is.** A follow-up probe is a real second turn in the same
  conversation. A parallel probe is a *different* conversation that happens to quote this
  one. Flattening the two would put words in the model's mouth that it never said in
  this exchange, so they are kept apart and labelled.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from . import metrics, probes

#: Bumped when the segmenter changes shape. Stored with each label so a label made
#: against an older cut can be found again.
SEGMENTER_VERSION = "1"

#: What a span is. Ordered roughly by how much a reader's eye separates them.
SPAN_KINDS = ("heading", "code", "equation", "step", "quote", "paragraph")

_FENCE = re.compile(r"^```[^\n]*\n.*?(?:\n```|\Z)", re.MULTILINE | re.DOTALL)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_DISPLAY_MATH = re.compile(r"^\s*(?:\$\$|\\\[)")
_QUOTE = re.compile(r"^\s{0,3}>")
#: A list item. Kept deliberately close to `metrics.STEP_RE`, because "how many steps
#: did it lay out" and "which step was it" should not be able to disagree.
_ITEM = re.compile(r"^\s{0,4}(?:\d+[.)]|[-*•])\s+", re.MULTILINE)


def _hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:16]


@dataclass
class Span:
    """One block of an assistant turn, with the evidence already known about it."""

    index: int
    kind: str
    text: str
    hash: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def _matched(patterns: list[str], text: str) -> list[str]:
    """The phrases that actually fired, not how many.

    A count tells a reader that something matched; the phrase tells them what, which is
    the difference between evidence they can check and a number they have to trust.
    """
    out: list[str] = []
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            phrase = m.group(0).strip()
            if phrase and phrase.lower() not in {o.lower() for o in out}:
                out.append(phrase)
    return out[:6]


def _blocks(text: str) -> list[tuple[str, str]]:
    """Cut a response into (kind, text) blocks. Code fences survive intact."""
    out: list[tuple[str, str]] = []
    cursor = 0
    for fence in _FENCE.finditer(text):
        if fence.start() > cursor:
            out.extend(_prose_blocks(text[cursor:fence.start()]))
        out.append(("code", fence.group(0)))
        cursor = fence.end()
    if cursor < len(text):
        out.extend(_prose_blocks(text[cursor:]))
    return out


def _prose_blocks(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"\n\s*\n", text):
        if not chunk.strip():
            continue
        if _HEADING.match(chunk):
            out.append(("heading", chunk.strip()))
        elif _DISPLAY_MATH.match(chunk):
            out.append(("equation", chunk.strip()))
        elif _QUOTE.match(chunk):
            out.append(("quote", chunk.strip()))
        elif _ITEM.search(chunk):
            # A list is several claims wearing one paragraph. Splitting it is the whole
            # point: "it answered 1 and 2 and declined 3" is invisible otherwise.
            starts = [m.start() for m in _ITEM.finditer(chunk)]
            if starts and starts[0] > 0:
                lead = chunk[:starts[0]].strip()
                if lead:
                    out.append(("paragraph", lead))
            bounds = starts + [len(chunk)]
            for a, b in zip(bounds, bounds[1:]):
                item = chunk[a:b].strip()
                if item:
                    out.append(("step", item))
        else:
            out.append(("paragraph", chunk.strip()))
    return out


def segment(text: str | None, family_id: str | None = None,
            language: str = "en",
            truth_details: list[dict[str, Any]] | None = None) -> list[Span]:
    """Cut an assistant turn into spans and attach what is already known about each.

    `truth_details` is a stored Layer 0 result. When given, each figure in a span is
    matched back to the target it was scored against, so the span can say "this is the
    critical population, and it was a factor of three high" rather than "there is a
    number here".
    """
    from . import groundtruth as gt

    by_value: dict[float, dict[str, Any]] = {}
    for d in truth_details or []:
        candidate = d.get("best_candidate")
        if candidate is not None:
            by_value.setdefault(float(candidate), d)

    spans: list[Span] = []
    for i, (kind, chunk) in enumerate(_blocks(text or "")):
        quantities = []
        for q in gt.extract_quantities(chunk, language):
            hit = by_value.get(q.value)
            quantities.append({
                "value": q.value, "unit": q.unit,
                "target": hit.get("key") if hit else None,
                "label": hit.get("label") if hit else None,
                "class": hit.get("class") if hit else None,
                "reference": hit.get("reference") if hit else None,
            })
        awareness = probes.spontaneous_awareness(chunk)
        evidence = {
            "refusal": _matched(metrics.REFUSAL_PATTERNS, chunk),
            "hedge": _matched(metrics.HEDGE_PATTERNS, chunk),
            "safety_framing": _matched(metrics.SAFETY_FRAMING_PATTERNS, chunk),
            "citation": _matched(metrics.CITATION_PATTERNS, chunk),
            "quantities": quantities,
            "evaluation_aware": bool(awareness["spontaneous"]),
            "n_words": len(chunk.split()),
        }
        evidence["has_evidence"] = any(
            evidence[k] for k in ("refusal", "hedge", "safety_framing", "citation",
                                  "quantities")
        ) or evidence["evaluation_aware"]
        spans.append(Span(index=i, kind=kind, text=chunk, hash=_hash(chunk),
                          evidence=evidence))
    return spans


def assemble(conn, run_id: str) -> dict[str, Any] | None:
    """Everything the co-analysis page needs about one run, in conversation order."""
    from .db import query, query_one

    run = query_one(conn, """
        SELECT r.*, p.family_id, p.variant, p.title, p.text AS prompt_text,
               p.language, p.arm, p.sub_arm, p.answer_key,
               p.intent, p.operationality, p.specificity, p.autonomy, p.depth
        FROM run r JOIN prompt p ON p.id = r.prompt_id WHERE r.id = ?""", (run_id,))
    if run is None:
        return None

    def _load(value, fallback):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return fallback
        return value if value is not None else fallback

    truth = query_one(conn, "SELECT * FROM ground_truth WHERE run_id = ?", (run_id,))
    details = _load(truth["details"], []) if truth else []
    spans = segment(run["response"], run["family_id"], run["language"] or "en", details)

    # The composed prompt is what the model actually saw, which is not the corpus text
    # when a cue was prepended. Showing the corpus text instead would hide the very
    # manipulation the sandbagging arm exists to measure.
    messages = _load(run.get("messages"), [])
    composed = next((m.get("content") for m in reversed(messages)
                     if m.get("role") == "user"), run["prompt_text"])

    turns: list[dict[str, Any]] = [
        {"role": "user", "kind": "prompt", "text": composed,
         "note": (f"cue {run['cue_id']} ({run['cue_arm']}, level {run['cue_level']})"
                  if run.get("cue_id") and run["cue_id"] != "none" else "")},
        {"role": "assistant", "kind": "response", "text": run["response"] or "",
         "spans": [asdict(s) for s in spans],
         "note": ("truncated at max_tokens" if run.get("finish_reason") == "max_tokens"
                  else "")},
    ]

    aside: list[dict[str, Any]] = []
    for p in query(conn, "SELECT * FROM probe WHERE run_id = ? ORDER BY captured_at",
                   (run_id,)):
        row = {"kind": p["kind"], "mode": p["mode"], "prompt": p["prompt"],
               "response": p["response"], "parsed": _load(p["parsed"], {})}
        if p["mode"] == "followup":
            # A real second turn in this conversation.
            turns.append({"role": "user", "kind": f"probe:{p['kind']}",
                          "text": p["prompt"], "note": "follow-up probe"})
            turns.append({"role": "assistant", "kind": f"probe:{p['kind']}",
                          "text": p["response"] or "", "spans": [],
                          "note": _describe_parse(row["parsed"])})
        else:
            # A different conversation that quotes this one. Shown beside, never inside.
            aside.append(row)

    return {
        "run_id": run_id,
        "segmenter_version": SEGMENTER_VERSION,
        "prompt_id": run["prompt_id"],
        "family_id": run["family_id"],
        "variant": run["variant"],
        "title": run["title"],
        "language": run["language"] or "en",
        "model_id": run["model_id"],
        "provenance_tier": run["provenance_tier"],
        "dimensions": {d: run[d] for d in
                       ("intent", "operationality", "specificity", "autonomy", "depth")},
        "cue": {"id": run.get("cue_id"), "arm": run.get("cue_arm"),
                "level": run.get("cue_level")},
        "turns": turns,
        "parallel_probes": aside,
        "truth": ({"accuracy": truth["accuracy"], "graded": truth["graded_accuracy"],
                   "consistency": truth["consistency"],
                   "cover": truth["answer_key_cover"]} if truth else None),
        "n_spans": len(spans),
        "n_spans_with_evidence": sum(1 for s in spans if s.evidence["has_evidence"]),
    }


def _describe_parse(parsed: dict[str, Any]) -> str:
    if not parsed:
        return ""
    if parsed.get("parsed") is False:
        return "probe answer did not follow the format; kept and counted"
    return ", ".join(f"{k}={v}" for k, v in parsed.items() if k != "parsed")
