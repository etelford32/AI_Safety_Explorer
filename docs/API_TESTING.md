# Testing against the live API

A staged plan for the first real runs of the context-depth work. Each stage is cheap, and
each one checks the assumption the next stage spends money on. Go on only when a stage passes.

The figures below come from `pricing.depth_estimate`, using list prices as of 2026-10-06 and
an assumed 1,500–3,000 output tokens per response (adaptive thinking counts as output).
`preflight` replaces them with exact token counts and one measured response.

**In one command:** `bash scripts/pilot.sh [MODEL]` runs stages 1–3 and the power step in
order. It stops at the first failure, asks before the paid pilot, and logs to `data/`.

## 0. Setup

```bash
pip install -e '.[dev,anthropic]'
export ANTHROPIC_API_KEY=...          # or `ant auth login`
explorer lint && explorer init
```

Use a fresh database for these runs (`--db data/api-tests.db`), so test cells never sit beside
campaign data.

## 1. `explorer apicheck` — about $0.05, under a minute

```bash
explorer --db data/api-tests.db apicheck --model claude-opus-5-5
```

| Stage | Passes when | If it fails |
|---|---|---|
| credentials | `count_tokens` answers | key, model id, network |
| sync call | one plain request succeeds | the printed request parameters |
| filler calibration | a 6,000-token filler measures within 0.5% | tokenizer drift — report it |
| cache write | the first padded request writes the filler | prefix under the model's minimum, or caching off |
| cache read | the second request reads at least 90% of it | something in the prefix varies between requests |
| pre-warm | `max_tokens: 0` is accepted, or the call falls back to 1 | the batch pays one extra cache write; nothing else changes |

Then once with the batch stage, which usually takes minutes but can take longer:

```bash
explorer --db data/api-tests.db apicheck --model claude-opus-5-5 --batch
```

This checks that a two-request batch succeeds, that its usage is marked `batch`, and whether
the batch read the filler from cache. Inside a batch, cache hits are best-effort. If the
batch stage reports 0 tokens read from cache, the cached batch estimates below are optimistic.
Plan on the uncached batch column instead.

## 2. `preflight` — free, plus one real call

```bash
explorer --db data/api-tests.db preflight --model claude-opus-5-5 --repeats 3 \
         --depths 0 8k 32k 128k
```

This sizes every filler exactly and keeps the calibration, so the run does not count tokens
again. It then measures one real response and prints the four-way cost table.

## 3. Pilot — about $2–5 sync, or $1.80–2.80 batch (Opus 5.5)

One family, **two repeats** (power needs between-repeat noise, so one repeat is not enough),
two depths. Run it both ways, so the batch path can be compared with the synchronous one on
the same cells:

```bash
explorer --db data/api-tests.db run --campaign pilot-sync  --provider anthropic \
         --model claude-opus-5-5 --repeats 2 --only orbital_debris --depths 0 32k
explorer --db data/api-tests.db run --campaign pilot-batch --provider anthropic \
         --model claude-opus-5-5 --repeats 2 --only orbital_debris --depths 0 32k --batch
explorer --db data/api-tests.db analyse context --campaign pilot-sync
explorer --db data/api-tests.db analyse context --campaign pilot-batch
```

Check before going further:

- **Cache hits.** The printed cache share at 32k should be well above zero for the synchronous
  campaign: in each 18-cell repeat block, every cell after the first should read the filler.
- **Truncation.** Look for `stop_reason=max_tokens`. A truncated response is excluded as a
  corrupted measurement. If many cells are truncated, raise `--max-tokens`.
- **Refusals.** Refusals show up as `finish_reason=refusal` with a category in
  `stop_details`. That is data, not an error.
- **Compliance.** Compliance at depth 0 should be high on the benign variants. If it is low,
  read a few responses in Co-analysis before trusting the checker on this model.
- **Recorded cost.** Compare it with the pilot estimate. A large gap means the output-token
  assumption is wrong for this model, so re-run `preflight`.
- **Resume.** Stop the batch run while it is waiting (Ctrl-C), run the same command again,
  and check that it says `collecting batch …` rather than submitting a new one.

Then set the number of repeats from the pilot's noise (Amendment A1.6). This runs locally
and makes no API calls:

```bash
explorer --db data/api-tests.db power --noise-from pilot-sync --sims 40
```

Pilot data never enters a confirmatory analysis. See [PAPER.md](PAPER.md).

## 4. The full sweep

109 runnable prompts × 4 depths × 3 repeats = 1,308 cells.

| Model | Sync, no cache | Sync + cache | Batch, no cache | Batch + cache |
|---|---:|---:|---:|---:|
| claude-opus-5-5 | $260–300 | $54–93 | $130–150 | $28–48 |
| claude-sonnet-5-5 | $130–150 | $32–52 | $65–75 | $17–27 |

```bash
explorer --db data/explorer.db run --campaign v1-depth --provider anthropic \
         --model claude-opus-5-5 --repeats 3 --depths 0 8k 32k 128k --batch
```

## Things that would quietly spoil the measurement

- **No server-side refusal fallback.** The API can re-run a refused request on another model.
  For an instrument whose subject is the refusal boundary, that would swap the model being
  measured mid-cell. The provider never sends `fallbacks`. Keep it that way.
- **Effort defaults differ by model.** Opus 5.5 defaults to `medium` and most other current
  models to `high`. If you compare models, pass `--effort` explicitly so the comparison is not
  between defaults.
- **Thinking cannot be turned off on Opus 5.5, Sonnet 5.5 or Fable 5/5.1.** `--thinking off`
  is refused before any call. Use `--effort low` for the cheapest setting.
- **Hosted model ids can move.** Every run records `model_reported`. A drift comparison
  across weeks has to check it.
