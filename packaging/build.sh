#!/usr/bin/env bash
# Build "AI Safety Explorer.app" (macOS). Run from anywhere; it works from the repo root.
#
#   bash packaging/build.sh              # build the .app into dist/
#   bash packaging/build.sh --run        # build, then open it
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "The .app bundle is macOS-only (WKWebView + iconutil)."
  echo "On Linux/Windows, run the app in dev mode instead:  python scripts/app.py"
  exit 1
fi

# The build needs the desktop extra (pywebview, py2app, cairosvg). Use the active venv.
python - <<'PY'
import importlib.util, sys
missing = [m for m in ("webview", "py2app") if importlib.util.find_spec(m) is None]
if missing:
    sys.exit("missing build deps: %s\n  pip install -e '.[desktop]'" % ", ".join(missing))
PY

# Render the icon if it isn't there yet (best-effort; the app builds without it).
if [[ ! -f packaging/icon.icns ]]; then
  python packaging/make_icon.py || echo "icon step skipped (install cairosvg for the real icon)"
fi

rm -rf build dist
python packaging/setup_app.py py2app

echo
echo "built: dist/AI Safety Explorer.app"
echo "move it to /Applications, or double-click it in dist/."
if [[ "${1:-}" == "--run" ]]; then
  open "dist/AI Safety Explorer.app"
fi
