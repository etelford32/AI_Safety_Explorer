#!/usr/bin/env bash
# The staged live-API plan from docs/API_TESTING.md, in one command.
#
#   bash scripts/pilot.sh [MODEL]          # default claude-opus-5-5
#   bash scripts/pilot.sh MODEL --yes      # skip the confirmation before the paid pilot
#
# Stops at the first failed stage. Everything goes to data/api-tests.db, never to the
# campaign database. Spends about $0.10 before asking, then the pilot (~$4-8 for the
# sync and batch runs together, on Opus 5.5) only after you confirm.
set -euo pipefail

MODEL="${1:-claude-opus-5-5}"
YES="${2:-}"
DB="data/api-tests.db"
LOG="data/api-tests-$(date -u +%Y%m%dT%H%M%SZ).log"
mkdir -p data
exec > >(tee -a "$LOG") 2>&1

step() { printf '\n=== %s ===\n' "$*"; }
ex() { explorer --db "$DB" "$@"; }

if [ -z "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  echo "No ANTHROPIC_API_KEY in the environment. Add it to the environment settings first."
  exit 2
fi

step "0. corpus and database"
ex lint >/dev/null && echo "corpus lints clean"
ex init >/dev/null && echo "database ready: $DB"

step "1a. apicheck (synchronous stages)"
ex apicheck --model "$MODEL"

step "1b. apicheck with a two-request batch (minutes)"
ex apicheck --model "$MODEL" --batch

step "2. preflight: exact filler sizes and a four-way cost table"
ex preflight --model "$MODEL" --repeats 2 --depths 0 32k

if [ "$YES" != "--yes" ]; then
  printf '\nRun the paid pilot now (sync + batch, orbital_debris, 2 repeats, depths 0 and 32k)? [y/N] '
  read -r answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ] || { echo "stopped before the pilot"; exit 0; }
fi

step "3a. pilot, synchronous"
ex run --campaign pilot-sync --provider anthropic --model "$MODEL" \
       --repeats 2 --only orbital_debris --depths 0 32k

step "3b. pilot, batch"
ex run --campaign pilot-batch --provider anthropic --model "$MODEL" \
       --repeats 2 --only orbital_debris --depths 0 32k --batch --poll 30

step "3c. readings"
ex analyse context --campaign pilot-sync
ex analyse context --campaign pilot-batch

step "4. power from the pilot's own noise (local, no API calls)"
ex power --noise-from pilot-sync --sims 40 --out data/power/power.json

step "done"
echo "log: $LOG"
echo "next: compare the two pilots' readings, then size the full campaign from the R above"
