"""The readability layer, checked in a rendered page.

Each tile and each results panel answers its question in words, with a status that is an
icon and a word; the views that used to open empty now open on something; and demo data
says so on every view. All of it lives in the DOM, so only a browser can check it.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="playwright not installed; browser checks skipped")

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    from pathlib import Path

    from safety_explorer import corpus as corpus_mod, db, demo, server

    root = Path(__file__).resolve().parents[1]
    corpus = corpus_mod.load(root / "corpus")
    path = tmp_path_factory.mktemp("readability") / "x.db"
    conn = db.init_db(path)
    demo.seed(conn, corpus, repeats=1)
    conn.close()

    port = _free_port()
    threading.Thread(target=server.serve,
                     args=(str(path), str(root / "corpus"), "127.0.0.1", port),
                     daemon=True).start()
    time.sleep(1.5)
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def page(served):
    from playwright.sync_api import sync_playwright

    if not os.path.exists(CHROMIUM):
        pytest.skip("no chromium at the configured path")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        pg = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(served + "/#/overview", wait_until="networkidle")
        pg.wait_for_function("document.querySelectorAll('#ov-tiles .tile').length > 0"
                             " && !document.querySelector('#ov-tiles .tile.loading')", timeout=60000)
        yield pg
        assert not errors, errors
        browser.close()


def test_every_tile_says_what_its_number_means(page):
    reads = page.eval_on_selector_all("#ov-tiles .tile .tile-read", "els => els.map(e => e.textContent.trim())")
    assert len(reads) >= 10
    short = [r for r in reads if len(r) < 25]
    assert not short, short


def test_every_status_is_an_icon_and_a_word(page):
    badges = page.eval_on_selector_all(
        "#ov-tiles .badge",
        "els => els.map(e => ({svg: !!e.querySelector('svg'), text: e.textContent.trim()}))")
    assert badges, "no status badges rendered"
    assert all(b["svg"] and b["text"] for b in badges), badges
    # A tile whose top border says "finding" or "review" also says it in its badge.
    unlabelled = page.eval_on_selector_all(
        "#ov-tiles .tile[data-status='finding'], #ov-tiles .tile[data-status='review']",
        "els => els.filter(e => !e.querySelector('.tile-badge .badge')).map(e => e.id)")
    assert not unlabelled


def test_the_badge_key_explains_all_four_states(page):
    key = page.text_content("#ov-key")
    for word in ("Finding", "Review", "Clean", "No data"):
        assert word in key


def test_demo_data_is_announced_on_every_view(page, served):
    for view in ("overview", "results", "stance", "sessions"):
        page.goto(f"{served}/#/{view}")
        page.wait_for_selector("#demo-banner:not([hidden])", timeout=15000)
        assert "not a measurement of any model" in page.text_content("#demo-banner")


def test_a_mark_in_a_mini_chart_has_its_own_tooltip(page):
    mark = page.query_selector("#tile-truth .mk")
    assert mark is not None
    mark.hover()
    page.wait_for_selector("#chart-tip.show", timeout=5000)
    assert "correct" in page.text_content("#chart-tip")


def test_a_results_panel_opens_with_the_tiles_answer(page, served):
    tile = page.text_content("#tile-depth .tile-read").strip()
    page.goto(f"{served}/#/results/depth")
    page.wait_for_selector("#sec-depth .answer .ans-t", timeout=60000)
    assert page.text_content("#sec-depth .answer .ans-t").strip() == tile
    assert page.query_selector("#sec-depth .answer .badge svg") is not None


def test_the_surface_renders_without_a_click(page, served):
    page.goto(f"{served}/#/surface")
    page.wait_for_selector("#sf-out .grid .cell:not(.empty)", timeout=30000)
    # No human ratings in the demo, so it starts on a source that has data.
    assert page.eval_on_selector("#sf-source", "e => e.value") == "truth"
    assert page.query_selector("#sf-out .sf-scale") is not None


def test_compare_opens_on_a_comparison_with_a_plain_summary(page, served):
    page.goto(f"{served}/#/compare")
    page.wait_for_selector("#cmp-out .answer .ans-t", timeout=30000)
    assert "declared twin" in page.text_content("#cmp-out .answer .ans-t")


def test_stance_facets_follow_the_ladder_and_keep_twins_off_the_line(page, served):
    page.goto(f"{served}/#/stance")
    page.wait_for_selector("#st-variants .chart.facet", timeout=90000)
    first = page.query_selector("#st-variants .chart.facet")
    ticks = first.eval_on_selector_all("text.tick", "els => els.map(e => e.textContent)")
    assert [t for t in ticks if t.isalpha() and len(t) == 1] == list("ABCDEF")
    assert page.query_selector("#st-variants .facet-dot.twin") is not None


def test_the_start_states_say_what_the_view_is_for(page, served):
    page.goto(f"{served}/#/annotate")
    page.wait_for_selector("#ann-body .start-state", timeout=10000)
    assert "Start rating" in page.text_content("#ann-body")
    assert "Press start." not in page.text_content("#ann-body")
