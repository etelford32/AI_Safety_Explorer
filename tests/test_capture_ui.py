"""The capture userscript, run in a real browser against pages shaped like the chat sites.

Playwright answers requests for https://chatgpt.com and https://claude.ai with fixture pages
built from the markup hooks the script relies on (the author-role attribute, the user- and
assistant-message markers, the streaming indicators), so the script sees the right hostname
and the right structure without any network. A userscript manager is stood in for by a
shim: GM_xmlhttpRequest is routed to Python, which — like a real manager — sends the
request from outside the page, with no Origin.

What this proves is the script's logic end to end: roles, text without UI chrome, idempotent
re-sends, Follow waiting out a streaming reply, a regeneration becoming a branch, and the
selection fallback. What it cannot prove is that the live sites still use these hooks —
that is a manual check (docs/CAPTURE.md), and the panel's "found N turns" line is how a user
sees at once that a site has changed.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed; browser checks skipped")

from safety_explorer import db, sessions  # noqa: E402

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
ROOT = Path(__file__).resolve().parents[1]

CHATGPT = """<!doctype html><html><head><title>Orbital decay - ChatGPT</title>
<style>.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
.katex-mathml{position:absolute;clip:rect(1px,1px,1px,1px);width:1px;height:1px;overflow:hidden}</style>
</head><body><div id="thread">
<article><h5 class="sr-only">You said:</h5>
  <div data-message-author-role="user"><div class="whitespace-pre-wrap">How long does a 400 km debris cloud take to decay?</div></div></article>
<article><h6 class="sr-only">ChatGPT said:</h6>
  <div data-message-author-role="assistant"><div class="markdown">
    <p>Let's work it through together.</p>
    <pre><div>python<button>Copy code</button></div><code>t = H / v</code></pre>
    <p>Roughly <span class="katex"><span class="katex-mathml">tau</span><span class="katex-html">τ</span></span> ≈ 5 years.</p>
  </div></div></article>
</div>
<button data-testid="model-switcher-dropdown-button">Test model 2</button>
</body></html>"""

CLAUDE = """<!doctype html><html><head><title>Debris decay - Claude</title></head><body>
<div id="thread">
  <div data-testid="user-message"><p>Walk me through the decay estimate.</p></div>
  <div data-is-streaming="false"><div class="font-claude-response"><div class="font-claude-message">
    <p>Happy to — start from the drag equation.</p></div></div></div>
</div>
<button data-testid="model-selector-dropdown">Test model 1</button>
</body></html>"""

GENERIC = """<!doctype html><html><head><title>Local chat</title></head><body>
<pre id="log">User: is the pump sized right?
Assistant: Yes — 4 L/min covers 200 L in under an hour.</pre></body></html>"""

GM_SHIM = """
window.__gmStore = {};
window.GM_getValue = (k, d) => (k in window.__gmStore ? window.__gmStore[k] : d);
window.GM_setValue = (k, v) => { window.__gmStore[k] = v; };
window.GM_xmlhttpRequest = (o) => {
  window.__gmRequest(o.method, o.url, o.data || null)
    .then((r) => o.onload && o.onload(r))
    .catch((e) => o.onerror && o.onerror(e));
};
"""


@pytest.fixture(scope="module")
def explorer(tmp_path_factory):
    from safety_explorer import server

    path = tmp_path_factory.mktemp("capture-ui") / "c.db"
    db.init_db(path).close()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=server.serve, args=(str(path), str(ROOT / "corpus"), "127.0.0.1", port),
                     daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            script = urllib.request.urlopen(f"{base}/explorer-capture.user.js", timeout=2).read().decode()
            break
        except OSError:
            time.sleep(0.1)
    return base, path, script


def _gm_request(method, url, data):
    """What a userscript manager does: the request, from outside the page."""
    req = urllib.request.Request(url, method=method, data=data.encode() if data else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"status": r.status, "responseText": r.read().decode()}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "responseText": e.read().decode()}


@pytest.fixture
def page(explorer):
    import os

    from playwright.sync_api import sync_playwright

    if not os.path.exists(CHROMIUM):
        pytest.skip("no chromium at the configured path")
    _, _, script = explorer
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.expose_function("__gmRequest", _gm_request)
        def serve(html):
            return lambda route, _request: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

        for pattern, html in [("https://chatgpt.com/**", CHATGPT), ("https://claude.ai/**", CLAUDE),
                              ("https://chat.example.test/**", GENERIC)]:
            pg.route(pattern, serve(html))
        pg.add_init_script(GM_SHIM + "\n" + script)
        yield pg
        browser.close()


def turns_of(path, sid):
    conn = db.connect(path)
    try:
        return [(t["role"], t["text"]) for t in sessions.session_turns(conn, sid)]
    finally:
        conn.close()


def wait_for(pg, fn, timeout=10.0):
    """Poll `fn` while letting the page run. It must be the page's own wait: with the sync
    API, the shim's requests to Python are served only while Python is inside a Playwright
    call, so a plain sleep would stall every send."""
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        pg.wait_for_timeout(150)
    return fn()


def open_panel(pg):
    pg.wait_for_selector("[data-se='pill']", timeout=10000)
    pg.click("[data-se='pill']")
    pg.wait_for_selector("[data-se='panel']")


def test_chatgpt_send_follow_and_branch(page, explorer):
    _, path, _ = explorer
    sid = "chatgpt.com-abc123"
    page.goto("https://chatgpt.com/c/abc123")
    open_panel(page)
    assert "found 2 turn(s)" in page.inner_text("[data-se='found']")
    assert "Test model 2" in page.inner_text("[data-se='found']")

    page.click("[data-se='send']")
    turns = wait_for(page, lambda: len(turns_of(path, sid)) == 2 and turns_of(path, sid))
    assert [r for r, _ in turns] == ["user", "assistant"]
    user, answer = turns[0][1], turns[1][1]
    assert user == "How long does a 400 km debris cloud take to decay?"   # no "You said:"
    assert "t = H / v" in answer and "Copy code" not in answer            # no button chrome
    assert "τ" in answer and "tau" not in answer                           # one copy of the math

    conn = db.connect(path)
    sess = db.query_one(conn, "SELECT source, tier, label, meta FROM live_session WHERE id = ?", (sid,))
    meta = db.loads(sess["meta"], {})
    assert sess["source"] == "userscript:chatgpt.com" and sess["tier"] == "B"
    assert sess["label"] == "Orbital decay"
    assert meta["model_label"] == "Test model 2" and meta["capture"] == "userscript"

    # Sending again changes nothing.
    page.click("[data-se='send']")
    page.wait_for_timeout(1500)
    assert len(turns_of(path, sid)) == 2

    # Follow: a new exchange arrives, the reply streams — only finished turns are sent.
    page.click("[data-se='follow']")
    page.evaluate("""() => {
      document.getElementById('thread').insertAdjacentHTML('beforeend',
        `<article><div data-message-author-role="user"><div class="whitespace-pre-wrap">And for a deliberately fragmented target?</div></div></article>
         <article><div data-message-author-role="assistant"><div class="markdown"><p id="partial">The decay timescale</p></div></div></article>`);
      document.body.insertAdjacentHTML('beforeend', '<button data-testid="stop-button" id="stop">Stop</button>');
    }""")
    assert wait_for(page, lambda: len(turns_of(path, sid)) == 3, timeout=6)
    page.wait_for_timeout(2000)
    assert len(turns_of(path, sid)) == 3, "a reply still streaming must not be sent"
    page.evaluate("""() => {
      document.getElementById('partial').textContent = 'The decay timescale follows the same relation, about five years.';
      document.getElementById('stop').remove();
    }""")
    assert wait_for(page, lambda: len(turns_of(path, sid)) == 4, timeout=8)
    assert turns_of(path, sid)[3][1] == "The decay timescale follows the same relation, about five years."

    # A regeneration is a branch, never an overwrite.
    page.evaluate("""() => {
      const a = document.querySelectorAll('[data-message-author-role="assistant"]');
      a[a.length - 1].querySelector('.markdown').innerHTML = '<p>A regenerated answer instead.</p>';
    }""")
    branch = wait_for(page, lambda: len(turns_of(path, f"{sid}~b2")) == 4 and turns_of(path, f"{sid}~b2"), timeout=10)
    assert branch and branch[3][1] == "A regenerated answer instead."
    assert turns_of(path, sid)[3][1] == "The decay timescale follows the same relation, about five years."
    assert page.errors == []


def test_claude_send_and_streaming_guard(page, explorer):
    _, path, _ = explorer
    sid = "claude.ai-uuid-1"
    page.goto("https://claude.ai/chat/uuid-1")
    open_panel(page)
    assert "found 2 turn(s)" in page.inner_text("[data-se='found']")   # nested wrappers count once
    page.click("[data-se='send']")
    turns = wait_for(page, lambda: len(turns_of(path, sid)) == 2 and turns_of(path, sid))
    assert turns == [("user", "Walk me through the decay estimate."),
                     ("assistant", "Happy to — start from the drag equation.")]

    page.click("[data-se='follow']")
    page.evaluate("""() => {
      document.getElementById('thread').insertAdjacentHTML('beforeend',
        `<div data-testid="user-message"><p>And the fragmentation case?</p></div>
         <div id="s" data-is-streaming="true"><div class="font-claude-response"><p id="p">I can</p></div></div>`);
    }""")
    assert wait_for(page, lambda: len(turns_of(path, sid)) == 3, timeout=6)
    page.wait_for_timeout(2000)
    assert len(turns_of(path, sid)) == 3
    page.evaluate("""() => {
      document.getElementById('p').textContent = "I can't help optimise that, but the decay relation is the same.";
      document.getElementById('s').setAttribute('data-is-streaming', 'false');
    }""")
    assert wait_for(page, lambda: len(turns_of(path, sid)) == 4, timeout=8)
    assert page.errors == []


def test_selection_works_on_a_page_it_does_not_recognise(page, explorer):
    _, path, _ = explorer
    page.goto("https://chat.example.test/")
    open_panel(page)
    assert "No chat markup recognised" in page.inner_text("[data-se='found']")
    page.evaluate("""() => {
      const r = document.createRange(); r.selectNodeContents(document.getElementById('log'));
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    }""")
    page.click("[data-se='selection']")
    assert wait_for(page, lambda: "Sent 2 turn(s)" in page.inner_text("[data-se='msg']"), timeout=8)
    conn = db.connect(path)
    row = db.query_one(conn, "SELECT id FROM live_session WHERE source = 'userscript:chat.example.test'")
    assert row is not None
    assert [r for r, _ in turns_of(path, row["id"])] == ["user", "assistant"]
    assert page.errors == []


def test_a_down_server_is_said_plainly(page, explorer):
    page.goto("https://claude.ai/chat/uuid-2")
    open_panel(page)
    page.click("[data-se='panel'] details summary")
    page.fill("[data-se='server']", "http://127.0.0.1:9")    # nothing listens there
    page.press("[data-se='server']", "Enter")
    page.dispatch_event("[data-se='server']", "change")
    assert wait_for(page, lambda: "not reachable" in page.inner_text("[data-se='status']"), timeout=8)
