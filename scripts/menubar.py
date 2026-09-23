#!/usr/bin/env python3
"""Run the Explorer as a macOS menu-bar app — always on, quietly receiving sessions.

This turns the instrument from something you launch into something that sits in the menu bar
while you run a model, the way Ollama does. It changes nothing about the measurement: the
spine is still push-never-pull. The app is the always-on *receiver* — the local endpoint is
always there for a userscript, an agent hook, or a wrapped provider to stream turns into —
and the menu bar is a glance: is it alive, how much has flowed through, and is any live
session drifting right now.

One process holds both: the HTTP server runs in a background thread, and the menu-bar app
polls the same database and reflects it. Quit the menu-bar item and both go.

    pip install -e '.[openai,embeddings,menubar]'
    python scripts/menubar.py                 # or set it to run at login, see docs/BACKGROUND.md

macOS only (it uses `rumps`, which is AppKit under the hood). On Linux/Windows, run
`explorer serve` under your init system instead — the endpoint and the UI are identical.
"""

from __future__ import annotations

import sys
import threading
import webbrowser
from pathlib import Path

# Resolve paths from the repo root, not the current directory, so the corpus and database
# are found however the app was launched (a login item runs with an unpredictable CWD).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from safety_explorer import paths  # noqa: E402

DB_PATH = str(paths.default_db())
CORPUS_PATH = str(paths.corpus_dir())
PORT = 8713
BASE = f"http://127.0.0.1:{PORT}"
POLL_SECONDS = 20

try:
    import rumps
except ImportError:
    sys.exit("rumps is not installed. Run: pip install -e '.[menubar]'  (macOS only)")

from safety_explorer import db, server  # noqa: E402


def _start_server() -> None:
    """Run the Explorer server in this process, in a daemon thread."""
    t = threading.Thread(
        target=server.serve,
        kwargs=dict(db_path=DB_PATH, corpus_path=CORPUS_PATH, port=PORT),
        daemon=True,
    )
    t.start()


class ExplorerApp(rumps.App):
    """The menu bar. Title carries a badge; the menu is the glance and the shortcuts.

    A quiet instrument shows a plain icon; a drifting session turns it red with a count, so
    the one thing worth interrupting for — an agent's register moving under pressure — is
    visible without opening anything.
    """

    def __init__(self) -> None:
        super().__init__("Explorer", title="🛰", quit_button="Quit Explorer")
        self.summary = rumps.MenuItem("starting…", callback=None)
        self.register = rumps.MenuItem("", callback=None)
        self.sessions_header = rumps.MenuItem("Recent sessions", callback=None)
        self.menu = [
            rumps.MenuItem("Open dashboard", callback=self.open_dashboard),
            rumps.MenuItem("Paste a conversation…", callback=self.open_dashboard),
            None,
            self.summary,
            self.register,
            None,
            self.sessions_header,
            None,
        ]
        # A read connection of our own to the same file; the server owns the writer.
        self._conn = db.connect(DB_PATH)
        self._session_items: list[rumps.MenuItem] = []
        self._timer = rumps.Timer(self.refresh, POLL_SECONDS)
        self._timer.start()
        self.refresh(None)

    # -- actions ------------------------------------------------------------
    def open_dashboard(self, _) -> None:
        webbrowser.open(BASE)

    # -- polling ------------------------------------------------------------
    def refresh(self, _) -> None:
        try:
            s = server.status_report(self._conn, server.SERVE_STARTED)
        except Exception as exc:  # noqa: BLE001 — a poll must never crash the app
            self.title = "🛰 ⚠"
            self.summary.title = f"status unavailable: {type(exc).__name__}"
            return

        # Badge: red with a count when any session is in alert, amber for watch, else plain.
        if s["n_alert"]:
            self.title = f"🛰 🔴{s['n_alert']}"
        elif s["n_watch"]:
            self.title = f"🛰 🟡{s['n_watch']}"
        else:
            self.title = "🛰"

        up = _fmt_uptime(s.get("uptime_s"))
        self.summary.title = (f"{s['n_sessions']} session(s) · {s['n_turns']} turn(s)"
                              + (f" · {s['n_alert']} alert" if s['n_alert'] else "")
                              + (f" · up {up}" if up else ""))
        if s["embedding_trustworthy"]:
            self.register.title = f"Register: {s['embedding_backend']} ✓ (semantic)"
        else:
            self.register.title = "Register: lexicon — may under-read (set minilm)"

        # Rebuild the recent-session lines under the header, newest first.
        for item in self._session_items:
            if item.title in self.menu:
                del self.menu[item.title]
        self._session_items = []
        glyph = {"alert": "🔴", "watch": "🟡", "quiet": "🟢"}
        after = self.sessions_header.title
        for sess in s["sessions"][:6]:
            g = glyph.get(sess["drift"], "·")
            label = f"{g} {sess['label'][:28]} ({sess['n_turns']})"
            item = rumps.MenuItem(label, callback=self.open_dashboard)
            # Chain each after the previous so the list stays newest-first (insert_after the
            # header every time would reverse it).
            self.menu.insert_after(after, item)
            after = item.title
            self._session_items.append(item)
        if not s["sessions"]:
            item = rumps.MenuItem("no sessions yet — stream one in", callback=None)
            self.menu.insert_after(self.sessions_header.title, item)
            self._session_items.append(item)


def _fmt_uptime(seconds) -> str:
    if not seconds:
        return ""
    m = int(seconds // 60)
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h{m % 60:02d}m"


def main() -> int:
    # Create the database and its tables up front (idempotent), so the app's read connection
    # and the first status poll never race the server thread creating the file.
    db.init_db(DB_PATH)
    _start_server()
    ExplorerApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
