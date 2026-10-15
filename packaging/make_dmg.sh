#!/usr/bin/env bash
# Package the app as a disk image: the app, and a shortcut to Applications to drag it onto.
# Signs the image when sign.sh set SIGNING_IDENTITY, then checks it as a downloader gets it:
# the image verifies, the app inside passes codesign's strict check, and the app runs from
# the mounted image. The image is read-only, so an app that writes into itself fails here.
#
#   bash packaging/make_dmg.sh "dist/AI Safety Explorer.app" dist/AI-Safety-Explorer-macOS.dmg
set -euo pipefail

APP="${1:?usage: make_dmg.sh APP DMG}"
DMG="${2:?usage: make_dmg.sh APP DMG}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="AI Safety Explorer"

STAGE="$(mktemp -d)/$NAME"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/$NAME.app"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
# hdiutil is known to fail now and then on CI runners with "Resource busy"; try again.
for attempt in 1 2 3 4 5; do
  if hdiutil create -volname "$NAME" -srcfolder "$STAGE" -fs HFS+ -format UDZO \
      -imagekey zlib-level=9 -ov "$DMG"; then
    break
  fi
  if [[ "$attempt" == 5 ]]; then echo "hdiutil create failed"; exit 1; fi
  sleep $((attempt * 3))
done
if [[ -n "${SIGNING_IDENTITY:-}" ]]; then
  codesign --force --timestamp --sign "$SIGNING_IDENTITY" "$DMG"
fi
hdiutil verify "$DMG"

MNT="$(mktemp -d)"
hdiutil attach "$DMG" -nobrowse -readonly -noautoopen -mountpoint "$MNT" > /dev/null
trap 'hdiutil detach "$MNT" -force > /dev/null 2>&1 || true' EXIT
test -L "$MNT/Applications"
codesign --verify --deep --strict --verbose=2 "$MNT/$NAME.app"
bash "$HERE/smoke.sh" "$MNT/$NAME.app"
echo "disk image ok: $DMG ($(du -h "$DMG" | cut -f1))"
