"""Explorer UI server.

Standard library only, no build step. That is a deliberate choice for a research
instrument: the data is meant to outlive the code, and a tool that still starts in
five years beats one that needs a toolchain resurrected first.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import DIMENSION_LABELS, DIMENSIONS, HUMAN_METRICS, INVERTED_METRICS, __version__
from . import analysis, annotate, corpus as corpus_mod, db, ingest, jobs, lint, metrics, pricing

WEB_ROOT = Path(__file__).parent / "web"
CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}


class ExplorerHandler(BaseHTTPRequestHandler):
    server_version = f"SafetyExplorer/{__version__}"

    # Quieter log: one line per request, no HTML noise.
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"  {self.command} {self.path.split('?')[0]}")

    # -- plumbing ----------------------------------------------------------

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, name: str) -> None:
        path = (WEB_ROOT / name).resolve()
        if not path.is_file() or WEB_ROOT.resolve() not in path.parents:
            self._send_json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    @property
    def conn(self) -> sqlite3.Connection:
        return self.server.db_conn  # type: ignore[attr-defined]

    @property
    def db_path(self) -> str:
        return self.server.db_path  # type: ignore[attr-defined]

    @property
    def corpus(self):
        return self.server.corpus  # type: ignore[attr-defined]

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self._send_file("index.html")
            if url.path in ("/app.js", "/style.css"):
                return self._send_file(url.path.lstrip("/"))
            if url.path.startswith("/api/"):
                return self._send_json(self._api_get(url.path, q))
            self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        try:
            body = self._body()
            if url.path == "/api/run/start":
                return self._send_json(self._start_run(body))
            if url.path == "/api/run/cancel":
                return self._send_json({"cancelled": jobs.RUNNER.cancel()})
            if url.path == "/api/capture":
                return self._send_json(self._capture(body))
            if url.path == "/api/import":
                return self._send_json(self._import(body))
            if url.path == "/api/unmatched/assign":
                return self._send_json(self._assign_unmatched(body))
            if url.path == "/api/features/recompute":
                from .runner import recompute_features
                n = recompute_features(self.conn)
                return self._send_json({"ok": True, "recomputed": n})
            if url.path == "/api/annotate":
                aid = annotate.submit(
                    self.conn,
                    run_id=body["run_id"],
                    annotator=body.get("annotator", "local"),
                    scores={m: body.get("scores", {}).get(m) for m in HUMAN_METRICS},
                    refusal_label=body.get("refusal_label"),
                    blinded=bool(body.get("blinded", True)),
                    pass_index=int(body.get("pass_index", 0)),
                    notes=body.get("notes", ""),
                    escalate=bool(body.get("escalate", False)),
                    seconds_spent=body.get("seconds_spent"),
                    revealed=bool(body.get("revealed", False)),
                )
                return self._send_json({"ok": True, "annotation_id": aid})
            self._send_json({"error": "not found"}, 404)
        except (KeyError, ValueError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # -- API ---------------------------------------------------------------

    def _api_get(self, path: str, q: dict[str, str]) -> Any:
        if path == "/api/meta":
            report = lint.run(self.corpus)
            return {
                "version": __version__,
                "corpus_version": self.corpus.version,
                "corpus_hash": self.corpus.content_hash[:16],
                "lint_clean": report.clean,
                "lint_errors": [str(f) for f in report.errors],
                "dimensions": DIMENSIONS,
                "dimension_labels": DIMENSION_LABELS,
                # Per-level anchor text, so the slider always says what the value means.
                "anchors": {
                    d: (self.corpus.dimensions.get(d) or {}).get("anchors", [])
                    for d in DIMENSIONS
                },
                "metrics": HUMAN_METRICS,
                "inverted_metrics": sorted(INVERTED_METRICS),
                "rubric": annotate.RUBRIC,
                "refusal_labels": list(annotate.REFUSAL_LABELS),
                "campaigns": db.query(self.conn, "SELECT * FROM campaign ORDER BY created_at DESC"),
                "families": [
                    {"id": f.id, "name": f.name, "domain": f.domain,
                     "focal_dimension": f.focal_dimension, "status": f.status,
                     "reasoning_core": f.reasoning_core, "substrate_rule": f.substrate_rule,
                     "variants": [
                         {"id": v.id, "variant": v.variant, "title": v.title,
                          "vector": v.vector, "baseline": v.baseline,
                          "status": v.status}
                         for v in f.variants
                     ]}
                    for f in self.corpus.families
                ],
                "controls": [
                    {"id": v.id, "title": v.title, "arm": v.control_arm,
                     "vector": v.vector, "expected_benign": v.expected_benign}
                    for v in self.corpus.controls
                ],
            }

        if path == "/api/select":
            # Nearest authored variant to a point in the design space. The sliders
            # select; they never synthesise, because a generated prompt would have no
            # twin and no lint guarantee (docs/DATA_INGESTION.md).
            target = {d: int(q.get(d, 0)) for d in DIMENSIONS}
            best, best_dist = None, None
            for v in self.corpus.runnable:
                dist = sum((v.vector[d] - target[d]) ** 2 for d in DIMENSIONS)
                if best_dist is None or dist < best_dist:
                    best, best_dist = v, dist
            if best is None:
                return {"error": "no runnable variants"}
            return {"variant": self._variant_payload(best), "distance": best_dist,
                    "exact": best_dist == 0}

        if path == "/api/variant":
            v = self.corpus.by_id(q.get("id", ""))
            if not v:
                return {"error": "not found"}
            return {"variant": self._variant_payload(v)}

        if path == "/api/runs":
            sql = """SELECT r.id, r.prompt_id, r.repeat_index, r.model_id, r.provenance_tier,
                            r.lane, r.surface, r.captured_at, r.latency_ms, r.error,
                            r.campaign_id, c.name AS campaign_name
                     FROM run r LEFT JOIN campaign c ON c.id = r.campaign_id WHERE 1=1"""
            params: list[Any] = []
            if q.get("prompt_id"):
                sql += " AND r.prompt_id = ?"
                params.append(q["prompt_id"])
            if q.get("campaign_id"):
                sql += " AND r.campaign_id = ?"
                params.append(q["campaign_id"])
            sql += " ORDER BY r.captured_at DESC LIMIT 200"
            return {"runs": db.query(self.conn, sql, params)}

        if path == "/api/run":
            row = db.query_one(
                self.conn,
                """SELECT r.*, p.title, p.variant, p.family_id, p.arm, p.control_arm,
                          p.intent, p.operationality, p.specificity, p.autonomy, p.depth
                   FROM run r LEFT JOIN prompt p ON p.id = r.prompt_id WHERE r.id = ?""",
                (q.get("id", ""),),
            )
            if not row:
                return {"error": "not found"}
            row["messages"] = db.loads(row.get("messages"), [])
            row["usage"] = db.loads(row.get("usage"), {})
            row["params"] = db.loads(row.get("params"), {})
            row["unobservable"] = db.loads(row.get("unobservable"), [])
            feat = db.query_one(self.conn, "SELECT * FROM feature WHERE run_id = ?", (row["id"],))
            if feat:
                feat["extra"] = db.loads(feat.get("extra"), {})
            row["features"] = feat
            row["annotations"] = db.query(
                self.conn, "SELECT * FROM annotation WHERE run_id = ? ORDER BY pass_index",
                (row["id"],),
            )
            return {"run": row}

        if path == "/api/compare":
            return self._compare(q)

        if path == "/api/annotate/next":
            annotator = q.get("annotator", "local")
            blind = q.get("blind", "1") != "0"
            pass_index = int(q.get("pass_index", 0))
            ids = annotate.queue(
                self.conn, annotator, limit=1,
                campaign_id=q.get("campaign_id") or None,
                seed=int(q.get("seed", 20260919)),
                pass_index=pass_index, tiers=q.get("tiers", "AB"),
            )
            if not ids:
                return {"done": True, "progress": annotate.progress(self.conn, annotator)}
            annotate.log_serve(self.conn, ids[0], annotator, pass_index,
                               int(q.get("seed", 20260919)))
            item = annotate.item(self.conn, ids[0], blinded=blind)
            item["progress"] = annotate.progress(self.conn, annotator)
            item["done"] = False
            return item

        if path == "/api/annotate/reveal":
            return annotate.item(self.conn, q["run_id"], blinded=False)

        if path == "/api/surface":
            return analysis.surface(
                self.conn, q.get("x", "intent"), q.get("y", "operationality"),
                q.get("metric", "capability_retention"),
                q.get("campaign_id") or None, q.get("tiers", "A"),
                q.get("source", "human"),
            )

        if path == "/api/twins":
            deltas = analysis.twin_deltas(
                self.conn, self.corpus, q.get("campaign_id") or None,
                q.get("tiers", "A"), q.get("metric", "capability_retention"),
            )
            return {
                "summary": analysis.summarise_deltas(deltas),
                "auto_summary": analysis.summarise_deltas(deltas, value="auto_density_ratio"),
                "n": len(deltas),
            }

        if path == "/api/run/status":
            return jobs.RUNNER.status()

        if path == "/api/preflight":
            return self._preflight(q)

        if path == "/api/capture/queue":
            return self._capture_queue(q)

        if path == "/api/unmatched":
            return {"runs": ingest.unmatched(self.conn)}

        if path == "/api/export":
            rows = analysis.observations(
                self.conn, q.get("campaign_id") or None, q.get("tiers", "A"))
            escalated = {
                r["run_id"] for r in db.query(
                    self.conn, "SELECT run_id FROM annotation WHERE escalate = 1")
            }
            kept = [r for r in rows if r["run_id"] not in escalated]
            return {"rows": kept, "n": len(kept), "withheld": len(escalated),
                    "note": "Responses flagged `escalate` are withheld (CONTENT_POLICY.md)."}

        if path == "/api/depth":
            return analysis.depth_interaction(
                self.conn, self.corpus, q.get("campaign_id") or None,
                q.get("tiers", "A"), q.get("metric", "capability_retention"),
                q.get("source", "human"),
            )

        if path == "/api/truth":
            from . import groundtruth as gt

            rows = db.query(self.conn, """
                SELECT p.family_id, p.variant, g.accuracy, g.null_accuracy,
                       g.targets_hit, g.targets_total, g.n_candidates
                FROM ground_truth g JOIN run r ON r.id = g.run_id
                JOIN prompt p ON p.id = r.prompt_id
                WHERE r.error IS NULL""")
            by_variant: dict[str, list[float]] = {}
            nulls: list[float] = []
            for r in rows:
                if r["accuracy"] is not None:
                    by_variant.setdefault(r["variant"], []).append(r["accuracy"])
                if r["null_accuracy"] is not None:
                    nulls.append(r["null_accuracy"])
            mean_null = round(sum(nulls) / len(nulls), 4) if nulls else None
            return {
                "scored": len(rows),
                "by_variant": [
                    {"variant": k, "n": len(v), "accuracy": round(sum(v) / len(v), 4)}
                    for k, v in sorted(by_variant.items())
                ],
                "null_accuracy": mean_null,
                "null_ok": (mean_null is not None and mean_null < 0.10),
                "families": gt.families_with_ground_truth(),
                "keys": {
                    f: [{"key": t.key, "label": t.label, "value": t.value,
                         "unit": t.unit, "tol": t.tol, "kind": t.kind, "note": t.note}
                        for t in gt.targets_for(f)]
                    for f in gt.families_with_ground_truth()
                },
            }

        if path == "/api/controls":
            return analysis.control_report(self.conn, q.get("campaign_id") or None,
                                           q.get("tiers", "A"))
        if path == "/api/reliability":
            return analysis.reliability(self.conn, q.get("annotator") or None)
        if path == "/api/drift":
            return analysis.campaign_comparison(self.conn, q.get("metric", "capability_retention"),
                                                q.get("tiers", "A"))
        if path == "/api/progress":
            return annotate.progress(self.conn, q.get("annotator") or None)

        return {"error": "unknown endpoint"}

    # -- campaign control --------------------------------------------------

    def _provider_kwargs(self, body: dict[str, Any]) -> dict[str, Any]:
        kw: dict[str, Any] = {"max_tokens": int(body.get("max_tokens") or 8000)}
        if body.get("system"):
            kw["system"] = body["system"]
        if body.get("thinking"):
            kw["thinking"] = body["thinking"]
        if body.get("effort"):
            kw["effort"] = body["effort"]
        temp = body.get("temperature")
        if temp not in (None, ""):
            kw["temperature"] = float(temp)
        return kw

    def _preflight(self, q: dict[str, str]) -> dict[str, Any]:
        """Cost and validate before spending. Makes no model call — that is the CLI's job.

        The browser gets the cheap half: parameter legality plus an estimate from the
        corpus's own token counts. Actually probing the API is left to
        `explorer preflight`, so that opening a page can never spend money.
        """
        from .providers import get_provider

        model = q.get("model", "")
        repeats = int(q.get("repeats", 3))
        try:
            provider = get_provider(q.get("provider", "anthropic"), model,
                                    **self._provider_kwargs(dict(q)))
        except (ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}

        try:
            if hasattr(provider, "_build_kwargs"):
                kwargs = provider._build_kwargs([{"role": "user", "content": "x"}], {})
                params = {k: v for k, v in kwargs.items() if k != "messages"}
            else:
                params = provider.describe()
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "stage": "parameters"}

        runnable = self.corpus.runnable
        approx_in = sum(len(v.text) // 4 for v in runnable) * repeats
        cells = len(runnable) * repeats
        est = pricing.estimate(model, approx_in, 700 * cells)
        batch = pricing.estimate(model, approx_in, 700 * cells, batch=True)
        return {
            "ok": True, "params": params, "cells": cells,
            "prompts": len(runnable), "repeats": repeats,
            "alias_risk": provider.alias_risk(),
            "estimate": est, "batch_estimate": batch,
            "note": ("Estimate only — assumes ~700 output tokens per response. "
                     "Run `explorer preflight` for an exact count plus a real probe call."),
        }

    def _start_run(self, body: dict[str, Any]) -> dict[str, Any]:
        from .providers import get_provider
        from .runner import create_campaign, execute, snapshot_corpus

        if jobs.RUNNER.busy():
            return {"ok": False, "error": "a job is already running"}

        name = (body.get("campaign") or "").strip()
        if not name:
            return {"ok": False, "error": "campaign name is required"}

        report = lint.run(self.corpus)
        if not report.clean and not body.get("force"):
            return {"ok": False, "error": "corpus does not lint clean",
                    "lint_errors": [str(f) for f in report.errors]}

        try:
            provider = get_provider(body.get("provider", "mock"),
                                    body.get("model", "mock-1"),
                                    **self._provider_kwargs(body))
        except (ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}

        repeats = int(body.get("repeats") or 3)
        only = body.get("only") or None
        corpus = self.corpus
        db_path = self.db_path

        def work(job: jobs.Job) -> dict[str, Any]:
            # The worker gets its own connection: SQLite handles concurrent readers
            # under WAL, but sharing one connection across threads invites lock
            # contention exactly when a long write loop is running.
            conn = db.connect(db_path)
            try:
                snapshot_corpus(conn, corpus, report.clean)
                existing = db.query_one(conn, "SELECT id FROM campaign WHERE name = ?", (name,))
                if existing:
                    campaign_id = existing["id"]
                    job.note(f"resuming campaign '{name}'")
                else:
                    campaign_id = create_campaign(conn, name, provider, corpus, repeats,
                                                  body.get("notes", ""))
                    job.note(f"created campaign '{name}'")

                job.total = len(corpus.runnable) * repeats

                def progress(i: int, total: int, variant, status: str) -> None:
                    job.done = i
                    job.total = max(job.total, total)
                    job.current = variant.id
                    if status == "ok":
                        job.ok += 1
                    else:
                        job.errors += 1
                        job.note(f"{variant.id}: {status[:120]}")

                stats = execute(conn, campaign_id, corpus, provider, repeats,
                                only=only, surface=body.get("surface", "api"),
                                resume=True, on_progress=progress,
                                should_stop=lambda: job.cancelled)
                job.skipped = stats.get("skipped", 0)
                job.note(f"finished: {stats['ok']} ok, {stats['errors']} error(s), "
                         f"{stats['skipped']} already present")
                return {"campaign_id": campaign_id, **stats}
            finally:
                conn.close()

        job = jobs.RUNNER.start("campaign", name, work)
        return {"ok": True, "job": job.snapshot()}

    # -- manual capture (Lane 2) ------------------------------------------

    def _capture_queue(self, q: dict[str, str]) -> dict[str, Any]:
        """Prompts still uncaptured for a given model label and surface.

        Keyed on the model label rather than a campaign, because the point of this lane
        is a surface we cannot drive programmatically: the unit of work is "get this
        model, on this surface, through the corpus".
        """
        model = q.get("model", "")
        surface = q.get("surface", "web_chat")
        done = {
            r["prompt_id"] for r in db.query(
                self.conn,
                "SELECT DISTINCT prompt_id FROM run WHERE lane = 'manual' "
                "AND model_id = ? AND surface = ?", (model, surface))
        }
        pending = [v for v in self.corpus.runnable if v.id not in done]
        nxt = pending[0] if pending else None
        return {
            "model": model, "surface": surface,
            "captured": len(done), "total": len(self.corpus.runnable),
            "remaining": len(pending),
            "next": self._variant_payload(nxt) if nxt else None,
            "unobservable": ingest.UNOBSERVABLE.get(surface, ingest.MANUAL_UNOBSERVABLE),
        }

    def _capture(self, body: dict[str, Any]) -> dict[str, Any]:
        prompt_id = body.get("prompt_id")
        response = (body.get("response") or "").strip()
        model = (body.get("model") or "").strip()
        if not prompt_id or not response or not model:
            return {"ok": False, "error": "prompt_id, response and model are all required"}
        try:
            run_id = ingest.capture(
                self.conn, self.corpus, prompt_id, response,
                model_label=model, surface=body.get("surface", "web_chat"),
                repeat_index=int(body.get("repeat_index") or 0),
                notes=body.get("notes", ""),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "run_id": run_id,
                "queue": self._capture_queue({"model": model,
                                              "surface": body.get("surface", "web_chat")})}

    # -- bulk import (Lane 3) ---------------------------------------------

    def _import(self, body: dict[str, Any]) -> dict[str, Any]:
        import tempfile
        from pathlib import Path as _Path

        text = body.get("text") or ""
        if not text.strip():
            return {"ok": False, "error": "no content"}
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(text)
            tmp = _Path(fh.name)
        try:
            stats = ingest.import_file(
                self.conn, self.corpus, tmp,
                fmt=body.get("format", "jsonl"),
                surface=body.get("surface", "api"),
                tier=body.get("tier", "C"),
                default_model=body.get("model", "unknown"),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            tmp.unlink(missing_ok=True)
        return {"ok": True, **stats}

    def _assign_unmatched(self, body: dict[str, Any]) -> dict[str, Any]:
        """Triage an imported run that matched no prompt.

        Assigning by hand records `match_confidence = 0.0` rather than 1.0: a human
        said these go together, which is a different and weaker claim than a hash match,
        and the analysis should be able to tell them apart.
        """
        run_id, prompt_id = body.get("run_id"), body.get("prompt_id")
        if not run_id:
            return {"ok": False, "error": "run_id is required"}
        if body.get("discard"):
            self.conn.execute("DELETE FROM run WHERE id = ?", (run_id,))
            self.conn.commit()
            return {"ok": True, "discarded": run_id}
        if not self.corpus.by_id(prompt_id or ""):
            return {"ok": False, "error": f"unknown prompt '{prompt_id}'"}
        self.conn.execute(
            "UPDATE run SET prompt_id = ?, match_confidence = 0.0 WHERE id = ?",
            (prompt_id, run_id))
        self.conn.commit()
        return {"ok": True, "run_id": run_id, "prompt_id": prompt_id,
                "note": "recorded as a manual match (confidence 0.0)"}

    def _variant_payload(self, v) -> dict[str, Any]:
        runs = db.query(
            self.conn,
            """SELECT r.id, r.repeat_index, r.model_id, r.provenance_tier, r.captured_at,
                      c.name AS campaign_name
               FROM run r LEFT JOIN campaign c ON c.id = r.campaign_id
               WHERE r.prompt_id = ? ORDER BY r.captured_at DESC""",
            (v.id,),
        )
        fam = self.corpus.family(v.family_id) if v.family_id else None
        return {
            "id": v.id, "variant": v.variant, "title": v.title, "text": v.text,
            "vector": v.vector, "arm": v.arm, "control_arm": v.control_arm,
            "family_id": v.family_id, "family_name": fam.name if fam else None,
            "focal_dimension": fam.focal_dimension if fam else None,
            "baseline": v.baseline, "conversation_with": v.conversation_with,
            "output_format": v.output_format, "hazard_review": v.hazard_review,
            "hazard_rationale": v.hazard_rationale,
            "expected_benign": v.expected_benign, "status": v.status,
            "prompt_hash": v.prompt_hash[:16], "runs": runs,
        }

    def _compare(self, q: dict[str, str]) -> dict[str, Any]:
        """Baseline-vs-test comparison with a word-level diff of what disappeared."""
        test_id = q.get("test_run")
        base_id = q.get("baseline_run")

        test = db.query_one(self.conn, "SELECT * FROM run WHERE id = ?", (test_id,))
        if not test:
            return {"error": "test run not found"}

        if not base_id:
            # Resolve the twin baseline automatically: same repeat, same campaign.
            variant = self.corpus.by_id(test["prompt_id"])
            if variant and variant.baseline:
                row = db.query_one(
                    self.conn,
                    "SELECT * FROM run WHERE prompt_id = ? AND repeat_index = ? "
                    "AND campaign_id IS ? ORDER BY captured_at DESC LIMIT 1",
                    (variant.baseline, test["repeat_index"], test["campaign_id"]),
                )
                base = row
            else:
                base = None
        else:
            base = db.query_one(self.conn, "SELECT * FROM run WHERE id = ?", (base_id,))

        tf = db.query_one(self.conn, "SELECT * FROM feature WHERE run_id = ?", (test["id"],)) or {}
        out: dict[str, Any] = {
            "test": {
                "run_id": test["id"], "prompt_id": test["prompt_id"],
                "response": test["response"], "features": tf,
                "model_id": test["model_id"], "tier": test["provenance_tier"],
            },
            "baseline": None, "retention": None, "diff": None,
            "scores": self._score_table(test["id"], base["id"] if base else None),
        }
        if base:
            bf = db.query_one(self.conn, "SELECT * FROM feature WHERE run_id = ?", (base["id"],)) or {}
            out["baseline"] = {
                "run_id": base["id"], "prompt_id": base["prompt_id"],
                "response": base["response"], "features": bf,
                "model_id": base["model_id"], "tier": base["provenance_tier"],
            }
            out["retention"] = metrics.retention(tf, bf)
            out["diff"] = metrics.word_diff(base["response"] or "", test["response"] or "")
        return out

    def _score_table(self, test_run: str, base_run: str | None) -> dict[str, Any]:
        def mean_scores(run_id: str | None) -> dict[str, Any]:
            if not run_id:
                return {}
            rows = db.query(self.conn, "SELECT * FROM annotation WHERE run_id = ? AND pass_index = 0",
                            (run_id,))
            out: dict[str, Any] = {}
            for m in HUMAN_METRICS:
                vals = [r[m] for r in rows if r[m] is not None]
                out[m] = round(sum(vals) / len(vals), 2) if vals else None
            return out
        return {"baseline": mean_scores(base_run), "test": mean_scores(test_run)}


def serve(db_path: str, corpus_path: str, host: str = "127.0.0.1",
          port: int = 8713, annotator: str = "local") -> None:
    c = corpus_mod.load(Path(corpus_path))
    report = lint.run(c)
    conn = db.init_db(db_path)
    from .runner import snapshot_corpus
    snapshot_corpus(conn, c, report.clean)

    httpd = ThreadingHTTPServer((host, port), ExplorerHandler)
    # One connection, shared: the UI is single-user by design and SQLite handles the
    # rest. check_same_thread is off because ThreadingHTTPServer dispatches per request.
    httpd.db_conn = sqlite3.connect(db_path, check_same_thread=False)  # type: ignore[attr-defined]
    httpd.db_conn.row_factory = sqlite3.Row  # type: ignore[attr-defined]
    httpd.db_conn.execute("PRAGMA foreign_keys = ON")  # type: ignore[attr-defined]
    httpd.db_path = str(db_path)  # type: ignore[attr-defined]
    httpd.corpus = c  # type: ignore[attr-defined]
    httpd.annotator = annotator  # type: ignore[attr-defined]

    n_runs = db.query_one(conn, "SELECT COUNT(*) AS n FROM run")["n"]
    print(f"SAFETY EXPLORER {__version__}")
    print(f"  corpus {c.version} ({c.content_hash[:16]})  lint "
          f"{'clean' if report.clean else str(len(report.errors)) + ' error(s)'}")
    print(f"  {len(c.runnable)} runnable prompts, {n_runs} stored runs")
    print(f"  http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
