"""The loader's sequence — check, fetch, verify, start — with no window attached.

The window (`app.py`) and the headless command (`python -m explorer_loader --headless`) both
drive this, so the sequence a tester's app runs is the one the tests run. Progress goes out
through `emit(step, state, detail)`; the window renders it, the command prints it.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path
from typing import Callable

from . import DEFAULT_REPO, LOADER_VERSION, appupdate, github, launch
from .store import BUNDLED_ID, Store, VerifyError, read_version, verify

Emit = Callable[[str, str, str], None]
_HERE = object()                            # "the app this process runs from"


class Cancelled(Exception):
    pass


class Loader:
    def __init__(self, store: Store, bundled_dir: Path | None, emit: Emit | None = None,
                 api: str = github.API):
        self.store = store
        self.bundled_dir = bundled_dir
        self.emit = emit or (lambda step, state, detail="": None)
        self.api = api
        self.skip = threading.Event()       # the window's "Skip update" button
        self.stop = threading.Event()       # ends the background checks
        self.running: dict | None = None
        self.pending_update: dict | None = None
        self._app_lock = threading.Lock()

    # -- settings --------------------------------------------------------------------------

    @property
    def repo(self) -> str:
        return os.environ.get("EXPLORER_REPO") or self.store.state.get("repo") or DEFAULT_REPO

    @property
    def channel(self) -> str:
        return os.environ.get("EXPLORER_CHANNEL") or self.store.state.get("channel") or "stable"

    def token(self) -> str | None:
        """A read-only GitHub token, needed only while the repository is private."""
        env = os.environ.get("EXPLORER_GITHUB_TOKEN")
        if env:
            return env.strip()
        try:
            return (self.store.dir / "token").read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def set_token(self, token: str | None) -> None:
        f = self.store.dir / "token"
        if not token:
            f.unlink(missing_ok=True)
            return
        f.write_text(token.strip(), encoding="utf-8")
        f.chmod(stat.S_IRUSR | stat.S_IWUSR)      # 0600: readable by this user only

    # -- the sequence ----------------------------------------------------------------------

    def check(self, install: bool = True) -> dict:
        """Ask the channel what is current; download and verify it if it is new.

        Returns {'status': 'current'|'installed'|'failed'|'offline', ...}. Never raises for a
        network or verification problem — the Explorer still starts on what is installed.
        """
        self.emit("check", "active", f"{self.channel} channel · {self.repo}")
        try:
            target = github.resolve(self.repo, self.channel, self.token(), api=self.api)
        except github.UpdateError as e:
            self.emit("check", "warn", str(e))
            return {"status": "offline", "error": str(e)}
        self.store.state["last_check"] = time.time()
        self.store.save()
        note = f"{target.label}" + (f" — {target.fallback_note}" if target.fallback_note else "")

        if target.id in self.store.state["bad"]:
            self.emit("check", "warn", f"{target.label} failed to start before; staying on the last good version")
            return {"status": "failed", "target": target.label, "error": "previously failed"}
        if target.id == self.store.state.get("current") and self.store.path_of(target.id):
            self.emit("check", "done", f"up to date · {note}")
            return {"status": "current", "target": target.label}
        if not install:
            self.emit("check", "done", f"update available · {note}")
            return {"status": "available", "target": target.label}

        self.emit("check", "done", f"new version · {note}")
        tar = self.store.downloads / f"{target.id}.tar.gz"
        self.skip.clear()
        self.emit("download", "active", target.label)

        def progress(got: int, total: int | None) -> None:
            if self.skip.is_set():
                raise Cancelled()
            pct = f"{100 * got // total}%" if total else f"{got // 1024} KB"
            self.emit("download", "active", f"{target.label} · {pct}")

        try:
            github.download(target, tar, self.token(), progress=progress)
        except Cancelled:
            tar.unlink(missing_ok=True)
            self.emit("download", "warn", "skipped — starting the installed version")
            return {"status": "skipped"}
        except github.UpdateError as e:
            self.emit("download", "warn", str(e))
            return {"status": "offline", "error": str(e)}
        self.emit("download", "done", target.label)

        self.emit("verify", "active", "checking it will run on this app")
        try:
            path = self.store.install_tarball(tar, target.id)
            version = read_version(path)
        except (VerifyError, OSError) as e:
            self.emit("verify", "warn", f"not installed: {e}")
            self.store.mark_bad(target.id, str(e))
            return {"status": "failed", "target": target.label, "error": str(e)}
        self.store.record(target.id, {"version": version, "sha": target.sha, "ref": target.ref,
                                      "channel": target.channel, "label": target.label,
                                      "installed_at": time.time()})
        self.store.set_current(target.id)
        self.store.prune()
        self.emit("verify", "done", f"v{version} ({target.label})")
        return {"status": "installed", "target": target.label, "version": version}

    def start(self) -> dict:
        """Start the best available version: current, else last good, else the bundled copy."""
        failures = []
        for vid in self.store.candidates():
            tree = self.store.path_of(vid, self.bundled_dir)
            if tree is None:
                continue
            label = self.store.state["versions"].get(vid, {}).get("label") or vid
            self.emit("start", "active", f"starting {label}")
            try:
                if vid == BUNDLED_ID:
                    verify(tree)
                db = self.store.state.get("db_path")
                info = launch.start(tree, self.store.root, Path(db) if db else None)
            except (launch.StartError, VerifyError) as e:
                failures.append(f"{label}: {str(e).splitlines()[0]}")
                self.store.mark_bad(vid, str(e))
                self.emit("start", "warn", f"{label} would not start — trying the previous version")
                continue
            self.store.mark_good(vid)
            if vid == BUNDLED_ID:
                self.store.set_current(BUNDLED_ID)
            info.update(id=vid, label=label, channel=self.channel, loader=LOADER_VERSION)
            self.running = info
            self.emit("start", "done", f"v{info['version']} · {label}")
            return info
        raise launch.StartError("no installed version would start:\n" + "\n".join(failures))

    # -- the app itself (appupdate.py) --------------------------------------------------------

    @property
    def app_update_dir(self) -> Path:
        return self.store.dir / "app-update"

    def check_app(self, stage: bool = True, app=_HERE) -> dict:
        """Is a newer app released? If this copy can be replaced, download and stage it.

        Returns {'status': 'none' | 'staged' | 'available' | 'manual' | 'unsupported' |
        'offline' | 'failed' | 'source', ...}. 'source' means this is a checkout, not an app,
        so there is nothing to replace. Never raises: the Explorer runs whatever happens."""
        app = appupdate.running_app() if app is _HERE else app
        if app is None:
            return {"status": "source", "reason": appupdate.installable(None)}
        with self._app_lock:
            try:
                upd = appupdate.find(self.repo, self.token(), api=self.api)
            except github.UpdateError as e:
                return {"status": "offline", "error": str(e)}
            if upd is None:
                return {"status": "none", "version": LOADER_VERSION}
            base = {"version": upd.version, "tag": upd.tag, "page": upd.page,
                    "download": appupdate.download_page(self.repo), "from": LOADER_VERSION}
            why = appupdate.supported_here(upd)
            if why:
                return {**base, "status": "unsupported", "reason": why}
            why = appupdate.installable(app)
            if why:
                return {**base, "status": "manual", "reason": why}
            staged = self.store.state.get("app_update") or {}
            if staged.get("version") == upd.version and Path(staged.get("path") or "/-").is_dir():
                return {**base, "status": "staged", "path": staged["path"]}
            if not stage:
                return {**base, "status": "available"}
            work = self.app_update_dir
            try:
                z = appupdate.download(upd, work / "download.zip", self.token())
                path = appupdate.stage(z, work / "staged", upd, current=app)
            except github.UpdateError as e:
                return {**base, "status": "failed", "error": str(e)}
            finally:
                (work / "download.zip").unlink(missing_ok=True)
            self.store.state["app_update"] = {"version": upd.version, "tag": upd.tag,
                                              "path": str(path), "page": upd.page}
            self.store.save()
            return {**base, "status": "staged", "path": str(path)}

    def install_app_update(self, relaunch: bool, app=_HERE, spawn=None) -> dict:
        """Hand a staged app update to the installer script; the caller then quits.

        If a check is still downloading or staging, this does nothing rather than make the
        user wait at quit. The next quit installs it."""
        app = appupdate.running_app() if app is _HERE else app
        if not self._app_lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            return self._install_app_update(relaunch, app, spawn)
        finally:
            self._app_lock.release()

    def _install_app_update(self, relaunch: bool, app, spawn) -> dict:
        staged = self.store.state.get("app_update") or {}
        path = Path(staged.get("path") or "/-")
        if not staged or not path.is_dir() or (
                appupdate.parse_version(staged.get("version", "")) <=
                appupdate.parse_version(LOADER_VERSION)):
            if staged:
                self.store.state["app_update"] = None
                self.store.save()
            return {"status": "none"}
        why = appupdate.installable(app)
        if why:
            return {"status": "manual", "reason": why}
        kwargs = {"spawn": spawn} if spawn else {}
        appupdate.install(path, app, self.app_update_dir / "status", relaunch, **kwargs)
        self.store.state["app_update"] = None
        self.store.save()
        return {"status": "installing", "version": staged.get("version"), "relaunch": relaunch}

    def last_app_install(self) -> str | None:
        """What the last app install left behind, read once: "installed" or "failed: …"."""
        return appupdate.read_status(self.app_update_dir / "status")

    def background_checks(self, every_s: float, on_ready: Callable[[dict], None]) -> None:
        """While the app runs, look for updates now and then; download and verify them, and
        report one as ready. It is applied at the next launch, never under a running session."""
        def loop() -> None:
            while not self.stop.wait(every_s):
                if not self.store.state.get("auto_update", True):
                    continue
                res = self.check(install=True)
                if res.get("status") == "installed":
                    self.pending_update = res
                    on_ready(res)
        threading.Thread(target=loop, name="loader-updates", daemon=True).start()
