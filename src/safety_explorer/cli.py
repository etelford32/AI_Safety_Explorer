"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import HUMAN_METRICS, __version__
from . import analysis, annotate, corpus as corpus_mod, db, ingest, lint, runner
from .providers import get_provider


def _corpus_and_lint(args) -> tuple:
    c = corpus_mod.load(Path(args.corpus))
    report = lint.run(c)
    return c, report


def _require_clean(c, report, force: bool) -> None:
    if report.clean:
        return
    print(lint.format_report(report, c), file=sys.stderr)
    if force:
        print("\n--force given: running against a corpus that does not lint clean.\n"
              "Results from this campaign are not comparable with clean ones.",
              file=sys.stderr)
        return
    print("\nRefusing to run: the corpus has errors. Fix them, or pass --force and "
          "accept that the results are not comparable.", file=sys.stderr)
    raise SystemExit(2)


# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    c, report = _corpus_and_lint(args)
    conn = db.init_db(args.db)
    runner.snapshot_corpus(conn, c, report.clean)
    print(f"initialised {args.db}")
    print(f"corpus {c.version} ({c.content_hash[:16]}) — "
          f"{len(c.all_variants)} variants, {len(c.runnable)} runnable")
    print("lint:", "clean" if report.clean else f"{len(report.errors)} error(s)")
    return 0


def cmd_lint(args) -> int:
    c, report = _corpus_and_lint(args)
    print(lint.format_report(report, c))
    return 0 if report.clean else 1


def cmd_corpus(args) -> int:
    c, _ = _corpus_and_lint(args)
    if args.show:
        v = c.by_id(args.show)
        if not v:
            print(f"no such variant '{args.show}'", file=sys.stderr)
            return 1
        print(f"{v.id}  [{v.arm}{'/' + v.control_arm if v.control_arm else ''}]  {v.title}")
        print(f"vector: {v.vector}")
        print(f"hazard: {v.hazard_review} — {v.hazard_rationale}")
        if v.baseline:
            print(f"twin baseline: {v.baseline}")
        if v.conversation_with:
            print(f"follows: {v.conversation_with}")
        print(f"hash: {v.prompt_hash[:16]}\n")
        print(v.text)
        return 0

    for fam in c.families:
        mark = "" if fam.is_active else "  (stub, excluded from runs)"
        print(f"\n{fam.id}  [{fam.domain}]  focal={fam.focal_dimension}{mark}")
        for v in fam.variants:
            vec = "".join(str(getattr(v, d)) for d in ("intent", "operationality",
                                                       "specificity", "autonomy", "depth"))
            print(f"    {v.variant}  {vec}  {v.title}")
    print("\ncontrols")
    for v in c.controls:
        vec = "".join(str(getattr(v, d)) for d in ("intent", "operationality",
                                                   "specificity", "autonomy", "depth"))
        print(f"    {v.id:32s} {vec}  [{v.control_arm}]  {v.title}")
    print(f"\n{len(c.all_variants)} variants, {len(c.runnable)} runnable, "
          f"corpus {c.version} ({c.content_hash[:16]})")
    return 0


def cmd_run(args) -> int:
    c, report = _corpus_and_lint(args)
    _require_clean(c, report, args.force)

    conn = db.init_db(args.db)
    runner.snapshot_corpus(conn, c, report.clean)

    provider = get_provider(
        args.provider, args.model,
        temperature=args.temperature, max_tokens=args.max_tokens,
        system=args.system,
    )

    existing = db.query_one(conn, "SELECT id FROM campaign WHERE name = ?", (args.campaign,))
    if existing and args.resume:
        campaign_id = existing["id"]
        print(f"resuming campaign '{args.campaign}' ({campaign_id})")
    elif existing:
        print(f"campaign '{args.campaign}' already exists; pass --resume to continue it",
              file=sys.stderr)
        return 2
    else:
        campaign_id = runner.create_campaign(
            conn, args.campaign, provider, c, args.repeats, args.notes
        )
        print(f"created campaign '{args.campaign}' ({campaign_id})")

    if provider.alias_risk():
        print(f"note: '{args.model}' looks like a moving alias. It may be repointed "
              f"server-side, which weakens any longitudinal comparison. Pin a version "
              f"id if you can.")

    def progress(i, total, variant, status):
        bar = f"[{i:>4}/{total}]"
        flag = "" if status == "ok" else f"  ERROR: {status[:60]}"
        print(f"{bar} {variant.id:<34s} {flag}", flush=True)

    stats = runner.execute(
        conn, campaign_id, c, provider, args.repeats,
        only=args.only, surface=args.surface, resume=args.resume,
        on_progress=progress,
    )
    print(f"\n{stats['ok']} ok, {stats['errors']} error(s), "
          f"{stats['skipped']} already present, {stats['total']} cells total")
    return 0


def cmd_capture(args) -> int:
    c, _ = _corpus_and_lint(args)
    conn = db.init_db(args.db)
    runner.snapshot_corpus(conn, c, lint.run(c).clean)

    variant = c.by_id(args.prompt)
    if not variant:
        print(f"no such prompt '{args.prompt}'", file=sys.stderr)
        return 1

    if args.response:
        response = Path(args.response).read_text() if Path(args.response).exists() else args.response
    else:
        print("=" * 72)
        print(variant.text)
        print("=" * 72)
        print("Paste the response, then Ctrl-D:\n")
        response = sys.stdin.read()

    if not response.strip():
        print("empty response, nothing recorded", file=sys.stderr)
        return 1

    run_id = ingest.capture(
        conn, c, variant.id, response, model_label=args.model,
        surface=args.surface, repeat_index=args.repeat, notes=args.notes,
    )
    print(f"recorded {run_id} as Tier B ({args.surface})")
    print("unobservable for this surface:", ", ".join(ingest.UNOBSERVABLE.get(args.surface, [])))
    return 0


def cmd_import(args) -> int:
    c, _ = _corpus_and_lint(args)
    conn = db.init_db(args.db)
    runner.snapshot_corpus(conn, c, lint.run(c).clean)
    stats = ingest.import_file(
        conn, c, Path(args.path), fmt=args.format, surface=args.surface,
        tier=args.tier, default_model=args.model,
    )
    print(f"{stats['rows']} rows: {stats['matched']} matched, {stats['unmatched']} unmatched")
    if stats["unmatched"]:
        print("unmatched rows are stored with prompt_id NULL and are listed by "
              "'explorer unmatched' — triage them rather than ignoring them.")
    return 0


def cmd_unmatched(args) -> int:
    conn = db.connect(args.db)
    rows = ingest.unmatched(conn)
    if not rows:
        print("no unmatched runs")
        return 0
    for r in rows:
        print(f"{r['id']}  conf={r['match_confidence']:.2f}  {r['model_id']}")
        print(f"    {(r['preview'] or '').strip()[:120]}")
    return 0


def cmd_features(args) -> int:
    conn = db.connect(args.db)
    n = runner.recompute_features(conn)
    print(f"recomputed features for {n} runs")
    return 0


def cmd_annotate(args) -> int:
    conn = db.connect(args.db)
    if args.progress:
        print(json.dumps(annotate.progress(conn, args.annotator), indent=2))
        return 0

    pass_index = 1 if args.reliability else 0
    ids = annotate.queue(
        conn, args.annotator, limit=args.limit, campaign_id=args.campaign,
        seed=args.seed, pass_index=pass_index, tiers=args.tiers,
    )
    if not ids:
        print("nothing to annotate")
        return 0

    print(f"{len(ids)} item(s) queued for '{args.annotator}' "
          f"({'blinded' if not args.unblind else 'UNBLINDED'}, pass {pass_index})")
    print("The blinded web UI is the intended interface: explorer serve\n")
    for rid in ids:
        print(rid)
    return 0


def cmd_analyse(args) -> int:
    conn = db.connect(args.db)
    c, _ = _corpus_and_lint(args)

    if args.what == "twins":
        deltas = analysis.twin_deltas(conn, c, args.campaign, args.tiers, args.metric)
        summary = analysis.summarise_deltas(deltas)
        print(f"twin-pair deltas for '{args.metric}' (tiers {args.tiers})\n")
        print(f"  {'variant':<9}{'n':>4}{'fams':>6}{'median':>9}{'mean':>8}"
              f"{'ci95':>18}{'effect':>12}")
        for row in summary:
            ci = row["ci95"]
            ci_s = f"[{ci[0]}, {ci[1]}]" if ci[0] is not None else "—"
            prov = " *" if row.get("provisional") else ""
            print(f"  {str(row['variant']):<9}{row['n']:>4}{row.get('n_families',0):>6}"
                  f"{str(row['median']):>9}{str(row['mean']):>8}{ci_s:>18}"
                  f"{str(row['effect']):>12}{prov}")
        print("\n  * provisional: fewer than 3 observations or fewer than 2 families")
        print("  mean is reported because it is legible; the median and Cliff's delta "
              "are the values to cite.")
        return 0

    if args.what == "surface":
        s = analysis.surface(conn, args.x, args.y, args.metric, args.campaign,
                             args.tiers, args.source)
        print(f"{args.metric} over {args.x} (x) x {args.y} (y), tiers {args.tiers}")
        print(f"coverage {s['sampled_cells']}/25 cells\n")
        for yi in range(4, -1, -1):
            cells = []
            for xi in range(5):
                cell = s["grid"][yi][xi]
                if not cell:
                    cells.append("   .  ")
                else:
                    mark = "?" if cell["provisional"] else " "
                    cells.append(f"{cell['value']:5.2f}{mark}")
            print(f"  {args.y[:4]}={yi} |" + "".join(cells))
        print("        " + "".join(f"{xi:>6}" for xi in range(5)))
        print(f"        {'':>6}{args.x}")
        print("\n  .  no observations — not interpolated")
        print("  ?  provisional, fewer than 3 observations")
        return 0

    if args.what == "depth":
        d = analysis.depth_interaction(conn, c, args.campaign, args.tiers,
                                       args.metric, args.source)
        print(f"depth arm — '{args.metric}' ({args.source}, tiers {args.tiers})")
        print("positive gap = the expert phrasing fared worse than the introductory one\n")
        for focal, block in d["by_focal_dimension"].items():
            print(f"  focal dimension: {focal}   "
                  f"({block['n_families']} famil{'y' if block['n_families'] == 1 else 'ies'}: "
                  f"{', '.join(block['families'])})")
            print(f"    {'level':<7}{'focal':>7}{'n':>5}{'median gap':>13}{'95% CI':>18}{'effect':>12}")
            for lv in block["levels"]:
                ci = lv.get("ci95", (None, None))
                ci_s = f"[{ci[0]}, {ci[1]}]" if ci and ci[0] is not None else "—"
                prov = " *" if lv.get("provisional") else ""
                print(f"    {lv['level']:<7}{str(lv.get('focal_value','—')):>7}{lv['n']:>5}"
                      f"{str(lv.get('median_gap')):>13}{ci_s:>18}"
                      f"{str(lv.get('effect')):>12}{prov}")
            did = block["difference_in_differences"]
            print(f"\n    difference-in-differences (depth gap at level, minus at C):")
            for lvl in ("D", "E"):
                e = did.get(lvl, {})
                if not e.get("n"):
                    continue
                ci = e.get("ci95", (None, None))
                ci_s = f"[{ci[0]}, {ci[1]}]" if ci[0] is not None else "—"
                print(f"      {e['contrast']:<10} n={e['n']:<4} median={str(e.get('median')):<8}"
                      f" CI {ci_s:<16} {e.get('effect','')}")
            print(f"\n    {did['reading']}")
            print()
        print("  * provisional: fewer than 3 observations, or fewer than 2 families.")
        print("    A CI of [nan, nan] means only one family contributes — the interval")
        print("    bootstraps over families, so it cannot be estimated from one.")
        print(f"  {d['note']}")
        return 0

    if args.what == "controls":
        print(json.dumps(analysis.control_report(conn, args.campaign, args.tiers), indent=2))
        return 0
    if args.what == "reliability":
        print(json.dumps(analysis.reliability(conn, args.annotator), indent=2))
        return 0
    if args.what == "judge":
        print(json.dumps(analysis.judge_agreement(conn, args.metric), indent=2))
        return 0
    if args.what == "drift":
        print(json.dumps(analysis.campaign_comparison(conn, args.metric, args.tiers), indent=2))
        return 0
    return 1


def cmd_serve(args) -> int:
    from .server import serve
    serve(args.db, args.corpus, host=args.host, port=args.port, annotator=args.annotator)
    return 0


def cmd_export(args) -> int:
    conn = db.connect(args.db)
    obs = analysis.observations(conn, args.campaign, args.tiers)
    # Escalated responses are excluded from exports by policy (CONTENT_POLICY.md).
    escalated = {
        r["run_id"] for r in db.query(conn, "SELECT run_id FROM annotation WHERE escalate = 1")
    }
    rows = [o for o in obs if o["run_id"] not in escalated]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"wrote {len(rows)} rows to {out}"
          + (f" ({len(escalated)} escalated row(s) withheld)" if escalated else ""))
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="explorer",
        description="Safety Explorer — measuring how model behaviour changes as context "
                    "framing becomes riskier while the reasoning task stays similar.",
    )
    p.add_argument("--version", action="version", version=f"safety-explorer {__version__}")
    p.add_argument("--db", default="data/explorer.db", help="database path")
    p.add_argument("--corpus", default="corpus", help="corpus directory")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and load the corpus").set_defaults(func=cmd_init)
    sub.add_parser("lint", help="validate the corpus").set_defaults(func=cmd_lint)

    c = sub.add_parser("corpus", help="list or show prompts")
    c.add_argument("--show", metavar="VARIANT_ID", help="print one variant in full")
    c.set_defaults(func=cmd_corpus)

    r = sub.add_parser("run", help="execute a campaign against a provider")
    r.add_argument("--campaign", required=True)
    r.add_argument("--provider", default="mock", help="mock | anthropic | openai | local")
    r.add_argument("--model", default="mock-1")
    r.add_argument("--repeats", type=int, default=3)
    r.add_argument("--only", nargs="*", help="variant ids, family ids, or 'control'")
    r.add_argument("--temperature", type=float, default=0.0)
    r.add_argument("--max-tokens", type=int, default=2048)
    r.add_argument("--system", default=None, help="system prompt (recorded with the run)")
    r.add_argument("--surface", default="api")
    r.add_argument("--notes", default="")
    r.add_argument("--resume", action="store_true", default=True)
    r.add_argument("--no-resume", dest="resume", action="store_false")
    r.add_argument("--force", action="store_true", help="run despite lint errors")
    r.set_defaults(func=cmd_run)

    cap = sub.add_parser("capture", help="record a response pasted from a chat surface (Tier B)")
    cap.add_argument("--prompt", required=True)
    cap.add_argument("--model", required=True, help="the model label as the surface displays it")
    cap.add_argument("--surface", default="web_chat", help="web_chat | app | cli | third_party")
    cap.add_argument("--response", default=None, help="text or a file path; omit to read stdin")
    cap.add_argument("--repeat", type=int, default=0)
    cap.add_argument("--notes", default="")
    cap.set_defaults(func=cmd_capture)

    imp = sub.add_parser("import", help="bulk-import transcripts (Tier C)")
    imp.add_argument("path")
    imp.add_argument("--format", default="jsonl", choices=["jsonl", "chatml"])
    imp.add_argument("--surface", default="api")
    imp.add_argument("--tier", default="C", choices=["A", "B", "C"])
    imp.add_argument("--model", default="unknown")
    imp.set_defaults(func=cmd_import)

    sub.add_parser("unmatched", help="list imported runs that matched no prompt").set_defaults(func=cmd_unmatched)
    sub.add_parser("features", help="recompute automatic features from stored responses").set_defaults(func=cmd_features)

    a = sub.add_parser("annotate", help="queue responses for human annotation")
    a.add_argument("--annotator", default="local")
    a.add_argument("--limit", type=int, default=60)
    a.add_argument("--campaign", default=None)
    a.add_argument("--seed", type=int, default=20260919)
    a.add_argument("--tiers", default="AB")
    a.add_argument("--reliability", action="store_true", help="re-serve a 20%% subset for intra-rater alpha")
    a.add_argument("--unblind", action="store_true", help="not the reference set; recorded as unblinded")
    a.add_argument("--progress", action="store_true")
    a.set_defaults(func=cmd_annotate)

    an = sub.add_parser("analyse", help="run an analysis")
    an.add_argument("what", choices=["twins", "surface", "depth", "controls",
                                     "reliability", "judge", "drift"])
    an.add_argument("--campaign", default=None)
    an.add_argument("--metric", default="capability_retention", choices=list(HUMAN_METRICS))
    an.add_argument("--tiers", default="A", help="provenance tiers to include, e.g. A or AB")
    an.add_argument("--x", default="intent")
    an.add_argument("--y", default="operationality")
    an.add_argument("--annotator", default=None)
    an.add_argument("--source", default="human", choices=["human", "auto"],
                    help="human annotation, or automatic features (needs no annotation)")
    an.set_defaults(func=cmd_analyse)

    s = sub.add_parser("serve", help="start the Explorer UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8713)
    s.add_argument("--annotator", default="local")
    s.set_defaults(func=cmd_serve)

    e = sub.add_parser("export", help="export observations as JSONL")
    e.add_argument("--out", default="data/exports/observations.jsonl")
    e.add_argument("--campaign", default=None)
    e.add_argument("--tiers", default="A")
    e.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
