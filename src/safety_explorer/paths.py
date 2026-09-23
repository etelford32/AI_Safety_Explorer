"""Where the database and corpus live, in a dev checkout AND inside a packaged .app.

The desktop app is the reason this exists. A checkout run from the repo root finds the
corpus at `corpus/` and writes the database to `data/explorer.db` — fine, and unchanged. A
double-clickable `.app` is different in two ways that both have to be handled or the app
cannot start:

  * **The bundle is read-only.** macOS may run it from a translocated, read-only path, and
    even in /Applications you do not write inside another app's bundle. So the database
    moves to `~/Library/Application Support/AI Safety Explorer/`, the standard place for an
    app's own data, and the corpus is read from the bundle's Resources.
  * **There is no repo root.** `Path.cwd()` is unpredictable for a GUI launch, so nothing
    here is relative to the working directory — it is relative to the bundle (when frozen)
    or to the package's own location (in a checkout).

Every path can still be overridden by an environment variable, so a power user or a test can
point any of them anywhere without touching the resolution logic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The app's display name, used for the Application Support directory. Kept here so the
#: bundle, the LaunchAgent and this module agree on one spelling.
APP_NAME = "AI Safety Explorer"


def is_bundled() -> bool:
    """Are we running inside a frozen app bundle (py2app / PyInstaller) rather than a checkout?"""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """The directory holding read-only bundled resources (the corpus, web assets).

    In a py2app bundle the executable is at `<App>.app/Contents/MacOS/<exe>` and resources
    are at `<App>.app/Contents/Resources`. In a checkout it is the repo root (two levels up
    from this file: `src/safety_explorer/paths.py` -> repo root).
    """
    if is_bundled():
        return Path(sys.executable).resolve().parents[1] / "Resources"
    return Path(__file__).resolve().parents[2]


def corpus_dir() -> Path:
    """The corpus directory. `EXPLORER_CORPUS` overrides; otherwise it is beside the app."""
    override = os.environ.get("EXPLORER_CORPUS")
    return Path(override) if override else resource_dir() / "corpus"


def data_dir() -> Path:
    """The writable directory for this app's data, created if missing.

    `EXPLORER_DATA_DIR` overrides. Bundled, it is `~/Library/Application Support/<APP_NAME>`;
    in a checkout it is `data/` under the repo root, so a developer's runs stay in the tree.
    """
    override = os.environ.get("EXPLORER_DATA_DIR")
    if override:
        d = Path(override)
    elif is_bundled():
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        d = resource_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_db() -> Path:
    """The database path. `EXPLORER_DB` overrides; otherwise it sits in `data_dir()`."""
    override = os.environ.get("EXPLORER_DB")
    return Path(override) if override else data_dir() / "explorer.db"
