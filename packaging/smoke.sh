#!/usr/bin/env bash
# Does this app run? First its self-test: every package it carries imports and TLS works, on
# the architecture it runs as. Then its server: started headless on the copy of the Explorer
# it carries, and asked for /api/status.
#
#   bash packaging/smoke.sh "dist/AI Safety Explorer.app"                  # as this Mac runs it
#   bash packaging/smoke.sh "dist/AI Safety Explorer.app" --arch x86_64    # the Intel slice (Rosetta)
set -euo pipefail

APP="${1:?usage: smoke.sh APP [--arch ARCH]}"
ARCH=""
if [[ "${2:-}" == "--arch" ]]; then ARCH="${3:?--arch needs arm64 or x86_64}"; fi
EXE="$APP/Contents/MacOS/AI Safety Explorer"
RUN=("$EXE")
if [[ -n "$ARCH" ]]; then RUN=(arch "-$ARCH" "$EXE"); fi
LABEL="${ARCH:-native}"

if ! OUT="$("${RUN[@]}" --self-test)"; then
  echo "$OUT"
  echo "SELF-TEST FAILED ($LABEL): a package the app carries does not import"
  exit 1
fi
echo "self-test ($LABEL): $OUT"
if [[ -n "$ARCH" ]] && ! grep -q "\"machine\": \"$ARCH\"" <<<"$OUT"; then
  echo "SELF-TEST RAN AS THE WRONG ARCHITECTURE (wanted $ARCH)"
  exit 1
fi

SMOKE_DIR="$(mktemp -d)"
"${RUN[@]}" --headless --no-update --data-dir "$SMOKE_DIR" > "$SMOKE_DIR/smoke.log" 2>&1 &
PID=$!
OK=""
URL=""
for _ in $(seq 1 90); do
  URL=$(grep -o '"url": "[^"]*"' "$SMOKE_DIR/smoke.log" | head -1 | cut -d'"' -f4 || true)
  if [[ -n "$URL" ]] && curl -fsS "$URL/api/status" > /dev/null 2>&1; then OK=1; break; fi
  sleep 1
done
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
if [[ -z "$OK" ]]; then
  echo "SMOKE TEST FAILED ($LABEL): the app did not start the Explorer:"
  cat "$SMOKE_DIR/smoke.log"
  exit 1
fi
echo "smoke test passed ($LABEL): the app starts the Explorer ($URL)"
