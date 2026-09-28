# Getting conversations in — automatically

You probably already have the conversations you want to look at: a ChatGPT or Claude.ai
history, an agent's session logs, an eval harness's output. The Explorer reads them in the
shape they are already in. You never pick a format: it is detected from the content, shown to
you before anything is stored, and imported in the background.

## Six ways in, one path

| Way in | What you do | When it runs |
|---|---|---|
| **Drop anywhere** | Drag files, a zip or a whole folder onto any page of the Explorer | A preview opens; nothing is stored until you click **Import** |
| **Paste anywhere** | <kbd>⌘V</kbd> / <kbd>Ctrl V</kbd> a conversation while no text box has focus | Same preview |
| **Choose files** | **Add data → Choose files…** | Same preview |
| **The inbox** | Save or move a file into the inbox folder (shown on **Add data**) | Imported within seconds, then moved to `inbox/imported/` (or `inbox/failed/` with the reason logged) |
| **Connected folders** | **Add data → Found on this computer → Connect**, or **Watch another folder** | Imported now, then re-checked every few seconds; only files whose size or date changed are read again |
| **Browser capture** | The userscript's **Every ChatGPT/Claude chat: on** | Each conversation you open on that site is followed as you have it ([CAPTURE.md](CAPTURE.md)) |

From a terminal:

```bash
explorer import ~/Downloads/chatgpt-export.zip          # any file, zip or folder; format detected
explorer import ~/.claude/projects                      # every Claude Code session
explorer sources                                        # what is on this computer, names only
```

The inbox is `inbox/` next to the database (`data/inbox/` by default, or the app's data folder).
Set `EXPLORER_INBOX` to put it somewhere handier, such as `~/Desktop/Explorer Inbox`.

## What it recognises

| Format | Recognised by | Tier | Notes |
|---|---|---|---|
| **ChatGPT data export** (`.zip` or `conversations.json`) | the message tree (`mapping`, `current_node`) | B | The branch that was on screen is the conversation; edits and regenerations are counted, not merged. Custom instructions come in as a system turn; hidden system messages and reasoning traces are left out and counted. Tool calls (code interpreter, browsing) are `tool` turns. The model is taken from each reply. |
| **Claude.ai data export** (`.zip` or `conversations.json`) | `chat_messages` | B | Attachments are noted by name. |
| **Claude Code session logs** (`~/.claude/projects/*/*.jsonl`) | `sessionId` + `message` lines | B | An assistant message split across lines is put back together; tool calls and results are `tool` turns; thinking, meta lines, slash-command echoes and sub-agent sidechains are left out. Titled by the session summary and the project folder. |
| **Codex CLI session logs** (`~/.codex/sessions/**/rollout-*.jsonl`) | `session_meta` / `response_item` lines | B | The environment block is dropped; function calls and outputs are `tool` turns. |
| **Chat messages** (OpenAI or Anthropic request shape, fine-tuning files, most eval logs) | a `messages` array | C | A `system` field and a trailing `response` / `output` are folded in. Anthropic `tool_use` / `tool_result` blocks and OpenAI `tool_calls` become `tool` turns. |
| **ShareGPT** | `conversations` with `from` / `value` | C | |
| **Prompt / response pairs** (JSON, JSONL or CSV) | `prompt`/`input`/`question` + `response`/`output`/`answer` columns | C | See *Where things go* below. |
| **Conversation table** (CSV) | `role` + `content` columns, grouped by a conversation id column | C | |
| **Text transcript** | speaker labels (`User:` / `Assistant:`, "You said" / "ChatGPT said", `### User`) | C | Only when the speakers can be told apart. A block of text with no labels is **not** imported as a conversation — a wrong split would attribute the model's register to you. |
| **Anything else in JSON** | lists whose items read as messages (a role-like key and text) | C | LM Studio's `versions`, LangChain's `type`/`data`, and most chat apps' exports fall here. |

The provenance tier follows the source. A chat export or an agent's own log is **Tier B**: a
real surface, with the system prompt, sampling and routing unknown. A file someone assembled
is **Tier C**: provenance as stated. Tiers are never pooled silently with Tier A campaign data.

## Where things go

* **Every conversation becomes a session** in **Conversations**, with its source, tier and
  original timestamps.
* **Importing is idempotent.** The same export imported twice adds nothing; a newer export,
  or a log that grew, adds only the turns said since. A conversation that was *edited* since
  the last import (its stored turns are no longer a prefix of what arrived) is never
  rewritten — the new version is stored as a branch beside it, as the capture script does.
* **A conversation that opens with a corpus prompt also becomes a run.** Its first reply is a
  response to that prompt under chat-surface conditions — a Tier B capture — so it is stored
  as a run, scored against the answer key, and counts in Results. Only the *opening* question
  qualifies, and only on a verbatim or near-verbatim match (token overlap ≥ 0.9): asked
  mid-conversation, the same words arrive with context the corpus prompt never had, and
  pooling that reply would confound every comparison it entered.
* **A prompt / response pair** becomes a run when the prompt matches a corpus prompt, and a
  two-turn conversation otherwise.

## Which conversations are worth a look

Every conversation is read when it arrives, on the same pure-text path as a live session, and
flagged — each flag a reason to open it, never a verdict:

| Flag | Meaning |
|---|---|
| **Register shift** / Some shift | the register moved sharply (or somewhat) between turns |
| **Refusal** ×n | model turns that decline, in whole or in part |
| **Mentions testing** | model turns that talk about being tested or evaluated |
| **Agency n/5** | peak expressed agency is 3 or more — read it against what the conversation granted |
| **Answer key** | a question matches a corpus prompt, so the reply can be scored objectively |

**Conversations** sorts the flagged ones first (or newest first), filters to *Worth a look*,
*Live* or *Imported*, and searches titles and sources. The reading is cached against the
number of turns it read, so a list of thousands costs one query; the detail view of a very
long conversation (an agent's day) reads its most recent 600 turns and says so.

## Privacy and consent

The push-never-pull rule, carried over to files:

* **Nothing leaves the computer.** Parsing, storage and analysis are local.
* **Finding is not reading.** *Found on this computer* looks in a short list of well-known
  places — Claude Code's and Codex CLI's log folders, and chat exports in `~/Downloads` — and
  reads only names, sizes and dates (plus a zip's table of contents, to tell an export from
  any other archive). No file's contents are opened until you connect its folder or import it.
* **Connecting is revocable.** Disconnect a folder and nothing more is read from it; what was
  imported stays until you delete it.
* **Only this Explorer's own pages can import.** The intake endpoints refuse a request from a
  chat site or any other web page (the same origin rule as the rest of the local server —
  [CAPTURE.md](CAPTURE.md)); the capture userscript can post turns and nothing else.
* **Imported text is data, never instructions.** Nothing in a conversation is executed or
  passed to another model as a command.
