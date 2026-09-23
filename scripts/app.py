#!/usr/bin/env python3
"""Entry point for `AI Safety Explorer.app` — a native desktop window (macOS).

Thin shim: the launcher logic lives in `safety_explorer.desktop` so `explorer app` and the
bundled .app share one path. Run it from a checkout to get the window before any packaging:

    pip install -e '.[openai,embeddings,desktop]'
    python scripts/app.py

Build the .app with packaging/build.sh (see docs/DESKTOP.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Work whether launched from a checkout or a bundle.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from safety_explorer import desktop  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(desktop.run())
