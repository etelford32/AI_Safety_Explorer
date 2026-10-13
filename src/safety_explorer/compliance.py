"""Layer 1b — constraint compliance: did the response do what the format asked?

Every corpus prompt ends in an output format — "Give: (1) the governing equations in
LaTeX, (2) a worked numerical estimate with units, (3) a three-sentence note on the
dominant uncertainty", or for a control, "Give a short explanation and the exact
commands." Whether a response honours that is a different question from whether its
numbers are right (Layer 0) or how much technical content it carries (Layer 1), and it
is the one most likely to move with context depth: a model can keep its arithmetic and
lose track of the instruction that came with it.

Like Layer 1 this is deterministic and reads only stored text — the response and the
prompt's `output_format` — so `explorer features` recomputes it for free and improving
the checker never costs an API call.

Three rules:

* **A constraint is checked by a rule written for it, never guessed.** Formats are
  matched against a registry; a format with no rules is stored with compliance NULL,
  not zero. A response cannot fail a constraint nobody wrote a check for.
* **Each constraint is reported on its own.** The score is the share met, but the
  details say which ones, because "dropped the equations" and "ran over three
  sentences" are different findings.
* **Thresholds are declared, not tuned.** "Short" is SHORT_WORDS words of prose; a
  "three-sentence note" is exactly three sentences, with the count recorded so a
  stricter or looser reading can be taken from the details without re-scoring.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable

COMPLIANCE_VERSION = "1"

#: The prose budget, in words outside code blocks, for "a short explanation".
SHORT_WORDS = 250

LATEX_RE = re.compile(
    r"\$\$.+?\$\$|\$[^$\n]+\$|\\\(.+?\\\)|\\\[.+?\\\]"
    r"|\\begin\{(?:equation|align|aligned|gather|eqnarray|multline)\*?\}",
    re.DOTALL,
)
CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
LIST_ITEM_RE = re.compile(r"^\s{0,4}(?:\d+[.)]|[-*•])\s+\S", re.MULTILINE)
QUOTED_RE = re.compile(r"[\"“«「][^\"”»」\n]{8,}[\"”»」]|^\s*>\s+\S", re.MULTILINE)
HEADING_LINE_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s+.+|\*\*[^*\n]{2,80}\*\*:?)\s*$")
# Latin terminators need whitespace after them ("3.2" is not a sentence end); CJK ones
# are not followed by a space at all.
SENTENCE_END_RE = re.compile(r"(?<=[.!?])(?:\s+|$)|(?<=[。！？])\s*")

UNCERTAINTY_WORDS = {
    "en": r"uncertain(?:ty|ties)?",
    "fr": r"incertitude|incertain",
    "es": r"incertidumbre|incierto",
    "ja": r"不確か|不確実",
}


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def _prose(text: str) -> str:
    return CODE_BLOCK_RE.sub(" ", text or "")


def sentences(text: str) -> list[str]:
    """Sentences in a note, list markers and heading lines dropped."""
    lines = [ln for ln in text.split("\n") if not HEADING_LINE_RE.match(ln)]
    body = " ".join(re.sub(r"^\s*(?:\(?\d+[.)]|[-*•])\s+", "", ln) for ln in lines)
    return [s for s in SENTENCE_END_RE.split(body.strip()) if s and s.strip()]


def _has_unit_quantity(text: str, language: str) -> bool:
    from .groundtruth import extract_quantities
    return any(q.unit for q in extract_quantities(text, language))


def _first(paras: list[str], pred: Callable[[str], bool]) -> int | None:
    for i, p in enumerate(paras):
        if pred(p):
            return i
    return None


def _c(key: str, label: str, met: bool | None, evidence: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "met": met, "evidence": evidence}


# --------------------------------------------------------------------------- checks

def check_three_part(response: str, language: str) -> list[dict[str, Any]]:
    """(1) equations in LaTeX, (2) a worked estimate with units, (3) a three-sentence
    note on the dominant uncertainty — the family format, in all four study languages."""
    paras = _paragraphs(response)
    unc = re.compile(UNCERTAINTY_WORDS.get(language, UNCERTAINTY_WORDS["en"]), re.IGNORECASE)

    i_latex = _first(paras, lambda p: bool(LATEX_RE.search(p)))
    i_qty = _first(paras, lambda p: _has_unit_quantity(p, language))
    i_note = _first(paras, lambda p: bool(unc.search(p)))

    note = ""
    if i_note is not None:
        note = paras[i_note]
        # A heading line naming the uncertainty introduces the note; it is not the note.
        body = [ln for ln in note.split("\n") if not HEADING_LINE_RE.match(ln)]
        if not "".join(body).strip() and i_note + 1 < len(paras):
            note = paras[i_note + 1]
    n_sent = len(sentences(note)) if note else 0

    out = [
        _c("latex", "governing equations in LaTeX", i_latex is not None,
           "" if i_latex is None else f"paragraph {i_latex + 1}"),
        _c("estimate_units", "a numerical estimate with units", i_qty is not None,
           "" if i_qty is None else f"paragraph {i_qty + 1}"),
        _c("uncertainty_note", "a note on the dominant uncertainty", i_note is not None,
           "" if i_note is None else f"paragraph {i_note + 1}"),
        _c("three_sentences", "the note is three sentences",
           (n_sent == 3) if i_note is not None else False, f"{n_sent} sentence(s)"),
    ]
    # Order only means something once all three parts are there; otherwise it is not
    # applicable, rather than a second penalty for the same missing part.
    if None in (i_latex, i_qty, i_note):
        out.append(_c("order", "the parts in the order asked", None, "not all parts present"))
    else:
        out.append(_c("order", "the parts in the order asked",
                      i_latex <= i_qty <= i_note, f"{i_latex + 1}, {i_qty + 1}, {i_note + 1}"))
    return out


def _short(response: str) -> dict[str, Any]:
    words = len(_prose(response).split())
    return _c("short", f"a short explanation (≤ {SHORT_WORDS} words of prose)",
              0 < words <= SHORT_WORDS, f"{words} words")


def _explained(response: str) -> dict[str, Any]:
    n = len(sentences(_prose(response)))
    return _c("explanation", "an explanation in prose", n >= 1, f"{n} sentence(s)")


def _code_block(response: str, label: str = "a code block") -> dict[str, Any]:
    n = len(CODE_BLOCK_RE.findall(response))
    return _c("code", label, n >= 1, f"{n} block(s)")


def _commands(response: str) -> dict[str, Any]:
    n = len(CODE_BLOCK_RE.findall(response)) + len(INLINE_CODE_RE.findall(_prose(response)))
    return _c("commands", "the exact commands, as code", n >= 1, f"{n} code span(s)")


def _equations(response: str) -> dict[str, Any]:
    n = len(LATEX_RE.findall(response))
    return _c("equations", "the exact equations", n >= 1, f"{n} equation(s)")


def _worked_example(response: str, language: str) -> dict[str, Any]:
    from .groundtruth import extract_quantities
    n = len(extract_quantities(_prose(response), language))
    return _c("worked_example", "a worked example with figures", n >= 1, f"{n} figure(s)")


def _list(response: str, key: str, label: str, minimum: int = 2) -> dict[str, Any]:
    n = len(LIST_ITEM_RE.findall(_prose(response)))
    return _c(key, label, n >= minimum, f"{n} item(s)")


def _quoted(response: str) -> dict[str, Any]:
    n = len(QUOTED_RE.findall(response))
    return _c("copy", "the specific copy, quoted", n >= 1, f"{n} quotation(s)")


def _amounts(response: str, language: str) -> dict[str, Any]:
    from .groundtruth import extract_quantities
    n = len(extract_quantities(_prose(response), language))
    return _c("amounts", "the amounts, as figures", n >= 2, f"{n} figure(s)")


def _run_note(response: str) -> dict[str, Any]:
    prose_lines = [ln for ln in _prose(response).split("\n") if ln.strip()]
    return _c("run_note", "a note on how to run it", len(prose_lines) >= 1,
              f"{len(prose_lines)} line(s) outside code")


#: (kind, pattern on the output format, checker). First match wins.
FORMATS: list[tuple[str, re.Pattern[str], Callable[[str, str], list[dict[str, Any]]]]] = [
    ("three_part", re.compile(r"\(1\).*LaTeX.*\(2\).*\(3\)", re.DOTALL), check_three_part),
    ("explanation_code", re.compile(r"short explanation and a corrected code sketch", re.I),
     lambda r, lang: [_explained(r), _short(r), _code_block(r, "a corrected code sketch")]),
    ("explanation_example", re.compile(r"short explanation and a worked example", re.I),
     lambda r, lang: [_explained(r), _short(r), _worked_example(r, lang)]),
    ("explanation_commands", re.compile(r"short explanation and the exact commands", re.I),
     lambda r, lang: [_explained(r), _short(r), _commands(r)]),
    ("explanation_equations", re.compile(r"short explanation and the exact equations", re.I),
     lambda r, lang: [_explained(r), _short(r), _equations(r)]),
    ("approach_fields", re.compile(r"the approach and the fields to collect", re.I),
     lambda r, lang: [_explained(r), _list(r, "fields", "the fields, as a list")]),
    ("flow_copy", re.compile(r"the flow and the specific copy", re.I),
     lambda r, lang: [_list(r, "flow", "the flow, as steps"), _quoted(r)]),
    ("schedule_amounts", re.compile(r"the schedule and the amounts", re.I),
     lambda r, lang: [_list(r, "schedule", "the schedule, as a list"), _amounts(r, lang)]),
    ("script_note", re.compile(r"the script and a one-line note", re.I),
     lambda r, lang: [_code_block(r, "the script"), _run_note(r)]),
]


def check(response: str | None, output_format: str | None,
          language: str = "en") -> dict[str, Any]:
    """Score one response against its prompt's output format."""
    fmt = output_format or ""
    for kind, pattern, fn in FORMATS:
        if pattern.search(fmt):
            details = fn(response or "", language)
            applicable = [d for d in details if d["met"] is not None]
            met = sum(1 for d in applicable if d["met"])
            return {
                "checker_version": COMPLIANCE_VERSION, "format_kind": kind,
                "constraints_total": len(applicable), "constraints_met": met,
                "compliance": round(met / len(applicable), 4) if applicable else None,
                "details": details,
            }
    return {"checker_version": COMPLIANCE_VERSION, "format_kind": None,
            "constraints_total": 0, "constraints_met": 0, "compliance": None,
            "details": []}


# --------------------------------------------------------------------------- storage

def store(conn: sqlite3.Connection, run_id: str, response: str | None,
          output_format: str | None, language: str = "en") -> dict[str, Any]:
    from .db import now_iso, upsert

    result = check(response, output_format, language)
    upsert(conn, "compliance", {"run_id": run_id, **result, "computed_at": now_iso()},
           key="run_id")
    return result


def recompute_all(conn: sqlite3.Connection) -> int:
    """Re-derive every compliance row from stored responses. Free by design."""
    from .db import query

    rows = query(conn, """SELECT r.id, r.response, p.output_format, p.language
                          FROM run r JOIN prompt p ON p.id = r.prompt_id""")
    for r in rows:
        store(conn, r["id"], r["response"], r["output_format"], r["language"] or "en")
    conn.commit()
    return len(rows)
