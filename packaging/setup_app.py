"""py2app build recipe for `AI Safety Explorer.app` (macOS).

Run from the repo root via packaging/build.sh, or directly:

    python packaging/setup_app.py py2app

What this bundles and why:
  * `scripts/app.py` is the entry point — it starts the server in-process and opens the
    native WKWebView window.
  * `safety_explorer` goes in as a real package directory (not zipped), because the server
    reads its web assets and schema.sql with `Path(__file__).parent / ...`, which only works
    when the package is a directory on disk.
  * the corpus is copied into Resources; `safety_explorer.paths` reads it from there when
    frozen, and writes the database to ~/Library/Application Support instead of the
    read-only bundle.
  * the heavy semantic-backend stack (torch / sentence-transformers) is EXCLUDED by default
    so the app stays small (tens of MB, not gigabytes). The app runs fine on the lexicon
    register; to ship the semantic backend inside the bundle, drop the excludes below and
    rebuild — see docs/DESKTOP.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from safety_explorer import __version__  # noqa: E402

ICON = ROOT / "packaging" / "icon.icns"

OPTIONS = {
    "packages": ["safety_explorer"],
    "includes": ["webview"],
    # Keep the bundle lean: the app runs on the lexicon register out of the box, and the
    # semantic backend (torch) is a multi-gigabyte dependency better installed on the side.
    "excludes": ["torch", "sentence_transformers", "transformers", "scipy",
                 "sklearn", "scikit_learn", "pandas", "matplotlib", "tkinter"],
    "resources": [str(ROOT / "corpus")],
    "plist": {
        "CFBundleName": "AI Safety Explorer",
        "CFBundleDisplayName": "AI Safety Explorer",
        "CFBundleIdentifier": "com.parkersphysics.safety-explorer",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        # A normal windowed app (Dock icon + window), not a menu-bar-only agent.
        "LSUIElement": False,
        "NSHumanReadableCopyright": "MIT-licensed research instrument.",
    },
}
if ICON.exists():
    OPTIONS["iconfile"] = str(ICON)

setup(
    name="AI Safety Explorer",
    app=[str(ROOT / "scripts" / "app.py")],
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
