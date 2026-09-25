"""The loader: fetch a version from GitHub, prove it will run, start it — or fall back.

A stand-in for api.github.com serves releases, branch heads and source tarballs built from
this repository's own tree (with the version string patched so the test can tell which copy
is running). Broken variants — a syntax error, a newer loader contract, a missing dependency,
a path-traversal archive, code that fails at import — each have to be refused or rolled back
without the user ever being left with nothing to start.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from explorer_loader import github
from explorer_loader.core import Loader
from explorer_loader.store import BUNDLED_ID, KEEP, Store, VerifyError, verify

ROOT = Path(__file__).resolve().parents[1]
SHA = "1b98699" + "0" * 33


def build_tarball(prefix: str, version: str = "9.9.9", patch: dict[str, str] | None = None,
                  extra: list[tuple[str, bytes]] | None = None) -> bytes:
    """A GitHub-style source tarball of this repo's src/, corpus/ and pyproject.toml."""
    buf = io.BytesIO()
    patch = patch or {}
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(rel: str, data: bytes) -> None:
            info = tarfile.TarInfo(f"{prefix}/{rel}")
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))

        for base in ("src/safety_explorer", "corpus"):
            for p in sorted((ROOT / base).rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    rel = p.relative_to(ROOT).as_posix()
                    data = p.read_bytes()
                    if rel == "src/safety_explorer/__init__.py":
                        data = re.sub(rb'__version__ = "[^"]+"', f'__version__ = "{version}"'.encode(), data)
                    if rel in patch:
                        data = data + patch[rel].encode()
                    add(rel, data)
        py = (ROOT / "pyproject.toml").read_text()
        if "pyproject.toml" in patch:
            py = py + patch["pyproject.toml"]
        add("pyproject.toml", py.encode())
        for name, data in extra or []:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeGitHub:
    """Just enough of the GitHub REST API for the loader."""

    def __init__(self):
        self.release: str | None = None
        self.head = SHA
        self.tarballs: dict[str, bytes] = {}
        self.private = False
        self.token_seen: list[str | None] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.token_seen.append(self.headers.get("Authorization"))
                if fake.private and self.headers.get("Authorization") != "Bearer good-token":
                    return self.send(404, {"message": "Not Found"})
                p = self.path
                if p == "/repos/o/r":
                    return self.send(200, {"default_branch": "main"})
                if p == "/repos/o/r/releases/latest":
                    if not fake.release:
                        return self.send(404, {"message": "Not Found"})
                    return self.send(200, {"tag_name": fake.release, "published_at": "2026-09-25T00:00:00Z",
                                           "body": "notes"})
                if p.startswith("/repos/o/r/commits/"):
                    branch = p.rsplit("/", 1)[1]
                    if branch not in ("main", "feature"):
                        return self.send(404, {"message": "No commit found"})
                    return self.send(200, {"sha": fake.head, "commit": {
                        "message": "a change\n\nbody", "committer": {"date": "2026-09-25T00:00:00Z"}}})
                if p.startswith("/repos/o/r/tarball/"):
                    ref = p.rsplit("/", 1)[1]
                    if ref in fake.tarballs:
                        return self.send(200, fake.tarballs[ref], "application/x-gzip")
                    return self.send(404, {"message": "Not Found"})
                self.send(404, {"message": "Not Found"})

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.api = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def gh():
    f = FakeGitHub()
    yield f
    f.close()


# --- channels --------------------------------------------------------------------------------

def test_stable_follows_the_latest_release(gh):
    gh.release = "v9.9.9"
    t = github.resolve("o/r", "stable", api=gh.api)
    assert (t.id, t.label, t.channel, t.notes) == ("v9.9.9", "v9.9.9", "stable", "notes")
    assert t.tarball_url.endswith("/tarball/v9.9.9")


def test_stable_without_a_release_follows_the_branch_and_says_so(gh):
    t = github.resolve("o/r", "stable", api=gh.api)
    assert t.channel == "dev" and t.id == SHA[:12] and "no release" in t.fallback_note


def test_dev_and_branch_channels(gh):
    assert github.resolve("o/r", "dev", api=gh.api).label == f"main · {SHA[:7]}"
    assert github.resolve("o/r", "branch:feature", api=gh.api).ref == "feature"
    with pytest.raises(github.UpdateError, match="not found"):
        github.resolve("o/r", "branch:nope", api=gh.api)


def test_a_private_repo_needs_the_token(gh):
    gh.private = True
    with pytest.raises(github.UpdateError, match="private without a token"):
        github.resolve("o/r", "dev", api=gh.api)
    assert github.resolve("o/r", "dev", token="good-token", api=gh.api).sha == SHA


def test_offline_is_an_update_error_not_a_crash():
    with pytest.raises(github.UpdateError, match="offline"):
        github.resolve("o/r", "dev", api="http://127.0.0.1:9", timeout=2)


# --- verification ----------------------------------------------------------------------------

def unpack(tmp_path, data: bytes) -> Path:
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(tmp_path, filter="data") if hasattr(tarfile, "data_filter") else tf.extractall(tmp_path)
    return next(p for p in tmp_path.iterdir() if p.is_dir())


def test_this_repository_verifies():
    assert verify(ROOT)["api"] == 1


def test_a_syntax_error_is_refused(tmp_path):
    tree = unpack(tmp_path, build_tarball("o-r-x", patch={"src/safety_explorer/lint.py": "\ndef broken(:\n"}))
    with pytest.raises(VerifyError, match="does not compile"):
        verify(tree)


def test_a_newer_loader_contract_is_refused(tmp_path):
    tree = unpack(tmp_path, build_tarball("o-r-x"))
    py = tree / "pyproject.toml"
    assert "[tool.explorer-loader]\napi = 1" in py.read_text()
    py.write_text(py.read_text().replace("[tool.explorer-loader]\napi = 1", "[tool.explorer-loader]\napi = 2"))
    with pytest.raises(VerifyError, match="needs a newer app"):
        verify(tree)


def test_a_missing_dependency_is_refused(tmp_path):
    tree = unpack(tmp_path, build_tarball("o-r-x"))
    py = tree / "pyproject.toml"
    py.write_text(py.read_text().replace("dependencies = []", 'dependencies = ["surely-not-installed>=1"]'))
    with pytest.raises(VerifyError, match="surely-not-installed"):
        verify(tree)


def test_a_path_traversal_archive_is_refused(tmp_path):
    store = Store(tmp_path / "app")
    tar = tmp_path / "evil.tar.gz"
    tar.write_bytes(build_tarball("o-r-x", extra=[("../escaped.txt", b"x")]))
    with pytest.raises(VerifyError, match="unsafe path"):
        store.install_tarball(tar, "evil")
    assert not (tmp_path / "escaped.txt").exists()
    assert not (store.versions / "evil").exists()


# --- the sequence ----------------------------------------------------------------------------

def test_check_installs_verifies_and_is_idempotent(gh, tmp_path):
    gh.tarballs[SHA] = build_tarball("o-r-1b98699")
    store = Store(tmp_path / "app")
    store.state.update(repo="o/r", channel="dev")
    events = []
    loader = Loader(store, ROOT, lambda *e: events.append(e), api=gh.api)
    res = loader.check()
    assert res["status"] == "installed" and res["version"] == "9.9.9"
    assert store.state["current"] == SHA[:12]
    assert (store.versions / SHA[:12] / "src" / "safety_explorer" / "server.py").exists()
    assert [e[0] for e in events if e[1] == "done"] == ["check", "download", "verify"]
    assert loader.check()["status"] == "current"


def test_a_version_that_fails_verification_is_never_retried(gh, tmp_path):
    gh.tarballs[SHA] = build_tarball("o-r-1b98699", patch={"src/safety_explorer/db.py": "\nx = (\n"})
    store = Store(tmp_path / "app")
    store.state.update(repo="o/r", channel="dev")
    loader = Loader(store, ROOT, api=gh.api)
    assert loader.check()["status"] == "failed"
    assert SHA[:12] in store.state["bad"] and store.state["current"] is None
    assert loader.check()["status"] == "failed"          # not downloaded again


def test_candidates_and_prune(tmp_path):
    store = Store(tmp_path / "app")
    for i in range(KEEP + 3):
        (store.versions / f"v{i}").mkdir()
        os.utime(store.versions / f"v{i}", (i, i))
    store.state.update(current="v0", last_good="v1")
    assert store.candidates() == ["v0", "v1", BUNDLED_ID]
    store.mark_bad("v0", "boom")
    assert store.state["current"] == "v1" and store.candidates() == ["v1", BUNDLED_ID]
    store.prune()
    left = sorted(p.name for p in store.versions.iterdir())
    # The running version survives, plus the KEEP newest spares; the failed, oldest one goes.
    assert "v1" in left and "v0" not in left and len(left) == KEEP + 1


# --- end to end, in a subprocess (it imports the Explorer from the downloaded copy) ---------

def run_headless(gh, data_dir, channel="dev"):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "explorer_loader", "--headless", "--data-dir", str(data_dir),
         "--repo", "o/r", "--channel", channel, "--api", gh.api, "--bundled", str(ROOT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=str(ROOT))
    lines, running = [], None
    deadline = time.time() + 90
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip())
        if line.startswith('{"running"'):
            running = json.loads(line)["running"]
            break
    return proc, running, lines


def test_end_to_end_starts_the_downloaded_version(gh, tmp_path):
    gh.release = "v9.9.9"
    gh.tarballs["v9.9.9"] = build_tarball("o-r-abc1234", version="9.9.9")
    proc, running, lines = run_headless(gh, tmp_path / "app", channel="stable")
    try:
        assert running, "\n".join(lines[-30:])
        assert running["version"] == "9.9.9" and running["id"] == "v9.9.9"
        status = json.loads(urllib.request.urlopen(running["url"] + "/api/status", timeout=5).read())
        assert status["version"] == "9.9.9"
        assert (tmp_path / "app" / "explorer.db").exists()     # the shared database
    finally:
        proc.kill()
        proc.wait()


def test_a_version_that_will_not_start_rolls_back_to_the_bundled_copy(gh, tmp_path):
    # Compiles, verifies, and then fails the moment it is imported.
    gh.tarballs[SHA] = build_tarball("o-r-1b98699", version="9.9.9",
                                     patch={"src/safety_explorer/server.py": "\nraise RuntimeError('broken build')\n"})
    proc, running, lines = run_headless(gh, tmp_path / "app")
    try:
        assert running, "\n".join(lines[-30:])
        from safety_explorer import __version__
        assert running["id"] == BUNDLED_ID and running["version"] == __version__
        state = json.loads((tmp_path / "app" / "loader" / "state.json").read_text())
        assert SHA[:12] in state["bad"] and "broken build" in state["versions"][SHA[:12]]["failed"]
        assert any("would not start" in l for l in lines)
    finally:
        proc.kill()
        proc.wait()


# --- the loader page renders every state without a script error ------------------------------

def test_the_loader_page_renders_progress_and_errors():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    chromium = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    if not os.path.exists(chromium):
        pytest.skip("no chromium")
    html = (ROOT / "src" / "explorer_loader" / "loader.html").read_text()
    shim = """
      window.__S = {steps: {check: {state: 'done', detail: 'new version · v9.9.9'},
                            download: {state: 'active', detail: 'v9.9.9 · 42%'},
                            verify: {state: 'pending', detail: ''}, start: {state: 'pending', detail: ''}},
                    error: null, channel: 'stable', repo: 'o/r', loader_version: '1.0.0',
                    current_label: 'v9.9.8', auto_update: true, token_set: false, db_path: null,
                    data_dir: '/Users/x/Library/Application Support/AI Safety Explorer'};
      window.__calls = [];
      window.pywebview = {api: new Proxy({}, {get: (_, k) => async (...a) => {
        window.__calls.push(k); return k === 'state' ? window.__S : null; }})};
      setTimeout(() => window.dispatchEvent(new Event('pywebviewready')), 0);
    """
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=chromium)
        pg = b.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        # set_content does not run init scripts, so the bridge goes into the page itself.
        pg.set_content(html.replace("<script>", f"<script>{shim}</script><script>", 1))
        pg.wait_for_timeout(500)
        assert "active" in pg.get_attribute("li[data-step=download]", "class")
        assert pg.eval_on_selector("li[data-step=download] .bar > div", "e => e.style.width") == "42%"
        assert pg.is_visible("#skip")
        pg.click("#skip")
        pg.evaluate("() => { window.__S.error = 'no installed version would start'; "
                    "window.__S.steps.download = {state: 'warn', detail: 'skipped'}; }")
        pg.wait_for_timeout(400)
        assert pg.is_visible("#err") and pg.is_visible("#retry") and not pg.is_visible("#skip")
        assert "skip" in pg.evaluate("window.__calls")
        b.close()
    assert errors == []
