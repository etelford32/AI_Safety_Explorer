"""Browser capture: who may reach the local server, and the idempotent ingest behind it.

The capture userscript is the first thing that makes a *web page* a designed source of
turns. That turns "which pages may talk to 127.0.0.1" from a latent question into a rule,
and these tests are the rule: a chat site may write a turn and nothing else, an arbitrary
page may do nothing at all, a rebound hostile hostname is refused outright, and a program
with no Origin (an agent hook) keeps working exactly as before.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from safety_explorer import __version__, access, db, sessions

ROOT = Path(__file__).resolve().parents[1]


# --- the rules, as pure functions ----------------------------------------------------------

@pytest.mark.parametrize("host,bound,ok", [
    ("127.0.0.1:8713", "127.0.0.1", True),
    ("localhost:8713", "127.0.0.1", True),
    ("[::1]:8713", "127.0.0.1", True),
    ("evil.example:8713", "127.0.0.1", False),   # DNS rebinding arrives with its own name
    ("192.168.1.20:8713", "127.0.0.1", False),
    ("192.168.1.20:8713", "0.0.0.0", True),      # bound wide on purpose
    (None, "127.0.0.1", False),
])
def test_host_rule(host, bound, ok):
    assert access.host_ok(host, bound) is ok


@pytest.mark.parametrize("origin,path,verdict", [
    (None, "/api/demo/clear", "none"),                                  # a program
    ("http://127.0.0.1:8713", "/api/demo/clear", "same"),               # the Explorer's UI
    ("https://chatgpt.com", "/api/session/turn", "capture"),
    ("https://claude.ai", "/api/session/paste", "capture"),
    ("https://claude.ai", "/api/sessions", "denied"),                   # reads stay same-origin
    ("https://chatgpt.com", "/api/demo/clear", "denied"),
    ("https://evil.example", "/api/session/turn", "denied"),
    ("chrome-extension://abcdef", "/api/session/turn", "extension"),    # a userscript manager
    ("moz-extension://1234", "/api/status", "extension"),
    ("null", "/api/session/turn", "denied"),
])
def test_origin_rule(origin, path, verdict):
    assert access.origin_verdict(origin, "127.0.0.1:8713", path) == verdict


def test_an_origin_can_be_added(monkeypatch):
    assert access.origin_verdict("http://localhost:3000", "127.0.0.1:8713", "/api/session/turn") == "denied"
    monkeypatch.setenv("EXPLORER_ALLOWED_ORIGINS", "http://localhost:3000, https://chat.example/")
    assert access.origin_verdict("http://localhost:3000", "127.0.0.1:8713", "/api/session/turn") == "capture"
    assert access.origin_verdict("https://chat.example", "127.0.0.1:8713", "/api/session/turn") == "capture"


# --- idempotent ingest ---------------------------------------------------------------------

def test_turn_index_makes_a_resend_harmless(conn):
    for i, (role, text) in enumerate([("user", "hello"), ("assistant", "hi there")]):
        sessions.append_turn(conn, "s", role, text, turn_index=i, source="t")
    again = sessions.append_turn(conn, "s", "assistant", "  hi   there\n", turn_index=1)
    assert again["duplicate"] is True
    assert len(sessions.session_turns(conn, "s")) == 2


def test_an_edit_is_a_conflict_never_an_overwrite(conn):
    sessions.append_turn(conn, "s", "user", "hello", turn_index=0)
    sessions.append_turn(conn, "s", "assistant", "first answer", turn_index=1)
    with pytest.raises(sessions.TurnConflict) as exc:
        sessions.append_turn(conn, "s", "assistant", "a regenerated answer", turn_index=1)
    assert exc.value.expected == 2
    assert sessions.session_turns(conn, "s")[1]["text"] == "first answer"


def test_a_gap_is_refused_and_opens_nothing(conn):
    with pytest.raises(sessions.TurnGap):
        sessions.append_turn(conn, "fresh", "assistant", "turn five", turn_index=5)
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM live_session WHERE id = 'fresh'")["n"] == 0
    sessions.append_turn(conn, "s", "user", "hello", turn_index=0)
    with pytest.raises(sessions.TurnGap) as exc:
        sessions.append_turn(conn, "s", "user", "skipped ahead", turn_index=3)
    assert exc.value.expected == 1


def test_without_turn_index_turns_still_append(conn):
    sessions.append_turn(conn, "s", "user", "a")
    sessions.append_turn(conn, "s", "user", "a")
    assert len(sessions.session_turns(conn, "s")) == 2


def test_a_selection_is_split_and_says_how(conn):
    out = sessions.append_paste(conn, "User: how far?\nAssistant: about 400 km.\nUser: thanks",
                                session_id="sel-1", source="userscript:example")
    assert out["n_turns"] == 3 and out["convention"] == "speaker markers" and out["confident"]
    assert [t["role"] for t in sessions.session_turns(conn, "sel-1")] == ["user", "assistant", "user"]
    unmarked = sessions.append_paste(conn, "just some text with no speakers", session_id="sel-2")
    assert unmarked["confident"] is False and "single assistant turn" in unmarked["note"]


# --- over HTTP -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    from safety_explorer import server

    path = tmp_path_factory.mktemp("capture") / "c.db"
    db.init_db(path).close()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=server.serve, args=(str(path), str(ROOT / "corpus"), "127.0.0.1", port),
                     daemon=True).start()
    for _ in range(60):
        try:
            http.client.HTTPConnection("127.0.0.1", port, timeout=1).request("GET", "/api/meta")
            break
        except OSError:
            time.sleep(0.1)
    return port, path


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=None if body is None else json.dumps(body), headers=h)
    r = c.getresponse()
    raw = r.read()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        data = raw.decode()
    return r.status, dict(r.getheaders()), data


def turn(sid, i, role, text, **kw):
    return {"session_id": sid, "turn_index": i, "role": role, "text": text,
            "source": "userscript:chatgpt.com", "label": "t", **kw}


def test_a_chat_site_may_write_a_turn_and_is_told_so(live_server):
    port, _ = live_server
    status, hdrs, data = call(port, "POST", "/api/session/turn", turn("gpt-1", 0, "user", "hi"),
                              {"Origin": "https://chatgpt.com"})
    assert status == 200 and data["ok"]
    assert hdrs.get("Access-Control-Allow-Origin") == "https://chatgpt.com"


def test_an_arbitrary_page_may_not_write(live_server):
    port, path = live_server
    status, hdrs, _ = call(port, "POST", "/api/session/turn", turn("evil-1", 0, "user", "injected"),
                           {"Origin": "https://evil.example"})
    assert status == 403 and "Access-Control-Allow-Origin" not in hdrs
    conn = db.connect(path)
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM live_session WHERE id = 'evil-1'")["n"] == 0


def test_a_chat_site_may_not_read_or_act_beyond_capture(live_server):
    port, _ = live_server
    for method, p in [("GET", "/api/sessions"), ("GET", "/api/session?id=gpt-1"),
                      ("POST", "/api/demo/clear"), ("GET", "/api/overview")]:
        status, _, _ = call(port, method, p, {} if method == "POST" else None,
                            {"Origin": "https://claude.ai"})
        assert status == 403, p


def test_status_to_a_capture_source_is_about_its_own_session_only(live_server):
    port, _ = live_server
    call(port, "POST", "/api/session/turn", turn("other-conv", 0, "user", "private"), {})
    _, _, data = call(port, "GET", "/api/status?session=gpt-1", headers={"Origin": "https://chatgpt.com"})
    assert data["ok"] and data["session"]["id"] == "gpt-1"
    assert "sessions" not in data and "other-conv" not in json.dumps(data)
    _, _, full = call(port, "GET", "/api/status")          # the Explorer's own view is unchanged
    assert "sessions" in full


def test_a_rebound_hostname_is_refused(live_server):
    port, _ = live_server
    status, _, _ = call(port, "GET", "/api/sessions", headers={"Host": f"evil.example:{port}"})
    assert status == 403


def test_the_preflight(live_server):
    port, _ = live_server
    status, hdrs, _ = call(port, "OPTIONS", "/api/session/turn", headers={
        "Origin": "https://claude.ai", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Private-Network": "true"})
    assert status == 204
    assert hdrs["Access-Control-Allow-Origin"] == "https://claude.ai"
    assert "POST" in hdrs["Access-Control-Allow-Methods"]
    assert hdrs["Access-Control-Allow-Private-Network"] == "true"
    status, _, _ = call(port, "OPTIONS", "/api/session/turn", headers={"Origin": "https://evil.example"})
    assert status == 403


def test_conflicts_and_gaps_come_back_as_409(live_server):
    port, _ = live_server
    assert call(port, "POST", "/api/session/turn", turn("gpt-2", 0, "user", "q"))[0] == 200
    assert call(port, "POST", "/api/session/turn", turn("gpt-2", 1, "assistant", "a"))[0] == 200
    s, _, d = call(port, "POST", "/api/session/turn", turn("gpt-2", 1, "assistant", "a"))
    assert s == 200 and d["duplicate"]
    s, _, d = call(port, "POST", "/api/session/turn", turn("gpt-2", 1, "assistant", "changed"))
    assert s == 409 and d["conflict"] and d["expected"] == 2
    s, _, d = call(port, "POST", "/api/session/turn", turn("gpt-2", 4, "user", "later"))
    assert s == 409 and d["gap"] and d["expected"] == 2


def test_the_paste_endpoint(live_server):
    port, _ = live_server
    s, _, d = call(port, "POST", "/api/session/paste",
                   {"text": "You: is it safe?\nAssistant: yes, within limits.", "session_id": "sel-http"},
                   {"Origin": "https://claude.ai"})
    assert s == 200 and d["n_turns"] == 2
    assert call(port, "POST", "/api/session/paste", {"text": "   "})[0] == 400


def test_agents_without_an_origin_are_unaffected(live_server):
    port, _ = live_server
    s, _, d = call(port, "POST", "/api/session/turn",
                   {"session_id": "agent-x", "role": "assistant", "text": "working on it"})
    assert s == 200 and d["turn_index"] == 0


# --- the script itself ---------------------------------------------------------------------

def test_the_userscript_is_served_pointed_at_this_server(live_server):
    port, _ = live_server
    s, hdrs, text = call(port, "GET", "/explorer-capture.user.js")
    assert s == 200 and hdrs["Content-Type"].startswith("text/javascript")
    assert text.startswith("// ==UserScript==")
    assert f"const DEFAULT_SERVER = 'http://127.0.0.1:{port}';" in text
    for needle in ("@match        https://claude.ai/*", "@match        https://chatgpt.com/*",
                   "@connect      127.0.0.1", "@grant        GM_xmlhttpRequest"):
        assert needle in text


def test_the_script_version_tracks_the_package():
    src = (ROOT / "src/safety_explorer/web/explorer-capture.user.js").read_text()
    assert re.search(r"@version\s+(\S+)", src).group(1) == __version__
    assert f"const VERSION = '{__version__}';" in src
