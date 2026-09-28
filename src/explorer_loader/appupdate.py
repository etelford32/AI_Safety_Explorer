"""The app updates itself, too: the launcher, its Python and the packages it carries.

The loader keeps the Explorer *code* current on every launch (core.py). The app around that
code does not change with it: the window, the Python runtime, and the packages it carries
(pywebview, the Anthropic and OpenAI SDKs). CI builds the app once per release. This module
keeps it current, in three steps.

1. **Find.** Each GitHub Release carries `app.json`, written by CI (packaging/manifest.py). It
   names the launcher version the release's app contains, plus the size and SHA-256 of
   `AI-Safety-Explorer-macOS.zip`. A launcher newer than the running one is an update. Every
   channel follows releases for this, because apps are only built for releases.
2. **Download and stage.** This happens in the background, into the data folder.
   - The zip is checked against the manifest's size and SHA-256.
   - It is unpacked with `ditto`, which keeps the code signature intact.
   - The unpacked app must pass `codesign --verify --strict`, and must carry this app's bundle
     identifier and the promised version.
   - When this app is signed with a Developer ID, the new one must have the same Team ID.
3. **Install.** When the user quits, or chooses **Updates → Restart**, a small script waits
   for the app to exit and moves the new app into place. For a restart, it then opens the new
   app. If the move fails, the old app is put back, and the script leaves a note that the next
   launch reads.

Some copies cannot be replaced from inside, and the updater says why:
- an app opened straight from the disk image;
- an app opened from Downloads without being moved (macOS runs it from a read-only
  "translocated" copy);
- an app in a folder this user may not write to.

The user is then given the download link instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import DEFAULT_REPO, LOADER_VERSION, github

MANIFEST = "app.json"
ZIP = "AI-Safety-Explorer-macOS.zip"
DMG = "AI-Safety-Explorer-macOS.dmg"
BUNDLE_ID = "com.parkersphysics.safety-explorer"
APP_NAME = "AI Safety Explorer.app"
MAX_MANIFEST = 64 * 1024


def download_page(repo: str = DEFAULT_REPO) -> str:
    """The link that always serves the newest disk image."""
    return f"https://github.com/{repo}/releases/latest/download/{DMG}"


@dataclass
class AppUpdate:
    version: str            # the launcher version the new app contains
    tag: str                # the release it comes from
    zip_url: str            # where the zip is fetched from (see _asset_url)
    sha256: str
    size: int
    page: str               # the release's web page
    min_macos: str = "11.0"
    notes: str = ""


def parse_version(v: str) -> tuple[int, int, int]:
    m = re.match(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", v or "")
    if not m:
        return (0, 0, 0)
    return tuple(int(x or 0) for x in m.groups())  # type: ignore[return-value]


def running_app() -> Path | None:
    """The .app bundle this process runs from, or None when run from a checkout.

    py2app sets RESOURCEPATH to …/X.app/Contents/Resources."""
    res = os.environ.get("RESOURCEPATH")
    if not res:
        return None
    app = Path(res).parents[1]
    return app if app.suffix == ".app" else None


def installable(app: Path | None, access: Callable[[str, int], bool] = os.access) -> str | None:
    """Why this copy of the app cannot replace itself, or None if it can."""
    if app is None:
        return "this is not running from the app, so there is no app to replace"
    s = str(app)
    if "/AppTranslocation/" in s:
        return ("macOS is running this copy from a temporary read-only location, because it was "
                "opened without being moved to Applications first. Move it to Applications "
                "and open it from there.")
    if s.startswith("/Volumes/"):
        return "this copy is running from the disk image. Drag it to Applications first."
    if not (access(str(app.parent), os.W_OK) and access(s, os.W_OK)):
        return f"this user may not change apps in {app.parent}."
    return None


# -- finding an update ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


def _open_asset(url: str, token: str | None, timeout: float):
    """Open a release asset by its API URL.

    GitHub answers with a redirect to a signed download URL. That URL must be fetched
    without the Authorization header (the signature is the authorization), and urllib would
    otherwise forward the header."""
    headers = {"User-Agent": github.USER_AGENT, "Accept": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    handlers: list = [_NoRedirect()]
    if url.startswith("https:"):
        handlers.append(urllib.request.HTTPSHandler(context=github._ssl_context()))
    opener = urllib.request.build_opener(*handlers)
    try:
        return opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            plain = {"User-Agent": github.USER_AGENT, "Accept": "application/octet-stream"}
            return github._open(urllib.request.Request(e.headers["Location"], headers=plain),
                                timeout)
        raise


def _asset_url(asset: dict, token: str | None) -> str:
    """A public repository's assets come from the plain download URL, which does not count
    against GitHub's API rate limit. A lab behind one address shares that limit. A private
    one needs the API URL, which takes the token."""
    if not token and asset.get("browser_download_url"):
        return asset["browser_download_url"]
    return asset["url"]


def find(repo: str = DEFAULT_REPO, token: str | None = None, api: str = github.API,
         current: str = LOADER_VERSION, timeout: float = 8.0) -> AppUpdate | None:
    """The newest release's app, if it carries a newer launcher than `current`."""
    try:
        rel = github._get_json(f"{api}/repos/{repo}/releases/latest", token, timeout)
    except FileNotFoundError:
        return None
    assets = {a.get("name"): a for a in rel.get("assets") or []}
    if MANIFEST not in assets:
        return None                         # a release from before apps updated themselves
    try:
        with _open_asset(_asset_url(assets[MANIFEST], token), token, timeout) as r:
            manifest = json.loads(r.read(MAX_MANIFEST + 1)[:MAX_MANIFEST].decode())
        version = str(manifest["loader_version"])
        entry = manifest["files"][ZIP]
        sha, size = str(entry["sha256"]).lower(), int(entry["size"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        raise github.UpdateError(f"the release's app manifest could not be read ({e})") from e
    if parse_version(version) <= parse_version(current):
        return None
    if ZIP not in assets:
        raise github.UpdateError(f"release {rel.get('tag_name')} lists an app but has no {ZIP}")
    return AppUpdate(version=version, tag=rel.get("tag_name") or "",
                     zip_url=_asset_url(assets[ZIP], token),
                     sha256=sha, size=size, page=rel.get("html_url") or "",
                     min_macos=str(manifest.get("min_macos") or "11.0"),
                     notes=(rel.get("body") or "")[:2000])


def supported_here(update: AppUpdate) -> str | None:
    """Why this Mac cannot run the update, or None."""
    if sys.platform != "darwin":
        return None
    here = platform.mac_ver()[0]
    if here and parse_version(here) < parse_version(update.min_macos):
        return f"it needs macOS {update.min_macos}; this Mac runs {here}"
    return None


# -- downloading and staging --------------------------------------------------------------------

def download(update: AppUpdate, dest: Path, token: str | None = None,
             progress: Callable[[int, int], None] | None = None, timeout: float = 30.0) -> Path:
    """Fetch the zip and check it is byte for byte the one the manifest describes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    got = 0
    try:
        with _open_asset(update.zip_url, token, timeout) as r, open(dest, "wb") as out:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > update.size:
                    raise github.UpdateError("the download is larger than the release says")
                digest.update(chunk)
                out.write(chunk)
                if progress:
                    progress(got, update.size)
    except (urllib.error.URLError, OSError) as e:
        dest.unlink(missing_ok=True)
        raise github.UpdateError(f"app download failed: {getattr(e, 'reason', e)}") from e
    except github.UpdateError:
        dest.unlink(missing_ok=True)
        raise
    if got != update.size or digest.hexdigest() != update.sha256:
        dest.unlink(missing_ok=True)
        raise github.UpdateError("the downloaded app does not match the release's checksum; "
                                 "nothing was changed")
    return dest


def _team_id(app: Path, run=subprocess.run) -> str | None:
    out = run(["codesign", "-dv", "--verbose=2", str(app)], capture_output=True, text=True)
    m = re.search(r"^TeamIdentifier=(.+)$", (out.stderr or "") + (out.stdout or ""), re.M)
    team = m.group(1).strip() if m else None
    return None if team in (None, "", "not set") else team


def stage(zip_path: Path, workdir: Path, update: AppUpdate, current: Path | None = None,
          run=subprocess.run, on_mac: bool | None = None) -> Path:
    """Unpack the downloaded app and check it is what it claims to be, before it can replace
    this one. Returns the staged .app."""
    on_mac = sys.platform == "darwin" if on_mac is None else on_mac
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    if on_mac:
        # ditto keeps what a code signature covers: symlinks, modes, extended attributes.
        done = run(["ditto", "-x", "-k", str(zip_path), str(workdir)], capture_output=True, text=True)
        if done.returncode != 0:
            raise github.UpdateError(f"could not unpack the app: {done.stderr.strip()}")
    else:
        # Not a Mac: only the tests stage here, and they check what is checked, not signatures.
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():
                if name.startswith("/") or ".." in Path(name).parts:
                    raise github.UpdateError(f"unsafe path in the app zip: {name}")
            z.extractall(workdir)
    apps = sorted(workdir.glob("*.app"))
    if len(apps) != 1:
        raise github.UpdateError(f"the zip should hold one app, it holds {len(apps)}")
    app = apps[0]
    try:
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException) as e:
        raise github.UpdateError(f"the downloaded app has no readable Info.plist ({e})") from e
    want_id = BUNDLE_ID
    if current is not None:
        try:
            want_id = plistlib.loads((current / "Contents" / "Info.plist").read_bytes()).get(
                "CFBundleIdentifier", BUNDLE_ID)
        except (OSError, plistlib.InvalidFileException):
            pass
    if info.get("CFBundleIdentifier") != want_id:
        raise github.UpdateError(f"the downloaded app is {info.get('CFBundleIdentifier')!r}, "
                                 f"not {want_id!r}")
    if info.get("CFBundleShortVersionString") != update.version:
        raise github.UpdateError(f"the downloaded app is version "
                                 f"{info.get('CFBundleShortVersionString')!r}; the release "
                                 f"promised {update.version!r}")
    if on_mac:
        chk = run(["codesign", "--verify", "--deep", "--strict", str(app)],
                  capture_output=True, text=True)
        if chk.returncode != 0:
            raise github.UpdateError("the downloaded app's signature does not verify: "
                                     + (chk.stderr or chk.stdout).strip()[:300])
        mine = _team_id(current, run) if current is not None else None
        if mine and _team_id(app, run) != mine:
            raise github.UpdateError(f"the downloaded app is not signed by this app's "
                                     f"developer (Team ID {mine})")
    if app.name != APP_NAME:
        app = app.rename(workdir / APP_NAME)
    return app


# -- installing -----------------------------------------------------------------------------------

HELPER = r"""#!/bin/sh
# Written by AI Safety Explorer: install an app update once the app has quit.
#   helper.sh PID NEW APP RELAUNCH STATUS
PID="$1"; NEW="$2"; APP="$3"; RELAUNCH="$4"; STATUS="$5"
i=0
while kill -0 "$PID" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 600 ]; then echo "failed: the app did not quit" > "$STATUS"; exit 1; fi
  sleep 0.1
done
OLD="$APP.previous-$$"
ERR="$STATUS.err"
if mv "$APP" "$OLD" 2>"$ERR"; then
  if mv "$NEW" "$APP" 2>>"$ERR"; then
    rm -rf "$OLD"
    echo "installed" > "$STATUS"
  else
    mv "$OLD" "$APP"
    { echo "failed: could not move the new app into place"; cat "$ERR"; } > "$STATUS"
  fi
else
  { echo "failed: could not move this app aside"; cat "$ERR"; } > "$STATUS"
fi
rm -f "$ERR"
if [ "$RELAUNCH" = "1" ]; then open "$APP"; fi
exit 0
"""


def install(staged: Path, app: Path, status: Path, relaunch: bool, pid: int | None = None,
            spawn=subprocess.Popen) -> None:
    """Hand the swap to a script that runs after this process exits. The caller then quits."""
    status.parent.mkdir(parents=True, exist_ok=True)
    status.unlink(missing_ok=True)
    script = status.parent / "install.sh"
    script.write_text(HELPER, encoding="utf-8")
    spawn(["/bin/sh", str(script), str(pid or os.getpid()), str(staged), str(app),
           "1" if relaunch else "0", str(status)],
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          start_new_session=True)


def read_status(status: Path) -> str | None:
    """What the last install left behind ("installed", or "failed: …"), read once."""
    try:
        text = status.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    status.unlink(missing_ok=True)
    return text or None


def as_dict(update: AppUpdate) -> dict:
    return asdict(update)
