"""The intake, used the way a person would: choose files, drop them, paste — in a browser.

What can only be checked in a page: that the preview appears before anything is stored,
that the import lands in Conversations with its flags, that a drop anywhere and a paste
anywhere reach the same path, and that an agent's tool calls show in place.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="playwright not installed; browser checks skipped")

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
FIX = Path(__file__).resolve().parent / "fixtures" / "intake"


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    from safety_explorer import db, server

    root = Path(__file__).resolve().parents[1]
    base = tmp_path_factory.mktemp("intake-ui")
    home = base / "home"
    proj = home / ".claude" / "projects" / "-work-proj"
    proj.mkdir(parents=True)
    shutil.copy(FIX / "claude_code.jsonl", proj / "sess-cc.jsonl")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    path = base / "x.db"
    db.init_db(path).close()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=server.serve, args=(str(path), str(root / "corpus"), "127.0.0.1", port),
                     daemon=True).start()
    time.sleep(1.5)
    yield f"http://127.0.0.1:{port}"
    if old_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = old_home


@pytest.fixture
def page(served):
    from playwright.sync_api import sync_playwright

    if not os.path.exists(CHROMIUM):
        pytest.skip("no chromium at the configured path")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        pg = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        yield pg
        assert not errors, errors
        browser.close()


def test_choose_files_previews_then_imports_into_conversations(page, served):
    page.goto(served + "/#/collect", wait_until="networkidle")
    page.wait_for_selector("#intake-drop")
    page.set_input_files("#intake-file", [str(FIX / "chatgpt_conversations.json"),
                                          str(FIX / "claude_conversations.json"),
                                          str(FIX / "not_a_conversation.txt")])
    page.wait_for_selector("#ip-go", timeout=20000)
    modal = page.text_content(".modal")
    assert "ChatGPT export" in modal and "Claude.ai export" in modal
    assert "held nothing recognisable" in modal                 # the notes file, said plainly
    page.click("#ip-go")
    page.goto(served + "/#/sessions")
    page.wait_for_function("[...document.querySelectorAll('#sess-list .sess-row b')]"
                           ".some(b => b.textContent === 'Debris cascade')", timeout=20000)
    row = page.locator("#sess-list .sess-row", has_text="Debris cascade")
    assert "Refusal" in row.text_content()
    page.click("#sess-filter button[data-f='flagged']")
    page.wait_for_timeout(800)
    labels = page.eval_on_selector_all("#sess-list .sess-row b", "els => els.map(e => e.textContent)")
    assert labels == ["Debris cascade"]
    page.click("#sess-filter button[data-f='all']")


def test_a_drop_anywhere_reaches_the_same_preview(page, served):
    page.goto(served + "/#/overview", wait_until="networkidle")
    text = (FIX / "sharegpt.json").read_text()
    page.evaluate("""(text) => {
      const dt = new DataTransfer();
      dt.items.add(new File([text], 'sharegpt.json', { type: 'application/json' }));
      document.body.dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true }));
      window.__dt = dt;
    }""", text)
    assert page.is_visible("#drop-overlay")
    page.evaluate("() => document.body.dispatchEvent(new DragEvent('drop', { dataTransfer: window.__dt, bubbles: true, cancelable: true }))")
    page.wait_for_selector("#ip-go", timeout=20000)
    assert "ShareGPT" in page.text_content(".modal")
    assert not page.is_visible("#drop-overlay")
    page.click("#ip-cancel")


def test_a_paste_anywhere_reads_a_transcript(page, served):
    page.goto(served + "/#/overview", wait_until="networkidle")
    page.evaluate("""(text) => {
      const dt = new DataTransfer();
      dt.setData('text/plain', text);
      document.body.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
    }""", (FIX / "transcript.md").read_text())
    page.wait_for_selector("#ip-go", timeout=20000)
    assert "Text transcript" in page.text_content(".modal")
    page.click("#ip-cancel")


def test_the_add_data_page_shows_the_inbox_and_what_it_found(page, served):
    page.goto(served + "/#/collect", wait_until="networkidle")
    page.wait_for_selector("#inbox-path")
    assert page.text_content("#inbox-path").endswith("inbox")
    assert "Claude Code sessions" in page.text_content("#intake-body")
    page.click("[data-connect='claude_code']")
    page.goto(served + "/#/sessions")
    page.wait_for_function("[...document.querySelectorAll('#sess-list .sess-row b')]"
                           ".some(b => b.textContent.startsWith('proj'))", timeout=20000)
    page.locator("#sess-list .sess-row", has_text="proj").click()
    page.wait_for_selector("#sess-turns .live-turn.ctx", timeout=20000)
    ctx = page.eval_on_selector_all("#sess-turns .live-turn.ctx .ctx-t", "els => els.map(e => e.textContent)")
    assert any(t.startswith("→ Bash") for t in ctx) and any(t.startswith("← result") for t in ctx)
