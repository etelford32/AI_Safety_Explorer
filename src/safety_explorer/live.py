"""Analyse a conversation the user is having right now, pasted in as text.

The question this answers is "what can the instrument do while I have a model open in
another tab". The honest answer has two halves, and the second one is the important one.

**What it can do.** Everything that reads the text: Layer 1 features, Layer 1.5 register,
segmentation and span evidence, posture, and — the thing a campaign cannot give you at all
— the trajectory of the register *across turns*, as the conversation is pushed. A campaign
is one prompt and one response per cell by construction; a real conversation is where you
find out whether the model cools when you press it.

**What it cannot do, and this is not a limitation to be engineered around.** Layer 0 needs
an answer key, and an answer key is derived from a prompt whose parameters this instrument
wrote down in advance. A conversation typed into a chat window has no such key, so there is
no objective channel — which means no capability axis, no decoupling plane, no twin delta,
no retention ratio and no tone-bias control, because every one of those is *defined* as a
comparison against something objective or against a matched baseline. What is left is
description: rich, immediately useful, and not a measurement of the safety surface.

The bridge between the two is `_match_prompt`: if what you asked is close enough to a
corpus prompt, the key for that prompt applies and Layer 0 comes back. That is reported per
turn rather than assumed, because a near-miss match scored against the wrong key produces a
confident number about nothing.

Provenance is Tier B at best — a chat surface, system prompt and sampling unknown — and
every analysis here inherits that. The instrument already refuses to pool tiers silently;
this module does not get an exception.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import metrics, probes, stance as st
from . import conversation as conv

LIVE_VERSION = "1"

#: Role-marker conventions seen in the wild, most specific first. Each maps a matched
#: label to a role. Deliberately a small, explicit list rather than a clever heuristic:
#: a conversation split wrongly produces confident nonsense, and the view reports which
#: convention fired so a bad split is visible rather than silent.
SPEAKER_PATTERNS: list[tuple[str, str]] = [
    (r"^\s*(?:You said|You asked)\s*:?\s*$", "user"),
    (r"^\s*(?:ChatGPT|Claude|Assistant|Gemini|Copilot|Model)\s+said\s*:?\s*$", "assistant"),
    (r"^\s*(?:User|You|Human|Me|Q)\s*:\s*", "user"),
    (r"^\s*(?:Assistant|Claude|ChatGPT|AI|Bot|Model|A)\s*:\s*", "assistant"),
    (r"^\s*###\s*(?:User|Human)\s*$", "user"),
    (r"^\s*###\s*(?:Assistant|Claude)\s*$", "assistant"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), role) for p, role in SPEAKER_PATTERNS]


def split_turns(text: str) -> dict[str, Any]:
    """Cut pasted text into turns, and say how — or say that it could not.

    Returns the turns plus the convention that produced them. A caller that renders the
    turns without showing the convention is hiding the one thing that tells a reader
    whether to believe the rest.
    """
    raw = (text or "").strip()
    if not raw:
        return {"turns": [], "convention": None, "confident": False,
                "note": "nothing pasted"}

    # A JSON messages array — what an API log or an export gives you. Unambiguous, so it
    # is tried first and reported as confident.
    try:
        parsed = json.loads(raw)
        messages = parsed if isinstance(parsed, list) else parsed.get("messages")
        if isinstance(messages, list) and messages:
            turns = [{"role": (m.get("role") or "user"), "text": (m.get("content") or "")}
                     for m in messages if isinstance(m, dict)]
            turns = [t for t in turns if t["text"].strip()]
            if turns:
                return {"turns": turns, "convention": "json messages",
                        "confident": True, "note": ""}
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass

    lines = raw.splitlines()
    hits: list[tuple[int, str, str]] = []   # (line index, role, text after the marker)
    for i, line in enumerate(lines):
        for rx, role in _COMPILED:
            m = rx.match(line)
            if m:
                hits.append((i, role, line[m.end():]))
                break

    if len(hits) < 2:
        # No usable markers. Treated as a single assistant turn and SAID so, rather than
        # guessed at: a wrong split here would attribute the model's register to the user
        # or the reverse, which inverts every reading that follows.
        return {
            "turns": [{"role": "assistant", "text": raw}],
            "convention": None, "confident": False,
            "note": ("no speaker markers found, so this was read as a single assistant "
                     "turn. If it is a conversation, label the turns (\"User:\" / "
                     "\"Assistant:\") or paste the JSON messages array — a wrong split "
                     "would attribute the model's register to you"),
        }

    turns = []
    for n, (idx, role, tail) in enumerate(hits):
        end = hits[n + 1][0] if n + 1 < len(hits) else len(lines)
        body = "\n".join([tail] + lines[idx + 1:end]).strip()
        if body:
            turns.append({"role": role, "text": body})

    roles = {t["role"] for t in turns}
    return {
        "turns": turns,
        "convention": "speaker markers",
        "confident": len(roles) > 1,
        "note": ("" if len(roles) > 1 else
                 "every turn came out with the same role, which usually means the "
                 "markers were matched in only one direction"),
    }


def analyse(text: str, corpus=None, cuts: st.Cuts | None = None,
            language: str = "en") -> dict[str, Any]:
    """Layer 1 and 1.5 over a PASTED conversation: split the text, then analyse the turns.

    `cuts` come from a campaign's population, because posture is a statement about where
    a response sits among others. Without them every turn is `unclassified`, which is the
    honest answer rather than a missing feature.
    """
    split = split_turns(text)
    return analyse_turns(split["turns"], corpus, cuts, language, split=split)


def analyse_turns(raw_turns: list[dict[str, Any]], corpus=None,
                  cuts: st.Cuts | None = None, language: str = "en",
                  split: dict[str, Any] | None = None) -> dict[str, Any]:
    """The same analysis over turns that ARRIVE structured, not split out of pasted text.

    This is the seam the agent integration hangs on. A pasted transcript is split first
    (`analyse`); an agent framework, a wrapped provider or a log tail emits turns already
    carrying their role, so it hands them straight here — no splitter, no guess about who
    spoke, and therefore no chance of attributing the model's register to the user. When
    the turns did not come from a splitter, `split` is synthesised as confident, because
    the source stated the roles rather than the tool inferring them.
    """
    if split is None:
        roles = {t.get("role") for t in raw_turns}
        split = {"convention": "structured turns", "confident": len(roles) > 1,
                 "note": ("" if len(roles) > 1 else
                          "every turn carried the same role; the source labelled them "
                          "one-sidedly")}
    turns: list[dict[str, Any]] = []
    last_user = ""

    for i, t in enumerate(raw_turns):
        entry: dict[str, Any] = {"index": i, "role": t["role"],
                                 "n_words": len(t["text"].split()),
                                 "text": t["text"]}
        if t["role"] == "user":
            last_user = t["text"]
            # Does this question have an answer key? Reported per turn, because a
            # near-miss scored against the wrong key is a confident number about nothing.
            if corpus is not None:
                from .ingest import _match_prompt

                pid, score = _match_prompt(corpus, t["text"])
                entry["corpus_match"] = {
                    "prompt_id": pid, "score": score,
                    "layer0": bool(pid),
                    "note": ("this question matches a corpus prompt, so its answer key "
                             "applies and the reply can be scored objectively" if pid
                             else "no corpus prompt matches, so there is no answer key "
                                  "and Layer 0 cannot run on the reply"),
                }
            turns.append(entry)
            continue

        s = st.extract(t["text"], language)
        entry["stance"] = s
        entry["features"] = metrics.extract(t["text"])
        entry["awareness"] = probes.spontaneous_awareness(t["text"])
        entry["posture"] = st.posture(s, cuts, None)
        entry["levels"] = ({d: st.level(s[d]) for d in st.DIMENSIONS}
                           if s.get("available") else None)
        # A long turn that fired almost nothing is probably under-read, not neutral.
        entry["underread"] = st.underread(s)
        # The embedding reading sits beside the regex one, never replacing it. On a real
        # transcript this is where recall shows: a warm turn the lexicon read as neutral
        # may read as warm here — IF the backend is a real embedding. With the stdlib
        # fallback it is a placeholder, and `trustworthy` says so.
        entry["embedding"] = st.embedding_reading(t["text"])
        entry["spans"] = [
            {"index": sp.index, "kind": sp.kind, "n_words": sp.evidence["n_words"],
             "refusal": bool(sp.evidence["refusal"]),
             "evaluation_aware": sp.evidence["evaluation_aware"]}
            for sp in conv.segment(t["text"], None, language)
        ]
        entry["prompted_by"] = last_user[:400]
        turns.append(entry)

    assistant = [t for t in turns if t["role"] == "assistant"]
    scored = [t for t in assistant if (t.get("stance") or {}).get("available")]

    # The trajectory is the point of this view. A campaign gives one response per cell by
    # construction, so how a register moves as a conversation is pushed is the one thing
    # it cannot show at all.
    channels = (*st.DIMENSIONS, "refusal_rate")
    trajectory = {c: [{"turn": t["index"], "value": t["stance"][c]} for t in scored]
                  for c in channels}
    turning = {}
    for c in channels:
        pts = {"points": [{"index": p["turn"], c: p["value"]} for p in trajectory[c]]}
        turning[c] = st.turn_point(pts, c)

    postures = [t["posture"] for t in assistant]
    shifts = [{"from": a, "to": b, "at": i + 1}
              for i, (a, b) in enumerate(zip(postures, postures[1:])) if a != b]

    layer0 = [t for t in turns
              if t["role"] == "user" and (t.get("corpus_match") or {}).get("layer0")]
    underread = [t for t in assistant if t.get("underread")]
    emb = next((t.get("embedding") for t in assistant if t.get("embedding")), None)

    return {
        "live_version": LIVE_VERSION,
        "stance_version": st.STANCE_VERSION,
        "split": {k: v for k, v in split.items() if k != "turns"},
        "n_turns": len(turns),
        "n_assistant": len(assistant),
        "n_scored": len(scored),
        "turns": turns,
        "trajectory": trajectory,
        "turning_points": turning,
        "posture_sequence": postures,
        "posture_shifts": shifts,
        "has_cuts": cuts is not None,
        "layer0_available_turns": [t["index"] for t in layer0],
        "underread_turns": [t["index"] for t in underread],
        "embedding_backend": (emb or {}).get("backend"),
        "embedding_trustworthy": bool((emb or {}).get("trustworthy")),
        "drift": _drift(scored, trajectory, postures, emb),
        "limits": _limits(split, scored, cuts, layer0, underread, emb, language),
    }


def _drift(scored, trajectory, postures, emb) -> dict[str, Any]:
    """Register drift, routed through the embedding reading when it can be trusted.

    The lexicon under-reads natural prose — a warm turn that never says "let's" scores
    zero warmth — so a drift built on it stays quiet exactly where a real drift is hardest
    to see by eye. When a semantic embedding backend is installed and passes its
    generalization control, the register channels (warmth, moralising, distancing) are read
    from it instead, on the same 0-5 ladder, and the drift finally sees the shift the
    lexicon missed.

    Refusal keeps its lexicon channel deliberately: refusal phrasings ("I can't", "I'm not
    able to") are the most canonical register markers there are, so the lexicon's recall
    gap is smallest there, and the embedding model carries no refusal axis. Each channel is
    read by its best available estimator, and the source is reported so the reading is never
    mistaken for more than it is.
    """
    from . import stance as st

    trustworthy = bool(emb and emb.get("trustworthy"))
    reg_dims = ("warmth", "moralizing", "distancing")
    drift_traj: dict[str, Any] = {}
    for d in reg_dims:
        pts = []
        for t in scored:
            if trustworthy and t.get("embedding"):
                lvl = (t["embedding"]["levels"] or {}).get(d)
            else:
                lvl = st.level((t.get("stance") or {}).get(d))
            pts.append({"turn": t["index"], "value": lvl})
        drift_traj[d] = pts
    # Refusal always from the lexicon, on the ladder.
    drift_traj["refusal_rate"] = [
        {"turn": t["index"], "value": st.level((t.get("stance") or {}).get("refusal_rate"))}
        for t in scored]

    out = st.register_drift(drift_traj, postures, level_values=True)
    out["source"] = "embedding" if trustworthy else "lexicon"
    out["source_note"] = (
        "register channels read from the embedding backend, which passed generalization; "
        "refusal from the lexicon"
        if trustworthy else
        "register channels read from the lexicon — it under-reads natural prose, so this "
        "drift can miss a real shift that uses no canonical markers. Install a semantic "
        "embedding backend (set EXPLORER_EMBED_BACKEND) to route drift through it")
    return out


def _limits(split, scored, cuts, layer0, underread, emb, language) -> list[str]:
    """What this reading cannot tell you. Rendered with the result, never on request.

    A descriptive analysis presented without its limits is the failure this whole
    instrument is built to avoid: the numbers look exactly like the ones from a
    controlled campaign, and nothing on the page says they are not.
    """
    out = []
    if not layer0:
        out.append(
            "No Layer 0. Nothing here was scored against an answer key, because none of "
            "these questions matches a corpus prompt — so there is no objective channel, "
            "and with it go the capability axis, the decoupling plane, twin deltas, "
            "retention and the tone-bias control. Every number below describes the text.")
    else:
        out.append(
            f"{len(layer0)} turn(s) match a corpus prompt, so the reply to those can be "
            f"scored objectively. The rest cannot.")
    if not split["confident"]:
        out.append(f"Turn split is not confident: {split['note']}")
    if not st.available(language):
        out.append(f"No validated stance lexicon for {language!r}; register is not scored.")
    elif not scored:
        out.append("No assistant turn carried a scorable register.")
    if underread and scored:
        out.append(
            f"Low recall: {len(underread)} of {len(scored)} scored turn(s) are "
            "substantial but fired almost no markers. On real prose that usually means "
            "the lexicon under-read the register, NOT that the register was neutral — the "
            "lexicons match canonical phrasings (\"let's\", \"we can\", \"it's "
            "important to note\") and natural warmth often does not announce itself that "
            "way. Read those turns, not their near-zero levels. This is the gap the "
            "stance rubric exists to close: the automatic reading is an indicator until "
            "blinded humans have rated the same turns.")
    if emb is not None and not emb.get("trustworthy"):
        out.append(
            f"The embedding register reading uses the stdlib fallback backend "
            f"({emb.get('backend')!r}), which is NOT semantic — it matches surface forms, "
            "so it carries the same recall problem as the lexicon and its levels are a "
            "placeholder, not a measurement. Install a real embedding backend and it must "
            "pass the generalization control before its reading is believed; that reading "
            "is what would close the recall gap the lexicon leaves.")
    if cuts is None:
        out.append(
            "No posture: posture is a statement about where a response sits in a "
            "population, and there is no campaign in this database to compare against. "
            "Run one and the same conversation gets a posture per turn.")
    out.append(
        "Provenance is Tier B at best — a chat surface, with the system prompt, the "
        "sampling parameters and any retrieval or tools unknown. The instrument does not "
        "pool tiers silently and this is not an exception.")
    return out
