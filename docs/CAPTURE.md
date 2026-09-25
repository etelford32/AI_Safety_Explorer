# Capturing a browser chat — the userscript

The Explorer's rule is **push, never pull**: it never reaches into another app or scrapes a
screen. A chat you have in a browser tab reaches it the same way an agent's turns do — a
source *chooses* to send them. For Claude.ai and ChatGPT that source is a small userscript
(`src/safety_explorer/web/explorer-capture.user.js`). It adds an **Explorer** button to the
page, and it sends the conversation **only when you click Send, or while you have switched
Follow on for that one chat**, and **only to the Explorer on your own computer**.

Captured conversations arrive in **Sessions** as Tier B — a chat window does not show the
model version, the system prompt or the sampling settings, and the instrument says so rather
than pooling them with API runs.

---

## Install

1. **A userscript manager.**
   - Chrome / Edge / Brave: [Tampermonkey](https://www.tampermonkey.net/). Then open the
     extension's details page and turn on **Allow User Scripts** (Chrome requires it for any
     userscript manager).
   - Firefox: Tampermonkey or Violentmonkey.
   - Safari: the **Userscripts** app (App Store), or Tampermonkey for Safari.
2. **The script.** With the Explorer running, open **Sessions → Install the capture script**
   (or go to `http://127.0.0.1:8713/explorer-capture.user.js`). The manager recognises the
   `.user.js` address and offers to install it. The copy it installs is already pointed at the
   address you opened it from, so a server on another port works without editing.
3. The first time the script talks to the Explorer, the manager may ask whether it may connect
   to `127.0.0.1`. Allow it — that is the only address it talks to.

## Use

Open a chat on claude.ai or chatgpt.com. A small **Explorer** pill sits at the bottom right
(<kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>E</kbd> opens it too).

| Control | What it does |
|---|---|
| **found N turn(s)** | The script's reading of the page, before anything is sent: how many turns, how many are yours, and the model name as the page displays it. If this says 0 on a chat that plainly has messages, the site has changed its markup — see *When a site changes* below. |
| **Send conversation** | Sends every finished turn. Safe to click again: turns the Explorer already has are acknowledged, not duplicated. |
| **Follow** | For this chat only, sends each new turn as it finishes. A reply still being written is held back until the site stops streaming it *and* its text has stopped changing for about a second, so a half-written reply is never stored. Follow is remembered per chat and is off for every new one. |
| **Send selection** | Select part of any page and send just that. Where the selection covers messages the script recognises, those messages go with their roles; anywhere else, the text goes to the Explorer's own splitter (the Live view's), which reports how it split it — or that it could not find speaker markers and read the whole selection as one reply. Works on pages the script knows nothing about. |
| **open ↗** | Opens this session in the Explorer. |
| **drift** chip | The session's register-drift status as the Explorer reads it (quiet / watch / alert). |
| **Settings → Explorer address** | Where to send, if not `http://127.0.0.1:8713`. |

### What is sent

For each turn: its position, its role (you / the model), and its text as you see it — with the
page's controls (copy buttons, icons) and the hidden duplicate that math renderers keep beside
every formula removed. For the session: the chat's title, its address, the model name as the
page displays it, and the script's version. Nothing else on the page is read, and nothing is
sent anywhere but the Explorer address above.

### Edits and regenerations become branches

Every turn is sent with its index. If a turn at an index the Explorer already holds comes back
different — you edited a message, or regenerated a reply — the Explorer refuses to overwrite it
(a trajectory that silently changes under the reader is worse than none). The script then opens
a **branch**: a new session, `…~b2`, labelled *(branch 2)*, holding the conversation as it now
stands. Both versions stay, which is also the data you would want: the same chat, answered two
ways.

---

## Who may talk to the Explorer

Before this script, nothing in a browser was meant to reach the local server except its own
UI. Loopback kept other machines out, but not other web pages: any open page can send a request
to `127.0.0.1`. So the rule is now explicit, enforced before any handler runs, and tested
(`tests/test_capture.py`):

| Request | Allowed |
|---|---|
| No `Origin` header — an agent hook, `curl`, the simulator | Everything, as before |
| The Explorer's own pages | Everything |
| claude.ai, chatgpt.com, chat.openai.com, or a browser extension (where a userscript manager's requests come from) | **Only** `POST /api/session/turn`, `POST /api/session/paste`, and `GET /api/status` — which, to them, reports only whether the Explorer is up and the drift of the session named in the request, never the list of other conversations |
| Any other page | Nothing — refused with 403 |
| Any request whose `Host` is not a loopback name (a DNS-rebinding attack arrives with its own name) | Nothing |

To let another chat page send (for example a local UI at `http://localhost:3000`, if you extend
the script to it), add its origin: `EXPLORER_ALLOWED_ORIGINS=http://localhost:3000`. Requests
the script makes through the userscript manager do not need this; it matters only for the plain
`fetch` fallback the script uses when no manager API is present.

## The endpoint additions

- `POST /api/session/turn` accepts an optional `turn_index`. Same text at a stored index →
  `200 {"duplicate": true}`; different text → `409 {"conflict": true, "expected": n}`; an index
  past the end → `409 {"gap": true, "expected": n}`. Without `turn_index`, turns append as before.
- `POST /api/session/paste` `{text, session_id?, label?, source?, meta?}` splits the text with the
  Live view's splitter and stores the turns, returning the convention used and whether it was
  confident.
- `GET /explorer-capture.user.js` serves the script, pointed at the address it was fetched from.

---

## When a site changes

Both sites change their markup from time to time. The script keeps everything it depends on in
one table, `ADAPTERS`, at the top of the file:

- **ChatGPT**: each message element carries `data-message-author-role="user|assistant"`; the
  rendered body is `.markdown` (replies) or `.whitespace-pre-wrap` (your messages); a reply is
  streaming while a `[data-testid="stop-button"]` is on the page.
- **Claude.ai**: your messages are `[data-testid="user-message"]`; replies are
  `.font-claude-response` / `.font-claude-message`; a reply is streaming while an element has
  `data-is-streaming="true"`.

These hooks are exercised in a real browser against fixture pages built to that markup
(`tests/test_capture_ui.py`), with Playwright answering for `https://chatgpt.com` and
`https://claude.ai` so the script sees the right hostname. **They were not checked against the
live sites from the build environment**, which cannot sign in to either. The first thing to look
at on your machine is the *found N turns* line on a chat you know: if the counts are right, the
hooks hold. If they are not, patch the selectors in `ADAPTERS` (the browser's element inspector
shows the current ones), or use **Send selection**, which needs none of them.

## Troubleshooting

- **"not reachable at http://127.0.0.1:8713"** — the Explorer is not running (start
  `explorer serve`, the desktop app, or the menu-bar app), or it is on another port (fix it
  under Settings).
- **No Explorer button** — the manager is not running the script: check it is enabled for the
  site, and in Chrome that *Allow User Scripts* is on for the manager.
- **found 0 turns** — see *When a site changes*.
- **A reply never arrives while Following** — it is held until streaming stops. If the site's
  streaming marker has changed, the reply is still sent once its text has been still for about a
  second after the marker disappears; if the marker never disappears, click **Send
  conversation**.
