"""The blinding rule, checked in the browser where it actually lives.

The reveal rule is JavaScript, so a Python test cannot reach it — and the flaw it now
guards was invisible to every other kind of test. One annotation row carries all nine
metrics, so `mine[key] !== undefined` was true for every metric the instant the first
was submitted: rating one metric revealed all nine proposals, which is precisely the
anchoring the view exists to prevent. The server was correct, the storage was correct,
and the page leaked anyway.

Playwright is not a dependency of this project — the zero-dependency rule is deliberate
and applies to the runtime, not to what a developer may have installed — so these skip
cleanly when it is absent rather than forcing it into the install.
"""

import socket
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="playwright not installed; browser checks skipped")

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
METRICS = ["capability_retention", "technical_depth", "quantitative_content",
           "specificity", "boundary_precision", "recovery_quality",
           "over_refusal", "unsafe_assistance", "epistemic_quality"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    from pathlib import Path

    from safety_explorer import corpus as corpus_mod, db, runner, server
    from safety_explorer.providers import get_provider

    root = Path(__file__).resolve().parents[1]
    corpus = corpus_mod.load(root / "corpus")
    path = tmp_path_factory.mktemp("blind") / "x.db"
    conn = db.init_db(path)
    runner.snapshot_corpus(conn=conn, corpus=corpus, lint_clean=True)
    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "blind", provider, corpus, 1)
    runner.execute(conn, cid, corpus, provider, 1, only=["orbital_debris"])
    conn.close()

    port = _free_port()
    thread = threading.Thread(
        target=server.serve,
        args=(str(path), str(root / "corpus"), "127.0.0.1", port),
        daemon=True)
    thread.start()
    time.sleep(1.2)
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def page(served):
    import os

    from playwright.sync_api import sync_playwright

    if not os.path.exists(CHROMIUM):
        pytest.skip("no chromium at the configured path")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        pg = browser.new_page(viewport={"width": 1500, "height": 1200})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(served, wait_until="networkidle")
        pg.click('nav button[data-view="coanalyse"]')
        pg.wait_for_timeout(700)
        yield pg, errors
        browser.close()


def _open_a_conversation(pg, needle="orbital_debris C #0"):
    rows = pg.locator(".co-row")
    for i in range(rows.count()):
        if needle in rows.nth(i).inner_text():
            rows.nth(i).click()
            pg.wait_for_timeout(700)
            return True
    return False


def test_the_page_renders_without_script_errors(page):
    pg, errors = page
    assert _open_a_conversation(pg)
    assert pg.locator(".span").count() > 0
    assert errors == []


def test_rating_one_metric_does_not_reveal_the_other_eight(page):
    """The flaw, as a regression test.

    Ratings hide at the granularity they are STORED at: one row per run, so nothing is
    revealed until the whole rubric has been worked through.
    """
    pg, _ = page
    assert _open_a_conversation(pg)
    pg.click("#btn-propose")
    pg.wait_for_timeout(1600)

    hidden = pg.locator(".metric .mprop", has_text="rate first")
    assert hidden.count() == len(METRICS), "a proposal was visible before any rating"

    pg.locator('.metric .lv[data-metric="capability_retention"][data-level="4"]').click()
    pg.wait_for_timeout(700)
    assert hidden.count() == len(METRICS), "rating one metric leaked the other eight"


def test_the_proposals_appear_once_the_rubric_is_worked_through(page):
    pg, _ = page
    assert _open_a_conversation(pg)
    pg.click("#btn-propose")
    pg.wait_for_timeout(1600)

    for i, metric in enumerate(METRICS):
        pg.locator(f'.metric .lv[data-metric="{metric}"][data-level="{min(5, i)}"]').click()
        pg.wait_for_timeout(320)
    pg.wait_for_timeout(600)

    assert pg.locator(".metric .mprop", has_text="rate first").count() == 0
    assert pg.locator(".metric .mprop .cited").count() == len(METRICS)


def test_a_chosen_level_shows_the_anchor_it_committed_to(page):
    """The anchor is the measurement. A number with the descriptor hidden is the scale
    this rubric replaced."""
    pg, _ = page
    assert _open_a_conversation(pg)
    pg.locator('.metric .lv[data-metric="capability_retention"][data-level="4"]').click()
    pg.wait_for_timeout(700)
    anchor = pg.locator(".metric .anchor").first.inner_text()
    assert "evaluates it" in anchor, anchor


def test_a_span_label_is_one_keypress(page):
    pg, _ = page
    assert _open_a_conversation(pg)
    before = pg.locator(".span.done").count()
    pg.keyboard.press("1")
    pg.wait_for_timeout(700)
    assert pg.locator(".span.done").count() == before + 1
