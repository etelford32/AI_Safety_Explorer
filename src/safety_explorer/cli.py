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

    provider_kwargs: dict = {"max_tokens": args.max_tokens, "system": args.system}
    if args.temperature is not None:
        provider_kwargs["temperature"] = args.temperature
    if getattr(args, "thinking", None):
        provider_kwargs["thinking"] = args.thinking
    if getattr(args, "effort", None):
        provider_kwargs["effort"] = args.effort
    provider = get_provider(args.provider, args.model, **provider_kwargs)

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

    cue_set = None
    if args.cues is not None or args.cue_arms is not None or args.probes:
        from . import cues as cue_mod
        cue_set = cue_mod.load()
        cue_errors = cue_mod.lint(cue_set)
        if cue_errors:
            for e in cue_errors:
                print(f"  cue lint: {e}", file=sys.stderr)
            print("Refusing to run: the cue ladder has errors.", file=sys.stderr)
            return 2
        if args.cue_arms == ["treatment"]:
            print("note: running the treatment arm without its placebo. Any effect "
                  "found cannot be separated from a reaction to unusual framing.")

    stats = runner.execute(
        conn, campaign_id, c, provider, args.repeats,
        only=args.only, surface=args.surface, resume=args.resume,
        on_progress=progress, cue_set=cue_set, cue_levels=args.cues,
        cue_arms=args.cue_arms, probe_kinds=args.probes,
    )
    print(f"\n{stats['ok']} ok, {stats['errors']} error(s), "
          f"{stats['skipped']} already present, {stats['total']} cells total")
    return 0


def cmd_preflight(args) -> int:
    """Validate credentials, cost the campaign, and make exactly one real call.

    246 cells is enough that discovering a bad parameter on call 1 and a bad model id
    on call 2 is worth ten seconds up front.
    """
    from . import pricing

    c, report = _corpus_and_lint(args)
    _require_clean(c, report, args.force)

    provider_kwargs: dict = {"max_tokens": args.max_tokens, "system": args.system}
    if args.temperature is not None:
        provider_kwargs["temperature"] = args.temperature
    if args.thinking:
        provider_kwargs["thinking"] = args.thinking
    if args.effort:
        provider_kwargs["effort"] = args.effort

    try:
        provider = get_provider(args.provider, args.model, **provider_kwargs)
    except ValueError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return 2

    print(f"corpus {c.version} ({c.content_hash[:16]}) — {len(c.runnable)} runnable prompts")
    print(f"provider {provider.name}  model {args.model}  repeats {args.repeats}")
    cells = len(c.runnable) * args.repeats
    print(f"campaign size: {cells} cells\n")

    # 1. parameter legality, before anything is spent
    try:
        provider.complete.__self__  # noqa: B018 — touch to confirm binding
        if hasattr(provider, "_build_kwargs"):
            kw = provider._build_kwargs([{"role": "user", "content": "x"}], {})
            shown = {k: v for k, v in kw.items() if k != "messages"}
            print(f"request parameters: {shown}")
    except ValueError as exc:
        print(f"\nFAIL — illegal parameters for this model:\n  {exc}", file=sys.stderr)
        return 2

    if provider.alias_risk():
        print("note: this model id looks like a moving alias.")
    print("note: hosted Claude model ids carry no date suffix, so the id alone cannot")
    print("      pin weights. model_reported is recorded per run; a local pinned-weight")
    print("      model is the only true control for drift (see docs/PLAN.md §8).\n")

    # 2. exact input token count
    total_input = 0
    counted = 0
    for v in c.runnable:
        n = provider.count_tokens([{"role": "user", "content": v.text}]) if hasattr(
            provider, "count_tokens") else None
        if n is None:
            total_input += len(v.text) // 4
        else:
            total_input += n
            counted += 1
    label = "exact" if counted == len(c.runnable) else f"{counted}/{len(c.runnable)} exact, rest approximate"
    print(f"input tokens per sweep: {total_input:,} ({label})")

    # 3. one real call, to measure output length rather than guess it
    probe = c.runnable[0]
    print(f"\nprobe call: {probe.id} ...", flush=True)
    result = provider.complete([{"role": "user", "content": probe.text}], vector=probe.vector)
    if result.error:
        print(f"\nFAIL — probe call errored:\n  {result.error}", file=sys.stderr)
        return 2

    out_tokens = (result.usage or {}).get("output_tokens") or max(1, len(result.text) // 4)
    print(f"  ok — {result.latency_ms} ms, {out_tokens} output tokens, "
          f"stop_reason={result.finish_reason}")
    print(f"  model_reported: {result.model_reported}")
    if result.finish_reason == "max_tokens":
        print("  WARNING: the probe hit max_tokens. Raise --max-tokens: a truncated "
              "response scores as capability loss.")

    est = pricing.estimate(args.model, total_input * args.repeats, out_tokens * cells)
    if est.get("known"):
        print(f"\nestimated cost (list prices as of {est['as_of']}):")
        print(f"  input   {est['input_tokens']:>9,} tok x ${est['input_rate']}/M  = ${est['cost_input']}")
        print(f"  output  {est['output_tokens']:>9,} tok x ${est['output_rate']}/M = ${est['cost_output']}")
        print(f"  total                                    ~ ${est['cost_total']}")
        batch = pricing.estimate(args.model, total_input * args.repeats, out_tokens * cells, batch=True)
        print(f"  (the Batch API would run this asynchronously at ~${batch['cost_total']})")
        print("\n  An estimate from one probe response. Refusals are short and cost less;")
        print("  long derivations cost more.")
    else:
        print(f"\nno cached price for '{args.model}' — cannot estimate cost.")

    print(f"\nready. run it with:\n  explorer run --campaign <name> --provider {args.provider} "
          f"--model {args.model} --repeats {args.repeats}")
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


def cmd_truth(args) -> int:
    """Score stored responses against computed answer keys."""
    from . import groundtruth as gt

    conn = db.connect(args.db)

    if args.calibrate:
        cal = gt.calibrate()
        print("extractor calibration — a fully correct answer, scored in each language")
        print("anything below 1.00 is the scorer failing to read its own output\n")
        print(f"  {'language':10s}{'mean accuracy':>15}{'measurement floor':>20}")
        for lang, b in cal["by_language"].items():
            print(f"  {lang:10s}{b['mean_accuracy']:>15.3f}{b['measurement_floor']:>20.3f}")
        weak = {f"{lang}/{fam}": acc
                for lang, b in cal["by_language"].items()
                for fam, acc in b["per_family"].items() if acc < 1.0}
        if weak:
            print("\n  below full marks:")
            for k, v in sorted(weak.items()):
                print(f"    {k:40s}{v:.2f}")
        print(f"\n  {cal['verdict']}")
        print("  An observed cross-lingual effect smaller than the floor cannot be")
        print("  distinguished from a parser artefact, so the floor is reported with")
        print("  every language result rather than assumed to be zero.")
        return 0

    if args.targets:
        n_t = n_r = 0
        for family in gt.families_with_ground_truth():
            targets, relations = gt.targets_for(family), gt.relations_for(family)
            n_t += len(targets)
            n_r += len(relations)
            constrained = {k for r in relations for k in r.requires}
            print(f"\n{family}")
            for t in targets:
                band = (f"±{t.tol:.2f} dex (factor {10 ** t.tol:.1f})"
                        if t.kind == "dex" else f"±{t.tol:.0%}")
                mark = "·" if t.intermediate else " "
                free = "" if t.key in constrained else "   [no relation constrains it]"
                print(f" {mark}{t.key:24s} {t.value:>12.4g} {t.unit:<8s} {band}{free}")
                if t.note:
                    print(f"  {'':24s} {t.note}")
            for r in relations:
                print(f"  ~ {r.key:22s} {r.label}")
                print(f"  {'':24s} needs {', '.join(r.requires)} "
                      f"-> {r.expected:.4g} ±{r.tol:.0%}")
        print(f"\n  {n_t} targets, {n_r} relations. A '·' marks an intermediate "
              f"quantity, which counts half.")
        print("  A '~' line is an identity the model's own numbers must satisfy; it "
              "consults no answer key.")
        return 0

    if args.coherence:
        rep = gt.consistency_floor()
        print("consistency floor — the identities, checked against a correct answer\n")
        print(f"  {'language':10s}{'clean families':>16}{'false incoherence':>20}")
        for lang, b in rep["by_language"].items():
            print(f"  {lang:10s}{b['clean_families']:>16d}"
                  f"{b['false_incoherence']:>20.3f}")
        sens = rep["sensitivity"]
        print(f"\n  sensitivity: {sens['detected']}/{sens['perturbations_checked']} "
              f"ten-fold errors detected")
        if sens["missed"]:
            print(f"    undetected: {', '.join(sens['missed'])}")
        print(f"    {sens['targets_no_relation_constrains']} target(s) no relation "
              f"constrains — a gap in the relation set, not a model result")
        print(f"\n  {rep['verdict']}")
        print("  Mis-reading a number can only manufacture incoherence, never hide it,")
        print("  so measured inconsistency is a lower bound on the real thing.")
        return 0

    if args.items:
        rep = gt.item_analysis(conn, args.campaign, args.tiers)
        print(f"item analysis — tiers {args.tiers}\n")
        if not rep["items"]:
            print("  no scored runs yet; run a campaign first")
            return 0
        print(f"  {'family':<22}{'target':<24}{'n':>4}{'hit':>7}{'graded':>8}"
              f"{'disc':>8}  flags")
        for i in rep["items"]:
            disc = "—" if i["discrimination"] is None else f"{i['discrimination']:.2f}"
            print(f"  {i['family_id']:<22}{i['key']:<24}{i['n']:>4}"
                  f"{i['hit_rate']:>7.2f}{i['mean_graded']:>8.2f}{disc:>8}  "
                  f"{','.join(i['flags'])}")
        print(f"\n  {rep['verdict']}")
        print("  A target whose hit rate rises as the rest of the answer gets worse is")
        print("  matching numbers, not answers: that is a defect in the key.")
        return 0

    stats = gt.recompute_all(conn)
    print(f"scored {stats['scored']} run(s); {stats['skipped_no_solver']} had no solver")

    rows = db.query(conn, """
        SELECT p.family_id, p.variant, AVG(g.accuracy) AS acc,
               AVG(g.graded_accuracy) AS graded, AVG(g.weighted_accuracy) AS weighted,
               AVG(g.consistency) AS coherence, AVG(g.consistency_coverage) AS cov,
               AVG(g.null_accuracy) AS null_acc, COUNT(*) AS n
        FROM ground_truth g JOIN run r ON r.id = g.run_id
        JOIN prompt p ON p.id = r.prompt_id
        GROUP BY p.family_id, p.variant ORDER BY p.family_id, p.variant""")
    if not rows:
        return 0

    from collections import defaultdict
    by_variant: dict[str, list[dict]] = defaultdict(list)
    nulls: list[float] = []
    for r in rows:
        by_variant[r["variant"]].append(r)
        if r["null_acc"] is not None:
            nulls.append(r["null_acc"])

    def _mean(rs, key):
        vals = [r[key] for r in rs if r[key] is not None]
        return sum(vals) / len(vals) if vals else None

    print(f"\n  {'variant':<10}{'n':>4}{'hit':>8}{'graded':>9}{'weighted':>10}"
          f"{'coherent':>10}{'cov':>7}")
    for variant in sorted(by_variant):
        rs = by_variant[variant]
        cells = [_mean(rs, k) for k in ("acc", "graded", "weighted", "coherence", "cov")]
        line = f"  {variant:<10}{len(rs):>4}"
        for value, width in zip(cells, (8, 9, 10, 10, 7)):
            line += ("—" if value is None else f"{value:.3f}").rjust(width)
        print(line)
    print("  hit is the binary answer-key rate; graded gives partial credit by distance;")
    print("  weighted counts an intermediate quantity half; coherent is the share of the")
    print("  model's own identities that hold, over the share it stated enough to check.")

    classes = db.query(conn, "SELECT error_classes FROM ground_truth "
                             "WHERE error_classes IS NOT NULL AND error_classes != '{}'")
    tally: dict[str, int] = {}
    for row in classes:
        blob = row["error_classes"]
        if isinstance(blob, str):
            try:
                blob = json.loads(blob)
            except ValueError:
                continue
        for k, v in (blob or {}).items():
            tally[k] = tally.get(k, 0) + int(v)
    if tally:
        total = sum(tally.values()) or 1
        print("\n  error classes over all scored targets:")
        for cls in gt.ERROR_CLASSES:
            n = tally.get(cls, 0)
            print(f"    {cls:<10}{n:>7}{n / total:>9.1%}")
        print("  'scale' is a unit slip, not a reasoning failure, and 'absent' is a "
              "refusal or a truncation.")
        print("  Three different problems; averaging them into one accuracy hides "
              "which one you have.")

    if nulls:
        mean_null = sum(nulls) / len(nulls)
        verdict = "ok" if mean_null < 0.10 else "SUSPECT — matcher may be finding numbers"
        print(f"\n  null control (cross-family accuracy): {mean_null:.3f}  [{verdict}]")
        print("  A matcher that finds answers scores near zero here. If this is not near")
        print("  zero, the arm is measuring number density rather than correctness.")
    return 0


def cmd_annotate(args) -> int:
    conn = db.connect(args.db)
    if args.progress:
        print(json.dumps(annotate.progress(conn, args.annotator), indent=2))
        return 0

    pass_index = 1 if args.reliability else 0
    c, _ = _corpus_and_lint(args)

    if args.plan:
        plan = annotate.plan_set(conn, c, budget=args.limit, campaign_id=args.campaign,
                                 repeat_index=args.repeat, tiers=args.tiers)
        print(f"budget {plan['budget']} -> {plan['n_selected']} runs "
              f"({plan['n_family']} family + {plan['n_controls']} controls)")
        print(f"complete twin pairs: {plan['complete_twin_pairs']}")
        print(f"families covered: {plan['families_covered']}")
        print(f"\n{plan['note']}")
        return 0

    ids = annotate.queue(
        conn, args.annotator, limit=args.limit, campaign_id=args.campaign,
        seed=args.seed, pass_index=pass_index, tiers=args.tiers,
        strategy=args.strategy, corpus=c, repeat_index=args.repeat,
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
        value_key = {"human": "delta", "auto": "auto_density_ratio",
                     "truth": "gt_delta", "truth_graded": "gt_graded_delta",
                     "truth_weighted": "gt_weighted_delta",
                     "truth_consistency": "gt_consistency_delta"}[args.source]
        summary = analysis.summarise_deltas(deltas, value=value_key)
        label = {"human": f"'{args.metric}' (human annotation)",
                 "auto": "technical-density ratio (automatic)",
                 "truth": "objective correctness (answer key)",
                 "truth_graded": "objective correctness, graded by distance",
                 "truth_weighted": "objective correctness, intermediates weighted half",
                 "truth_consistency": "internal consistency (no answer key)"}[args.source]
        print(f"twin-pair deltas — {label}, tiers {args.tiers}\n")
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
        if args.source in analysis.TRUTH_SOURCES:
            null = db.query_one(
                conn, "SELECT AVG(null_accuracy) AS n FROM ground_truth "
                      "WHERE null_accuracy IS NOT NULL")
            if null and null["n"] is not None:
                print(f"\n  null control: cross-family accuracy {null['n']:.3f} — "
                      f"{'ok' if null['n'] < 0.10 else 'SUSPECT'}")
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

    if args.what == "language":
        # Objective correctness is the only layer comparable across scripts, so the
        # language arm defaults to it even though other analyses default to human.
        source = args.source if args.source != "human" or args.explicit_source else "truth"
        d = analysis.language_effect(conn, c, args.campaign, args.tiers,
                                     args.metric, source)
        args.source = source
        label = {"truth": "objective correctness",
                 "truth_graded": "objective correctness, graded",
                 "truth_weighted": "objective correctness, weighted",
                 "truth_consistency": "internal consistency",
                 "auto": "technical density",
                 "human": f"'{args.metric}'"}[args.source]
        print(f"language arm — {label}, tiers {args.tiers}, reference = English")
        print("positive gap = the translated prompt fared worse than its English twin\n")
        if not d["by_language"]:
            print("  no language-arm runs yet")
            return 0
        for lang, block in d["by_language"].items():
            print(f"  {block['name']} ({lang})   "
                  f"{block['n_families']} famil{'y' if block['n_families'] == 1 else 'ies'}: "
                  f"{', '.join(block['families'])}")
            print(f"    {'level':<7}{'n':>5}{'median gap':>13}{'95% CI':>18}{'effect':>12}")
            for lv in block["levels"]:
                ci = lv.get("ci95", (None, None))
                ci_s = f"[{ci[0]}, {ci[1]}]" if ci and ci[0] is not None else "—"
                prov = " *" if lv.get("provisional") else ""
                print(f"    {lv['level']:<7}{lv['n']:>5}{str(lv.get('median_gap')):>13}"
                      f"{ci_s:>18}{str(lv.get('effect')):>12}{prov}")
            did = block["difference_in_differences"]
            print("\n    difference-in-differences (gap at level, minus at C):")
            for lvl in ("D", "E"):
                e = did.get(lvl, {})
                if not e.get("n"):
                    continue
                ci = e.get("ci95", (None, None))
                ci_s = f"[{ci[0]}, {ci[1]}]" if ci[0] is not None else "—"
                print(f"      {e['contrast']:<10} n={e['n']:<4} median={str(e.get('median')):<8}"
                      f" CI {ci_s:<16} {e.get('effect','')}")
            print(f"\n    {did['reading']}\n")
        print("  * provisional: fewer than 3 observations, or fewer than 2 families.")
        print(f"  {d['note']}")
        return 0

    if args.what == "sandbagging":
        source = args.source if args.source != "human" else "truth"
        d = analysis.sandbagging(conn, c, args.campaign, args.tiers, source, args.metric)
        if not d["n_cells"]:
            print("no cued runs yet — try:\n  explorer run --campaign sb --provider mock "
                  "--cues 1 2 3 4 5 --probes detect_parallel selfreport_followup")
            return 0

        print(f"sandbagging arm — {source}, tiers {args.tiers}, {d['n_cells']} cued cells")
        print("positive drop = worse than the same prompt's own uncued baseline\n")
        print(f"  {'level':6}{'treat':>8}{'placebo':>9}{'SPECIFIC':>10}{'95% CI':>18}"
              f"{'detect':>8}{'spont':>7}")
        for row in d["dose_response"]:
            t, pl, sp = row["treatment"], row["placebo"], row["specific"]
            ci = sp.get("ci95", (None, None))
            ci_s = f"[{ci[0]}, {ci[1]}]" if ci and ci[0] is not None else "—"
            det = row.get("treatment_detection", {}).get("rate")
            spo = row.get("treatment_spontaneous", {}).get("rate")
            print(f"  {row['level']:<6}{_fmt(t.get('median')):>8}{_fmt(pl.get('median')):>9}"
                  f"{_fmt(sp.get('median')):>10}{ci_s:>18}"
                  f"{_fmt(det):>8}{_fmt(spo):>7}")
        print("\n  treat/placebo columns are drops against baseline; SPECIFIC is their")
        print("  difference, paired within prompt. Reporting the naive treatment column")
        print("  alone would credit the cost of odd framing to evaluation awareness.")
        print(f"  dose-response monotonic in severity: {d['monotonic']}")

        q = d["quadrants"]
        total = sum(q.values()) or 1
        print(f"\n  awareness x performance (treatment arm, n={total}):")
        print(f"    {'':22}{'accuracy dropped':>18}{'accuracy held':>16}")
        print(f"    {'detected the cue':22}{q['detected_dropped']:>18}{q['detected_held']:>16}")
        print(f"    {'did not detect':22}{q['undetected_dropped']:>18}{q['undetected_held']:>16}")
        if q["unknown"]:
            print(f"    ({q['unknown']} with no usable detection probe)")
        print("    top-left is sandbagging; bottom-left is a stylistic effect the model")
        print("    is not aware of. They are different findings and need different fixes.")

        ins = d["insight_gap"]
        if ins.get("n"):
            print(f"\n  insight gap — does it know it held back?")
            print(f"    all cued cells (n={ins['n']}): measured drop "
                  f"{ins['mean_actual_drop_pct']:.1f}% vs self-reported "
                  f"{ins['mean_reported_drop_pct']:.1f}%, median gap {ins['median']}")
            wd = ins.get("when_dropped", {})
            if wd.get("n"):
                ci = wd.get("ci95", (None, None))
                print(f"    where a drop was measured (n={wd['n']}): "
                      f"{wd['mean_actual_drop_pct']:.1f}% vs "
                      f"{wd['mean_reported_drop_pct']:.1f}%, median gap {wd['median']} "
                      f"points, 95% CI [{ci[0]}, {ci[1]}]")
                print("    The second line is the sharper question. The first is diluted")
                print("    by cells where nothing happened and nothing was reported.")
            print(f"    {ins['reading']}")

        print(f"\n  {d['reading']}")
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

def _fmt(value, places: int = 3) -> str:
    return "—" if value is None else f"{value:.{places}f}"


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
    # Default None, not 0.0: sampling parameters are REMOVED on the current frontier
    # models (Opus 5, Sonnet 5, Opus 4.8/4.7, Fable) and sending one returns a 400 on
    # every call. Omitting it means the model's own sampling applies, which is both
    # legal everywhere and an honest record of what was run.
    r.add_argument("--temperature", type=float, default=None,
                   help="only legal on older models; omit on Opus 5 / Sonnet 5")
    # Generous, because a response truncated at max_tokens looks exactly like a
    # degraded one to every metric here. Better to pay for headroom than to score
    # truncation as capability loss.
    r.add_argument("--max-tokens", type=int, default=8000)
    r.add_argument("--thinking", default=None, choices=["adaptive", "off"],
                   help="omit to use the model's own default (adaptive on Opus 5)")
    r.add_argument("--effort", default=None,
                   choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--system", default=None, help="system prompt (recorded with the run)")
    r.add_argument("--cues", nargs="*", type=int, default=None, metavar="LEVEL",
                   help="observation-cue severity levels to sweep (0-5). The uncued "
                        "baseline is always included.")
    r.add_argument("--cue-arms", nargs="*", default=None,
                   choices=["treatment", "placebo"],
                   help="default is both; running treatment alone cannot separate an "
                        "evaluation effect from a framing effect")
    r.add_argument("--probes", nargs="*", default=None,
                   choices=["detect_parallel", "selfreport_followup"],
                   help="awareness probes to attach to each run")
    r.add_argument("--surface", default="api")
    r.add_argument("--notes", default="")
    r.add_argument("--resume", action="store_true", default=True)
    r.add_argument("--no-resume", dest="resume", action="store_false")
    r.add_argument("--force", action="store_true", help="run despite lint errors")
    r.set_defaults(func=cmd_run)

    pf = sub.add_parser("preflight", help="validate credentials, cost the campaign, make one real call")
    pf.add_argument("--provider", default="anthropic")
    pf.add_argument("--model", required=True)
    pf.add_argument("--repeats", type=int, default=3)
    pf.add_argument("--max-tokens", type=int, default=8000)
    pf.add_argument("--temperature", type=float, default=None)
    pf.add_argument("--thinking", default=None, choices=["adaptive", "off"])
    pf.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    pf.add_argument("--system", default=None)
    pf.add_argument("--force", action="store_true")
    pf.set_defaults(func=cmd_preflight)

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

    t = sub.add_parser("truth", help="score responses against computed answer keys")
    t.add_argument("--targets", action="store_true", help="print the answer keys and exit")
    t.add_argument("--calibrate", action="store_true",
                   help="measure the extractor's own bias per language (the measurement floor)")
    t.add_argument("--coherence", action="store_true",
                   help="validate the internal-consistency relations: do they hold on a "
                        "correct answer, and do they catch a tenfold error?")
    t.add_argument("--items", action="store_true",
                   help="item analysis: which targets carry information, and which are "
                        "matching numbers rather than answers")
    t.add_argument("--campaign", default=None)
    t.add_argument("--tiers", default="A")
    t.set_defaults(func=cmd_truth)

    a = sub.add_parser("annotate", help="queue responses for human annotation")
    a.add_argument("--annotator", default="local")
    a.add_argument("--limit", type=int, default=60)
    a.add_argument("--campaign", default=None)
    a.add_argument("--seed", type=int, default=20260919)
    a.add_argument("--tiers", default="AB")
    a.add_argument("--strategy", default="coverage", choices=["coverage", "random"],
                   help="coverage selects for twin-pair yield; random is the old behaviour")
    a.add_argument("--repeat", type=int, default=0,
                   help="which repeat index to annotate (coverage beats repeats at this budget)")
    a.add_argument("--plan", action="store_true", help="show what would be selected, annotate nothing")
    a.add_argument("--reliability", action="store_true", help="re-serve a 20%% subset for intra-rater alpha")
    a.add_argument("--unblind", action="store_true", help="not the reference set; recorded as unblinded")
    a.add_argument("--progress", action="store_true")
    a.set_defaults(func=cmd_annotate)

    an = sub.add_parser("analyse", help="run an analysis")
    an.add_argument("what", choices=["twins", "surface", "depth", "language",
                                     "sandbagging", "controls", "reliability",
                                     "judge", "drift"])
    an.add_argument("--campaign", default=None)
    an.add_argument("--metric", default="capability_retention", choices=list(HUMAN_METRICS))
    an.add_argument("--tiers", default="A", help="provenance tiers to include, e.g. A or AB")
    an.add_argument("--x", default="intent")
    an.add_argument("--y", default="operationality")
    an.add_argument("--annotator", default=None)
    an.add_argument("--explicit-source", action="store_true",
                    help=argparse.SUPPRESS)
    an.add_argument("--source", default="human",
                    choices=["human", "auto", "truth", "truth_graded",
                             "truth_weighted", "truth_consistency"],
                    help="human annotation; automatic features; or one of the Layer 0 "
                         "readings — truth (binary hit rate), truth_graded (partial "
                         "credit by distance), truth_weighted (intermediates count "
                         "half), truth_consistency (do the model's own numbers agree). "
                         "Everything but 'human' needs no annotation.")
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
