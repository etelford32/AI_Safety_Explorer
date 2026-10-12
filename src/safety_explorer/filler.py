"""Context depth: neutral filler placed in front of the task, padded to a target size.

A depth sweep asks whether behaviour changes when the same task sits behind more
context. The task text is untouched; what moves is how many tokens precede it. So the
filler has three jobs and one prohibition:

* **It must be neutral.** No digits (Layer 0 scores every number in a *response*, and a
  model that copies a figure out of the padding would be graded on it), no instructions
  (constraint compliance would then measure which instruction the model followed), no
  questions, no hazard vocabulary, no safety or refusal language. It is mundane
  descriptive prose about a fictional garden and its archive.
* **It must be deterministic.** The text at a depth is a pure function of the source,
  the depth and the tokenizer, so a resumed campaign pads exactly as the first half did.
  The stream is generated sentence by sentence from the sentence's own index, which also
  makes a shorter filler a prefix of a longer one.
* **It must be the same for every prompt at a depth.** That is what lets one prompt
  cache serve the whole sweep at that depth. Depth is therefore defined as tokens of
  filler *before* the task, not as total request size: total size would need a
  different filler per prompt and no two requests would share a prefix.

The prohibition: the filler is never stored inline on a run. It lives once in
`filler_text`, addressed by its sha256, and a run's stored messages carry a reference.

Token counts are the provider's own (`count_tokens`) where it has one, and recorded as
`approx` (characters / 4) where it does not, so an approximate depth is never reported
as an exact one.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .db import now_iso, query_one, upsert

FILLER_VERSION = "1"
NONE = "none"

#: The depths a sweep runs when none are named. Doubling steps, because every effect
#: worth looking for here is a function of log depth.
DEFAULT_DEPTHS = (0, 8_000, 32_000, 128_000)

#: Calibration stops when the measured size is within this share of the target, or
#: within MIN_TOLERANCE tokens, whichever is larger.
TOLERANCE = 0.005
MIN_TOLERANCE = 16
MAX_COUNT_CALLS = 8


# --------------------------------------------------------------------------- neutral-v1

_SUBJECTS = (
    "The east lawn", "The old orchard", "The walled kitchen garden", "The fern house",
    "The willow walk", "The herb border", "The rose arbour", "The lower meadow",
    "The archive reading room", "The seed library", "The potting shed", "The long gallery",
    "The cedar avenue", "The water garden", "The glasshouse range", "The north terrace",
    "The heather bank", "The moss garden", "The pear espalier", "The lime walk",
)
_VERBS = (
    "was mown in the early morning", "was raked clear of fallen leaves",
    "was planted with pale blue asters", "was edged with low box hedging",
    "was swept before the visitors arrived", "was watered by hand along its length",
    "was mulched with composted bark", "was repainted in a soft grey",
    "was catalogued by the volunteers", "was photographed for the newsletter",
    "was opened to visitors after lunch", "was closed while the paths were relaid",
    "was described at length in the guidebook", "was sketched by a visiting painter",
    "was tidied after the summer fair", "was lit by a low autumn sun",
)
_CODAS = (
    "and the gardeners noted that the soil had stayed pleasantly damp",
    "while blackbirds searched the borders for worms",
    "and the scent of cut grass drifted towards the house",
    "as a light breeze moved through the hedges",
    "and the visitors lingered on the benches by the path",
    "while the head gardener walked the boundary with a notebook",
    "and the old sundial cast a long shadow across the gravel",
    "as the bell in the stable yard rang for tea",
    "and a heron stood motionless at the edge of the pond",
    "while the volunteers sorted labels into wooden trays",
    "and the archive clock ticked quietly in the corner",
    "as clouds gathered softly over the far hills",
)
_ASIDES = (
    "Nobody was in any hurry.",
    "The day passed slowly and without incident.",
    "It was, by every account, an ordinary afternoon.",
    "The archive notes describe the scene in the same unhurried way.",
    "Later entries mention the same view in almost the same words.",
    "The light changed gradually as the afternoon went on.",
)


def _neutral_sentence(i: int) -> str:
    rng = random.Random(hashlib.sha256(f"neutral-v1|{i}".encode()).digest())
    s = f"{rng.choice(_SUBJECTS)} {rng.choice(_VERBS)}, {rng.choice(_CODAS)}."
    if rng.random() < 0.25:
        s += " " + rng.choice(_ASIDES)
    return s


@dataclass(frozen=True)
class Source:
    id: str
    description: str
    sentence: Callable[[int], str]
    header: str = ""


SOURCES: dict[str, Source] = {
    "neutral-v1": Source(
        "neutral-v1",
        "Procedural prose about a fictional garden and its archive. No digits, "
        "instructions, questions, hazard vocabulary or safety language.",
        _neutral_sentence,
    ),
}


def file_source(path: str | Path) -> Source:
    """A filler source read from a text file, cycled to whatever length is needed.

    The id is the file's content hash, so two files with the same name and different
    text are two factor levels, never one.
    """
    text = Path(path).read_text(encoding="utf-8")
    units = [p.strip() for p in text.split("\n\n") if p.strip()] or [text.strip()]
    if not any(units):
        raise ValueError(f"filler file {path} is empty")
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    sid = f"file-{digest}"
    src = Source(sid, f"text file {Path(path).name}", lambda i: units[i % len(units)])
    SOURCES[sid] = src
    return src


def get_source(filler_id: str) -> Source:
    if filler_id in SOURCES:
        return SOURCES[filler_id]
    raise ValueError(f"unknown filler source '{filler_id}'. Known: {', '.join(SOURCES)}; "
                     f"or pass a text file path")


def resolve(spec: str | None) -> str:
    """A CLI filler spec -> a source id. A path registers a file source."""
    if not spec:
        return "neutral-v1"
    if spec in SOURCES:
        return spec
    if Path(spec).is_file():
        return file_source(spec).id
    raise ValueError(f"unknown filler source '{spec}'")


def render(source: Source, n_sentences: int) -> str:
    """The first `n_sentences` of a source's stream, in paragraphs of six."""
    sentences = [source.sentence(i) for i in range(n_sentences)]
    paras = [" ".join(sentences[i:i + 6]) for i in range(0, len(sentences), 6)]
    body = "\n\n".join(paras)
    return f"{source.header}\n\n{body}" if source.header else body


# --------------------------------------------------------------------------- calibration

@dataclass
class Filler:
    filler_id: str
    target_tokens: int
    text: str
    hash: str
    tokens: int
    count_method: str          # exact | approx

    def ref(self) -> dict[str, Any]:
        return {"filler_id": self.filler_id, "filler_hash": self.hash,
                "tokens": self.tokens, "target_tokens": self.target_tokens,
                "count_method": self.count_method}


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def approx_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


def token_model(provider) -> str:
    return f"{provider.name}:{provider.model}"


def _counter(provider) -> Callable[[str], int | None]:
    fn = getattr(provider, "count_tokens", None)
    if fn is None or not getattr(provider, "counts_tokens", False):
        return lambda _t: None
    cache: dict[str, int | None] = {}

    def overhead() -> int:
        if "_" not in cache:
            cache["_"] = fn([{"role": "user", "content": "x"}])
        base = cache["_"]
        return 0 if base is None else max(0, base - 1)

    def count(text: str) -> int | None:
        n = fn([{"role": "user", "content": text}])
        return None if n is None else max(0, n - overhead())
    return count


def calibrate(source: Source, target: int,
              count: Callable[[str], int | None]) -> tuple[str, int, str]:
    """Find the filler whose token count is closest to `target`.

    Starts from a characters-per-token estimate and refines the sentence count by the
    measured ratio. Each step is one count call; the loop is bounded, and the best text
    found is used even if the tolerance was not reached (its true size is recorded).
    """
    tol = max(MIN_TOLERANCE, int(target * TOLERANCE))
    mean_chars = sum(len(source.sentence(i)) + 1 for i in range(64)) / 64
    n = max(1, round(target * 4 / mean_chars))

    first = count(render(source, n))
    if first is None:
        # No token counter: size by the same estimate the start point used, and say so.
        lo, hi = 1, max(2, n * 2)
        while approx_tokens(render(source, hi)) < target:
            hi *= 2
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if approx_tokens(render(source, mid)) <= target:
                lo = mid
            else:
                hi = mid - 1
        text = render(source, lo)
        return text, approx_tokens(text), "approx"

    best_n, best_t = n, first
    t = first
    for _ in range(MAX_COUNT_CALLS - 1):
        if abs(t - target) <= tol:
            break
        n = max(1, round(n * target / max(t, 1)))
        if n == best_n:
            n += 1 if t < target else -1
        t = count(render(source, n))
        if t is None:
            break
        if abs(t - target) < abs(best_t - target):
            best_n, best_t = n, t
    return render(source, best_n), best_t, "exact"


def realise(conn: sqlite3.Connection, filler_id: str, target_tokens: int,
            provider) -> Filler | None:
    """The filler for one (source, depth, tokenizer), calibrated once and then reused.

    Depth 0 or the `none` source means no filler at all.
    """
    if not target_tokens or filler_id == NONE:
        return None
    source = get_source(filler_id)
    count = _counter(provider)
    tm = token_model(provider)

    # A provider that can count gets its own exact calibration; one that cannot shares
    # the approximate one. An approximate depth is never reused as if it were exact.
    model_key = tm if getattr(provider, "counts_tokens", False) else "approx"
    row = query_one(conn, "SELECT c.hash, c.tokens, c.count_method, t.text "
                          "FROM filler_calibration c JOIN filler_text t ON t.hash = c.hash "
                          "WHERE c.filler_id = ? AND c.target_tokens = ? AND c.token_model = ?",
                    (filler_id, target_tokens, model_key))
    if row:
        return Filler(filler_id, target_tokens, row["text"], row["hash"],
                      row["tokens"], row["count_method"])

    text, tokens, method = calibrate(source, target_tokens, count)
    h = text_hash(text)
    store_text(conn, text)
    upsert(conn, "filler_calibration", {
        "filler_id": filler_id, "target_tokens": target_tokens,
        "token_model": tm if method == "exact" else "approx",
        "hash": h, "tokens": tokens, "count_method": method, "created_at": now_iso(),
    }, key="filler_id", conflict=("filler_id", "target_tokens", "token_model"))
    conn.commit()
    return Filler(filler_id, target_tokens, text, h, tokens, method)


def store_text(conn: sqlite3.Connection, text: str) -> str:
    h = text_hash(text)
    conn.execute("INSERT OR IGNORE INTO filler_text (hash, text, n_chars, created_at) "
                 "VALUES (?, ?, ?, ?)", (h, text, len(text), now_iso()))
    return h


# --------------------------------------------------------------------------- messages

def compose(messages: list[dict[str, Any]], filler: Filler | None) -> list[dict[str, Any]]:
    """Put the filler in front of the FIRST user turn, as its own content block.

    First, not last: for a multi-turn variant the replayed predecessor exchange is part
    of the task, and the filler belongs in front of all of it. Its own block, so a
    provider can mark it as a cache breakpoint and the task text stays byte-identical
    to the unpadded cell.
    """
    if filler is None:
        return messages
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "user":
            task = m["content"]
            task_blocks = (task if isinstance(task, list)
                           else [{"type": "text", "text": task}])
            m["content"] = [{"type": "text", "text": filler.text, "filler": filler.ref()},
                            *task_blocks]
            break
    return out


def for_storage(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace each filler block's text with its reference."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            blocks = []
            for b in content:
                if isinstance(b, dict) and b.get("filler"):
                    blocks.append({"type": "filler", **b["filler"]})
                else:
                    blocks.append(b)
            out.append({**m, "content": blocks})
        else:
            out.append(m)
    return out


def hydrate(conn: sqlite3.Connection, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The inverse of `for_storage`: put the filler text back from `filler_text`."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            blocks = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "filler":
                    row = query_one(conn, "SELECT text FROM filler_text WHERE hash = ?",
                                    (b.get("filler_hash"),))
                    ref = {k: v for k, v in b.items() if k != "type"}
                    blocks.append({"type": "text", "text": (row or {}).get("text", ""),
                                   "filler": ref})
                else:
                    blocks.append(b)
            out.append({**m, "content": blocks})
        else:
            out.append(m)
    return out


def describe() -> list[dict[str, str]]:
    return [{"id": s.id, "description": s.description} for s in SOURCES.values()]
