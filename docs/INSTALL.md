# Installing AI Safety Explorer

Two ways in: **the app** (for testers — download once, it keeps itself up to date) or
**from source** (for development).

---

## The app (macOS 11 or later, Intel or Apple Silicon)

**[Download AI Safety Explorer for Mac](https://github.com/etelford32/AI_Safety_Explorer/releases/latest/download/AI-Safety-Explorer-macOS.dmg)**
(`.dmg`). This link always serves the newest release.

1. Open the downloaded **AI-Safety-Explorer-macOS.dmg** and drag **AI Safety Explorer** onto
   **Applications**.
2. Open it from Applications. The first time only, macOS asks you to confirm it, because the app
   is not yet signed with an Apple Developer ID (see below).

That is the whole install. From then on the app keeps itself up to date.

### The first launch

macOS refuses apps from outside the App Store unless Apple has notarized them. Until this one
is notarized ([SIGNING.md](SIGNING.md)), you approve it once.

**macOS 15 (Sequoia) and later:**

1. Double-click the app. macOS says *"AI Safety Explorer" Not Opened*. Click **Done**.
2. Open **System Settings → Privacy & Security**, and scroll down to *Security*. It says
   *"AI Safety Explorer" was blocked to protect your Mac*. Click **Open Anyway**, and confirm
   with your password or Touch ID.
3. Click **Open Anyway** once more in the dialog that follows.

**macOS 14 (Sonoma) and earlier:** right-click the app, choose **Open**, then click **Open**.

After that the app opens normally, including after it updates itself. Updates change the
Explorer code the app runs, not the app you approved.

**If macOS says the app "is damaged and can't be opened":** the app's signature no longer
matches its files. This usually means a download was cut short, or a third-party unzip tool
changed the bundle. It is not the missing Developer ID. Download the `.dmg` again. If the message persists, run this in Terminal:

```bash
xattr -dr com.apple.quarantine "/Applications/AI Safety Explorer.app"
```

It removes the "downloaded from the internet" mark, so Gatekeeper no longer checks the app.
Do this only for a copy you downloaded from this repository's Releases page.

**Checking the download (optional).** Each release lists a `.sha256` beside the file.
`shasum -a 256 ~/Downloads/AI-Safety-Explorer-macOS.dmg` should print the same value.

**Prefer a zip?** `AI-Safety-Explorer-macOS.zip` holds the same app
([latest](https://github.com/etelford32/AI_Safety_Explorer/releases/latest/download/AI-Safety-Explorer-macOS.zip)).
Unzip it, then move the app to Applications.

### What happens at launch

A window opens on four steps:

1. **Check for updates** — asks GitHub what the current version is on your channel.
2. **Download** — only if there is something new. *Skip this update* starts what you have.
3. **Verify it runs on this app** — the new code must compile on the app's Python, declare a
   loader contract this app speaks, and need nothing the app does not carry. A version that
   fails any of these is never switched to.
4. **Start the Explorer** — in the same window.

If a new version verifies but then fails to start, the app sets it aside and starts the last
version that worked; if none does, it starts the copy built into the app, which works offline.
You are never left with nothing to run.

While you work, the app checks for updates every half hour. A new version is downloaded and
verified in the background and announced in the Explorer; it takes effect at the next launch
(or **Updates → Restart**), never in the middle of a session.

### Channels

| Channel | Follows | For |
|---|---|---|
| **Stable** (default) | the latest GitHub Release | testers, and any result you intend to report — a release is a fixed, citable version |
| **Latest development code** | the head of the default branch | following development as it happens |
| **Branch** | the head of a named branch | trying unmerged work |

Switch in the **Updates** menu, or in the loader window's **Settings**. With no release
published yet, Stable follows the development branch and says so.

### Your data

Everything stays on your Mac, in `~/Library/Application Support/AI Safety Explorer/`:
`explorer.db` (your runs, annotations and sessions — shared by every version, never touched
by an update) and `loader/` (installed versions and settings). **Data → Open Data Folder**
opens it. **Data → Use a Different Database…** points the app at another `explorer.db` — for
example the one a source checkout writes to `data/explorer.db`.

The only network traffic the app makes on its own is to GitHub, to check for and download
updates. Campaigns you run talk to the model provider you choose; the browser capture script
talks only to this app on `127.0.0.1`.

### A private fork

The repository is public, so the app needs no GitHub account or token. If you point a build at
a *private* fork, the loader needs a read-only token: a fine-grained token with **Contents:
read** on that one repository, pasted into **Settings → GitHub token**. It is stored in the
app's data folder, readable only by your user.

### Uninstall

Delete the app, and — if you want your data gone too —
`~/Library/Application Support/AI Safety Explorer/`.

---

## From source (any OS)

```bash
git clone https://github.com/etelford32/AI_Safety_Explorer.git
cd AI_Safety_Explorer
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'           # add ,anthropic,openai for those providers; ,desktop for the window
explorer demo                     # optional: mock data on every screen
explorer serve                    # http://127.0.0.1:8713  — or `explorer app` for a window
git pull                          # to update
```

Requires Python 3.11+. The Explorer has no third-party runtime dependencies.

The self-updating loader also runs from a checkout, without packaging:
`python scripts/loader_app.py` (window) or `python scripts/loader_app.py --headless`.

### Building the app yourself

```bash
pip install -e '.[desktop,anthropic,openai]'
bash packaging/build.sh           # builds dist/AI Safety Explorer.app and smoke-tests it
```

A tag push (`v1.2.3`) builds the same app in GitHub Actions and attaches it to that release
(`.github/workflows/app.yml`).
