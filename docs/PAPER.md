# From campaign to paper

The paper's numbers come from the database, through the hypotheses as Amendment A1 of
[PREREGISTRATION.md](PREREGISTRATION.md) states them. None of them is typed by hand. The order
below matters, because some steps are only meaningful before the data exists.

## 1. Before any real data: the amendment

Amendment A1 is committed before any Tier A campaign against a real model. It makes three
choices:

- **Graded accuracy is the primary outcome.** Layer 0 graded accuracy (`truth_graded`) is
  primary, and blinded human ratings validate it.
- **H13 is added.** The context-depth arm becomes a pre-registered hypothesis.
- **The confirmatory family is fixed.** Holm–Bonferroni is applied across it.

`explorer paper` enforces the timing from stored timestamps. A campaign created before
2026-10-12 is analysed as exploratory, whatever it shows.

## 2. The pilot sets the sample size

```bash
explorer run --campaign pilot --provider anthropic --model claude-opus-5-5 \
             --repeats 2 --only orbital_debris impactor_deflection --depths 0 32k
explorer power --noise-from pilot --repeats 3 5 8 --sims 40
```

The pilot needs **two or more repeats**, because between-repeat noise cannot be measured
from one. `power --noise-from` measures that noise and calibrates the mock to reproduce
it. It then simulates the confirmatory interaction tests (H4b, H11b, H13b) many times at
zero effect and at the documented effect, and applies the A1.6 rule: the smallest number
of repeats with **≥ 80% power** and a **≤ 5% false-positive rate**.

The pilot's data never enters a confirmatory analysis.

## 3. The campaigns

```bash
explorer run --campaign v1 --provider anthropic --model claude-opus-5-5 \
             --repeats <R from step 2> --cues 1 3 5 --depths 0 8k 32k 128k --batch
```

You can split the main, cued and depth arms into separate campaigns if you prefer. Then
name each one with `--cue-campaign` and `--depth-campaign` in step 5.

## 4. Validation

Blinded annotation as before (`explorer annotate`, then the UI). Then:
- a second annotator, so there is an inter-rater α;
- a 20% re-serve, for intra-rater α.

`explorer paper` reports the Spearman correlation between human `capability_retention` and
graded accuracy against the A1.3 criterion: ρ ≥ 0.5, with the CI lower bound ≥ 0.3. If
the criterion fails, every Layer 0 result carries the flag.

## 5. The numbers

```bash
explorer truth && explorer features     # re-score under the frozen versions (free)
explorer paper --campaign v1 --out paper/generated
```

This writes `paper/generated/`:

| File | What it is |
|---|---|
| `numbers.tex` | One macro per reported number: `\resHFourBest`, `\resHFourBci`, `\resHFourBp`, `\resHFourBverdict`, … |
| `tables/hypotheses.tex` | Every hypothesis: estimate, CI, p, Holm threshold, verdict |
| `tables/context_depth.tex`, `figures/context_depth.tex`, `figures/ladder.tex` | Tables and pgfplots figures; no image conversion step |
| `preamble.tex` | The packages those need |
| `results.json`, `MANIFEST.json` | Everything, machine-readable: versions, models, `model_reported`, truncation by depth, git commit, database hash |
| `README.md` | The results in plain words |

In the paper: `\input{generated/preamble}` in the preamble, `\input{generated/numbers}`,
then write *"the depth interaction was \resHFourBest{} (95\% CI \resHFourBci{})"*.
Re-running `explorer paper` updates every number in place.

**Warnings change what a result may be called.** The manifest lists them, and the
verdicts carry them:
- pre-amendment data;
- runs scored under a different solver version;
- more than 5% truncation at any depth.

## What the simulator found before any money was spent

`explorer power --scales 0 1 --repeats 3 5 8 --sims 20`, at two noise levels, on
2026-10-12. Each cell is 20 simulated campaigns, so every rate below carries a Wilson
interval about ±0.2 wide. The **pooled** false-positive rates (all null cells of a test)
are the more reliable figures. The nominal rate for a one-sided "95% CI excludes zero"
test is 2.5%.

| Test | Pooled false-positive rate at zero effect | Power at the documented effect (noise 0.15) |
|---|---|---|
| H4b (expertise × risk) | **9 / 120 = 7.5%** | 0.55 (R=3), 0.50 (R=5), 0.80 (R=8) |
| H11b (cue − placebo) | 0 / 120 | 0.95–1.00 at every R |
| H13b (context depth) | 3 / 60 = 5% | 1.00 at every R |

H13b's figures come from the rerun with independent draws at each depth (see below).

**H4b runs hot.** Its false-positive rate is about three times the nominal 2.5%, at both
noise levels. The cause is the interval, not the data. It is a percentile bootstrap over
only four intent-focal families, and with that few groups the interval is too narrow.
Discrete per-target outcomes make it worse. H4b is also the one test with weak power at
realistic noise: it needs R = 8 to reach 0.8. This has to be settled **before** data,
because changing a test afterwards is exactly what pre-registration forbids. The
options:

1. **Add intent-focal families.** This fixes the cause, but needs corpus work.
2. **Use a test that stays valid with few groups.** With four families, an exact
   family-level sign-flip test cannot reach p < 0.05 at all (the smallest p is 1/16).
   That is itself the honest statement of what four families can support.
3. **Keep the test, but demote H4b** to secondary, with its simulated false-positive
   rate reported beside it.

**H13b, after a fix to the mock.** In the first run H13b showed 0 false positives, which
was an artefact. The mock seeded each response from the task text alone, so a padded cell
reproduced its depth-0 response exactly, and no test could misfire on it. Power runs now
use `context_reseed`, which gives each depth its own draw, as a real model would. After
that change H13b's false-positive rate is 5% pooled and its power stays 1.00.

**The noise assumption decides R.** At the mock's own noise every test reaches the bar at
R = 3. At noise 0.15 H4b does not reach it at any R, so the amendment's rule says to run
at R = 8 and report H4b as underpowered. The real figure comes from
`--noise-from <pilot>`; until then neither noise level is more than a placeholder.
