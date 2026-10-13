"""`explorer summary`: what a set of campaigns found, side by side, on one screen.

It shows two things:

* **Run health first.** How each campaign's calls ended: finished, cut off at
  `max_tokens`, stopped by the API's refusal, or errored. Cut-off responses are excluded
  from every analysis, so a model that truncates often is read on a skewed subset. This
  table says so before any number is read.
* **Then the Layer 0 readings, model by model.** Graded correctness of each prompt against
  its twin: the ladder rungs, the depth arm and the language arm. Then the control arms.

It reads aggregates and writes aggregates: `summary.json` and `SUMMARY.md`. Neither file
contains prompt or response text, so either can be shared, posted or attached to an issue
as it is.

It describes; it does not test. Pilot campaigns are exploratory under Amendment A1, and pilot
data never enter a confirmatory analysis. Hypotheses are evaluated by `explorer paper`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import __version__, analysis, db

#: The ladder as the summary reads it (docs/PLAN.md). D and E move each family's focal
#: dimension, which is intent in most families.
LADDER = (("C", "C vs B", "a real system, benign"),
          ("D", "D vs C", "ambiguous"),
          ("E", "E vs C", "explicitly risky"),
          ("F", "F vs C", "follow-up after E (recovery)"))
DEPTH = (("C_intro", "at C"), ("D_intro", "at D"), ("E_intro", "at E"))
CONTROL_ARMS = (("alarming_benign", "alarming wording, harmless request"),
                ("innocuous_consequential", "plain wording, consequential request"))
TRUNCATION_WARN = 0.05          # the share paper.py treats as a problem at any depth
NOTE = ("Descriptive. Pilot campaigns are exploratory under Amendment A1: they never enter a "
        "confirmatory analysis. Hypotheses are tested by `explorer paper`.")


def select(conn, names: list[str] | None = None, prefix: str | None = None) -> list[dict]:
    """Campaigns by name or id, in the order given; else every one with the prefix; else all."""
    if names:
        out = []
        for n in names:
            row = db.query_one(conn, "SELECT * FROM campaign WHERE name = ? OR id = ?", (n, n))
            if row is None:
                raise LookupError(n)
            out.append(row)
        return out
    rows = db.query(conn, "SELECT * FROM campaign ORDER BY created_at")
    return [r for r in rows if not prefix or r["name"].startswith(prefix)]


def _cell(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row.get(k) for k in ("n", "n_pairs", "n_families", "median", "mean", "ci95",
                                    "cliffs_delta", "effect", "provisional")}


def campaign(conn, corpus, camp: dict[str, Any], tiers: str = "A") -> dict[str, Any]:
    from . import pricing
    cid = camp["id"]
    params = db.loads(camp["params"]) if isinstance(camp["params"], str) else (camp["params"] or {})
    deltas = analysis.twin_deltas(conn, corpus, cid, tiers)
    rows = {(r["arm"], r["variant"]): r
            for r in analysis.summarise_deltas(deltas, value="gt_graded_delta")}
    lang = [d for d in deltas if d.get("sub_arm") == "language"]
    by_lang = {r["language"]: r for r in analysis.summarise_deltas(
        lang, key="language", value="gt_graded_delta", split_language=False)}
    controls = analysis.control_report(conn, cid, tiers)["arms"]
    cost = pricing.campaign_cost(conn, cid)
    return {
        "id": cid,
        "name": camp["name"],
        "provider": camp["provider"],
        "model": camp["model_id"],
        "repeats": camp["n_repeats"],
        "created_at": camp["created_at"],
        "corpus_version": camp["corpus_version"],
        "settings": {k: params.get(k) for k in ("effort", "max_tokens", "thinking", "temperature")
                     if params.get(k) is not None},
        "health": analysis.run_health(conn, cid),
        "cost_usd": cost["cost"] if cost.get("runs_priced") else None,
        "ladder": {v: _cell(rows.get(("ladder", v))) for v, _, _ in LADDER},
        "depth": {v: _cell(rows.get(("depth", v))) for v, _ in DEPTH},
        "language": {k: _cell(v) for k, v in sorted(by_lang.items())},
        "controls": {arm: {"n": a.get("n"), "median_refusal_signal": a.get("median_refusal_signal"),
                           "median_over_refusal": a.get("median_over_refusal"),
                           "median_unsafe_assistance": a.get("median_unsafe_assistance")}
                     for arm, a in controls.items()},
    }


def warnings(camps: list[dict[str, Any]]) -> list[str]:
    out = []
    for c in camps:
        h = c["health"]
        if h["truncated_share"] > TRUNCATION_WARN:
            out.append(f"{c['name']}: {h['truncated']} of {h['runs']} responses "
                       f"({h['truncated_share']:.0%}) were cut off at max_tokens and are not "
                       f"analysed, so its readings rest on the rest. Re-run it as a new "
                       f"campaign with a higher --max-tokens (thinking counts against it).")
        if h["errors"]:
            out.append(f"{c['name']}: {h['errors']} call(s) errored; `run --resume` retries them.")
    def shown(v):
        return ", ".join(f"{k} {x}" for k, x in v.items()) or "defaults" if isinstance(v, dict) \
            else str(v)
    for key, what in (("settings", "settings"), ("repeats", "repeat counts"),
                      ("corpus_version", "corpus versions")):
        if len({json.dumps(c[key], sort_keys=True) for c in camps}) > 1:
            out.append(f"the campaigns differ in {what} ("
                       + "; ".join(f"{c['name']}: {shown(c[key])}" for c in camps)
                       + "), so a difference between them may be the setting, not the model.")
    return out


def build(conn, corpus, camps: list[dict[str, Any]], tiers: str = "A") -> dict[str, Any]:
    from .groundtruth import SOLVER_VERSION
    read = [campaign(conn, corpus, c, tiers) for c in camps]
    null = db.query_one(conn, "SELECT AVG(null_accuracy) AS n FROM ground_truth "
                              "WHERE null_accuracy IS NOT NULL")
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "instrument": __version__,
        "solver": SOLVER_VERSION,
        "tiers": tiers,
        "metric": "graded correctness against the answer key (Layer 0, truth_graded); "
                  "delta = prompt minus its twin, so negative means less correct",
        "null_control": round(null["n"], 4) if null and null["n"] is not None else None,
        "note": NOTE,
        "campaigns": read,
        "warnings": warnings(read),
    }


# -- rendering ------------------------------------------------------------------------------------

def _fmt_cell(c: dict[str, Any] | None) -> str:
    if not c or c.get("effect") == "no data":
        return "—"
    if c.get("effect") == "not comparable":
        return "not comparable"
    lo, hi = c["ci95"]
    ci = f"[{lo:+.2f}, {hi:+.2f}]" if lo is not None else "[—]"
    return f"{c['median']:+.2f} {ci} n={c['n']}"


def _rows(s: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """(label, one cell per campaign) for every line of the summary, in order."""
    cs = s["campaigns"]
    out: list[tuple[str, list[str]]] = [("## run health", [])]

    def pct(h, k):
        return f"{h[k]} ({h[k] / h['runs']:.0%})" if h["runs"] else "0"
    out += [
        ("calls", [str(c["health"]["runs"]) for c in cs]),
        ("usable for analysis", [pct(c["health"], "usable") for c in cs]),
        ("cut off at max_tokens", [pct(c["health"], "truncated") for c in cs]),
        ("API refusal stops", [str(c["health"]["refusal_stops"]) for c in cs]),
        ("errors", [str(c["health"]["errors"]) for c in cs]),
        ("settings", [", ".join(f"{k} {v}" for k, v in c["settings"].items()) or "defaults"
                      for c in cs]),
        ("repeats", [str(c["repeats"]) for c in cs]),
        ("cost (list price)", [f"${c['cost_usd']:.2f}" if c["cost_usd"] is not None else "—"
                               for c in cs]),
        ("## ladder: change in graded correctness against the twin (median [95% CI] n)", []),
    ]
    out += [(f"{label}  {desc}", [_fmt_cell(c["ladder"].get(v)) for c in cs])
            for v, label, desc in LADDER]
    out.append(("## depth arm: introductory phrasing against the expert twin", []))
    out += [(f"introductory {where}", [_fmt_cell(c["depth"].get(v)) for c in cs])
            for v, where in DEPTH]
    langs = sorted({lang for c in cs for lang in c["language"]})
    if langs:
        out.append(("## language arm: the translation against its English twin (C, D, E pooled)", []))
        out += [(f"{lang}", [_fmt_cell(c["language"].get(lang)) for c in cs]) for lang in langs]
    out.append(("## controls: median refusal signal (0 = none, 1 = refusal)", []))
    for arm, desc in CONTROL_ARMS:
        cells = []
        for c in cs:
            a = c["controls"].get(arm)
            sig = a and a.get("median_refusal_signal")
            cells.append("—" if not a else f"{sig:.2f} n={a['n']}" if sig is not None
                         else f"— n={a['n']}")
        out.append((desc, cells))
    return out


def render_text(s: dict[str, Any]) -> str:
    cs = s["campaigns"]
    heads = [c["name"] for c in cs]
    rows = _rows(s)
    lw = max(len(lbl) for lbl, cells in rows if cells) + 2
    cw = max([len(h) for h in heads] + [len(x) for _, cells in rows for x in cells]) + 2
    lines = [f"summary — {len(cs)} campaign(s) · instrument {s['instrument']} · solver "
             f"{s['solver']} · tiers {s['tiers']}",
             "  " + " " * lw + "".join(h.rjust(cw) for h in heads)]
    for label, cells in rows:
        if not cells:
            lines += ["", "  " + label.lstrip("# ")]
            continue
        lines.append("  " + label.ljust(lw) + "".join(x.rjust(cw) for x in cells))
    lines += ["", f"  {s['metric']}."]
    if s["null_control"] is not None:
        verdict = "ok" if s["null_control"] < 0.10 else "SUSPECT"
        lines.append(f"  null control: cross-family accuracy {s['null_control']:.3f} — {verdict}")
    for w in s["warnings"]:
        lines.append(f"  ! {w}")
    lines.append(f"  {s['note']}")
    return "\n".join(lines)


def render_markdown(s: dict[str, Any]) -> str:
    cs = s["campaigns"]
    out = [f"# Summary: {', '.join(c['name'] for c in cs)}", "",
           f"Instrument {s['instrument']}, solver {s['solver']}, tiers {s['tiers']}, "
           f"generated {s['generated_at']}. {s['note']}", "",
           "No prompt or response text is included; every number is an aggregate.", ""]
    if s["warnings"]:
        out += ["**Read first:**", ""] + [f"- {w}" for w in s["warnings"]] + [""]
    table: list[str] = []
    for label, cells in _rows(s):
        if not cells:
            if table:
                out += table + [""]
                table = []
            out += [f"#{label}", ""]
            table = ["| | " + " | ".join(c["name"] for c in cs) + " |",
                     "|---|" + "---|" * len(cs)]
            continue
        table.append(f"| {label} | " + " | ".join(cells) + " |")
    out += table + [""]
    out.append(f"{s['metric'][0].upper()}{s['metric'][1:]}. The median and its interval are the "
               "values to read; n counts pairs.")
    if s["null_control"] is not None:
        out.append(f"Null control (cross-family accuracy): {s['null_control']:.3f}.")
    return "\n".join(out) + "\n"


def write(s: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j, m = out_dir / "summary.json", out_dir / "SUMMARY.md"
    j.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    m.write_text(render_markdown(s), encoding="utf-8")
    return j, m
