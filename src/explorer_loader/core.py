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

from . import DEFAULT_REPO, LOADER_VERSION, github, launch
from .store import BUNDLED_ID, Store, VerifyError, read_version, verify

Emit = Callable[[str, str, str], None]


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
            return (self.store.dir / "token").read_text().strip() or None
        except OSError:
            return None

    def set_token(self, token: str | None) -> None:
        f = self.store.dir / "token"
        if not token:
            f.unlink(missing_ok=True)
            return
        f.write_text(token.strip())
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
