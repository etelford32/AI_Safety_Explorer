"""Readability checks that need no browser.

The interface is read for hours, often in a screenshot pasted into a paper draft. Two
properties are cheap to hold and expensive to lose quietly: every text colour stays legible
on every surface it sits on, and demo data announces itself wherever it could be mistaken
for a measurement.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from safety_explorer import demo, server, sessions

CSS = (Path(__file__).resolve().parents[1] / "src" / "safety_explorer" / "web" / "style.css").read_text(encoding="utf-8")


def _token(name: str) -> str:
    # The first definition of a token on :root is the one the page uses.
    return re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", CSS).group(1)


def _contrast(fg: str, bg: str) -> float:
    def lum(h: str) -> float:
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    a, b = sorted((lum(fg), lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@pytest.mark.parametrize("ink", ["ink", "ink-dim", "ink-faint"])
@pytest.mark.parametrize("surface", ["bg", "panel", "panel-2"])
def test_every_text_ink_meets_wcag_aa_on_every_surface(ink, surface):
    # 4.5:1 is WCAG AA for small text — and nearly everything here is small text. The
    # faint ink was 2.5:1 before this test existed, on the panel titles and footnotes.
    ratio = _contrast(_token(ink), _token(surface))
    assert ratio >= 4.5, f"--{ink} on --{surface} is {ratio:.2f}:1"


def test_the_three_inks_stay_distinct():
    # Legible is not enough: the hierarchy needs three steps a reader can tell apart.
    ink, dim, faint = (_contrast(_token(n), _token("panel")) for n in ("ink", "ink-dim", "ink-faint"))
    assert ink - dim > 3 and dim - faint > 1.5


def test_panel_titles_are_sentences_not_letter_spaced_capitals():
    rule = re.search(r"\.panel h2 \{([^}]*)\}", CSS).group(1)
    assert "text-transform: none" in rule and "var(--ink)" in rule


def test_demo_presence_is_counted_and_cleared(populated, corpus, monkeypatch):
    monkeypatch.setattr(demo, "CUED_FAMILIES", ["control_autonomy"])
    conn, _ = populated
    assert demo.present(conn) == {"runs": 0, "sessions": 0}
    demo.seed(conn, corpus, repeats=1)
    got = demo.present(conn)
    assert got["runs"] > 0 and got["sessions"] == len(demo.SESSIONS)
    demo.clear(conn)
    assert demo.present(conn) == {"runs": 0, "sessions": 0}


def test_a_real_session_is_not_demo_data(conn):
    sessions.append_turn(conn, "real", "user", "a real conversation", source="paste")
    assert demo.present(conn)["sessions"] == 0


def test_status_carries_the_demo_counts_the_banner_reads(populated, corpus, monkeypatch):
    monkeypatch.setattr(demo, "CUED_FAMILIES", ["control_autonomy"])
    conn, _ = populated
    assert server.status_report(conn)["demo"] == {"runs": 0, "sessions": 0}
    demo.seed(conn, corpus, repeats=1)
    assert server.status_report(conn)["demo"]["runs"] > 0
