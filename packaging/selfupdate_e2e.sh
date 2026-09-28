#!/usr/bin/env bash
# The app replaces itself, for real, before a release is published.
#
# A copy of the built app is installed in a scratch Applications folder. A stand-in GitHub
# serves a release whose app is newer: the same build, relabelled 9.9.9 and signed again.
# The installed app is asked to update itself. Two cases are checked:
#   * in a folder it may not write to, it declines and says why;
#   * then it downloads, checks and swaps in the new app, which must verify and run.
#
#   bash packaging/selfupdate_e2e.sh "dist/AI Safety Explorer.app"
set -euo pipefail

APP="${1:?usage: selfupdate_e2e.sh APP}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="AI Safety Explorer.app"
T="$(mktemp -d)"
SERVER=""
trap 'if [[ -n "$SERVER" ]]; then kill "$SERVER" 2>/dev/null || true; fi; chmod -R u+w "$T" 2>/dev/null || true' EXIT

mkdir -p "$T/Applications" "$T/new" "$T/api/repos/o/r/releases" "$T/api/assets"
INSTALLED="$T/Applications/$NAME"
ditto "$APP" "$INSTALLED"

# The "newer" app, zipped the way the release zips it.
NEW="$T/new/$NAME"
ditto "$APP" "$NEW"
plutil -replace CFBundleShortVersionString -string 9.9.9 "$NEW/Contents/Info.plist"
if [[ -n "${SIGNING_IDENTITY:-}" ]]; then
  # The installed app has a Developer ID, so the update must carry the same Team ID.
  codesign --force --deep --options runtime --entitlements "$HERE/entitlements.plist" \
    --sign "$SIGNING_IDENTITY" "$NEW"
else
  codesign --force --deep --sign - "$NEW"
fi
(cd "$T/new" && ditto -c -k --keepParent "$NAME" "$T/api/assets/app.zip")
SHA="$(shasum -a 256 "$T/api/assets/app.zip" | cut -d' ' -f1)"
SIZE="$(stat -f%z "$T/api/assets/app.zip")"
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
API="http://127.0.0.1:$PORT"
cat > "$T/api/assets/app.json" <<JSON
{"loader_version": "9.9.9", "min_macos": "11.0",
 "files": {"AI-Safety-Explorer-macOS.zip": {"sha256": "$SHA", "size": $SIZE}}}
JSON
cat > "$T/api/repos/o/r/releases/latest" <<JSON
{"tag_name": "v9.9.9", "html_url": "$API/", "body": "a test release",
 "assets": [{"name": "app.json", "url": "$API/assets/app.json"},
            {"name": "AI-Safety-Explorer-macOS.zip", "url": "$API/assets/app.zip"}]}
JSON
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$T/api" > "$T/server.log" 2>&1 &
SERVER=$!
for _ in $(seq 1 50); do curl -fsS "$API/assets/app.json" > /dev/null 2>&1 && break; sleep 0.2; done

update() {
  "$INSTALLED/Contents/MacOS/AI Safety Explorer" --update-app --repo o/r --api "$API" \
    --data-dir "$T/data"
}

# 1. A folder this user may not write to: it declines, says why, and changes nothing.
chmod a-w "$T/Applications"
if OUT="$(update)"; then echo "$OUT"; echo "FAILED: it claimed to update a read-only copy"; exit 1; fi
echo "$OUT"
grep -q '"status": "manual"' <<<"$OUT"
grep -q 'may not change apps' <<<"$OUT"
chmod u+w "$T/Applications"

# 2. It may: download, check, stage, and swap in once this process has exited.
OUT="$(update)"
echo "$OUT"
grep -q '"status": "staged"' <<<"$OUT"
grep -q '"status": "installing"' <<<"$OUT"
STATUS="$T/data/loader/app-update/status"
for _ in $(seq 1 150); do [[ -s "$STATUS" ]] && break; sleep 0.2; done
echo "installer: $(cat "$STATUS" 2>/dev/null || echo 'no status written')"
grep -qx "installed" "$STATUS"
VERSION="$(plutil -extract CFBundleShortVersionString raw "$INSTALLED/Contents/Info.plist")"
[[ "$VERSION" == "9.9.9" ]] || { echo "FAILED: the installed app is still $VERSION"; exit 1; }
LEFT="$(find "$T/Applications" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')"
[[ "$LEFT" == "1" ]] || { echo "FAILED: left behind:"; ls -la "$T/Applications"; exit 1; }
codesign --verify --deep --strict --verbose=2 "$INSTALLED"
bash "$HERE/smoke.sh" "$INSTALLED"
echo "self-update ok: the app replaced itself with the release's newer app, which runs"
