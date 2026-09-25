"""Where new Explorer code comes from: GitHub, over plain HTTPS, standard library only.

Three channels:

* **stable** — the latest GitHub Release. What testers should run, and what a paper cites:
  a tagged, fixed version whose results can be reproduced.
* **dev** — the head of the repository's default branch. What the author runs while
  developing: every pushed change arrives on the next launch.
* **branch:<name>** — the head of a named branch, for trying work before it is merged.

`stable` falls back to `dev` when the repository has no release yet, and says so.

A token is optional. A public repository needs none (unauthenticated requests allow 60 API
calls an hour, and a launch uses two or three). A private one needs a read-only token,
which the loader keeps in the macOS Keychain.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

API = "https://api.github.com"
USER_AGENT = "ai-safety-explorer-loader"


class UpdateError(Exception):
    """A check or download failed in a way the window should explain in words."""


@dataclass
class Target:
    """One installable version of the Explorer on the remote."""

    id: str              # directory-safe identity: the tag for a release, the short sha otherwise
    sha: str | None      # full commit sha when known
    ref: str             # the tag or branch it came from
    channel: str
    tarball_url: str
    label: str           # what the window shows: "v0.31.0" or "dev · 1b98699"
    published: str | None = None
    notes: str = ""
    fallback_note: str = ""


def _ssl_context():
    """Certificate roots for HTTPS: the platform's *and* certifi's.

    A bundled Python often finds no system roots at all and fails every request with
    CERTIFICATE_VERIFY_FAILED, so certifi's bundle (shipped inside the app) is added. But
    certifi alone would ignore any authority the machine or network adds — a university or
    corporate proxy that inspects TLS, or one named in SSL_CERT_FILE — and a tester behind one
    would see the app as permanently offline. The union trusts both.
    """
    import ssl
    ctx = ssl.create_default_context()         # platform roots, and SSL_CERT_FILE if set
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    return ctx


def _open(req: urllib.request.Request, timeout: float):
    ctx = _ssl_context() if req.full_url.startswith("https:") else None
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def _headers(token: str | None) -> dict[str, str]:
    h = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get_json(url: str, token: str | None, timeout: float) -> dict:
    req = urllib.request.Request(url, headers=_headers(token))
    try:
        with _open(req, timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(url) from e
        if e.code in (401, 403):
            detail = "the token was refused" if token else "GitHub refused an unauthenticated request"
            raise UpdateError(f"{detail} ({e.code}). A private repository needs a read-only "
                              "token; a rate-limited one needs a few minutes.") from e
        raise UpdateError(f"GitHub answered {e.code} for {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError(f"could not reach GitHub ({getattr(e, 'reason', e)}) — offline?") from e


def resolve(repo: str, channel: str, token: str | None = None, timeout: float = 8.0,
            api: str = API) -> Target:
    """The version the channel currently points at."""
    if channel == "stable":
        try:
            rel = _get_json(f"{api}/repos/{repo}/releases/latest", token, timeout)
        except FileNotFoundError:
            t = resolve(repo, "dev", token, timeout, api)
            t.fallback_note = "no release published yet — following the development branch"
            return t
        tag = rel["tag_name"]
        return Target(id=_safe(tag), sha=None, ref=tag, channel="stable",
                      tarball_url=f"{api}/repos/{repo}/tarball/{tag}",
                      label=tag, published=rel.get("published_at"),
                      notes=(rel.get("body") or "")[:2000])

    if channel == "dev":
        try:
            info = _get_json(f"{api}/repos/{repo}", token, timeout)
        except FileNotFoundError as e:
            raise UpdateError(f"repository {repo} not found — private without a token, or "
                              "renamed?") from e
        branch = info.get("default_branch") or "main"
    elif channel.startswith("branch:"):
        branch = channel.split(":", 1)[1].strip()
        if not branch:
            raise UpdateError("empty branch name")
    else:
        raise UpdateError(f"unknown channel {channel!r}")

    try:
        c = _get_json(f"{api}/repos/{repo}/commits/{urllib.request.quote(branch, safe='')}",
                      token, timeout)
    except FileNotFoundError as e:
        raise UpdateError(f"branch {branch!r} not found in {repo}") from e
    sha = c["sha"]
    msg = ((c.get("commit") or {}).get("message") or "").split("\n")[0]
    date = ((c.get("commit") or {}).get("committer") or {}).get("date")
    return Target(id=sha[:12], sha=sha, ref=branch, channel=channel,
                  tarball_url=f"{api}/repos/{repo}/tarball/{sha}",
                  label=f"{branch} · {sha[:7]}", published=date, notes=msg)


def download(target: Target, dest_file, token: str | None = None,
             progress: Callable[[int, int | None], None] | None = None,
             timeout: float = 30.0) -> None:
    """Stream the tarball to `dest_file`, reporting (bytes so far, total or None)."""
    req = urllib.request.Request(target.tarball_url, headers=_headers(token))
    try:
        with _open(req, timeout) as r, open(dest_file, "wb") as out:
            total = r.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            got = 0
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
    except urllib.error.HTTPError as e:
        raise UpdateError(f"download failed: GitHub answered {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError(f"download failed: {getattr(e, 'reason', e)}") from e


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:64]
