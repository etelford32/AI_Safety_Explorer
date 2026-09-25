"""The AI Safety Explorer loader: the one piece you install, which keeps the rest current.

The Explorer changes weekly; an app that has to be rebuilt and re-downloaded for every
change is an app nobody keeps current. So the downloadable app is split in two:

* **The loader** (this package) — small and stable. It opens a window, asks GitHub whether
  newer Explorer code exists on the chosen channel, downloads it, *verifies* it will run on
  this app before switching to it, and then starts it in the same window. It changes
  rarely, and only a change to it needs a new download.
* **The Explorer** (`safety_explorer`) — the code that changes. It arrives as a source
  tarball from GitHub and runs in-process on the loader's Python. It has no third-party
  runtime dependencies, which is what makes this safe: a new version needs nothing the app
  does not already carry — and when one ever does, verification says so and keeps the
  version that works.

Nothing is ever replaced in place. Each version lives in its own directory, the last one
that started cleanly is remembered, and a version that fails to start is set aside for the
last good one — and, failing that, for the copy baked into the app, which always works
offline.
"""

LOADER_VERSION = "1.0.0"

#: The contract between loader and Explorer: the Explorer exposes
#: `safety_explorer.server.serve(db_path, corpus_path, host, port)` and reads its paths from
#: EXPLORER_CORPUS / EXPLORER_DATA_DIR / EXPLORER_DB. A version that needs a different
#: contract declares a higher `[tool.explorer-loader] api` in its pyproject, and this loader
#: declines it rather than half-starting it.
LOADER_API = 1

DEFAULT_REPO = "etelford32/AI_Safety_Explorer"
