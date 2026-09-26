# Contributing

Thank you for helping. This is a research instrument, so a few rules matter more here than
in most code.

## Set up

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
explorer lint          # the corpus must lint clean
python -m pytest -q    # the browser tests skip themselves without Playwright + Chromium
```

## The rules that keep the measurements honest

- **The corpus varies framing over non-hazardous substrate.** Every prompt must satisfy
  `docs/CONTENT_POLICY.md` and carry a hazard review; `explorer lint` enforces it. A prompt
  that would give real uplift if answered is a bug, whatever its label.
- **Twins differ along one dimension.** The linter checks it; do not silence it.
- **Analyses are pre-registered.** Changing an analysis after seeing data is allowed only in
  the open: say so in `docs/PREREGISTRATION.md` and in the pull request, and keep the original
  analysis runnable.
- **Controls stay with their measures.** A new reading ships with the control that could show
  it to be an artefact (a null control, a calibration, a floor), and with a test of that control.
- **Nothing is pooled silently.** Provenance tiers, cue arms and languages are kept apart unless
  a caller asks to pool them, and the output says so when they are.
- **Tests come with the change.** A bug fix starts with the test that reproduces it.

## Pull requests

Keep them focused, describe what changed and why, and note anything that changes a reported
number. If you change something the self-updating app depends on (the `safety_explorer.server`
entry point, or the `EXPLORER_*` path variables), raise `[tool.explorer-loader] api` in
`pyproject.toml` and say so — older apps will then decline the version instead of breaking.

## Releasing

A release is a merged pull request. Bump `__version__` in `src/safety_explorer/__init__.py`,
set `version` and `date-released` in `CITATION.cff`, and add `docs/releases/v<version>.md` (the
release notes). When that lands on `main`, `.github/workflows/release.yml` builds and
smoke-tests the macOS app, creates the `v<version>` tag and the GitHub Release from those notes,
and attaches the app. Apps on the stable channel pick the release up at their next launch.
Nobody pushes tags by hand. Notes drafted for a version the code does not carry yet wait until
the version bump lands with them.

## Reporting problems

Bugs and ideas: GitHub issues. Security problems: see `SECURITY.md`. A model producing
genuinely hazardous content during a run is a finding for that model's provider, not an issue here.
