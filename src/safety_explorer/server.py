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
from . import analysis, annotate, corpus as corpus_mod, db, lint, metrics

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

        if path == "/api/depth":
            return analysis.depth_interaction(
                self.conn, self.corpus, q.get("campaign_id") or None,
                q.get("tiers", "A"), q.get("metric", "capability_retention"),
                q.get("source", "human"),
            )

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
