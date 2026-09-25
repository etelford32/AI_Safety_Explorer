#!/usr/bin/env python3
"""Entry point of `AI Safety Explorer.app`: the self-updating loader.

The app opens a window, brings the Explorer code up to date from GitHub (verified before it
is used), and starts it in the same window. See src/explorer_loader/ and docs/INSTALL.md.

From a checkout, the same thing without packaging:

    python scripts/loader_app.py               # the window (needs pywebview)
    python scripts/loader_app.py --headless    # no window: check, update, start, print URL
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from explorer_loader.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
