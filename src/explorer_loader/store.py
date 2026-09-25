"""Installed Explorer versions: where they live, which one runs, and how one earns its place.

Layout, under the app's data directory (macOS: ~/Library/Application Support/AI Safety
Explorer/):

    explorer.db            the database — shared by every version, never touched here
    loader/state.json      channel, current and last-good version, settings
    loader/versions/<id>/  one directory per installed version: src/, corpus/, pyproject.toml
    loader/downloads/      tarballs in flight

A downloaded version is **verified before it can become current**: it must have the
expected shape, declare a loader contract this loader speaks, compile on this Python, and
need no package this app does not carry. A version that passes still has to *start*: the
launcher records the last version that did (`last_good`), and one that fails is marked
`bad` and skipped from then on. Old versions are pruned, but never the current one, the
last good one, or the one baked into the app.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tarfile
import time
import tomllib
from pathlib import Path
from typing import Any

from . import LOADER_API

KEEP = 3                    # installed versions kept besides current / last good / bundled
BUNDLED_ID = "bundled"

#: Distribution name -> import name, where they differ.
IMPORT_NAMES = {"sentence-transformers": "sentence_transformers", "pyobjc": "objc",
                "pywebview": "webview"}


class VerifyError(Exception):
    pass


def app_dir() -> Path:
    override = os.environ.get("EXPLORER_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AI Safety Explorer"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ai-safety-explorer"


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else app_dir()
        self.dir = self.root / "loader"
        self.versions = self.dir / "versions"
        self.downloads = self.dir / "downloads"
        for d in (self.versions, self.downloads):
            d.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.state = self._load()

    # -- state -------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        default = {"channel": "stable", "repo": None, "current": None, "last_good": None,
                   "auto_update": True, "db_path": None, "versions": {}, "bad": [],
                   "last_check": None}
        try:
            data = json.loads(self.state_file.read_text())
            return {**default, **data}
        except (OSError, ValueError):
            return default

    def save(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, self.state_file)          # atomic: a crash never leaves half a file

    def path_of(self, vid: str, bundled_dir: Path | None = None) -> Path | None:
        if vid == BUNDLED_ID:
            return bundled_dir
        p = self.versions / vid
        return p if p.is_dir() else None

    def record(self, vid: str, meta: dict[str, Any]) -> None:
        self.state["versions"][vid] = {**self.state["versions"].get(vid, {}), **meta}
        self.save()

    def set_current(self, vid: str) -> None:
        self.state["current"] = vid
        self.save()

    def mark_good(self, vid: str) -> None:
        self.state["last_good"] = vid
        if vid in self.state["bad"]:
            self.state["bad"].remove(vid)
        self.save()

    def mark_bad(self, vid: str, why: str) -> None:
        if vid not in self.state["bad"] and vid != BUNDLED_ID:
            self.state["bad"].append(vid)
        self.state["versions"].setdefault(vid, {})["failed"] = why[:500]
        if self.state["current"] == vid:
            self.state["current"] = self.state.get("last_good")
        self.save()

    def candidates(self) -> list[str]:
        """Versions to try, in order: current, last good, then the bundled copy."""
        order = []
        for vid in (self.state.get("current"), self.state.get("last_good"), BUNDLED_ID):
            if vid and vid not in order and vid not in self.state["bad"]:
                order.append(vid)
        if BUNDLED_ID not in order:
            order.append(BUNDLED_ID)
        return order

    # -- install -----------------------------------------------------------------------

    def install_tarball(self, tarball: Path, vid: str) -> Path:
        """Unpack a GitHub source tarball into versions/<vid> and verify it.

        Unpacked beside the final location and moved into place only once verified, so a
        half-extracted or failing version is never visible as installed.
        """
        dest = self.versions / vid
        staging = self.versions / f".staging-{vid}-{int(time.time() * 1000)}"
        try:
            with tarfile.open(tarball, "r:*") as tf:
                _safe_extract(tf, staging)
            roots = [p for p in staging.iterdir() if p.is_dir()]
            # GitHub wraps the tree in one "<owner>-<repo>-<sha>/" directory.
            tree = roots[0] if len(roots) == 1 and not (staging / "src").exists() else staging
            verify(tree)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(tree), str(dest))
            return dest
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            try:
                tarball.unlink()
            except OSError:
                pass

    def prune(self) -> list[str]:
        keep = {self.state.get("current"), self.state.get("last_good"), BUNDLED_ID}
        installed = sorted((p for p in self.versions.iterdir()
                            if p.is_dir() and not p.name.startswith(".")),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        removed = []
        spare = [p for p in installed if p.name not in keep]
        for p in spare[KEEP:]:
            shutil.rmtree(p, ignore_errors=True)
            self.state["versions"].pop(p.name, None)
            removed.append(p.name)
        if removed:
            self.save()
        return removed


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract, refusing any member that would land outside `dest` or is a device/link."""
    dest.mkdir(parents=True)
    base = dest.resolve()
    members = []
    for m in tf.getmembers():
        target = (dest / m.name).resolve()
        if base not in target.parents and target != base:
            raise VerifyError(f"refusing unsafe path in archive: {m.name}")
        if m.issym() or m.islnk() or m.isdev():
            continue
        members.append(m)
    if hasattr(tarfile, "data_filter"):         # 3.12, and 3.11.4+
        tf.extractall(dest, members=members, filter="data")
    else:  # pragma: no cover
        tf.extractall(dest, members=members)


def read_version(tree: Path) -> str:
    text = (tree / "src" / "safety_explorer" / "__init__.py").read_text()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise VerifyError("no __version__ in safety_explorer/__init__.py")
    return m.group(1)


def verify(tree: Path) -> dict[str, Any]:
    """Will this tree run on this app? Raises VerifyError with a sentence saying why not."""
    pkg = tree / "src" / "safety_explorer"
    for need in (pkg / "__init__.py", pkg / "server.py", tree / "corpus", tree / "pyproject.toml"):
        if not need.exists():
            raise VerifyError(f"not an Explorer source tree: missing {need.relative_to(tree)}")
    version = read_version(tree)

    try:
        project = tomllib.loads((tree / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise VerifyError(f"unreadable pyproject.toml: {e}") from e
    api = (project.get("tool", {}).get("explorer-loader", {}) or {}).get("api", 1)
    if int(api) > LOADER_API:
        raise VerifyError(f"version {version} needs a newer app (loader contract {api}; this "
                          f"app speaks {LOADER_API}). Download the latest app from the "
                          "Releases page.")

    missing = []
    for dep in project.get("project", {}).get("dependencies", []) or []:
        name = re.split(r"[\s<>=!~;\[]", dep, maxsplit=1)[0].strip()
        mod = IMPORT_NAMES.get(name.lower(), name.replace("-", "_").lower())
        if name and importlib.util.find_spec(mod) is None:
            missing.append(name)
    if missing:
        raise VerifyError(f"version {version} needs {', '.join(missing)}, which this app does "
                          "not include. Download the latest app from the Releases page.")

    # Compile every module on this interpreter: a syntax this Python does not speak fails
    # here, not halfway through starting.
    for py in pkg.rglob("*.py"):
        try:
            compile(py.read_text(encoding="utf-8"), str(py), "exec")
        except SyntaxError as e:
            raise VerifyError(f"{py.relative_to(tree)} does not compile on Python "
                              f"{sys.version.split()[0]}: {e.msg} (line {e.lineno})") from e
    return {"version": version, "api": int(api)}
