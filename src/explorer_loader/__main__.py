"""`python -m explorer_loader` — the loader window, or its sequence without one.

    python -m explorer_loader                  the window (what the .app runs)
    python -m explorer_loader --headless       check, update, start; print the URL; serve
    python -m explorer_loader --check-only     check and install an update, then exit

Options for trying channels and for tests: --channel stable|dev|branch:<name>, --repo
owner/name, --data-dir PATH, --api URL (a stand-in for api.github.com), --bundled DIR.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import LOADER_VERSION, github
from .core import Loader
from .store import Store


def ensure_utf8() -> None:
    """Make this process read and write text as UTF-8, whatever it was launched with.

    A Python embedded in an app bundle starts in the plain "C" locale — the UTF-8 coercion
    the `python` command applies (PEP 538) happens only for the command, not an embedded
    interpreter — so the default text encoding is ASCII, and the first file with an em dash in
    it fails to read. The Explorer reads many. Setting LC_CTYPE here fixes every later
    open()/read_text() in the process (they consult the current locale), stdout and stderr are
    re-encoded, and the environment is set for any child process.
    """
    import locale
    for name in ("en_US.UTF-8", "C.UTF-8", "UTF-8"):
        try:
            locale.setlocale(locale.LC_CTYPE, name)
            break
        except locale.Error:
            continue
    os.environ.setdefault("LANG", "en_US.UTF-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def bundled_dir() -> Path | None:
    """The Explorer copy baked into the app (offline fallback), or the checkout in dev."""
    res = os.environ.get("RESOURCEPATH")                     # set by py2app
    if res and (Path(res) / "baseline" / "src" / "safety_explorer").is_dir():
        return Path(res) / "baseline"
    root = Path(__file__).resolve().parents[2]              # src/explorer_loader -> repo
    if (root / "src" / "safety_explorer").is_dir() and (root / "corpus").is_dir():
        return root
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="explorer_loader")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--channel")
    p.add_argument("--repo")
    p.add_argument("--data-dir")
    p.add_argument("--api", default=github.API)
    p.add_argument("--bundled")
    p.add_argument("--no-update", action="store_true", help="start what is installed")
    p.add_argument("--version", action="version", version=f"explorer-loader {LOADER_VERSION}")
    a = p.parse_args(argv)
    ensure_utf8()

    store = Store(Path(a.data_dir) if a.data_dir else None)
    if a.channel:
        store.state["channel"] = a.channel
    if a.repo:
        store.state["repo"] = a.repo
    store.save()
    bundled = Path(a.bundled) if a.bundled else bundled_dir()

    if not (a.headless or a.check_only):
        from .app import run_window
        return run_window(store, bundled, api=a.api)

    def emit(step: str, state: str, detail: str = "") -> None:
        print(json.dumps({"step": step, "state": state, "detail": detail}), flush=True)

    loader = Loader(store, bundled, emit, api=a.api)
    result = {"check": None}
    if not a.no_update:
        result["check"] = loader.check()
    if a.check_only:
        print(json.dumps({"result": result}), flush=True)
        return 0
    info = loader.start()
    print(json.dumps({"running": info}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
