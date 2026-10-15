#!/usr/bin/env bash
# Notarize with Apple and staple the ticket to what was submitted, so Gatekeeper accepts it
# on first launch, even offline. Needs a Developer ID signature (packaging/sign.sh) and
# an App Store Connect API key (docs/SIGNING.md). Without the key it does nothing.
#
#   bash packaging/notarize.sh "dist/AI Safety Explorer.app"          # zipped to submit
#   bash packaging/notarize.sh dist/AI-Safety-Explorer-macOS.dmg
#
# Environment: APPLE_API_KEY (the .p8 key, as text or base64), APPLE_API_KEY_ID,
# APPLE_API_ISSUER; SIGNING_IDENTITY (set by sign.sh).
set -euo pipefail

TARGET="${1:?usage: notarize.sh APP|DMG}"
if [[ -z "${APPLE_API_KEY:-}" ]]; then
  echo "Notarization not configured: skipped (docs/SIGNING.md)."
  exit 0
fi
if [[ -z "${SIGNING_IDENTITY:-}" ]]; then
  echo "::error::notarization needs a Developer ID signature: set MACOS_CERTIFICATE too"
  exit 1
fi
: "${APPLE_API_KEY_ID:?APPLE_API_KEY_ID is not set}"
: "${APPLE_API_ISSUER:?APPLE_API_ISSUER is not set}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
KEY="$TMP/AuthKey.p8"
if grep -q "BEGIN PRIVATE KEY" <<<"$APPLE_API_KEY"; then
  printf '%s\n' "$APPLE_API_KEY" > "$KEY"
else
  printf '%s' "$APPLE_API_KEY" | base64 --decode > "$KEY"
fi
AUTH=(--key "$KEY" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER")

SUBMIT="$TARGET"
if [[ -d "$TARGET" ]]; then
  SUBMIT="$TMP/submit.zip"
  ditto -c -k --keepParent "$TARGET" "$SUBMIT"
fi

set +e
xcrun notarytool submit "$SUBMIT" "${AUTH[@]}" --wait --timeout 45m --output-format json \
  > "$TMP/result.json"
set -e
cat "$TMP/result.json"
STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$TMP/result.json" || true)"
ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "$TMP/result.json" || true)"
if [[ "$STATUS" != "Accepted" ]]; then
  if [[ -n "$ID" ]]; then xcrun notarytool log "$ID" "${AUTH[@]}" || true; fi
  echo "::error::notarization of $(basename "$TARGET") ended as '${STATUS:-no result}'"
  exit 1
fi
xcrun stapler staple "$TARGET"
xcrun stapler validate "$TARGET"
echo "notarized and stapled: $TARGET"
