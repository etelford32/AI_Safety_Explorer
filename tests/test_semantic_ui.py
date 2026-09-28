"""Semantic reading, used the way a person would: find a model, test it, use it."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed; browser checks skipped")

from fake_embedder import FakeEmbedServer  # noqa: E402

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    from safety_explorer import db, server, stance as st

    root = Path(__file__).resolve().parents[1]
    base = tmp_path_factory.mktemp("semantic-ui")
    fake = FakeEmbedServer()
    fake.installed = {"nomic-embed-text", "llama3"}          # bge-m3 has to be downloaded
    (base / "embedding.json").write_text(json.dumps({"ollama_url": fake.url, "lmstudio_url": "http://127.0.0.1:9/v1"}))
    path = base / "x.db"
    db.init_db(path).close()
    old = os.environ.pop("EXPLORER_EMBED_BACKEND", None)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=server.serve, args=(str(path), str(root / "corpus"), "127.0.0.1", port),
                     daemon=True).start()
    time.sleep(1.5)
    yield f"http://127.0.0.1:{port}", fake
    fake.stop()
    st.reset_register_model()
    if old is not None:
        os.environ["EXPLORER_EMBED_BACKEND"] = old


def test_download_test_and_use_a_local_model(served):
    from playwright.sync_api import sync_playwright

    base, fake = served
    if not os.path.exists(CHROMIUM):
        pytest.skip("no chromium at the configured path")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        pg = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "/#/overview", wait_until="networkidle")
        pg.evaluate("() => openSemantic()")
        pg.wait_for_selector("[data-sem-pull='bge-m3']", timeout=15000)
        body = pg.text_content("#sem-body")
        assert "Lexicon only" in body and "nomic-embed-text" in body
        pg.click("[data-sem-pull='bge-m3']")
        # Downloaded, then tested automatically.
        pg.wait_for_selector("#sem-use", timeout=30000)
        result = pg.text_content("#sem-result")
        assert "ollama:bge-m3" in result and "English, French, Spanish, Japanese" in result
        assert pg.eval_on_selector("#sem-use", "b => b.disabled") is False
        pg.click("#sem-use")
        pg.wait_for_function("[...document.querySelectorAll('#ov-strip .strip-chip')]"
                             ".some(c => c.textContent.includes('ollama:bge-m3'))", timeout=20000)
        # An English-only model is trusted in English and declines Japanese.
        pg.evaluate("() => openSemantic()")
        pg.wait_for_selector("[data-sem-test='ollama:nomic-embed-text']", timeout=15000)
        pg.click("[data-sem-test='ollama:nomic-embed-text']")
        pg.wait_for_selector(".sem-table", timeout=30000)
        verdicts = pg.eval_on_selector_all(".sem-table tr:last-child td .badge", "els => els.map(e => e.textContent.trim())")
        assert verdicts[0] == "Trusted" and verdicts[-1] == "Not trusted"     # en … ja
        assert not errors, errors
        browser.close()
