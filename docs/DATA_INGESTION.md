# Getting data into the Safety Explorer

This is the question most likely to silently determine what the results mean, so it
gets its own document.

The naive answer is "call the API in a loop." That works for one lane and quietly
breaks the other two, and it makes an assumption that is false in practice: that the
thing you can reach through an API is the same thing a user talks to in a chat
window. It usually is not — different system prompt, different safety scaffolding,
sometimes a different serving configuration behind the same name.

So the design has **three ingestion lanes** feeding **one provenance model**.

---

## The provenance model

Everything that enters the database arrives as a `run` row, whatever lane it came
through. Every run carries:

| Field | Meaning |
|---|---|
| `lane` | `api` · `manual` · `import` |
| `provenance_tier` | `A` · `B` · `C` — how much we actually know (below) |
| `model_id` | the id we *asked for* |
| `model_reported` | the id the response *claimed to be*, when available |
| `model_alias_risk` | whether `model_id` is a moving alias (e.g. `-latest`) |
| `params` | temperature, top_p, max_tokens, seed, system prompt hash |
| `surface` | `api` · `web_chat` · `app` · `cli` · `third_party` |
| `unobservable` | explicit JSON list of what we know we cannot see |
| `corpus_version`, `prompt_hash` | exactly which prompt text produced this |
| `captured_at`, `latency_ms`, `finish_reason`, `usage` | timing and accounting |

### Provenance tiers

Rather than pretending all data is equal, each run declares how much is actually
known about how it was produced.

- **Tier A — fully specified.** API lane. We control the system prompt, sampling
  parameters and model id; we recorded the response verbatim with usage and timing.
  Reproducible up to sampling noise.
- **Tier B — partially specified.** Manual capture from a chat surface. We control
  the prompt text and know the surface and approximate time; we do **not** know the
  system prompt, safety scaffolding, or sampling parameters. Reproducible in
  spirit, not in detail.
- **Tier C — externally sourced.** Bulk import of transcripts produced elsewhere.
  Prompt text may not be byte-identical to a corpus prompt; matching is by hash where
  possible and fuzzy otherwise, and the match confidence is stored.

Analyses report tier composition, and the default surface aggregation uses Tier A
only. Mixing tiers is allowed but must be explicit — `--tiers AB` — and is labelled
in every output. This is the mechanism that stops convenience data from quietly
becoming the result.

---

## Lane 1 — Live API runner (primary)

`explorer run --campaign <name> --provider anthropic --model <id>`

A thin provider interface (`providers/base.py`) with implementations for Anthropic
and OpenAI-compatible endpoints, plus:

- **`mock`** — a deterministic, seeded pseudo-model. Not a joke feature: it lets the
  entire pipeline, linter, metrics, UI and analysis be developed and tested with zero
  API spend and zero network, and it gives the test suite a fixed point. It produces
  plausible responses whose degradation pattern is a *known function of the
  dimension vector*, so the analysis code can be validated against ground truth
  before being pointed at a real model.
- **`local`** — any OpenAI-compatible local endpoint (llama.cpp, vLLM, Ollama).
  Important for longitudinal work: a local model with pinned weights is the only
  configuration that *cannot* silently change under you, which makes it a control
  for RQ8.

The runner is resumable (skips cells already present for the campaign), rate-limit
aware with exponential backoff, and writes each response to the database as it
arrives rather than at the end — a 3-hour campaign that dies at 2h50m should not
lose anything.

### Multi-turn variants

Variant F (boundary recovery) is only meaningful *in the same conversation* as its E
predecessor. The runner therefore supports variants declaring
`conversation_with = "<variant_id>"`, replaying the prior exchange as context. Runs
store the full message list, not just the final prompt.

---

## Lane 2 — Manual capture (the honest lane)

The models people actually complain about are usually reached through a chat UI, not
an API. Excluding that surface because it is inconvenient would mean measuring the
wrong thing.

`explorer capture --prompt <prompt_id> --surface web_chat --model "GPT-5.6 Sol"`

opens an editor (or reads stdin), and the web UI has an equivalent paste form that:

1. shows the exact prompt text with a **copy** button,
2. takes a pasted response,
3. asks for the small set of things a human genuinely knows (surface, model label as
   displayed, approximate time, whether a new conversation was started),
4. stores it as Tier B with `unobservable` pre-populated with the standard list
   (system prompt, sampling params, safety scaffolding, routing).

Manual capture is slow — realistically 3–5 minutes per response. That is the real
constraint on corpus size, and it is the reason the corpus is 58 prompts rather than
500. A 58-prompt manual pass is about four hours. A 500-prompt manual pass never
happens, and a benchmark that never gets run is worth nothing.

---

## Lane 3 — Bulk import

`explorer import transcripts.jsonl --format jsonl --surface api --tier C`

For transcripts produced elsewhere. Expected shape (one object per line):

```json
{"prompt": "...", "response": "...", "model": "...", "timestamp": "...", "params": {}}
```

Prompts are matched to the corpus by SHA-256 of normalised text; unmatched rows are
stored with `prompt_id = null` and `match_confidence`, and are visible in the UI as
*unmatched* so they can be triaged rather than silently dropped.

A `--format chatml` adapter handles message-list transcripts.

---

## What we deliberately do **not** do

- **No scraping of chat UIs.** Automating a web chat interface violates terms of
  service and produces data whose provenance is worse than the manual lane, not
  better, because it hides the automation from the recorded metadata.
- **No prompt synthesis at run time.** A generated prompt has no twin and no lint
  guarantee, so it cannot support the central comparison. The UI's sliders *select*
  from the authored corpus; they do not generate.
- **No silent retries on refusal.** If a model refuses, that is the measurement.
  Retrying until compliance would invert the instrument's purpose. Retries happen
  only on transport errors, and the retry count is stored.

---

## Recommended first collection

The minimum that produces a publishable-quality result:

1. `explorer init && explorer lint` — verify the corpus is sound.
2. `explorer run --provider mock --campaign smoke` — verify the pipeline end to end,
   free and offline.
3. `explorer preflight --model claude-opus-5 --repeats 3` — validate credentials and
   parameter legality, count input tokens exactly, make **one** real call, and print a
   cost estimate. Ten seconds that prevents discovering a bad parameter on cell 1 of
   246.
4. `explorer run --provider anthropic --model claude-opus-5 --campaign v1-baseline --repeats 3`
   — Tier A, 82 prompts × 3 repeats = 246 responses.
5. `explorer annotate --plan --limit 60` — inspect what will be served, then
   `explorer serve` and annotate through the blinded UI. Roughly two evenings.
6. `explorer annotate --reliability` — re-serve 20% blind for intra-rater α.
7. Optionally repeat step 4 against a chat surface via the manual lane for a
   Tier A / Tier B comparison. That comparison is itself an interesting result: it
   measures how much of "the model feels different" is the model versus the harness
   around it.

Step 7 is the one most people skip and the one most likely to explain the original
complaint that motivated this project.

## Two things that would waste the run

**Sampling parameters are removed on the current frontier models.** Sending
`temperature` to Opus 5, Sonnet 5, Opus 4.8/4.7 or the Fable family returns a 400 —
on every call, so a campaign fails entirely rather than degrading. The provider refuses
to send one to those models and says so; `preflight` catches it before any spend.

**Hosted model ids carry no date suffix.** `claude-opus-5` *is* the id; there is no
dated variant to pin. So a hosted id cannot pin weights for longitudinal purposes. The
honest response is not to pretend otherwise: every run records `model_reported` (what
the server says it served) and `captured_at`, and a local pinned-weight model via
`--provider local` remains the only true control for drift.

## Spending the annotation budget

Human annotation is the scarce resource, and **which** responses get annotated matters
more than how many. A twin delta needs *both* members of a pair rated, so sampling runs
uniformly at random is close to the worst possible use of the budget: 60 annotations
drawn at random from a 246-run campaign complete about **7** twin pairs out of ~123,
because the chance of catching both members of a pair is roughly (60/246)².

`explorer annotate` selects for coverage instead, which completes about **42** pairs
from the same 60 ratings. Baselines are shared — within a family, rating
{C, D, E, C_intro, D_intro, E_intro} is 6 ratings that complete 5 pairs, because C is
the baseline for D, E and C_intro at once. Selection round-robins across families so
none is starved, and reserves a share for controls, which have no twins and would
otherwise never be selected.

Run `explorer annotate --plan` to see the selection before committing an evening to it.
