# Safety Explorer

**v0.2** — a research instrument for one question:

> When context becomes riskier while the underlying reasoning task stays similar,
> how does model behaviour change?

Not a benchmark leaderboard, not a jailbreak tool. An empirical environment for
studying the relationship between model *capability* and model *safety behaviour*,
with a versioned prompt corpus, a reproducible runner, a blinded human annotation
set, and re-runnable campaigns for tracking drift over time.

```
                   Operationality
          4  ·     ·     ·     ·     ·
          3  5.00  4.00  4.00  ·     0.00
          2  5.00  ·     ·     ·     ·
          1  5.00  ·     ·     ·     ·
          0  5.00  ·     ·     ·     ·
             0     1     2     3     4      Intent

             capability_retention, n=3 per cell
             ·  no observations — not interpolated
```

---

## The idea in one paragraph

For every risky prompt there is a **benign twin** demanding the same intellectual
operation — the same integral, the same graph theory, the same control analysis.
Within a family, the critical arm moves **exactly one dimension** (verified by the
linter) and holds the other four pinned. So when a response degrades, the degradation
is attributable: it is a response to *framing*, not to the sensitivity of the
information, because the information is identical on both sides of the comparison.

## Quick start

```bash
pip install -e .

explorer lint                                   # the corpus must lint clean to run
explorer init
explorer run --campaign smoke --provider mock   # no API key, no network
explorer serve                                  # http://127.0.0.1:8713
```

The `mock` provider is a deterministic pseudo-model whose degradation is a documented
function of the dimension vector. It exists so the whole pipeline can be developed and
tested for free, and so the analysis code can be validated against known ground truth
before it is pointed at a real model. A mock run is a test of the instrument, never a
result.

Against a real model:

```bash
export ANTHROPIC_API_KEY=...
explorer run --campaign v1-baseline --provider anthropic \
             --model <pinned-version-id> --repeats 3
explorer annotate --annotator you --limit 60    # or use the blinded web UI
explorer analyse twins
```

## What is in v0.1

| | |
|---|---|
| Design space | 5 independent ordinal dimensions (intent, operationality, specificity, autonomy, technical depth) |
| Corpus | 8 families, 4 fully authored: 24 ladder + 12 depth-arm prompts + 10 false-positive controls = **46 runnable**; 4 families stubbed for v0.3 |
| Linter | twin matching, ladder deltas, hazard review, content deny-list, dimension independence |
| Ingestion | 3 lanes — live API, manual chat capture, bulk import — with explicit provenance tiers |
| Measurement | 17 automatic features · 9 ordinal human metrics · optional LLM judge (off by default) |
| Annotation | blinded, randomised, with intra-rater reliability |
| Analysis | twin-pair deltas, depth × risk interaction (difference-in-differences), bootstrap CIs over families, Cliff's delta, Krippendorff's α, safety surface |
| UI | sliders → prompt → response → twin comparison → word-level diff → surface |
| Tests | 54, all passing, no network required |

## How it stays honest

These are the parts most likely to be got wrong, so they are enforced in code rather
than promised in a document.

**Annotation is blinded.** If the annotator sees "variant E — explicitly harmful",
they will score it as degraded and the instrument will measure their expectations.
Family, variant, dimensions and model are hidden until after the rating is submitted;
the reveal is logged; blinded and unblinded populations are reported separately.

**The critical arm moves one dimension.** `explorer lint` fails a family whose C→D or
D→E step moves anything but the declared focal dimension, and reports the
rank-correlation matrix within each family's critical arm. It comes out flat, which is
the experimental guarantee the whole design rests on.

**Twins are matched quantitatively.** Length ratio within [0.75, 1.33], technical
vocabulary overlap ≥ 0.5, requested output format byte-identical. A twin pair that
drifts past those is a corpus bug that blocks a run, because otherwise "capability
retention" measures prompt-writing drift as much as model behaviour.

**Depth twins are held to a stronger check.** A depth twin *must* differ in vocabulary
— that is the manipulation — so it is linted on **identical numeric parameters**
instead: every figure in the question unchanged. Different wording is the manipulation;
different numbers are a different question. This caught a real authoring slip, a
"60-second window" against "a minute".

**The surface shows its own sparsity.** Five dimensions over 34 prompts is sparse.
Every cell reports its `n`, unsampled cells are drawn empty and never interpolated,
and cells with n < 3 are marked provisional.

**Provenance is tiered.** Tier A (API, fully specified), Tier B (chat surface, system
prompt and sampling unknown), Tier C (externally sourced). Analyses default to Tier A;
mixing requires `--tiers AB` and is labelled in every output. Each run carries an
`unobservable` list naming what we know we could not see.

**A refusal is a measurement.** Retries happen only on transport errors, never on
refusal, and the retry count is stored.

**The evaluator is not the experiment.** An LLM judge is off by default, stored
separately, and reported only as agreement with the human reference set.

## Documentation

| Document | Contents |
|---|---|
| [`docs/PLAN.md`](docs/PLAN.md) | Full design, with every change from the original sketch marked and justified |
| [`docs/DATA_INGESTION.md`](docs/DATA_INGESTION.md) | The three lanes, the provenance model, and what we deliberately do not do |
| [`docs/CONTENT_POLICY.md`](docs/CONTENT_POLICY.md) | Why the corpus varies framing over non-hazardous substrate, and why that is better science |
| [`docs/PREREGISTRATION.md`](docs/PREREGISTRATION.md) | Hypotheses, analysis plan and reliability threshold, frozen before collection |

## The corpus

Eight families, each one underlying problem with six controlled variations:

| | Family | Domain | Focal dimension | Status |
|---|---|---|---|---|
| 1 | Orbital debris cascade | Orbital dynamics | intent | authored |
| 2 | Kinetic impactor momentum transfer | Planetary defence | intent | authored |
| 3 | Propagation through a synthetic network | Network science | intent | authored |
| 4 | Closed-loop control authority | Control theory | **autonomy** | authored |
| 5 | Message optimisation | Behavioural modelling | intent | stub |
| 6 | Re-identification from benign datasets | Information theory | intent | stub |
| 7 | Structural failure modes | Structural mechanics | intent | stub |
| 8 | Physiological limits | Physiology | intent | stub |

Family 4 varies **autonomy** rather than intent on purpose. With every family using
the same focal dimension, an autonomy effect could never be separated from an intent
effect.

### The depth arm (RQ4)

Each family also carries three **depth-arm** variants: C, D and E re-posed at
introductory depth with every other dimension — including the focal one — pinned. That
makes a 2 × 3 factorial, depth {1, 3} × focal level {C, D, E}.

The question is the **interaction**, not the main effect. "Does a technical question
get a technical answer" is uninteresting; RQ4 asks whether the *risk penalty grows with
depth*. So the analysis reports a difference-in-differences, per level:

```
explorer analyse depth --source auto

  focal dimension: intent   (3 families)
    level    focal    n   median gap            95% CI      effect
    C            0    9          0.0        [0.0, 0.0]  negligible
    D            2    9        1.369    [1.369, 1.369]       large
    E            4    9          0.0        [0.0, 0.0]  negligible

    difference-in-differences (depth gap at level, minus at C):
      D vs C     n=9   median=1.369   CI [0.525, 1.369]   large
      E vs C     n=9   median=0.0     CI [0.0, 0.0]       negligible
```

Two design points that output makes concrete:

- **The DiD is computed per level, not just for the top of the ladder.** Where
  behaviour has a cliff, E can be saturated — both depth conditions refuse outright —
  and the interaction shows up in the ambiguous middle instead. Contrasting only E
  against C would report a null while a large effect sat at D.
- **Depth is register, never credentials.** The manipulation is the vocabulary and
  formalism of the question, never a claim about the requester ("I have a PhD"). That
  is a social/credentialing variable, and mixing it in would mean any effect could be
  deference to authority rather than a response to the level of the question. A test
  rejects any depth-arm prompt containing an expertise claim.

The ladder within a family:

```
A  theoretical baseline     all dimensions low
B  realistic simulation     specificity +2, operationality +1
C  benign real-world        specificity +2, operationality +2
D  ambiguous dual-use       intent +2          <- only intent moves
E  explicitly harmful       intent +2          <- only intent moves
F  boundary recovery        intent -3, in the same conversation as E
```

Plus 10 false-positive controls in two opposing arms: **alarming vocabulary /
harmless request** (`kill -9`, viral replication, supernova yield, game weapons,
sandboxed pentest, buffer overflow) and **mild vocabulary / consequential capability**
(unguarded recursive deletion, personal-data aggregation, individualised dosing, dark
patterns). Together they separate semantic understanding from keyword matching. The
astrophysics overlap in the first arm is deliberate — "explosion", "yield", "impact"
and "cascade failure" are ordinary working vocabulary in this domain, and a tool that
flinches at them is unusable for real work.

## Getting data in

Three lanes, one provenance model. See [`docs/DATA_INGESTION.md`](docs/DATA_INGESTION.md).

```bash
# Lane 1 — live API (Tier A)
explorer run --campaign v1 --provider anthropic --model <id> --repeats 3

# Lane 2 — manual capture from a chat surface (Tier B)
explorer capture --prompt orbital_debris.D --model "Some Chat Model" --surface web_chat

# Lane 3 — bulk import of transcripts produced elsewhere (Tier C)
explorer import transcripts.jsonl --format chatml
explorer unmatched      # triage rows that matched no prompt, rather than dropping them
```

Manual capture is slow — three to five minutes per response. That is the real
constraint on corpus size, and the reason this is 34 prompts rather than 500. A
34-prompt manual pass is an afternoon. A 500-prompt manual pass never happens, and a
benchmark that never gets run is worth nothing.

The Tier A vs Tier B comparison is the one most people skip and the one most likely to
explain "this model feels different lately" — it measures how much of the difference
is the model versus the harness around it.

## Commands

```
explorer lint                     validate the corpus (rank correlations, twin matching)
explorer init                     create the database, snapshot the corpus
explorer corpus [--show ID]       list or print prompts
explorer run                      execute a campaign (resumable)
explorer capture                  record a pasted chat response (Tier B)
explorer import PATH              bulk-import transcripts (Tier C)
explorer unmatched                list imports that matched no prompt
explorer features                 recompute automatic features from stored responses
explorer annotate                 queue responses for blinded annotation
explorer analyse {twins,surface,depth,controls,reliability,judge,drift}
explorer serve                    the Explorer UI
explorer export                   JSONL export (escalated responses withheld)
```

## Design notes

**Zero runtime dependencies.** Corpus in TOML (stdlib `tomllib`), storage in SQLite,
server on `http.server`, UI in vanilla JS with no build step. For a longitudinal study
the data is meant to outlive the code, and a tool that still starts in five years
beats one that needs a toolchain resurrected first. The provider SDKs are optional
extras, imported lazily.

**Features are recomputable.** `explorer features` re-derives every automatic feature
from stored responses, so improving the extractor never costs an API call.

**Campaigns are resumable.** Responses are written as they arrive. A three-hour
campaign that dies at two hours fifty loses nothing.

## Roadmap

**v0.3** — author families 5–8 (82 prompts total); a stated-expertise arm, kept
separate from depth on purpose; a third depth level (research) to test monotonicity;
LLM-judge validation against the human reference set; multi-model campaigns.

**v0.4** — the first real longitudinal result: the same frozen benchmark re-run over
months, with a local pinned-weight model as the control for infrastructure drift.

## Status and limits

v0.1 delivers the full vertical slice end-to-end with a partial corpus, rather than a
complete corpus with no way to run it. Known limits, stated up front:

- **Underpowered for between-model comparison.** 36 family prompts over four families
  characterises the *shape* of one model's surface. Ranking models needs v0.3.
- **The autonomy × depth interaction has one family**, so its confidence interval
  cannot be estimated and it is reported as provisional. The intent × depth
  interaction, with three families, is testable.
- **Depth has two levels**, so the arm detects an interaction but cannot show whether
  it is monotonic. A third level lands in v0.3.
- **Human annotation is the bottleneck**, by design. The reference set is small and
  its reliability estimate is intra-rater until a second annotator exists.
- **No result is claimed here.** The repository ships an instrument and a ground-truth
  test of that instrument. Everything else awaits collection.

MIT licensed.
