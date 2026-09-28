"""The app replaces itself with a newer release's app (explorer_loader/appupdate.py).

Everything here runs on any OS against a stand-in GitHub and a stand-in app bundle. The
signature checks (`ditto`, `codesign`) run for real only on macOS; here they are simulated
where a test needs them. CI also installs a real update into a real built app
(packaging/selfupdate_e2e.sh).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import plistlib
import socket
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from explorer_loader import LOADER_VERSION, appupdate, github
from explorer_loader.core import Loader
from explorer_loader.store import Store


def app_zip(version: str, bundle_id: str = appupdate.BUNDLE_ID, name: str = "AI Safety Explorer.app",
            extra_app: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in [name] + (["Other.app"] if extra_app else []):
            z.writestr(f"{n}/Contents/Info.plist", plistlib.dumps(
                {"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version}))
            z.writestr(f"{n}/Contents/MacOS/AI Safety Explorer", b"#!/bin/sh\necho new\n")
            z.writestr(f"{n}/Contents/Resources/marker", version.encode())
    return buf.getvalue()


class FakeRelease:
    """GitHub's releases/latest with assets, and asset downloads that redirect, like GitHub."""

    def __init__(self):
        self.tag = "v9.0.0"
        self.zip = app_zip("9.0.0")
        self.manifest: dict | None = self.manifest_for(self.zip, "9.0.0")
        self.seen: list[tuple[str, str | None]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="application/json", headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.seen.append((self.path, self.headers.get("Authorization")))
                base = f"http://127.0.0.1:{fake.port}"
                if self.path == "/repos/o/r/releases/latest":
                    assets = [{"name": appupdate.ZIP, "url": f"{base}/repos/o/r/releases/assets/2",
                               "browser_download_url": f"{base}/o/r/releases/download/v9/2"}]
                    if fake.manifest is not None:
                        assets.append({"name": appupdate.MANIFEST,
                                       "url": f"{base}/repos/o/r/releases/assets/1",
                                       "browser_download_url": f"{base}/o/r/releases/download/v9/1"})
                    return self.send(200, {"tag_name": fake.tag, "assets": assets, "body": "notes",
                                           "html_url": "https://github.com/o/r/releases/tag/v9"})
                if self.path.startswith(("/repos/o/r/releases/assets/", "/o/r/releases/download/")):
                    n = self.path.rsplit("/", 1)[1]
                    return self.send(302, b"", headers={"Location": f"{base}/signed/{n}?sig=x"})
                if self.path == "/signed/1?sig=x":
                    return self.send(200, json.dumps(fake.manifest).encode())
                if self.path == "/signed/2?sig=x":
                    return self.send(200, fake.zip, "application/zip")
                self.send(404, {"message": "Not Found"})

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.api = f"http://127.0.0.1:{self.port}"

    @staticmethod
    def manifest_for(data: bytes, version: str) -> dict:
        return {"loader_version": version, "min_macos": "11.0",
                "files": {appupdate.ZIP: {"sha256": hashlib.sha256(data).hexdigest(),
                                          "size": len(data)}}}

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def rel():
    f = FakeRelease()
    yield f
    f.close()


def installed_app(tmp_path: Path, version: str = LOADER_VERSION) -> Path:
    app = tmp_path / "Applications" / "AI Safety Explorer.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": appupdate.BUNDLE_ID, "CFBundleShortVersionString": version}))
    (app / "Contents" / "Resources" / "marker").write_text(version)
    return app


# -- finding ------------------------------------------------------------------------------------

def test_a_newer_launcher_in_the_latest_release_is_an_update(rel):
    upd = appupdate.find("o/r", api=rel.api, current="1.1.0")
    assert upd.version == "9.0.0" and upd.tag == "v9.0.0"
    assert upd.size == len(rel.zip) and upd.sha256 == hashlib.sha256(rel.zip).hexdigest()
    assert upd.page.endswith("/releases/tag/v9")


def test_the_same_or_an_older_launcher_is_not_an_update(rel):
    assert appupdate.find("o/r", api=rel.api, current="9.0.0") is None
    assert appupdate.find("o/r", api=rel.api, current="10.0") is None


def test_a_release_without_a_manifest_offers_nothing(rel):
    rel.manifest = None                   # every release before apps updated themselves
    assert appupdate.find("o/r", api=rel.api, current="1.0.0") is None


def test_the_token_is_not_forwarded_to_the_signed_download_url(rel):
    appupdate.find("o/r", token="secret", api=rel.api, current="1.0.0")
    auth = dict(rel.seen)
    assert auth["/repos/o/r/releases/assets/1"] == "Bearer secret"     # private: the API URL
    assert auth["/signed/1?sig=x"] is None


def test_a_public_repository_downloads_outside_the_api_rate_limit(rel, tmp_path):
    upd = appupdate.find("o/r", api=rel.api, current="1.0.0")
    appupdate.download(upd, tmp_path / "a.zip")
    paths = [p for p, _ in rel.seen]
    assert "/o/r/releases/download/v9/1" in paths and "/o/r/releases/download/v9/2" in paths
    assert not any(p.startswith("/repos/o/r/releases/assets/") for p in paths)


def test_versions_compare_as_numbers():
    assert appupdate.parse_version("1.10.0") > appupdate.parse_version("1.9.9")
    assert appupdate.parse_version("v2") == (2, 0, 0)
    assert appupdate.parse_version("junk") == (0, 0, 0)


# -- downloading --------------------------------------------------------------------------------

def test_the_download_must_match_the_manifest(rel, tmp_path):
    upd = appupdate.find("o/r", api=rel.api, current="1.0.0")
    got = []
    path = appupdate.download(upd, tmp_path / "a.zip", progress=lambda n, t: got.append((n, t)))
    assert path.read_bytes() == rel.zip and got[-1] == (len(rel.zip), len(rel.zip))

    good = rel.zip
    rel.zip = good + b"tampered"                      # the file grew; the manifest did not
    with pytest.raises(github.UpdateError, match="larger than the release says"):
        appupdate.download(upd, tmp_path / "b.zip")
    rel.zip = good[:-1] + bytes([good[-1] ^ 1])       # same size, one bit different
    with pytest.raises(github.UpdateError, match="does not match the release's checksum"):
        appupdate.download(upd, tmp_path / "c.zip")
    assert not (tmp_path / "b.zip").exists() and not (tmp_path / "c.zip").exists()


# -- staging ------------------------------------------------------------------------------------

def staged_from(tmp_path, data: bytes, version="9.0.0", current=None, **kw):
    z = tmp_path / "u.zip"
    z.write_bytes(data)
    upd = appupdate.AppUpdate(version=version, tag="v9", zip_url="", sha256="", size=0, page="")
    return appupdate.stage(z, tmp_path / "staged", upd, current=current, on_mac=False, **kw)


def test_staging_unpacks_the_app_and_checks_what_it_is(tmp_path):
    app = staged_from(tmp_path, app_zip("9.0.0"), current=installed_app(tmp_path))
    assert app.name == "AI Safety Explorer.app"
    assert (app / "Contents" / "Resources" / "marker").read_text() == "9.0.0"


@pytest.mark.parametrize("data,match", [
    (app_zip("9.0.0", bundle_id="com.example.other"), "not 'com.parkersphysics"),
    (app_zip("8.0.0"), "promised '9.0.0'"),
    (app_zip("9.0.0", extra_app=True), "one app, it holds 2"),
])
def test_staging_refuses_an_app_that_is_not_the_promised_one(tmp_path, data, match):
    with pytest.raises(github.UpdateError, match=match):
        staged_from(tmp_path, data)


def test_on_a_mac_the_signature_and_team_must_verify(tmp_path):
    current = installed_app(tmp_path)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[:2])
        if cmd[0] == "ditto":                         # unpack as ditto would
            with zipfile.ZipFile(cmd[3]) as z:
                z.extractall(cmd[4])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["codesign", "--verify"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        team = "AAAA111111" if str(current) in cmd[-1] else "EVIL000000"
        return SimpleNamespace(returncode=0, stdout="", stderr=f"TeamIdentifier={team}\n")

    z = tmp_path / "u.zip"
    z.write_bytes(app_zip("9.0.0"))
    upd = appupdate.AppUpdate(version="9.0.0", tag="v9", zip_url="", sha256="", size=0, page="")
    with pytest.raises(github.UpdateError, match="not signed by this app's developer"):
        appupdate.stage(z, tmp_path / "staged", upd, current=current, run=run, on_mac=True)
    assert ["ditto", "-x"] in calls and ["codesign", "--verify"] in calls


# -- where it may install -----------------------------------------------------------------------

def test_copies_that_cannot_replace_themselves_say_why(tmp_path):
    assert "not running from the app" in appupdate.installable(None)
    tr = Path("/private/var/folders/x/T/AppTranslocation/ABC/d/AI Safety Explorer.app")
    assert "Move it to Applications" in appupdate.installable(tr)
    assert "disk image" in appupdate.installable(Path("/Volumes/AI Safety Explorer/AI Safety Explorer.app"))
    app = installed_app(tmp_path)
    assert "may not change apps" in appupdate.installable(app, access=lambda p, m: False)
    assert appupdate.installable(app, access=lambda p, m: True) is None


# -- installing ---------------------------------------------------------------------------------

def run_installer(tmp_path, staged: Path, app: Path) -> str:
    status = tmp_path / "state" / "status"
    blocker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
    appupdate.install(staged, app, status, relaunch=False, pid=blocker.pid)
    assert not status.exists()                     # it waits for the app to quit
    blocker.wait()
    deadline = time.time() + 20
    while time.time() < deadline and not status.exists():
        time.sleep(0.1)
    time.sleep(0.2)
    return appupdate.read_status(status)


@pytest.mark.skipif(sys.platform == "win32", reason="the installer is a POSIX shell script")
def test_the_installer_swaps_the_app_once_it_has_quit(tmp_path):
    app = installed_app(tmp_path, "1.1.0")
    staged = staged_from(tmp_path, app_zip("9.0.0"))
    assert run_installer(tmp_path, staged, app) == "installed"
    assert (app / "Contents" / "Resources" / "marker").read_text() == "9.0.0"
    assert sorted(p.name for p in app.parent.iterdir()) == ["AI Safety Explorer.app"]


@pytest.mark.skipif(sys.platform == "win32", reason="the installer is a POSIX shell script")
def test_a_failed_install_puts_the_old_app_back_and_says_so(tmp_path):
    app = installed_app(tmp_path, "1.1.0")
    note = run_installer(tmp_path, tmp_path / "no-such-staged.app", app)
    assert note.startswith("failed: could not move the new app into place")
    assert (app / "Contents" / "Resources" / "marker").read_text() == "1.1.0"
    assert sorted(p.name for p in app.parent.iterdir()) == ["AI Safety Explorer.app"]


# -- the loader's view of it --------------------------------------------------------------------

@pytest.fixture
def loader(rel, tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    store.state["repo"] = "o/r"
    monkeypatch.setattr(appupdate, "installable", lambda app, access=os.access: None)
    real_stage = appupdate.stage
    monkeypatch.setattr(appupdate, "stage",
                        lambda z, w, u, current=None: real_stage(z, w, u, current, on_mac=False))
    return Loader(store, None, api=rel.api)


def test_check_app_downloads_and_stages_once_then_installs(loader, rel, tmp_path):
    app = installed_app(tmp_path)
    res = loader.check_app(app=app)
    assert res["status"] == "staged" and res["version"] == "9.0.0" and res["from"] == LOADER_VERSION
    assert (Path(res["path"]) / "Contents" / "Resources" / "marker").read_text() == "9.0.0"
    downloads = sum(1 for p, _ in rel.seen if p == "/signed/2?sig=x")
    assert loader.check_app(app=app)["status"] == "staged"            # not downloaded again
    assert loader.check_app(app=None)["status"] == "source"           # a checkout: nothing to do
    assert sum(1 for p, _ in rel.seen if p == "/signed/2?sig=x") == downloads
    assert not (loader.app_update_dir / "download.zip").exists()

    spawned = []
    out = loader.install_app_update(relaunch=True, app=app, spawn=lambda cmd, **kw: spawned.append(cmd))
    assert out == {"status": "installing", "version": "9.0.0", "relaunch": True}
    assert spawned[0][0] == "/bin/sh" and spawned[0][3:6] == [res["path"], str(app), "1"]
    assert loader.store.state["app_update"] is None                   # handed over, once
    assert loader.install_app_update(relaunch=False, app=app)["status"] == "none"


def test_quitting_while_an_update_downloads_does_not_wait_for_it(loader, tmp_path):
    loader._app_lock.acquire()
    try:
        assert loader.install_app_update(relaunch=False, app=installed_app(tmp_path)) == {
            "status": "busy"}
    finally:
        loader._app_lock.release()


def test_check_app_reports_a_copy_it_may_not_replace(loader, rel, tmp_path, monkeypatch):
    monkeypatch.setattr(appupdate, "installable", lambda app, access=os.access: "read-only")
    res = loader.check_app(app=installed_app(tmp_path))
    assert res["status"] == "manual" and res["reason"] == "read-only"
    assert res["download"].endswith("/releases/latest/download/AI-Safety-Explorer-macOS.dmg")
    assert not any(p == "/signed/2?sig=x" for p, _ in rel.seen)       # nothing downloaded


def test_check_app_says_up_to_date_and_survives_a_bad_download(loader, rel, tmp_path):
    rel.manifest = rel.manifest_for(rel.zip, LOADER_VERSION)
    assert loader.check_app(app=installed_app(tmp_path))["status"] == "none"
    rel.manifest = rel.manifest_for(b"something else", "9.0.0")
    res = loader.check_app(app=tmp_path / "Applications" / "AI Safety Explorer.app")
    assert res["status"] == "failed"
    assert "larger than the release says" in res["error"] or "checksum" in res["error"]
    assert loader.store.state.get("app_update") is None


def test_the_update_app_command_prints_what_it_did(rel, tmp_path, capsys, monkeypatch):
    from explorer_loader import __main__ as entry
    monkeypatch.delenv("RESOURCEPATH", raising=False)
    code = entry.main(["--update-app", "--repo", "o/r", "--api", rel.api,
                       "--data-dir", str(tmp_path / "data")])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and out["app_update"]["status"] == "source"
    assert "not running from the app" in out["app_update"]["reason"]


# -- the window's side: what it says, and Restart installing it -----------------------------------

class FakeWindow:
    def __init__(self):
        self.js: list[str] = []
        self.destroyed = False

    def evaluate_js(self, js):
        self.js.append(js)

    def destroy(self):
        self.destroyed = True


def test_the_window_announces_an_app_update_once_and_restart_installs_it(
        loader, tmp_path, monkeypatch):
    from explorer_loader.app import Controller
    ctl = Controller(loader.store, None, api=loader.api)
    ctl.window = FakeWindow()
    ctl.app_update_found({"status": "staged", "version": "9.0.0"})
    ctl.app_update_found({"status": "staged", "version": "9.0.0"})
    assert len(ctl.window.js) == 1 and "App update ready" in ctl.window.js[0]
    ctl.app_update_found({"status": "failed", "error": "x"})          # quiet unless asked
    ctl.app_update_found({"status": "source"}, asked=True)            # a checkout: quiet
    assert len(ctl.window.js) == 1
    ctl.app_update_found({"status": "manual", "version": "9.0.0",
                          "reason": "<in /Volumes & co>", "download": "https://x/dmg"})
    assert "&lt;in /Volumes &amp; co&gt;" in ctl.window.js[-1] and "https://x/dmg" in ctl.window.js[-1]

    app = installed_app(tmp_path)
    ctl.loader.check_app(app=app)
    monkeypatch.setattr(appupdate, "running_app", lambda: app)
    installs = []
    monkeypatch.setattr(appupdate, "install",
                        lambda staged, a, status, relaunch, **kw: installs.append((a, relaunch)))
    ctl.relaunch()
    assert installs == [(app, True)] and ctl.window.destroyed
