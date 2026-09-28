#!/usr/bin/env bash
# Sign the app.
#
# With a Developer ID certificate (docs/SIGNING.md), every binary inside is signed, inside
# out, with the hardened runtime and a secure timestamp: what Apple's notarization requires.
# Without one, the app keeps the ad-hoc signature py2app gave it. Either way, codesign's
# strict check must pass before anything is packaged.
#
#   bash packaging/sign.sh "dist/AI Safety Explorer.app"
#
# Environment (all optional; in CI they come from the repository's Actions secrets):
#   MACOS_CERTIFICATE           the Developer ID Application certificate, as base64 of a .p12
#   MACOS_CERTIFICATE_PASSWORD  the .p12's password
#   MACOS_SIGNING_IDENTITY      which identity to use (default: the certificate's own)
set -euo pipefail

APP="${1:?usage: sign.sh APP}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ -z "${MACOS_CERTIFICATE:-}" ]]; then
  echo "No Developer ID certificate configured: the app keeps its ad-hoc signature."
  echo "(macOS will ask the user to approve it once: docs/INSTALL.md. To remove that step: docs/SIGNING.md.)"
  codesign --verify --deep --strict --verbose=2 "$APP"
  exit 0
fi

TMP="${RUNNER_TEMP:-$(mktemp -d)}"
KEYCHAIN="$TMP/explorer-signing.keychain-db"
KC_PASS="$(uuidgen)"
security create-keychain -p "$KC_PASS" "$KEYCHAIN"
security set-keychain-settings -lut 21600 "$KEYCHAIN"
security unlock-keychain -p "$KC_PASS" "$KEYCHAIN"
printf '%s' "$MACOS_CERTIFICATE" | base64 --decode > "$TMP/cert.p12"
security import "$TMP/cert.p12" -k "$KEYCHAIN" -P "${MACOS_CERTIFICATE_PASSWORD:-}" \
  -T /usr/bin/codesign
rm -f "$TMP/cert.p12"
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KC_PASS" "$KEYCHAIN" \
  > /dev/null
# On the search list, so codesign finds the identity here and in the disk-image step.
# shellcheck disable=SC2046
security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')

IDENTITY="${MACOS_SIGNING_IDENTITY:-}"
if [[ -z "$IDENTITY" ]]; then
  IDENTITY="$(security find-identity -v -p codesigning "$KEYCHAIN" \
    | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)"
fi
if [[ -z "$IDENTITY" ]]; then
  echo "::error::the certificate holds no 'Developer ID Application' identity"
  exit 1
fi
echo "signing as: $IDENTITY"

SIGN=(codesign --force --timestamp --options runtime --sign "$IDENTITY")
ENTITLEMENTS="$HERE/entitlements.plist"
# Inside out: every Mach-O file (executables with the entitlements), then nested bundles,
# then the app. `codesign --deep` would miss the extension modules under Resources.
python3 "$HERE/macho.py" sign-order "$APP" | while IFS=$'\t' read -r kind path; do
  if [[ "$kind" == "exe" ]]; then
    "${SIGN[@]}" --entitlements "$ENTITLEMENTS" "$path"
  else
    "${SIGN[@]}" "$path"
  fi
done
"${SIGN[@]}" --entitlements "$ENTITLEMENTS" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign --display --verbose=2 "$APP" 2>&1 | grep -E "^(Authority|TeamIdentifier|Timestamp|Runtime)" || true

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "SIGNING_IDENTITY=$IDENTITY" >> "$GITHUB_ENV"
fi
