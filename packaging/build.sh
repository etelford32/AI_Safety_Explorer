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

# The build needs the desktop extra (pywebview, py2app, cairosvg, certifi). Use the active venv.
python - <<'PY'
import importlib.util, sys
missing = [m for m in ("webview", "py2app", "certifi") if importlib.util.find_spec(m) is None]
if missing:
    sys.exit("missing build deps: %s\n  pip install -e '.[desktop]'" % ", ".join(missing))
PY

# Render the icon if it isn't there yet (best-effort; the app builds without it).
if [[ ! -f packaging/icon.icns ]]; then
  python packaging/make_icon.py || echo "icon step skipped (install cairosvg for the real icon)"
fi

rm -rf build dist
python packaging/setup_app.py py2app

# Smoke test the bundle before calling it built: start it without a window, on the baseline
# copy it carries, and ask the server it starts whether it is up.
APP="dist/AI Safety Explorer.app"
SMOKE_DIR="$(mktemp -d)"
"$APP/Contents/MacOS/AI Safety Explorer" --headless --no-update --data-dir "$SMOKE_DIR" \
  > "$SMOKE_DIR/smoke.log" 2>&1 &
PID=$!
OK=""
for _ in $(seq 1 60); do
  URL=$(grep -o '"url": "[^"]*"' "$SMOKE_DIR/smoke.log" | head -1 | cut -d'"' -f4 || true)
  if [[ -n "$URL" ]] && curl -fsS "$URL/api/status" > /dev/null 2>&1; then OK=1; break; fi
  sleep 1
done
kill "$PID" 2>/dev/null || true
if [[ -z "$OK" ]]; then
  echo "SMOKE TEST FAILED — the bundled app did not start the Explorer:"
  cat "$SMOKE_DIR/smoke.log"
  exit 1
fi
echo "smoke test passed: the bundle starts the Explorer ($URL)"

echo
echo "built: $APP"
echo "move it to /Applications, or double-click it in dist/."
if [[ "${1:-}" == "--run" ]]; then
  open "$APP"
fi
