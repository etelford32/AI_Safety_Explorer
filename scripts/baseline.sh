#!/usr/bin/env bash
# Baseline: the whole 109-prompt corpus against each model, no padding, no cues.
#
#   bash scripts/baseline.sh                       # Opus 5.5, Sonnet 5.5, Haiku 5.5
#   bash scripts/baseline.sh claude-opus-5-5       # one model
#
# Batch API (half price), 2 repeats, effort pinned to `high` so the models are compared
# at the same setting rather than at their different defaults. About $5-10 for all
# three. Doubles as the pilot: `explorer power --noise-from baseline-<model>` sizes the
# full study from it. Exploratory by Amendment A1 (pilot data never enters a
# confirmatory analysis). Resumable: re-run the same command after any interruption.
set -euo pipefail

MODELS=("$@")
[ ${#MODELS[@]} -eq 0 ] && MODELS=(claude-opus-5-5 claude-sonnet-5-5 claude-haiku-5-5)
DB="${EXPLORER_DB:-data/explorer.db}"
mkdir -p data/exports
LOG="data/baseline-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1
ex() { explorer --db "$DB" "$@"; }

if [ -z "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  echo "No ANTHROPIC_API_KEY in the environment."; exit 2
fi
# An organisation-level key also needs: export ANTHROPIC_WORKSPACE_ID=wrkspc_...
if [ -n "${ANTHROPIC_WORKSPACE_ID:-}" ]; then echo "workspace: ${ANTHROPIC_WORKSPACE_ID:0:10}…"; fi

ex lint >/dev/null && ex init >/dev/null
echo "== credentials and parameters (about \$0.05)"
ex apicheck --model "${MODELS[0]}"

for m in "${MODELS[@]}"; do
  echo; echo "== baseline-$m"
  ex run --campaign "baseline-$m" --provider anthropic --model "$m" \
         --repeats 2 --effort high --batch --poll 60 --resume
done

echo; echo "== readings"
for m in "${MODELS[@]}"; do
  echo; echo "--- $m"
  ex analyse twins --campaign "baseline-$m" --source truth_graded
  ex analyse controls --campaign "baseline-$m"
  ex export --campaign "baseline-$m" --out "data/exports/baseline-$m.jsonl"
done

echo; echo "== summary: the models side by side (local, no API calls)"
SUMMARY=()
for m in "${MODELS[@]}"; do SUMMARY+=(--campaign "baseline-$m"); done
ex summary "${SUMMARY[@]}" --out data/summary/baseline

echo; echo "== repeats needed for the full study (local, no API calls)"
ex power --noise-from "baseline-${MODELS[0]}" --sims 40 --out data/power/power.json

echo; echo "log: $LOG · data: $DB · exports: data/exports/ · browse: explorer serve"
echo "share: data/summary/baseline/SUMMARY.md holds aggregates only, no prompt or response text"
