"""py2app build recipe for `AI Safety Explorer.app` (macOS) — the self-updating loader.

Run from the repo root via packaging/build.sh (or in CI, .github/workflows/app.yml).

What the app is: the **loader** (`src/explorer_loader`) plus a Python runtime. At launch it
fetches the current Explorer code from GitHub, verifies it, and runs it in-process. So the
app is built rarely — only when the loader, or the set of packages the app carries, changes.

What goes in, and why:
  * `explorer_loader` — the entry point and window.
  * a **baseline** copy of the Explorer (`Resources/baseline/`: src/safety_explorer, the
    corpus, pyproject.toml) — the version the app falls back to offline, or if every
    downloaded version fails. It is whatever the repo held when the app was built.
  * the **whole standard library**, not just the modules the baseline happens to import:
    py2app packs only what it sees imported, and a later Explorer version may import a
    module this one did not. The Explorer has no third-party runtime dependencies, so the
    stdlib is its entire world.
  * pywebview and the pyobjc frameworks it drives (WebKit, AppKit), certifi (so HTTPS to
    GitHub works without the system certificate store a bundled Python cannot find), and —
    when installed in the build environment — the Anthropic and OpenAI SDKs, so the app can
    run campaigns against those providers.
  * NOT the semantic embedding stack (torch): multi-gigabyte, and the Explorer runs on the
    lexicon register without it.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from explorer_loader import LOADER_VERSION  # noqa: E402

ICON = ROOT / "packaging" / "icon.icns"

# -- the baseline Explorer, staged so py2app copies it as one folder --------------------------
STAGE = ROOT / "build" / "stage" / "baseline"
if STAGE.exists():
    shutil.rmtree(STAGE)
ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
shutil.copytree(ROOT / "src" / "safety_explorer", STAGE / "src" / "safety_explorer", ignore=ignore)
shutil.copytree(ROOT / "corpus", STAGE / "corpus", ignore=ignore)
shutil.copy2(ROOT / "pyproject.toml", STAGE / "pyproject.toml")

# -- the whole standard library ------------------------------------------------------------
SKIP = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "ensurepip", "venv",
        "pydoc_data", "msvcrt", "winreg", "winsound", "nt", "this", "antigravity",
        "_tkinter", "_msi", "msilib", "_winapi", "_overlapped", "_wmi"}
STDLIB = sorted(m for m in sys.stdlib_module_names
                if m not in SKIP and not m.startswith("_")
                and importlib.util.find_spec(m) is not None)

OPTIONAL = [p for p in ("anthropic", "openai", "certifi") if importlib.util.find_spec(p)]

OPTIONS = {
    "packages": ["explorer_loader", "safety_explorer", "webview", *OPTIONAL],
    "includes": STDLIB + ["webview.platforms.cocoa", "WebKit", "Foundation", "AppKit",
                          "objc", "Quartz", "Security", "UniformTypeIdentifiers"],
    "excludes": ["torch", "sentence_transformers", "transformers", "scipy", "sklearn",
                 "scikit_learn", "pandas", "matplotlib", "tkinter", "PyInstaller",
                 "py2app", "setuptools._vendor", "pytest", "playwright"],
    "resources": [str(STAGE)],
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "AI Safety Explorer",
        "CFBundleDisplayName": "AI Safety Explorer",
        "CFBundleIdentifier": "com.parkersphysics.safety-explorer",
        # The app's own version is the loader's: the Explorer inside updates itself.
        "CFBundleShortVersionString": LOADER_VERSION,
        "CFBundleVersion": LOADER_VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
        "NSHumanReadableCopyright": "MIT licensed. © 2026 Elliot Telford.",
        # Belt and braces for the UTF-8 fix in explorer_loader.ensure_utf8: Launch Services
        # applies this environment when the app is opened from Finder, the Dock or `open`.
        "LSEnvironment": {"LANG": "en_US.UTF-8", "PYTHONUTF8": "1"},
    },
}
if ICON.exists():
    OPTIONS["iconfile"] = str(ICON)

setup(
    name="AI Safety Explorer",
    app=[str(ROOT / "scripts" / "loader_app.py")],
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
