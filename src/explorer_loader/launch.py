"""Start one installed Explorer version in this process, or say exactly why it would not.

The loader and the Explorer share one Python. Starting a version means pointing the import
system at that version's `src/`, telling it where its corpus and the shared database are
(through the environment variables `safety_explorer.paths` already honours), and running its
server on a free loopback port. If the import or the server fails, every module that version
loaded is dropped from `sys.modules` so the next candidate starts clean.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"


class StartError(Exception):
    pass


_INSERTED: list[str] = []


def free_port(preferred: int = 8713) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind((HOST, port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise StartError("no free port on 127.0.0.1")


def _purge() -> None:
    for name in [m for m in sys.modules if m == "safety_explorer" or m.startswith("safety_explorer.")]:
        del sys.modules[name]


def start(tree: Path, data_dir: Path, db_path: Path | None = None,
          timeout: float = 25.0) -> dict:
    """Start the Explorer in `tree`; return {'url', 'port', 'version'} once it answers."""
    src = str(tree / "src")
    _purge()
    # Drop the path a previous (failed) candidate put here, then put this one first so it
    # wins over any other copy on the path — an editable install in a dev venv included.
    for old in _INSERTED:
        while old in sys.path:
            sys.path.remove(old)
    _INSERTED[:] = [src]
    sys.path.insert(0, src)
    os.environ["EXPLORER_CORPUS"] = str(tree / "corpus")
    os.environ["EXPLORER_DATA_DIR"] = str(data_dir)
    os.environ["EXPLORER_DB"] = str(db_path or (data_dir / "explorer.db"))

    errors: list[str] = []
    try:
        import safety_explorer  # noqa: F401
        from safety_explorer import server
    except Exception:  # noqa: BLE001 — any import failure disqualifies this version
        _purge()
        raise StartError("the code did not import:\n" + traceback.format_exc(limit=6))

    port = free_port()

    def run() -> None:
        try:
            server.serve(os.environ["EXPLORER_DB"], os.environ["EXPLORER_CORPUS"],
                         host=HOST, port=port)
        except Exception:  # noqa: BLE001
            errors.append(traceback.format_exc(limit=8))

    threading.Thread(target=run, name="explorer-server", daemon=True).start()
    url = f"http://{HOST}:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if errors:
            _purge()
            raise StartError("the server stopped while starting:\n" + errors[0])
        try:
            with urllib.request.urlopen(f"{url}/api/status", timeout=2) as r:
                if r.status == 200:
                    return {"url": url, "port": port, "version": safety_explorer.__version__}
        except OSError:
            time.sleep(0.2)
    raise StartError(f"the server did not answer within {timeout:.0f}s")
