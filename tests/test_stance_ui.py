"""The stance charts, checked in the browser where their bugs actually live.

Two of the three defects found while building these views were invisible to every
Python test and to the JSON the server returns, because they were *layout*:

  * `<td class="cell">` in the posture matrix collided with the surface heatmap's
    long-standing `.cell` rule, which sets `display: flex` and a fixed 62x42 box. The
    data was right, the markup was right, and the cells stacked vertically inside
    their own rows — a 6x6 matrix rendering as an unreadable column.
  * `<table class="grid">` collided with `.grid` the same way.

A class-name collision cannot be caught by reading either file on its own, and the
API returns identical bytes whether or not it happened. Only a rendered page knows.

Playwright is not a dependency of this project — the zero-dependency rule applies to
the runtime — so these skip cleanly when it is absent.
"""

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

    from safety_explorer import corpus as corpus_mod, db, runner, server
    from safety_explorer.providers import get_provider

    root = Path(__file__).resolve().parents[1]
    corpus = corpus_mod.load(root / "corpus")
    path = tmp_path_factory.mktemp("stance") / "x.db"
    conn = db.init_db(path)
    runner.snapshot_corpus(conn=conn, corpus=corpus, lint_clean=True)
    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "stance", provider, corpus, 1)
    # Two families, so the posture matrix has more than one cell to fill and the
    # decoupling plane has both refusals and worked answers in it.
    runner.execute(conn, cid, corpus, provider, 1,
                   only=["orbital_debris", "kinetic_impactor"])
    conn.close()

    port = _free_port()
    threading.Thread(target=server.serve,
                     args=(str(path), str(root / "corpus"), "127.0.0.1", port),
                     daemon=True).start()
    time.sleep(1.5)
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
        pg.click('nav button[data-view="stance"]')
        pg.wait_for_selector("#st-decouple svg.chart", timeout=20000)
        pg.errors = errors
        yield pg
        browser.close()


def test_the_view_renders_without_a_page_error(page):
    assert page.errors == []


def test_the_decoupling_plane_draws_a_dot_per_scored_run(page):
    n_dots = len(page.query_selector_all("#st-decouple .dot-hit"))
    n_rows = page.evaluate(
        "() => Object.values(STANCE.data.decoupling.cells)"
        ".reduce((a, c) => a + c.n, 0)")
    assert n_dots == n_rows > 0


def test_every_matrix_cell_in_a_row_sits_on_the_same_line(page):
    """The `.cell` collision, pinned.

    Cells stacking inside their row is what a `display: flex` leak looks like, and it
    is indistinguishable from correct markup until something measures the geometry.
    """
    rows = page.evaluate("""() => [...document.querySelectorAll(
        '#st-posture table.matrix tbody tr')].map(
        r => [...r.children].map(c => Math.round(c.getBoundingClientRect().y)))""")
    assert rows, "no matrix rows rendered"
    for tops in rows:
        assert len(set(tops)) == 1, f"cells wrapped within one row: {tops}"


def test_matrix_cells_are_table_cells_not_flex_boxes(page):
    displays = page.evaluate(
        "() => [...document.querySelectorAll('#st-posture table.matrix td')]"
        ".map(c => getComputedStyle(c).display)")
    assert displays and set(displays) == {"table-cell"}, set(displays)


def test_the_stance_tables_are_not_inline_blocks(page):
    """`.grid` is the surface heatmap's inline-block box; these tables are not that."""
    displays = page.evaluate(
        "() => [...document.querySelectorAll('#v-stance table.tbl')]"
        ".map(t => getComputedStyle(t).display)")
    assert displays and set(displays) == {"table"}, set(displays)


def test_a_dimension_with_no_markers_gets_a_card_not_a_flat_line(page):
    """Plotting an all-zero series draws a chart of nothing against a 0.0 axis.

    It reads as "no data" when it actually means "this model never does this", which is
    a finding and belongs in words.
    """
    zero_dims = page.evaluate("""() => {
        const bv = STANCE.data.by_variant;
        const dims = ['warmth', 'deference', 'directiveness', 'moralizing',
                      'distancing', 'hedging'];
        return dims.filter(d => Object.values(bv).every(c => !c[d]));
    }""")
    cards = page.eval_on_selector_all(
        "#st-variants .facet-null .facet-null-k", "els => els.map(e => e.textContent)")
    assert sorted(cards) == sorted(zero_dims)
    plotted = page.eval_on_selector_all(
        "#st-variants svg.facet text.facet-title", "els => els.map(e => e.textContent)")
    assert not (set(plotted) & set(zero_dims))


def test_hovering_a_dot_opens_a_tooltip(page):
    """An 8px dot you must land on dead-centre is not hoverable; the target is ~24px."""
    dots = page.query_selector_all("#st-decouple .dot-hit")
    assert dots
    dots[len(dots) // 2].hover()
    page.wait_for_timeout(250)
    tip = page.query_selector("#chart-tip")
    assert tip is not None and tip.is_visible()
    assert "capability" in tip.inner_text()


def test_the_trajectory_renders_for_an_opened_conversation(page):
    page.click('nav button[data-view="coanalyse"]')
    page.wait_for_selector("#co-list .co-row", timeout=20000)
    page.query_selector_all("#co-list .co-row")[0].click()
    page.wait_for_selector("#co-trajectory .chart, #co-trajectory .empty-state",
                           timeout=20000)
    assert page.errors == []


def test_both_axes_of_the_plane_are_labelled(page):
    """A plane with no scale cannot be read: 0.5 warmth and 5 warmth look identical."""
    ticks = page.eval_on_selector_all(
        "#st-decouple svg.chart text.tick", "els => els.map(e => e.textContent)")
    assert "0" in ticks
    assert any(t.startswith("1.0") or t == "1.0" for t in ticks)
    cut = page.evaluate("() => String(STANCE.data.decoupling.warm_cut)")
    assert cut in ticks, f"the warm cut {cut} is drawn but not labelled: {ticks}"
