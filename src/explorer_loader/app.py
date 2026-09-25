"""The loader window: progress while it updates, then the Explorer in the same window.

One native window (pywebview — WKWebView on macOS). It opens on the loader page, which
shows the four steps as they happen; when the Explorer is up, the same window navigates to
it. Two menus stay available while you work: **Updates** (check now, switch between stable
releases and development code, restart) and **Data** (open the data folder, use another
database). An update found while you work is downloaded and verified in the background and
announced in the Explorer; it is applied at the next launch, never under a running session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import LOADER_VERSION, github, launch
from .core import Loader
from .store import Store

STEPS = ("check", "download", "verify", "start")
UPDATE_EVERY_S = 30 * 60


class Controller:
    def __init__(self, store: Store, bundled: Path | None, api: str = github.API):
        self.store = store
        self.lock = threading.Lock()
        self.steps = {k: {"state": "pending", "detail": ""} for k in STEPS}
        self.error: str | None = None
        self.window = None
        self.loader = Loader(store, bundled, self.on_emit, api=api)

    # -- progress --------------------------------------------------------------------------

    def on_emit(self, step: str, state: str, detail: str = "") -> None:
        with self.lock:
            if step in self.steps:
                self.steps[step] = {"state": state, "detail": detail}

    def snapshot(self) -> dict:
        st = self.store.state
        cur = st["versions"].get(st.get("current") or "", {})
        with self.lock:
            return {
                "steps": {k: dict(v) for k, v in self.steps.items()},
                "error": self.error,
                "channel": self.loader.channel, "repo": self.loader.repo,
                "loader_version": LOADER_VERSION,
                "current_label": cur.get("label") or (st.get("current") or ""),
                "auto_update": st.get("auto_update", True),
                "token_set": bool(self.loader.token()),
                "db_path": st.get("db_path"),
                "data_dir": str(self.store.root),
            }

    # -- the launch sequence ---------------------------------------------------------------

    def sequence(self) -> None:
        self.error = None
        for k in STEPS:
            self.on_emit(k, "pending", "")
        try:
            if self.store.state.get("auto_update", True):
                self.loader.check()
            else:
                self.on_emit("check", "done", "automatic updates are off")
            if self.steps["download"]["state"] == "pending":
                self.on_emit("download", "done", "nothing to download")
            if self.steps["verify"]["state"] == "pending":
                self.on_emit("verify", "done", "installed version already verified")
            info = self.loader.start()
        except launch.StartError as e:
            self.error = str(e)
            return
        time.sleep(0.5)                     # long enough to read "started"
        self.window.set_title(f"AI Safety Explorer {info['version']} — {info['label']}")
        self.window.load_url(info["url"])
        self.loader.background_checks(UPDATE_EVERY_S, self.update_ready)

    def toast(self, html: str, tone: str = "") -> None:
        if self.window:
            self.window.evaluate_js(
                f"typeof toast === 'function' && toast({json.dumps(html)}, {json.dumps(tone)})")

    def update_ready(self, res: dict) -> None:
        self.toast(f"Update ready: <b>{res.get('target')}</b> (v{res.get('version')}). "
                   "It starts next launch — or now from <b>Updates → Restart</b>.", "good")

    # -- menu and page actions -------------------------------------------------------------

    def check_now(self) -> None:
        def run() -> None:
            res = self.loader.check(install=True)
            s = res.get("status")
            if s == "installed":
                self.update_ready(res)
            elif s == "current":
                self.toast(f"Up to date — {res.get('target')}.", "good")
            elif s == "skipped":
                self.toast("Update skipped.")
            else:
                self.toast(f"Could not update: {res.get('error') or s}.", "bad")
        threading.Thread(target=run, daemon=True).start()

    def switch_channel(self, channel: str) -> None:
        self.store.state["channel"] = channel
        self.store.save()
        self.toast(f"Updates now come from the <b>{channel}</b> channel — restarting…")
        threading.Timer(1.2, self.relaunch).start()

    def open_data(self) -> None:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(self.store.root)])

    def choose_db(self) -> str | None:
        import webview
        picked = self.window.create_file_dialog(
            webview.FileDialog.OPEN, directory=str(Path.home()),
            file_types=("Explorer database (*.db)", "All files (*.*)"))
        if picked:
            self.store.state["db_path"] = str(picked[0])
            self.store.save()
            return str(picked[0])
        return None

    def relaunch(self) -> None:
        """Start a fresh copy of the app, then close this one."""
        self.loader.stop.set()
        res = os.environ.get("RESOURCEPATH")
        if res and sys.platform == "darwin":
            bundle = Path(res).parents[1]               # …/X.app/Contents/Resources -> X.app
            subprocess.Popen(["/bin/sh", "-c", f'sleep 1; open -n "{bundle}"'])
        else:
            subprocess.Popen([sys.executable, "-m", "explorer_loader"])
        if self.window:
            self.window.destroy()


class PageApi:
    """What the loader page may call (window.pywebview.api.*)."""

    def __init__(self, ctl: Controller):
        self._ctl = ctl

    def state(self) -> dict:
        return self._ctl.snapshot()

    def skip(self) -> None:
        self._ctl.loader.skip.set()

    def retry(self) -> None:
        threading.Thread(target=self._ctl.sequence, daemon=True).start()

    def choose_db(self) -> str | None:
        return self._ctl.choose_db()

    def clear_token(self) -> None:
        self._ctl.loader.set_token(None)

    def save_settings(self, channel: str, auto_update: bool, token: str) -> None:
        st = self._ctl.store.state
        st["channel"] = channel if channel and channel != "branch:" else "stable"
        st["auto_update"] = bool(auto_update)
        self._ctl.store.save()
        if token:
            self._ctl.loader.set_token(token)

    def relaunch(self) -> None:
        self._ctl.relaunch()


def run_window(store: Store, bundled: Path | None, api: str = github.API) -> int:
    try:
        import webview
        from webview.menu import Menu, MenuAction, MenuSeparator
    except ImportError:
        print("The loader window needs pywebview: pip install pywebview — or run "
              "`python -m explorer_loader --headless`.", file=sys.stderr)
        return 2

    ctl = Controller(store, bundled, api=api)
    html = (Path(__file__).parent / "loader.html").read_text(encoding="utf-8")
    ctl.window = webview.create_window("AI Safety Explorer", html=html, js_api=PageApi(ctl),
                                       width=1280, height=860, min_size=(900, 600))
    menu = [
        Menu("Updates", [
            MenuAction("Check for Updates Now", ctl.check_now),
            MenuSeparator(),
            MenuAction("Use Stable Releases", lambda: ctl.switch_channel("stable")),
            MenuAction("Use Latest Development Code", lambda: ctl.switch_channel("dev")),
            MenuSeparator(),
            MenuAction("Restart", ctl.relaunch),
        ]),
        Menu("Data", [
            MenuAction("Open Data Folder", ctl.open_data),
            MenuAction("Use a Different Database…", lambda: ctl.choose_db() and ctl.relaunch()),
        ]),
    ]
    # Persistent web storage, so the Explorer's reading preferences (density, text size,
    # collapsed panels) survive a restart; pywebview's default private mode forgets them.
    storage = store.dir / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    webview.start(ctl.sequence, menu=menu, private_mode=False, storage_path=str(storage))
    ctl.loader.stop.set()
    return 0
