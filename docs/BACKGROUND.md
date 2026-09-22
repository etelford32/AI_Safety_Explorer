# Running it as a background app

The Explorer can sit in your menu bar and stay on while you run a model, the way Ollama
does. Nothing about the measurement changes — the spine is still **push, never pull**. The
background app is the always-on *receiver*: the local endpoint is always there for a source
to stream turns into, and the menu bar is a glance — is it alive, how much has flowed
through, and is any live session drifting right now.

One process holds both halves: the HTTP server runs in a background thread and the menu-bar
app polls the same database and reflects it. Quit the menu-bar item and both stop.

## macOS — the menu-bar app

```bash
pip install -e '.[openai,embeddings,menubar]'   # adds rumps
python scripts/menubar.py
```

A 🛰 icon appears in the menu bar. Its menu:

- **Open dashboard** / **Paste a conversation…** — open the UI at `http://127.0.0.1:8713`.
- A live summary line — `N session(s) · N turn(s) · N alert · up 12m`.
- The register backend — `minilm ✓ (semantic)` when the embedding backend passed its
  generalization control, or `lexicon — may under-read (set minilm)` when it fell back.
- **Recent sessions**, each with a drift glyph (🟢 quiet · 🟡 watch · 🔴 alert).

The icon itself carries the badge: plain 🛰 when quiet, `🛰 🟡N` when N sessions are on
watch, `🛰 🔴N` when N are alerting — so an agent's register moving under pressure is
visible without opening anything.

### Start it at login

```bash
bash scripts/install-menubar.sh              # register + start now, and at every login
bash scripts/install-menubar.sh --uninstall  # remove it
```

The installer writes a LaunchAgent (`~/Library/LaunchAgents/com.parkersphysics.safety-explorer.plist`)
with your real venv and repo paths, sets `EXPLORER_EMBED_BACKEND=minilm`, and loads it. Logs
go to `/tmp/explorer-menubar.log`. It runs the app from the repo root, so the corpus and the
database (`data/explorer.db`) are found regardless of where login launches it from.

## Linux / Windows

There is no menu-bar app, but the same always-on receiver is just `explorer serve` under
your init system — the endpoint and the UI are identical.

- **Linux (systemd user unit):** `~/.config/systemd/user/explorer.service` running
  `explorer serve` with `WorkingDirectory=` your repo root and `Environment=EXPLORER_EMBED_BACKEND=minilm`,
  then `systemctl --user enable --now explorer`.
- **Windows:** a Task Scheduler task "At log on" running `explorer serve` from the repo root,
  or `pythonw` for no console window.

## What "always on" buys you

Nothing new gets observed just by being on — a source still has to emit. What it buys is
that the source never has to wait for you to start a server:

- a **browser userscript** streaming a chat tab's turns (see `docs/TESTING.md` § 7 and the
  capture userscript) always has somewhere to POST;
- an **agent hook or wrapped provider** (`docs/INTEGRATION.md`) can stream a long run into a
  session you glance at in the menu bar;
- and the **drift** and **power-seeking** readings on those sessions surface as a badge the
  moment they cross from quiet to watch to alert, which is the whole point of a monitor.

`GET /api/status` is the summary the menu bar polls — version, uptime, session and turn
counts, per-session drift, and the register backend's trust — so any other status surface
(a shell prompt, a different tray app) can read the same thing.
