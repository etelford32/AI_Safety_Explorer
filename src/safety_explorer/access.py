"""Who may talk to the local server.

Binding to loopback keeps other machines out. It does not keep other *web pages* out: any
page open in the browser can send a request to 127.0.0.1, and a page on a hostile domain
can even rebind its own name to 127.0.0.1 and read the replies (DNS rebinding). Before the
capture userscript, nothing browser-side was meant to reach the server except its own UI,
so this was latent. The userscript makes browser pages a *designed* source of turns, and
that is the point at which "which pages" has to become an explicit, tested rule:

* **Host.** When bound to loopback, a request must name a loopback host. A rebound
  hostile domain arrives with its own name in `Host` and is refused, so it can neither
  read nor write.
* **Origin.** A request with no `Origin` is a program — an agent hook, curl, the
  simulator — and is allowed, as before. A same-origin request is the Explorer's own UI.
  A cross-origin request is allowed only to the few capture endpoints, and only from the
  chat sites the userscript runs on (plus any in `EXPLORER_ALLOWED_ORIGINS`) or from a
  browser extension (which is where a userscript manager's requests come from). Anything
  else is refused before it reaches a handler — so a page that is not a capture source
  can neither write a turn nor read a single analysis.

Push, never pull, still holds: this admits a *source that chose to emit*, and says which.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

#: The chat surfaces the capture userscript runs on.
CAPTURE_ORIGINS = ("https://claude.ai", "https://chatgpt.com", "https://chat.openai.com")

#: Where a userscript manager's (or any extension's) requests originate. An installed
#: extension already has broader access to the browser than this endpoint grants.
EXTENSION_SCHEMES = ("chrome-extension", "moz-extension", "safari-web-extension")

#: The only endpoints a cross-origin source may reach: write a turn, write a selection,
#: and ask whether the Explorer is up. Every read of stored data stays same-origin.
CAPTURE_PATHS = frozenset({"/api/session/turn", "/api/session/paste", "/api/status"})


def allowed_origins() -> set[str]:
    """The capture origins, plus any the user adds (comma-separated) — e.g. a local chat UI
    at http://localhost:3000 that the userscript has been extended to."""
    extra = os.environ.get("EXPLORER_ALLOWED_ORIGINS", "")
    return set(CAPTURE_ORIGINS) | {o.strip().rstrip("/") for o in extra.split(",") if o.strip()}


def _hostname(host_header: str | None) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):                      # [::1]:8713
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def is_loopback(host: str) -> bool:
    return _hostname(host) in LOOPBACK_HOSTS


def host_ok(host_header: str | None, bound_host: str) -> bool:
    """A loopback-bound server answers only to a loopback name. A server the user bound to
    another interface on purpose (`--host 0.0.0.0`) has chosen to be reachable by name."""
    if not is_loopback(bound_host):
        return True
    return is_loopback(host_header or "")


def origin_verdict(origin: str | None, host_header: str | None, path: str) -> str:
    """Classify a request's origin: 'none' (a program), 'same' (the Explorer's own UI),
    'capture' (an allowed chat site), 'extension', or 'denied'."""
    if not origin:
        return "none"
    o = origin.strip().rstrip("/")
    parsed = urlparse(o)
    if parsed.scheme in ("http", "https") and host_header and parsed.netloc.lower() == host_header.strip().lower():
        return "same"
    if path not in CAPTURE_PATHS:
        return "denied"
    if o in allowed_origins():
        return "capture"
    if parsed.scheme in EXTENSION_SCHEMES:
        return "extension"
    return "denied"
