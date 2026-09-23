"""The Explorer as a native desktop window (macOS WKWebView, via pywebview).

The window wraps the same local web UI the browser shows; the server runs in-process on a
loopback port and the window points at it. Nothing about the measurement changes — this is a
front door, not a new surface. The launcher logic lives here (importable) so both
`explorer app` and `scripts/app.py` (the .app entry point) call one function.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from . import __version__, paths, server

HOST = "127.0.0.1"


def _free_port(preferred: int = 8713) -> int:
    """The standard port if free, else any free one — a second launch, or a separate
    `explorer serve`, should not stop the window from opening."""
    with socket.socket() as s:
        try:
            s.bind((HOST, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _start_server(port: int) -> None:
    threading.Thread(
        target=server.serve,
        kwargs=dict(db_path=str(paths.default_db()),
                    corpus_path=str(paths.corpus_dir()), host=HOST, port=port),
        daemon=True,
    ).start()


def _wait_until_up(base: str, timeout: float = 15.0) -> bool:
    """Block until the server answers /api/status, so the window never opens on a
    connection-refused page. The server starts in well under a second."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/status", timeout=1) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.15)
    return False


def run() -> int:
    """Start the server and open the native window. Returns when the window closes."""
    try:
        import webview
    except ImportError:
        print("pywebview is not installed. Run: pip install -e '.[desktop]'", file=sys.stderr)
        return 2

    port = _free_port()
    base = f"http://{HOST}:{port}"
    _start_server(port)
    if not _wait_until_up(base):
        print("the Explorer server did not come up — check the console for a traceback.",
              file=sys.stderr)
        return 1

    webview.create_window(
        f"AI Safety Explorer {__version__}",
        url=base,
        width=1280, height=860, min_size=(900, 600),
    )
    # http.server is threaded and the window owns the main loop; when the window closes the
    # daemon server thread goes with the process.
    webview.start()
    return 0
