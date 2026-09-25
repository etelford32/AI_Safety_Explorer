"""The server under the load the Overview puts on it: many analyses requested at once.

The HTTP server answers each request on its own thread. Until v0.30.1 every thread shared
one SQLite connection, which Python's sqlite3 module does not make safe for concurrent use:
two threads stepping statements on it at once fail with "bad parameter or other API misuse"
(or, worse, read each other's rows). It went unnoticed while the UI asked for one analysis
at a time; the Overview asks for a dozen in parallel, and on a Mac it failed on first load.
Each request now gets its own connection. This test is the Overview's first load, repeated.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from safety_explorer import db, runner, sessions
from safety_explorer.providers import get_provider

ROOT = Path(__file__).resolve().parents[1]

OVERVIEW_BURST = [
    "/api/status", "/api/overview", "/api/demo", "/api/activity?limit=30",
    "/api/sandbagging?source=truth&tiers=A",
    "/api/depth?metric=capability_retention&source=truth_graded&tiers=A",
    "/api/reliability", "/api/stance?tiers=A", "/api/truth", "/api/controls?tiers=A",
    "/api/powerseeking?tiers=A", "/api/language?source=truth&tiers=A", "/api/runs",
    "/api/sessions", "/api/drift",
]


@pytest.fixture(scope="module")
def busy_server(tmp_path_factory):
    from safety_explorer import corpus as corpus_mod, server

    corpus = corpus_mod.load(ROOT / "corpus")
    path = tmp_path_factory.mktemp("busy") / "b.db"
    conn = db.init_db(path)
    runner.snapshot_corpus(conn=conn, corpus=corpus, lint_clean=True)
    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "busy", provider, corpus, 1)
    runner.execute(conn, cid, corpus, provider, 1,
                   only=["orbital_debris", "impactor_deflection", "control_autonomy",
                         "alarming_benign"])
    sessions.append_turn(conn, "s1", "user", "hello", source="t")
    conn.close()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=server.serve, args=(str(path), str(ROOT / "corpus"), "127.0.0.1", port),
                     daemon=True).start()
    for _ in range(80):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", "/api/meta")
            c.getresponse().read()
            break
        except OSError:
            time.sleep(0.1)
    return port


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    return path, r.status, body


def post(port, path, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    c.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse()
    return path, r.status, r.read()


def test_the_overview_burst_never_errors(busy_server):
    port = busy_server
    for _ in range(3):
        with ThreadPoolExecutor(max_workers=len(OVERVIEW_BURST)) as ex:
            results = list(ex.map(lambda p: get(port, p), OVERVIEW_BURST))
        failures = [(p, s, b[:200]) for p, s, b in results if s != 200]
        assert failures == []


def test_reads_and_writes_together(busy_server):
    """Turns streaming in (the capture script, an agent) while analyses are read."""
    port = busy_server
    with ThreadPoolExecutor(max_workers=24) as ex:
        writes = [ex.submit(post, port, "/api/session/turn",
                            {"session_id": f"w{i}", "role": "user", "text": f"hello {i}"})
                  for i in range(30)]
        reads = [ex.submit(get, port, p) for p in OVERVIEW_BURST]
        results = [f.result() for f in writes + reads]
    assert [(p, s) for p, s, _ in results if s != 200] == []
