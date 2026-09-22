#!/usr/bin/env bash
# Install the Explorer menu-bar app as a macOS login item, so it is always on while you run
# a model. Generates a LaunchAgent with your real paths (no hand-editing), loads it, and
# prints how to remove it. Re-run it any time to update the paths.
#
#   bash scripts/install-menubar.sh            # register + start at login
#   bash scripts/install-menubar.sh --uninstall
set -euo pipefail

LABEL="com.parkersphysics.safety-explorer"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installer is macOS-only (it uses launchd). On Linux/Windows, run 'explorer serve'"
  echo "under your own init system — the endpoint and UI are identical."
  exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $PLIST — the menu-bar app will not start at login. Quit the running one from"
  echo "its menu-bar icon."
  exit 0
fi

# Resolve the repo root from this script's location, and the venv python inside it.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "no venv python at $PY"
  echo "create it first:  cd '$ROOT' && python3 -m venv .venv && source .venv/bin/activate \\"
  echo "                  && pip install -e '.[openai,embeddings,menubar]'"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>${ROOT}/scripts/menubar.py</string>
  </array>
  <key>WorkingDirectory</key><string>${ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>EXPLORER_EMBED_BACKEND</key><string>minilm</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/explorer-menubar.log</string>
  <key>StandardErrorPath</key><string>/tmp/explorer-menubar.log</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed $PLIST"
echo "the 🛰 Explorer icon should appear in your menu bar now, and at every login."
echo "logs: /tmp/explorer-menubar.log"
echo "remove with:  bash scripts/install-menubar.sh --uninstall"
