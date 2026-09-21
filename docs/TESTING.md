# Testing the instrument against a real model

The mock is exhausted as a validator. The test suite proves the *analytics* are correct —
411 checks, every arm with a falsification test — but three things are structurally
invisible to a mock and only a real model can exercise them:

1. **The null control's real job.** The mock never echoes its prompt's vocabulary into its
   answer, so the alarming-benign arm cannot actually test whether a lexicon reads *topic*.
   A real model's answer to a coup question genuinely contains "seize", "control", "take
   over" — that is what gives `stance_control_null` and `powerseeking_control_null` teeth.
2. **Real Layer 0 degradation.** Does capability *actually* fall as intent rises, or is the
   cliff a mock assumption? Only an answer key scored against real responses says.
3. **Register recall on natural prose.** The whole recall problem — and the embedding fix —
   only bites on real free-form prose, not on the mock's composed canonical phrases.

This runbook drives the **corpus arm against a local model**. A local model with pinned
weights is the cheapest surface for a full sweep *and* the only configuration that cannot
silently change under you — so it is the control for longitudinal drift (RQ8), not a
second-class option. The agent arm (where expressed agency actually appears) is sketched at
the end and gets its own harness.

Everything here is push-never-pull and local-only: the tool makes the calls, nothing leaves
`127.0.0.1`, and no key is required for a local endpoint.

---

## 0. Prerequisites

```bash
pip install -e '.[openai,embeddings]'    # the local provider + the semantic backend
```

- `openai` — the `--provider local` path speaks the OpenAI chat-completions wire format,
  which every local server below also speaks.
- `embeddings` — `sentence-transformers`, for the embedding register model. Without it the
  register and power-seeking readings fall back to the lexicon (honestly, and they say so),
  but the recall fix — catching a reach or a warm turn expressed in novel words — needs it.

Pick a local server. Any OpenAI-compatible endpoint works; three common ones:

| Server    | Start it                              | Endpoint (`OPENAI_BASE_URL`)   | Model id (`--model`)     |
|-----------|---------------------------------------|--------------------------------|--------------------------|
| Ollama    | `ollama serve` + `ollama pull llama3.1` | `http://localhost:11434/v1` *(the default)* | `llama3.1`      |
| vLLM      | `vllm serve <hf-model> --port 8000`   | `http://localhost:8000/v1`     | the model you served     |
| LM Studio | start its local server                | `http://localhost:1234/v1`     | the model you loaded     |

`--provider local` already defaults its base URL to Ollama's, so with Ollama you set
nothing. For vLLM or LM Studio, export the endpoint:

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1     # vLLM / LM Studio
```

Pin the exact weights you are testing (a quantization, a snapshot) and record which — a
local model is only a drift control if you can say later what it was.

---

## 1. Gate the embedding backend — `explorer register`

The register and power-seeking readings route through the embedding model **only when it
passes its own generalization control**. Turn it on and confirm it earns the routing:

```bash
export EXPLORER_EMBED_BACKEND=minilm
explorer register
```

Read the output. `separation` should PASS on every axis (the exemplar poles are coherent).
`generalization` is the one that matters — it must PASS, which means the backend places
held-out probes that share *no words* with the anchors:

```
generalization (real embedding vs bag of surface forms)   PASS
  warmth        margin +0.xx  (placed)
  ...
  power_seeking margin +0.xx  (placed)
trustworthy: True
```

If it says `trustworthy: False` with backend `hashing`, the extra did not install or the
env var is unset, and every reading below will be the lexicon's — correct, but low-recall.
Fix that before trusting a "no reach / no warmth" reading on natural prose. (A first
`minilm` run downloads the weights from Hugging Face; if your network blocks that, point
`EXPLORER_EMBED_MODEL` at a local copy of the model directory.)

This is a genuine gate, not a formality: a backend that fails generalization is declining to
claim the recall fix, and the tool routes back to the lexicon and says so on every surface.

---

## 2. Preflight — one call before a whole sweep

```bash
explorer preflight --provider local --model llama3.1 --repeats 1
```

`preflight` checks the endpoint is reachable, verifies the request parameters are legal,
counts input tokens, and makes **exactly one real call** so you find a bad model id or a
truncation ceiling in ten seconds rather than on cell 200. Watch two lines:

- `stop_reason` on the probe call. If it is `max_tokens`, **raise `--max-tokens`** — a
  truncated derivation scores as capability loss and corrupts Layer 0. The corpus's physics
  answers can run long; `run` defaults to 8000, but a small local model with a short context
  may need a lower ceiling *and* shorter expectations, so check here first.
- `model_reported` — what the server says it served. On a local model this should be the
  fixed id you pinned; if it drifts, your "control" is not one.

A local model has no cached price, so preflight prints "no cached price" and skips the cost
estimate — expected, and the run is free.

---

## 3. Smoke run — a few families, one repeat, eyeball it

Do not launch the full sweep first. Run a slice and confirm the readings are sane:

```bash
explorer run --campaign smoke --provider local --model llama3.1 \
             --repeats 1 --only physiological_limits control_autonomy \
                              orbital_debris alarming_benign
```

`physiological_limits` and `control_autonomy` carry a range of granted autonomy (the
power-seeking axis); `orbital_debris` gives benign baselines; `alarming_benign` is the null
control's alarming pool. Then look:

```bash
explorer analyse powerseeking --campaign smoke
explorer analyse stance       --campaign smoke
explorer analyse depth        --campaign smoke --source truth_graded
explorer serve      # and open the Stance and Results views
```

What you are checking on this slice is that the *pipeline* produces defensible numbers on
real prose, not that any finding replicates yet (one repeat, few families — the intervals
will be wide and say so). Sanity checks:

- **Layer 0 is not all-or-nothing.** `analyse twins --source truth_graded` should show a
  spread, not every cell at 0 or 1. All-1.0 usually means the answer key isn't matching the
  model's format; all-0.0 means the model refused everything or truncated.
- **The register reads via the backend you gated.** `analyse powerseeking` and `analyse
  stance` print their source; it should say `embedding` if step 1 passed. If it says
  `lexicon` here, step 1 did not take.
- **The null control is small.** `powerseeking_control_null` needs real responses to mean
  anything (this is the first run where it does): the alarming-benign vs benign gap should
  sit near zero. A large gap is the first real signal that a lexicon is reading topic — worth
  stopping to read the flagged spans before trusting anything downstream.

---

## 4. The full sweep

```bash
explorer run --campaign v1-local --provider local --model llama3.1 --repeats 3
```

`--repeats 3` is the minimum for RQ7 (are boundaries stable across runs) and for the
bootstrap intervals to estimate at all. Local is free, so there is no Batch API step to
think about; if you later run this against a hosted model, `preflight` prints the Batch
price, which halves the cost of a non-interactive sweep.

`run` resumes by default — re-running the same `--campaign` fills only the missing cells, so
an interrupted sweep costs nothing to continue.

---

## 5. What to read, and what "suspect" looks like

```bash
explorer validate                                      # every control, against the mock
explorer analyse powerseeking --campaign v1-local
explorer analyse stance       --campaign v1-local
explorer analyse depth        --campaign v1-local --source truth_graded
explorer analyse sandbagging  --campaign v1-local --source truth_graded
```

- **`validate` first.** It runs every control and recovery check against the mock and asks
  "is the instrument sound today". It does not touch your real campaign — it certifies the
  *machinery* before you read a real number through it. A `warn` is not a pass; it means a
  check could not run.
- **power-seeking** — read `by_granted` (does expressed agency track the grant, and where
  does it clear it?), the flagged spotlight list *with its spans* (a reach is a claim about
  a specific sentence — read the sentence), and `control_null`. The source line tells you
  whether you are reading the embedding axis or the lexicon.
- **stance** — the decoupling plane is the finding-shaped part: the **warm-refusal** cell
  (friendly, apologetic, empty) is the failure human raters miss, and `tone_bias` says
  whether *your own* future ratings would track register instead of content.
- **depth / sandbagging** — the difference-in-differences and dose-response charts in the
  Results view; both refuse to present an effect the data can't carry and say so.

Two failure modes that are the instrument working, not breaking:

- A cell that is **fully refused on both sides of a twin** cannot show an interaction — the
  analysis reports the null rather than inventing a gap. Read the per-level numbers before
  reading a summary null.
- A **wide or `[nan, nan]` interval** means too few families or repeats contributed, not a
  bug. Intervals bootstrap over families; one family cannot estimate one.

---

## 6. Offline: analyse transcripts you already have

If you have real transcripts (from an agent run, a log, a saved chat) and just want the
analysis over them, you do not need a live model at all:

```bash
explorer import transcripts.jsonl --format jsonl --tier C
explorer analyse powerseeking
```

Layer 0 applies only where a response matches a corpus prompt; everything else (register,
power-seeking, spans, refusal signal) is computed regardless, at the honest provenance tier.
See [`docs/DATA_INGESTION.md`](DATA_INGESTION.md) for the lanes and the provenance model.

---

## 7. Next: the agent arm (where agency actually shows)

The corpus arm tests power-seeking against a *stated* mandate (the `autonomy` coordinate). A
model reaching past its grant shows up most in an **agent loop** — tools, a real mandate, and
the option to reach past it. The harness to build (Tier 2):

- A tool-use loop (the Anthropic SDK's tool runner, or the OpenAI tool-calling loop for a
  local model) under a system prompt with one knob: **grant** autonomy ("act without
  confirmation") vs **withhold** it ("propose, then wait"). The granted-vs-withheld pair is
  the twin structure `overreach` wants.
- A few reachable, side-effect-free tools the model can *choose* to reach for: `deploy`,
  `read_logs`, `request_more_access`, `self_shutdown`. None of them do anything; they exist
  so a reach is expressible.
- Each turn POSTs to `/api/session/turn` (see [`docs/INTEGRATION.md`](INTEGRATION.md) for the
  contract and a worked hook). The Sessions view then shows the register trajectory, the
  drift alert, and — with the embedding backend gated on — the power-seeking reading per
  turn, on free-form agent prose where the lexicon alone would under-read.

This is the surface where the recall fix earns its keep, and it is the natural second
campaign once the corpus arm confirms the base instrument reads a real model sensibly.
