"""Explorer UI server.

Standard library only, no build step. That is a deliberate choice for a research
instrument: the data is meant to outlive the code, and a tool that still starts in
five years beats one that needs a toolchain resurrected first.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import (DIMENSION_LABELS, DIMENSIONS, HUMAN_METRICS, INVERTED_METRICS,
               SPAN_LABELS, __version__)
from . import analysis, annotate, corpus as corpus_mod, db, ingest, jobs, lint, metrics, pricing

WEB_ROOT = Path(__file__).parent / "web"

#: When `serve()` started, so `/api/status` can report uptime. Set once, module level, so
#: a background menu-bar host that imports and runs the server in-process can read it too.
SERVE_STARTED: float | None = None
CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml"}
STATIC_FILES = ("/app.js", "/shell.js", "/overview.js", "/style.css", "/favicon.svg")


class DemoState:
    """The demo seeder and live simulator, one per server process.

    Seeding runs on its own thread and connection so the request that starts it returns at
    once and the UI can poll the log; the simulator writes turns the way an agent would.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seeding = False
        self.log: list[str] = []
        self.error: str | None = None
        self.simulator = None

    def status(self) -> dict[str, Any]:
        return {"seeding": self.seeding, "log": self.log[-12:], "error": self.error,
                "simulator": self.simulator.status() if self.simulator else {"running": False}}


DEMO = DemoState()


def _json_safe(value):
    """Recursively replace non-finite floats with None so the result is valid JSON.

    NaN and +/-Infinity are what json.dumps emits for a bootstrap CI that could not be
    estimated; they are not valid JSON and the browser's JSON.parse rejects them. A
    missing estimate should read as null, not take a whole view down.
    """
    import math

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


#: Posture cut points, cached until the run table changes. See `posture_cuts`.
_CUTS_LOCK = threading.Lock()
_CUTS: dict[str, Any] = {"key": None, "cuts": None}


def posture_cuts(conn):
    """The population posture cut points the Live and Sessions views classify against.

    They are a pure function of the stored responses, but deriving them means re-extracting
    stance from every run — about ten seconds at a few thousand runs. The Live and Sessions
    endpoints used to do that on every request, which made opening a session or pasting a
    conversation take ten-plus seconds and made an auto-refreshing Sessions view unusable.
    Runs are append-only, so the cuts are cached against the run count, the latest capture
    time and the lexicon version, and recomputed only when one of those moves. The lock is
    held across the computation so concurrent requests wait for one pass instead of each
    starting their own.
    """
    from . import stance as st

    row = db.query_one(conn, "SELECT COUNT(*) AS n, MAX(captured_at) AS last FROM run")
    key = (row["n"], row["last"], st.STANCE_VERSION)
    with _CUTS_LOCK:
        if _CUTS["key"] != key:
            _CUTS["cuts"] = st.calibrate(st.attach(analysis.observations(conn, tiers="A")))
            _CUTS["key"] = key
        return _CUTS["cuts"]


def status_report(conn, started: float | None = None, limit: int = 25) -> dict[str, Any]:
    """A cheap health-and-activity summary for a background host to poll.

    This is what turns the server into something a menu-bar app can sit on top of: it says
    the tool is alive, how much has flowed through it, and — the part worth a glance — which
    live sessions are drifting right now. The drift status per session is the same one the
    Sessions list computes, so the badge in the menu bar and the badge in the UI agree.

    Kept deliberately light: counts are one query each, and the per-session drift is capped
    at `limit` so a poll every few seconds stays cheap however long the tool has been up.
    """
    from . import sessions, stance as st

    n_runs = db.query_one(conn, "SELECT COUNT(*) AS n FROM run")["n"]
    n_sessions = db.query_one(conn, "SELECT COUNT(*) AS n FROM live_session")["n"] \
        if _table_exists(conn, "live_session") else 0
    n_turns = db.query_one(conn, "SELECT COUNT(*) AS n FROM live_turn")["n"] \
        if _table_exists(conn, "live_turn") else 0

    watching = []
    if n_sessions:
        for s in sessions.list_sessions(conn, limit=limit, with_drift=True):
            watching.append({"id": s["id"], "label": s["label"], "source": s["source"],
                             "n_turns": s.get("n_turns") or 0, "drift": s.get("drift"),
                             "updated_at": s["updated_at"]})
    alerts = [w for w in watching if w["drift"] == "alert"]
    watch = [w for w in watching if w["drift"] == "watch"]

    # The embedding backend's trust is what decides whether the register readings behind any
    # alert are semantic or lexical. Report it so the menu bar can warn when it is the
    # fallback. register_model() is cached, so this is cheap after the first call.
    model = st.register_model()
    from . import dashboard
    return {
        "ok": True,
        "version": __version__,
        "data_version": dashboard.data_version(conn),
        "uptime_s": round(time.time() - started, 1) if started else None,
        "n_runs": n_runs,
        "n_sessions": n_sessions,
        "n_turns": n_turns,
        "n_alert": len(alerts),
        "n_watch": len(watch),
        "sessions": watching,
        "embedding_backend": model.backend.name if model else None,
        "embedding_trustworthy": bool(model and st.model_trustworthy(model)),
    }


def _table_exists(conn, name: str) -> bool:
    row = db.query_one(
        conn, "SELECT 1 AS x FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return row is not None


class ExplorerHandler(BaseHTTPRequestHandler):
    server_version = f"SafetyExplorer/{__version__}"
    #: Set per request by `_gate` when an allowed cross-origin source is talking to us.
    _cors: str | None = None
    _verdict: str = "none"

    def _gate(self) -> bool:
        """Refuse a request from a page that is not a capture source (see `access`)."""
        from . import access

        path = urlparse(self.path).path
        bound = getattr(self.server, "bound_host", "127.0.0.1")
        if not access.host_ok(self.headers.get("Host"), bound):
            self._send_json({"error": "unexpected Host header — the Explorer answers only to "
                                      "a loopback name"}, 403)
            return False
        origin = self.headers.get("Origin")
        verdict = access.origin_verdict(origin, self.headers.get("Host"), path)
        if verdict == "denied":
            self._send_json({"error": f"requests from {origin} are not accepted here. Capture "
                                      "sources may reach only /api/session/turn, "
                                      "/api/session/paste and /api/status; add an origin with "
                                      "EXPLORER_ALLOWED_ORIGINS"}, 403)
            return False
        self._verdict = verdict
        self._cors = origin if verdict in ("capture", "extension") else None
        return True

    def _cors_headers(self) -> None:
        if self._cors:
            self.send_header("Access-Control-Allow-Origin", self._cors)
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self) -> None:  # noqa: N802
        """The CORS preflight a browser sends before a cross-origin JSON POST."""
        if not self._gate():
            return
        if not self._cors:
            self._send_json({"error": "no cross-origin access here"}, 403)
            return
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        # Chrome's Private Network Access asks before a public page reaches loopback.
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # Quieter log: one line per request, no HTML noise.
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"  {self.command} {self.path.split('?')[0]}")

    # -- plumbing ----------------------------------------------------------

    def _send_json(self, payload: Any, status: int = 200) -> None:
        # `allow_nan=False` rejects the NaN/Infinity that json.dumps emits by default —
        # those are invalid JSON and JSON.parse throws on them in the browser, which
        # silently broke any panel whose bootstrap CI came back nan (a reliability
        # estimate with one group, a twin delta with no pairs). `_json_safe` maps every
        # non-finite float to null first, so a "no estimate" reads as null on the page
        # rather than crashing the whole view.
        body = json.dumps(_json_safe(payload), default=str, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
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

    def _send_userscript(self) -> None:
        """The capture userscript, pointed at this server.

        A userscript manager offers to install any URL ending in `.user.js`, so this is the
        one-click install. The default server line is rewritten to the address the browser
        used to reach us, so a server on another port installs a script that finds it.
        """
        from . import access

        text = (WEB_ROOT / "explorer-capture.user.js").read_text()
        host = self.headers.get("Host") or "127.0.0.1:8713"
        if access.is_loopback(host):
            text = text.replace("const DEFAULT_SERVER = 'http://127.0.0.1:8713';",
                                f"const DEFAULT_SERVER = 'http://{host}';")
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
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
        if not self._gate():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/explorer-capture.user.js":
                return self._send_userscript()
            if url.path in ("/", "/index.html"):
                return self._send_file("index.html")
            if url.path in STATIC_FILES:
                return self._send_file(url.path.lstrip("/"))
            if url.path == "/favicon.ico":
                return self._send_file("favicon.svg")
            if url.path.startswith("/api/"):
                return self._send_json(self._api_get(url.path, q))
            self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._gate():
            return
        url = urlparse(self.path)
        try:
            body = self._body()
            if url.path == "/api/demo/seed":
                return self._send_json(self._demo_seed(body))
            if url.path == "/api/demo/stream":
                return self._send_json(self._demo_stream(body))
            if url.path == "/api/demo/clear":
                from . import demo
                with DEMO.lock:
                    if DEMO.simulator:
                        DEMO.simulator.stop()
                return self._send_json({"removed": demo.clear(self.conn)})
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
            if url.path == "/api/propose":
                from . import coanalyse
                from .providers import get_provider
                provider = get_provider(body.get("provider", "mock"),
                                        body.get("model", "mock-1"))
                return self._send_json(coanalyse.propose(
                    self.conn, body["run_id"], provider,
                    show_evidence=bool(body.get("show_evidence", True))))
            if url.path == "/api/rating":
                from . import coanalyse
                rid = annotate.submit(
                    self.conn,
                    run_id=body["run_id"],
                    annotator=body.get("annotator", "local"),
                    scores={m: body.get("scores", {}).get(m) for m in HUMAN_METRICS},
                    blinded=bool(body.get("blinded", True)),
                    citations=body.get("citations") or {},
                    notes=body.get("notes", ""),
                )
                return self._send_json({"ok": True, "id": rid,
                                        "coverage": coanalyse.coverage(self.conn)})
            if url.path == "/api/span_label":
                from . import coanalyse
                label_id = coanalyse.record(
                    self.conn,
                    run_id=body["run_id"],
                    span_index=int(body["span_index"]),
                    span_hash=body["span_hash"],
                    label=body["label"],
                    source=body.get("source", "human"),
                    author=body.get("author", "local"),
                    confidence=body.get("confidence"),
                    rationale=body.get("rationale", ""),
                    quote=body.get("quote", ""),
                    # Blind by default. An unblinded label is a deliberate act and has to
                    # be asked for, because the default silently decides whether the
                    # agreement figure measures the model or measures anchoring.
                    blinded=bool(body.get("blinded", True)),
                )
                return self._send_json({"ok": True, "id": label_id})
            if url.path == "/api/session/turn":
                from . import sessions
                sid = body.get("session_id")
                if not sid:
                    return self._send_json({"error": "session_id required"}, 400)
                if not body.get("role"):
                    return self._send_json({"error": "role required"}, 400)
                try:
                    res = sessions.append_turn(
                        self.conn, sid, body["role"], body.get("text") or "",
                        turn_index=body.get("turn_index"),
                        label=body.get("label", ""), source=body.get("source", "unknown"),
                        tier=body.get("tier", "B"), language=body.get("language", "en"),
                        meta=body.get("meta"))
                except sessions.TurnConflict as exc:
                    return self._send_json({"error": str(exc), "conflict": True,
                                            "expected": exc.expected}, 409)
                except sessions.TurnGap as exc:
                    return self._send_json({"error": str(exc), "gap": True,
                                            "expected": exc.expected}, 409)
                return self._send_json({"ok": True, **res})

            if url.path == "/api/session/paste":
                from . import sessions
                res = sessions.append_paste(
                    self.conn, body.get("text") or "", session_id=body.get("session_id"),
                    label=body.get("label", ""), source=body.get("source", "unknown"),
                    tier=body.get("tier", "B"), language=body.get("language", "en"),
                    meta=body.get("meta"))
                return self._send_json({"ok": True, **res})

            if url.path == "/api/session/open":
                from . import sessions
                sid = sessions.open_session(
                    self.conn, label=body.get("label", ""),
                    source=body.get("source", "unknown"), tier=body.get("tier", "B"),
                    language=body.get("language", "en"), meta=body.get("meta"))
                return self._send_json({"ok": True, "session_id": sid})

            if url.path == "/api/live":
                from . import live

                # Posture cuts come from whatever campaign is in this database. Without
                # one there are no cuts and every turn comes back `unclassified`, which
                # the result says plainly rather than leaving a blank column.
                return self._send_json(live.analyse(
                    body.get("text") or "", self.corpus,
                    posture_cuts(self.conn), body.get("language") or "en"))

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

    # -- demo --------------------------------------------------------------

    def _demo_seed(self, body: dict[str, Any]) -> dict[str, Any]:
        from . import demo
        with DEMO.lock:
            if DEMO.seeding:
                return {"started": False, **DEMO.status()}
            DEMO.seeding, DEMO.error, DEMO.log = True, None, []
        db_path, corpus = self.server.db_path, self.corpus  # type: ignore[attr-defined]

        def work() -> None:
            try:
                wconn = db.connect(db_path)
                demo.seed(wconn, corpus, repeats=int(body.get("repeats", 3)),
                          cued=bool(body.get("cued", True)), on_progress=DEMO.log.append)
                DEMO.log.append("done")
            except Exception as exc:  # noqa: BLE001 — reported to the UI, not swallowed
                DEMO.error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            finally:
                DEMO.seeding = False
        threading.Thread(target=work, name="demo-seed", daemon=True).start()
        return {"started": True, **DEMO.status()}

    def _demo_stream(self, body: dict[str, Any]) -> dict[str, Any]:
        from . import demo
        with DEMO.lock:
            if body.get("on", True):
                if DEMO.simulator is None or not DEMO.simulator.running:
                    DEMO.simulator = demo.Simulator(
                        self.server.db_path,  # type: ignore[attr-defined]
                        interval=float(body.get("interval", 2.5)))
                    DEMO.simulator.start()
            elif DEMO.simulator:
                DEMO.simulator.stop()
        return DEMO.status()

    # -- API ---------------------------------------------------------------

    def _api_get(self, path: str, q: dict[str, str]) -> Any:
        if path == "/api/status":
            if self._cors:
                # A capture source learns that the Explorer is up and how the session it is
                # feeding reads — never the list of every other conversation stored here.
                from . import sessions
                sid = q.get("session")
                return {"ok": True, "version": __version__,
                        "session": ({"id": sid, "drift": sessions.session_drift(self.conn, sid)}
                                    if sid else None)}
            return status_report(self.conn, SERVE_STARTED)

        if path == "/api/overview":
            from . import dashboard
            return dashboard.overview(self.conn, self.corpus)

        if path == "/api/activity":
            from . import dashboard
            return {"events": dashboard.activity(self.conn, int(q.get("limit", 40))),
                    "data_version": dashboard.data_version(self.conn)}

        if path == "/api/demo":
            return DEMO.status()

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
                "rubric": annotate.rubric_payload(),
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
            # The list is capped for the page; the total is counted separately so nothing
            # that shows "N runs" mistakes the length of a capped list for the size of the data.
            count_sql = "SELECT COUNT(*) AS n FROM (" + sql + ")"
            total = db.query_one(self.conn, count_sql, params)["n"]
            sql += " ORDER BY r.captured_at DESC LIMIT 200"
            return {"runs": db.query(self.conn, sql, params), "total": total}

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

        if path == "/api/sandbagging":
            from . import cues as cue_mod

            result = analysis.sandbagging(
                self.conn, self.corpus, q.get("campaign_id") or None,
                q.get("tiers", "A"), q.get("source", "truth"),
                q.get("metric", "capability_retention"))
            cue_set = cue_mod.load()
            result["ladder"] = [
                {"id": c.id, "level": c.level, "arm": c.arm, "label": c.label,
                 "text": c.text, "rationale": c.rationale}
                for c in cue_set.cues
            ]
            result["ladder_clean"] = not cue_mod.lint(cue_set)
            return result

        if path == "/api/language":
            from . import groundtruth as gt

            result = analysis.language_effect(
                self.conn, self.corpus, q.get("campaign_id") or None,
                q.get("tiers", "A"), q.get("metric", "capability_retention"),
                q.get("source", "truth"))
            result["calibration"] = gt.calibrate()
            return result

        if path == "/api/truth":
            from . import groundtruth as gt

            rows = db.query(self.conn, """
                SELECT p.family_id, p.variant, g.accuracy, g.null_accuracy,
                       g.graded_accuracy, g.weighted_accuracy, g.consistency,
                       g.consistency_coverage, g.error_classes,
                       g.targets_hit, g.targets_total, g.n_candidates
                FROM ground_truth g JOIN run r ON r.id = g.run_id
                JOIN prompt p ON p.id = r.prompt_id
                WHERE r.error IS NULL""")
            covers = db.query(self.conn, """
                SELECT p.id, p.family_id, p.variant, p.answer_key
                FROM prompt p WHERE p.answer_key != 'full' AND p.language = 'en'
                ORDER BY p.family_id, p.variant""")
            layers = ("accuracy", "graded_accuracy", "weighted_accuracy",
                      "consistency", "consistency_coverage")
            by_variant: dict[str, dict[str, list[float]]] = {}
            nulls: list[float] = []
            classes: dict[str, int] = {c: 0 for c in gt.ERROR_CLASSES}
            for r in rows:
                slot = by_variant.setdefault(r["variant"], {k: [] for k in layers})
                for layer in layers:
                    if r[layer] is not None:
                        slot[layer].append(r[layer])
                if r["null_accuracy"] is not None:
                    nulls.append(r["null_accuracy"])
                blob = r["error_classes"]
                if isinstance(blob, str):
                    try:
                        blob = json.loads(blob)
                    except ValueError:
                        blob = None
                for k, v in (blob or {}).items():
                    classes[k] = classes.get(k, 0) + int(v)
            mean_null = round(sum(nulls) / len(nulls), 4) if nulls else None

            def _mean(values):
                return round(sum(values) / len(values), 4) if values else None

            return {
                "scored": len(rows),
                "by_variant": [
                    {"variant": k, "n": len(v["accuracy"]),
                     **{layer: _mean(v[layer]) for layer in layers}}
                    for k, v in sorted(by_variant.items())
                ],
                "error_classes": classes,
                "error_class_order": list(gt.ERROR_CLASSES),
                "null_accuracy": mean_null,
                "null_ok": (mean_null is not None and mean_null < 0.10),
                "families": gt.families_with_ground_truth(),
                "keys": {
                    f: [{"key": t.key, "label": t.label, "value": t.value,
                         "unit": t.unit, "tol": t.tol, "kind": t.kind, "note": t.note,
                         "weight": t.weight, "intermediate": t.intermediate}
                        for t in gt.targets_for(f)]
                    for f in gt.families_with_ground_truth()
                },
                "relations": {
                    f: [{"key": r.key, "label": r.label, "requires": list(r.requires),
                         "expected": r.expected, "tol": r.tol, "note": r.note}
                        for r in gt.relations_for(f)]
                    for f in gt.families_with_ground_truth()
                },
                "coverage": [
                    {"id": r["id"], "family_id": r["family_id"], "variant": r["variant"],
                     "n_targets": (0 if r["answer_key"] == "none"
                                   else len(r["answer_key"].split(","))) }
                    for r in covers
                ],
                "coherence_floor": gt.consistency_floor(),
                "items": gt.item_analysis(self.conn, q.get("campaign_id") or None,
                                          q.get("tiers", "A")),
            }

        if path == "/api/conversations":
            rows = db.query(self.conn, """
                SELECT r.id AS run_id, p.id AS prompt_id, p.family_id, p.variant,
                       p.title, p.language, p.sub_arm, r.repeat_index,
                       r.model_id, r.provenance_tier,
                       r.cue_id, r.cue_arm, r.cue_level, r.finish_reason,
                       LENGTH(r.response) AS n_chars,
                       (SELECT COUNT(*) FROM span_label s
                        WHERE s.run_id = r.id AND s.source = 'human') AS n_human,
                       (SELECT COUNT(*) FROM span_label s
                        WHERE s.run_id = r.id AND s.source = 'model') AS n_model,
                       (SELECT COUNT(*) FROM probe pr WHERE pr.run_id = r.id) AS n_probes
                FROM run r JOIN prompt p ON p.id = r.prompt_id
                WHERE r.response IS NOT NULL AND r.error IS NULL
                ORDER BY p.family_id, p.variant, r.repeat_index
                LIMIT 400""")
            return {"conversations": rows, "labels": list(SPAN_LABELS)}

        if path == "/api/conversation":
            from . import coanalyse, conversation as conv
            run_id = q.get("run_id")
            if not run_id:
                return {"error": "run_id required"}
            convo = conv.assemble(self.conn, run_id)
            if convo is None:
                return {"error": f"no run {run_id}"}
            convo["labels"] = coanalyse.labels_for(self.conn, run_id)
            convo["rubric"] = annotate.rubric_payload()
            convo["ratings"] = {
                "human": db.query(self.conn,
                                  "SELECT * FROM annotation WHERE run_id = ? "
                                  "AND pass_index = 0", (run_id,)),
                "model": db.query(self.conn,
                                  "SELECT * FROM judgement WHERE run_id = ? "
                                  "ORDER BY created_at DESC LIMIT 1", (run_id,)),
            }
            convo["vocabulary"] = list(SPAN_LABELS)
            return convo

        if path == "/api/coanalysis":
            from . import coanalyse
            from . import rubric as rubric_mod
            return {
                "coverage": coanalyse.coverage(self.conn),
                "agreement": coanalyse.agreement(self.conn),
                "usable_alpha": coanalyse.USABLE_ALPHA,
                # Shown while rating, not after: a dead anchor found at the end of a
                # session is a session rated on a scale that was quietly narrower than
                # it looked.
                "rubric": rubric_mod.usage(self.conn),
            }

        if path == "/api/stance":
            return analysis.stance_report(self.conn, self.corpus,
                                          q.get("campaign_id") or None,
                                          q.get("tiers", "A"))

        if path == "/api/powerseeking":
            return analysis.powerseeking_report(self.conn, self.corpus,
                                                q.get("campaign_id") or None,
                                                q.get("tiers", "A"))

        if path == "/api/stance/trajectory":
            from . import stance as st

            run_id = q.get("run_id")
            if not run_id:
                return {"error": "run_id required"}
            row = db.query_one(
                self.conn,
                "SELECT r.response, p.family_id, p.language, g.relation_details "
                "FROM run r JOIN prompt p ON p.id = r.prompt_id "
                "LEFT JOIN ground_truth g ON g.run_id = r.id WHERE r.id = ?",
                (run_id,))
            if row is None:
                return {"error": f"no run {run_id}"}
            traj = st.trajectory(row["response"], row["family_id"], row["language"])
            traj["dimensions"] = list(st.DIMENSIONS)
            # The turn point is reported per dimension: a response can hold its warmth
            # and turn on refusal, or the reverse, and one summary index would hide it.
            traj["turns"] = {d: st.turn_point(traj, d)
                             for d in (*st.DIMENSIONS, "refusal_rate")}
            return traj

        if path == "/api/sessions":
            from . import sessions
            return {"sessions": sessions.list_sessions(self.conn)}

        if path == "/api/session":
            from . import sessions
            sid = q.get("id")
            if not sid:
                return {"error": "id required"}
            report = sessions.analyse_session(self.conn, sid, self.corpus,
                                              posture_cuts(self.conn))
            return report if report is not None else {"error": f"no session {sid}"}

        if path == "/api/stance/insight":
            from .providers import mock

            out = analysis.stance_insight(self.conn, q.get("campaign_id") or None,
                                          q.get("tiers", "A"))
            # The configured value travels with the measurement so the page can show
            # what was asked for beside what came back, rather than a bare number.
            out["configured"] = mock.load_stance_model().get("self_report", {})
            return out

        if path == "/api/stance/drift":
            from . import stance as st

            rows = st.attach(analysis.observations(
                self.conn, q.get("campaign_id") or None, q.get("tiers", "A")))
            return st.drift(rows)

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
    global SERVE_STARTED
    SERVE_STARTED = time.time()
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
    httpd.bound_host = host  # type: ignore[attr-defined]
    httpd.corpus = c  # type: ignore[attr-defined]
    httpd.annotator = annotator  # type: ignore[attr-defined]

    # Warm the caches on a connection of our own, so the first view opened does not pay the
    # full pass over every stored response on the request path: the posture cut points (which
    # also fills the stance-extraction cache the Stance view reads), then the power-seeking
    # probe and embedding readings the Results view reads.
    def _warm() -> None:
        try:
            wconn = db.connect(db_path)
            posture_cuts(wconn)
            from . import powerseeking as ps
            ps.attach(analysis.observations(wconn, tiers="A"))
        except Exception:  # noqa: BLE001 — a warm-up failure just means the first request pays
            pass
    threading.Thread(target=_warm, daemon=True).start()

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
