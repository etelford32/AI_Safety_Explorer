"""The loader window: progress while it updates, then the Explorer in the same window.

One native window (pywebview — WKWebView on macOS). It opens on the loader page, which
shows the four steps as they happen; when the Explorer is up, the same window navigates to
it. Two menus stay available while you work: **Updates** (check now, switch between stable
releases and development code, restart) and **Data** (open the data folder, use another
database). An update found while you work is downloaded and verified in the background and
announced in the Explorer; it is applied at the next launch, never under a running session.

The app itself is kept current the same way (appupdate.py). A newer app in the latest
release is downloaded and checked in the background, and installed when you quit, or at
once with Updates → Restart.
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import LOADER_VERSION, appupdate, github, launch
from .core import Loader
from .store import Store

STEPS = ("check", "download", "verify", "start")
UPDATE_EVERY_S = 30 * 60
APP_CHECK_AFTER_S = 20                       # after the Explorer is up, not during the launch


class Controller:
    def __init__(self, store: Store, bundled: Path | None, api: str = github.API):
        self.store = store
        self.lock = threading.Lock()
        self.steps = {k: {"state": "pending", "detail": ""} for k in STEPS}
        self.error: str | None = None
        self.window = None
        self.loader = Loader(store, bundled, self.on_emit, api=api)
        self.app_told: set[str] = set()     # app-update versions already announced

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
        last_install = self.loader.last_app_install()
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
        self.window.set_title(f"AI Safety Explorer {info['version']} — by Elliot Telford")
        self.window.load_url(info["url"])
        self.loader.background_checks(UPDATE_EVERY_S, self.update_ready)
        if last_install:
            threading.Timer(4.0, self.report_app_install, args=(last_install,)).start()
        self.background_app_checks()

    # -- the app itself ----------------------------------------------------------------------

    def background_app_checks(self) -> None:
        def loop() -> None:
            wait = APP_CHECK_AFTER_S
            while not self.loader.stop.wait(wait):
                wait = UPDATE_EVERY_S
                if self.store.state.get("auto_update", True):
                    self.app_update_found(self.loader.check_app())
        threading.Thread(target=loop, name="loader-app-updates", daemon=True).start()

    def app_update_found(self, res: dict, asked: bool = False) -> None:
        """Say what a check for a newer app found: once per version, unless asked."""
        s, v = res.get("status"), html.escape(str(res.get("version")))
        reason = html.escape(str(res.get("reason") or res.get("error") or s))
        if s in ("none", "offline", "unsupported", "failed", "source") and not asked:
            return
        if not asked and f"{s}:{v}" in self.app_told:
            return
        self.app_told.add(f"{s}:{v}")
        if s == "staged":
            self.toast(f"App update ready: <b>{v}</b> (this app is {LOADER_VERSION}). It "
                       "installs when you quit, or now from <b>Updates → Restart</b>.", "good", 12000)
        elif s == "manual":
            self.toast(f"A new version of the app is available ({v}), but this copy can't "
                       f"replace itself: {reason} "
                       f"<a href=\"{res.get('download')}\" target=\"_blank\">Download it</a>.", "", 15000)
        elif s == "none":
            self.toast(f"The app is up to date ({LOADER_VERSION}).", "good")
        elif s == "source":
            return                              # a checkout: the code check below says enough
        elif s == "unsupported":
            self.toast(f"A new version of the app ({v}) is out, but {reason}.")
        else:
            self.toast(f"Could not check for a new app: {reason}.", "bad")

    def report_app_install(self, note: str) -> None:
        if note == "installed":
            self.toast(f"The app was updated to <b>{LOADER_VERSION}</b>.", "good")
        else:
            self.toast(f"The app update could not be installed ({html.escape(note)}). Your app is "
                       f"unchanged. <a href=\"{appupdate.download_page(self.loader.repo)}\" "
                       "target=\"_blank\">Download the new version</a>.", "bad", 15000)

    def toast(self, html: str, tone: str = "", ms: int = 3200) -> None:
        if self.window:
            self.window.evaluate_js(f"typeof toast === 'function' && "
                                    f"toast({json.dumps(html)}, {json.dumps(tone)}, {int(ms)})")

    def update_ready(self, res: dict) -> None:
        self.toast(f"Update ready: <b>{res.get('target')}</b> (v{res.get('version')}). "
                   "It starts next launch — or now from <b>Updates → Restart</b>.", "good")

    # -- menu and page actions -------------------------------------------------------------

    def check_now(self) -> None:
        def run() -> None:
            self.app_update_found(self.loader.check_app(), asked=True)
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
        """Start a fresh copy of the app, then close this one. A staged app update is
        installed on the way: the installer opens the new app once this one has quit."""
        self.loader.stop.set()
        if self.loader.install_app_update(relaunch=True).get("status") == "installing":
            if self.window:
                self.window.destroy()
            return
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
    # Quitting is when a staged app update goes in (a no-op if Restart already did it).
    ctl.loader.install_app_update(relaunch=False)
    return 0
