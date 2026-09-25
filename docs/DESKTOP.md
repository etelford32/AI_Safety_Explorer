# The desktop app (macOS)

A double-clickable `AI Safety Explorer.app` with a real window and a Dock icon, wrapping the
same local web UI the browser shows. The Python server runs inside the app process on a
loopback port; the window is a native **WKWebView** pointed at it. Nothing about the
measurement changes — same UI, same endpoints, same database, same push-never-pull spine.
This is a front door, not a new surface.

Two front doors now share one launcher (`safety_explorer.desktop`):

- **windowed app** — a normal app with a Dock icon and a window (this page);
- **menu-bar app** — always-on, glance-at-a-badge (`docs/BACKGROUND.md`).

They are separate on purpose: a windowed app owns the main GUI loop, a menu-bar agent owns a
different one, and running both in one process fights over it. Pick the shape you want.

## The downloadable app is a self-updating loader (v0.31)

`AI Safety Explorer.app` is no longer a frozen copy of the Explorer. It is a small, stable
**loader** (`src/explorer_loader/`, entry `scripts/loader_app.py`) plus a Python runtime. At
launch it checks GitHub on the chosen channel (stable releases, the development branch, or a
named branch), downloads new code, verifies it will run on this app (it compiles, its loader
contract is one this app speaks, it needs nothing the app lacks), and starts it in the same
window — falling back to the last version that started, and finally to the copy baked into the
app. The app itself only needs rebuilding when the loader or the packages it carries change.
Installing, channels and data: [`INSTALL.md`](INSTALL.md). The build smoke-tests the bundle
(`packaging/build.sh`), and `.github/workflows/app.yml` builds it on a version tag and attaches
it to the release.

`explorer app` (below) still opens a window on the code in your checkout, for development.

## Run it now (dev mode — no packaging)

```bash
pip install -e '.[openai,embeddings,desktop]'
python scripts/app.py          # or:  explorer app
```

A native window opens on the dashboard. This works before any build, and on Linux/Windows
too (pywebview uses the platform WebKit / WebView2) — handy for development. The window is
the .app's exact runtime, so what you see here is what the bundle shows.

## Build the .app

```bash
bash packaging/build.sh        # → dist/AI Safety Explorer.app
bash packaging/build.sh --run  # build, then open it
```

Then drag `dist/AI Safety Explorer.app` to `/Applications`. It builds with **py2app**, which
bundles the `safety_explorer` package (with its web assets), the corpus (into the app's
Resources), and the pywebview runtime.

**First-launch Gatekeeper.** The app is unsigned (fine for your own machine). macOS will
refuse a double-click the first time — right-click the app → **Open** → **Open**, once. For
sharing it beyond your machine you'd sign and notarize it; that's out of scope for a personal
research build.

## Where your data lives

- **Dev / checkout:** `data/explorer.db` in the repo, as always.
- **Bundled .app:** `~/Library/Application Support/AI Safety Explorer/explorer.db` — because
  the bundle is read-only. `safety_explorer.paths` handles the split; every path is
  overridable with `EXPLORER_DB`, `EXPLORER_DATA_DIR`, `EXPLORER_CORPUS`.

Your campaigns and sessions persist there across app updates. Rebuilding the .app does not
touch them.

## The semantic backend (torch) is not in the bundle

By default the build **excludes** torch / sentence-transformers, so the .app stays tens of
megabytes instead of gigabytes. The app runs fine on the lexicon register — it just labels
those readings "may under-read," honestly. Two ways to get the semantic backend:

- **Simplest:** run the app in dev mode (`python scripts/app.py`) inside a venv that has
  `[embeddings]`, with `EXPLORER_EMBED_BACKEND=minilm`. You get the window and the semantic
  register without a multi-gigabyte bundle.
- **All-in bundle:** delete the torch/sentence-transformers entries from the `excludes` list
  in `packaging/setup_app.py` and rebuild. The .app will be large but self-contained.

## The icon

`packaging/icon.svg` is the source. The build renders it to `.icns` automatically if
`cairosvg` is installed (it's in the `desktop` extra); otherwise the app builds with the
default icon and you can add the real one later:

```bash
python packaging/make_icon.py     # icon.svg → icon.icns (needs cairosvg + macOS iconutil)
```

## Model side

The app is the observer; you still run the model yourself (Ollama on Apple Silicon is the
least friction — see `docs/TESTING.md`). The desktop window is where you'd watch a live
session's register trajectory and power-seeking reading while an agent runs.
