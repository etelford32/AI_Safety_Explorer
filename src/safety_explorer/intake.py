"""Intake — hand the Explorer anything that holds a conversation, and it works out what it is.

Getting data in used to mean knowing which of four doors to use and which shape each wanted:
a JSONL of prompt/response rows for the Tier C import, a JSON messages array for the Live
view, one HTTP call per turn for a session. Most people who want to look at how a model
behaves already *have* the conversations — a ChatGPT or Claude.ai data export, an agent's
session logs, an eval harness's output — in whatever shape the tool that produced them
chose. So this module reads the shape instead of asking for one:

    detect_path(path) / detect_bytes(name, data)  ->  [Batch, ...]

A `Batch` is one source's worth of conversations in one recognised format, with the
provenance tier that format earns. Detection is by **content**, never by file extension —
a `conversations.json` from ChatGPT and one from Claude.ai share a name and nothing else —
and every format is parsed to the same `Conversation` (a key, a title, turns with roles),
so the importer, the triage and the views never need to know where a conversation came from.

The rules the rest of the instrument keeps, kept here too:

* **Roles are read, never guessed.** A structured format states who spoke. A plain-text
  transcript goes through the Live view's splitter, and a transcript it cannot split
  confidently is *not* imported as a conversation — a wrong split attributes the model's
  register to the user.
* **Provenance travels with the data.** A chat export is Tier B (a chat surface: system
  prompt, sampling and routing unknown). An agent's own log is Tier B. Anything else —
  a messages file, a spreadsheet — is Tier C: provenance as stated by whoever made it.
* **Only what was said is kept.** Hidden system scaffolding, reasoning traces and image
  payloads are left out and counted, not silently dropped: the count goes in the meta.
  Tool calls and their results are kept as `tool` turns, clipped, because for an agent
  they are where the behaviour is; the register readings skip them.
* **Nothing leaves the machine.** Parsing is local, and so is everything downstream.

Storing what was detected is `intake_store.import_batches`; finding sources on the computer
and watching them is `sources`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

INTAKE_VERSION = "1"

#: A tool result or a pasted log can be enormous; one turn is clipped rather than letting a
#: 2 MB stack trace become a "turn" the register reading chokes on. The clip is stated.
MAX_TOOL_CHARS = 2000
MAX_TURN_CHARS = 60000

#: Files bigger than this are not read as a single JSON document into memory; JSONL is
#: streamed line by line whatever its size.
MAX_JSON_BYTES = 512 * 1024 * 1024

#: format -> (what the UI calls it, provenance tier, session source label)
FORMATS: dict[str, tuple[str, str, str]] = {
    "chatgpt_export": ("ChatGPT export", "B", "import:chatgpt"),
    "claude_export": ("Claude.ai export", "B", "import:claude"),
    "claude_code": ("Claude Code session log", "B", "import:claude-code"),
    "codex_cli": ("Codex CLI session log", "B", "import:codex"),
    "messages": ("Chat messages (OpenAI / Anthropic shape)", "C", "import:messages"),
    "sharegpt": ("ShareGPT conversations", "C", "import:sharegpt"),
    "generic_json": ("Conversation JSON (structure detected)", "C", "import:json"),
    "pairs": ("Prompt / response pairs", "C", "import:pairs"),
    "csv_messages": ("Conversation table (CSV)", "C", "import:csv"),
    "transcript": ("Text transcript", "C", "import:text"),
}

_USER = {"user", "human", "you", "me", "customer", "person", "prompter", "questioner", "q"}
_ASSISTANT = {"assistant", "ai", "bot", "gpt", "chatgpt", "claude", "model", "gemini", "bard",
              "copilot", "llm", "agent", "char", "character", "a", "response"}
_SYSTEM = {"system", "developer", "context"}
_TOOL = {"tool", "function", "ipython", "observation", "function_call", "tool_result",
         "tool_use", "function_response"}

_PROMPT_KEYS = ("prompt", "input", "question", "instruction", "query")
_RESPONSE_KEYS = ("response", "completion", "output", "answer", "reply", "generation")


@dataclass
class Conversation:
    """One conversation, whatever it came from. `key` is stable within its source, so a
    re-import of the same export finds the same conversation again."""

    key: str
    title: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)   # {role, text, at}
    created_at: str | None = None
    updated_at: str | None = None
    model: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def add(self, role: str, text: Any, at: Any = None, limit: int = MAX_TURN_CHARS) -> None:
        text = text if isinstance(text, str) else text_of(text)
        text = (text or "").strip()
        if not text:
            return
        if len(text) > limit:
            text = text[:limit] + f"\n[… clipped at import: {len(text) - limit:,} more characters]"
        self.turns.append({"role": role, "text": text, "at": iso(at)})


@dataclass
class Batch:
    """What one file (or one member of an archive) turned out to be."""

    format: str
    origin: str
    conversations: list[Conversation] = field(default_factory=list)
    pairs: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped: int = 0

    @property
    def label(self) -> str:
        return FORMATS[self.format][0]

    @property
    def tier(self) -> str:
        return FORMATS[self.format][1]

    @property
    def source(self) -> str:
        return FORMATS[self.format][2]

    def describe(self, sample: int = 6) -> dict[str, Any]:
        """The preview the UI shows before anything is stored."""
        convs = self.conversations
        times = sorted(t for c in convs for t in (c.created_at, c.updated_at) if t)
        models = sorted({c.model for c in convs if c.model})
        return {
            "format": self.format, "label": self.label, "tier": self.tier,
            "source": self.source, "origin": self.origin,
            "n_conversations": len(convs),
            "n_turns": sum(len(c.turns) for c in convs),
            "n_pairs": len(self.pairs),
            "first": times[0] if times else None, "last": times[-1] if times else None,
            "models": models[:8],
            "sample": [{"title": c.title, "n_turns": len(c.turns), "at": c.updated_at or c.created_at}
                       for c in convs[:sample]],
            "notes": self.notes, "skipped": self.skipped,
        }


# --------------------------------------------------------------------------- helpers


def iso(ts: Any) -> str | None:
    """A timestamp in any of the shapes exports use, as ISO-8601 UTC — or None."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        if ts <= 0:
            return None
        if ts > 1e12:           # milliseconds
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(ts, str):
        s = ts.strip()
        if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
            return iso(float(s))
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat(timespec="seconds")
    return None


def norm_role(value: Any) -> str | None:
    """user / assistant / system / tool, from whatever a format calls its speakers."""
    if isinstance(value, dict):
        value = value.get("role") or value.get("name") or value.get("type")
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in _USER:
        return "user"
    if v in _ASSISTANT:
        return "assistant"
    if v in _SYSTEM:
        return "system"
    if v in _TOOL:
        return "tool"
    return None


def text_of(content: Any) -> str:
    """The readable text in a message's content, whichever way it is nested."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float)):
        return str(content)
    if isinstance(content, list):
        parts = [text_of(p) for p in content]
        return "\n\n".join(p for p in parts if p and p.strip())
    if isinstance(content, dict):
        kind = content.get("type") or content.get("content_type") or ""
        if kind in ("image", "image_url", "input_image", "image_asset_pointer", "image_file"):
            return "[image]"
        if kind in ("thinking", "redacted_thinking", "reasoning"):
            return ""
        for k in ("text", "value", "content", "parts", "message", "body", "data"):
            if k in content:
                return text_of(content[k])
    return ""


def clip(text: str, n: int = MAX_TOOL_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f" […{len(text) - n:,} more characters]"


def _digest(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


def _title_from(conv: Conversation) -> str:
    first = next((t["text"] for t in conv.turns if t["role"] == "user"), "") or \
        next((t["text"] for t in conv.turns), "")
    line = " ".join(first.split())
    return (line[:77] + "…") if len(line) > 80 else (line or "untitled conversation")


_STOP = {
    "en": "the and is to of you that it in for with this are be have not can what i your will".split(),
    "fr": "le la les et est des une un pour que qui dans pas vous je du sur avec ce il nous mais".split(),
    "es": "el la los las y es que en un una por para con no se lo del al como pero su muy".split(),
    "de": "der die das und ist nicht ich sie es ein eine zu mit auf für den von dem sich auch".split(),
    "it": "il lo gli e è di che per un una non con del della sono ho anche questo ma".split(),
    "pt": "o os as é de que em um uma para com não do da no na se por mas você".split(),
}


def detect_language(texts: list[str]) -> str:
    """The language a conversation is in, roughly: by script for Japanese, Chinese,
    Korean, Cyrillic and Arabic, and by the commonest function words for Latin-script
    languages. Only as good as it needs to be: it decides which lexicon (if any) may read
    the text and which language a backend has to be trusted in, and anything uncertain
    falls back to English — the pre-detection behaviour."""
    sample = " ".join(texts)[:6000]
    if not sample.strip():
        return "en"
    kana = sum(1 for c in sample if "\u3040" <= c <= "\u30ff")
    han = sum(1 for c in sample if "\u4e00" <= c <= "\u9fff")
    hangul = sum(1 for c in sample if "\uac00" <= c <= "\ud7af")
    cyr = sum(1 for c in sample if "\u0400" <= c <= "\u04ff")
    arab = sum(1 for c in sample if "\u0600" <= c <= "\u06ff")
    letters = sum(1 for c in sample if c.isalpha()) or 1
    if (kana + han) / letters > 0.2:
        return "ja" if kana > 0.05 * (kana + han) else "zh"
    if hangul / letters > 0.2:
        return "ko"
    if cyr / letters > 0.3:
        return "ru"
    if arab / letters > 0.3:
        return "ar"
    words = re.findall(r"[a-zà-öø-ÿ']+", sample.lower())
    if len(words) < 8:
        return "en"
    scores = {lang: sum(1 for w in words if w in set(sw)) for lang, sw in _STOP.items()}
    best = max(scores, key=scores.get)
    # Only move off English on a clear margin: a mixed or short text stays English.
    if best != "en" and scores[best] >= 1.5 * max(scores["en"], 1) and scores[best] >= 4:
        return best
    return "en"


def _finish(conv: Conversation) -> Conversation:
    if not conv.title:
        conv.title = _title_from(conv)
    if "language" not in conv.meta:
        conv.meta["language"] = detect_language(
            [t["text"] for t in conv.turns[:24] if t["role"] in ("user", "assistant")])
    stamps = [t["at"] for t in conv.turns if t.get("at")]
    conv.created_at = conv.created_at or (min(stamps) if stamps else None)
    conv.updated_at = conv.updated_at or (max(stamps) if stamps else conv.created_at)
    return conv


def _useful(conv: Conversation) -> bool:
    """A conversation worth storing has at least one turn from each side, or a user turn
    with a reply of any kind; a lone system prompt is not a conversation."""
    roles = {t["role"] for t in conv.turns}
    return "assistant" in roles or ("user" in roles and len(conv.turns) > 1)


# --------------------------------------------------------------------------- entry points


def detect_path(path: str | Path) -> list[Batch]:
    """Everything recognisable in a file, a folder or an archive on disk."""
    p = Path(path).expanduser()
    if p.is_dir():
        out: list[Batch] = []
        for f in sorted(p.rglob("*")):
            if f.is_file() and not _ignorable(f.name):
                out.extend(detect_path(f))
        return out
    name = p.name
    with p.open("rb") as fh:
        head = fh.read(4)
    if head.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(p) as zf:
            return _from_zip(zf, name)
    if p.suffix.lower() in (".jsonl", ".ndjson") or (
            p.stat().st_size > MAX_JSON_BYTES and head[:1] in (b"{", b"\xef")):
        with p.open("r", encoding="utf-8-sig", errors="replace") as fh:
            return _from_jsonl(name, _json_lines(fh))
    if p.stat().st_size > MAX_JSON_BYTES:
        return [Batch("transcript", name, notes=[f"{name} is too large to read as one document"])]
    return detect_bytes(name, p.read_bytes())


def detect_bytes(name: str, data: bytes) -> list[Batch]:
    """Everything recognisable in an upload or a paste."""
    if data.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return _from_zip(zf, name)
    return detect_text(name, _decode(data))


def detect_text(name: str, text: str) -> list[Batch]:
    s = (text or "").lstrip("﻿").strip()
    if not s:
        return []
    if s[:1] in "[{":
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            obj = None
        if obj is not None:
            return _from_json(name, obj)
        if "\n" in s:
            return _from_jsonl(name, _json_lines(io.StringIO(s)))
    if name.lower().endswith((".csv", ".tsv")) or _looks_like_csv(s):
        got = _from_csv(name, s)
        if got:
            return got
    return _from_transcript(name, s)


def _ignorable(name: str) -> bool:
    return name.startswith(".") or name.startswith("__MACOSX") or name.lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".mp3", ".m4a", ".wav", ".mp4",
         ".pdf", ".docx", ".xlsx", ".html", ".htm", ".css", ".js", ".py", ".db", ".sqlite"))


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _json_lines(fh: Iterable[str]) -> Iterator[dict[str, Any]]:
    for line in fh:
        line = line.strip()
        if not line or line[:1] not in "{[":
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


# --------------------------------------------------------------------------- archives


def _from_zip(zf: zipfile.ZipFile, name: str) -> list[Batch]:
    members = [i for i in zf.infolist() if not i.is_dir() and not _ignorable(Path(i.filename).name)]
    names = {Path(i.filename).name for i in members}
    out: list[Batch] = []
    for info in members:
        base = Path(info.filename).name
        # An official export carries the same conversations twice (JSON and an HTML view)
        # plus account files; the JSON is the one to read.
        if "conversations.json" in names and base in ("user.json", "users.json", "projects.json",
                                                       "message_feedback.json", "shared_conversations.json",
                                                       "model_comparisons.json", "memories.json"):
            continue
        origin = f"{name}/{info.filename}"
        if info.file_size > MAX_JSON_BYTES and not base.lower().endswith((".jsonl", ".ndjson")):
            out.append(Batch("transcript", origin, notes=[f"{info.filename} is too large to read"]))
            continue
        with zf.open(info) as fh:
            if base.lower().endswith((".jsonl", ".ndjson")):
                out.extend(_from_jsonl(origin, _json_lines(io.TextIOWrapper(fh, "utf-8", "replace"))))
            else:
                out.extend(detect_text(origin, _decode(fh.read())))
    return [b for b in out if b.conversations or b.pairs or b.notes]


# --------------------------------------------------------------------------- JSON


def _from_json(name: str, obj: Any) -> list[Batch]:
    items = obj if isinstance(obj, list) else [obj]
    dicts = [x for x in items if isinstance(x, dict)]

    if dicts and all("mapping" in d for d in dicts[:5]):
        return [_chatgpt(name, dicts)]
    if dicts and all("chat_messages" in d for d in dicts[:5]):
        return [_claude_export(name, dicts)]
    if dicts and all(isinstance(d.get("conversations"), list) for d in dicts[:5]):
        return [_sharegpt(name, dicts)]
    if dicts and all(isinstance(d.get("messages"), list) for d in dicts[:5]):
        return [_messages(name, dicts)]
    if dicts and _is_message_list(dicts):
        return [_messages(name, [{"messages": dicts}])]
    if dicts and _pair_keys(dicts[0]):
        return [_pairs(name, dicts)]
    if isinstance(obj, dict) and isinstance(obj.get("data"), list):
        return _from_json(name, obj["data"])
    return [_generic(name, obj)]


def _from_jsonl(name: str, rows: Iterator[dict[str, Any]]) -> list[Batch]:
    head: list[dict[str, Any]] = []
    for row in rows:
        head.append(row)
        if len(head) >= 40:
            break
    if not head:
        return []

    def everything() -> Iterator[dict[str, Any]]:
        yield from head
        yield from rows

    types = {r.get("type") for r in head}
    if (types & {"user", "assistant"} and any(isinstance(r.get("message"), dict) for r in head)
            and any("sessionId" in r or "uuid" in r for r in head)):
        return [_claude_code(name, everything())]
    if types & {"session_meta", "response_item", "turn_context", "event_msg"} or any(
            r.get("type") == "message" and isinstance(r.get("content"), list)
            and any(isinstance(c, dict) and c.get("type") in ("input_text", "output_text")
                    for c in r["content"]) for r in head):
        return [_codex(name, everything())]
    if all(isinstance(r.get("messages"), list) for r in head):
        return [_messages(name, everything())]
    if all(isinstance(r.get("conversations"), list) for r in head):
        return [_sharegpt(name, everything())]
    if all("mapping" in r for r in head):
        return [_chatgpt(name, everything())]
    if all("chat_messages" in r for r in head):
        return [_claude_export(name, everything())]
    if _is_message_list(head):
        return [_messages(name, [{"messages": list(everything())}])]
    if all(_pair_keys(r) for r in head):
        return [_pairs(name, everything())]
    return [_generic(name, list(everything()))]


def _is_message_list(items: list[dict[str, Any]]) -> bool:
    hits = [1 for d in items if _as_message(d) is not None]
    return len(hits) >= 2 and len(hits) >= 0.6 * len(items)


def _pair_keys(d: dict[str, Any]) -> tuple[str, str] | None:
    lower = {k.lower(): k for k in d}
    p = next((lower[k] for k in _PROMPT_KEYS if k in lower), None)
    r = next((lower[k] for k in _RESPONSE_KEYS if k in lower), None)
    return (p, r) if p and r else None


# --------------------------------------------------------------------------- ChatGPT


def _chatgpt(name: str, convs: Iterable[dict[str, Any]]) -> Batch:
    """ChatGPT's data export: a tree of messages per conversation (every edit and
    regeneration is a branch). The branch that was on screen — `current_node` back to the
    root — is the conversation; the other branches are counted, not merged."""
    b = Batch("chatgpt_export", name)
    hidden = thoughts = 0
    for c in convs:
        mapping = c.get("mapping") or {}
        if not isinstance(mapping, dict) or not mapping:
            b.skipped += 1
            continue
        node = c.get("current_node")
        if node not in mapping:
            leaves = [k for k, v in mapping.items() if not (v or {}).get("children")]
            node = max(leaves, key=lambda k: ((mapping[k].get("message") or {}).get("create_time") or 0),
                       default=None)
        path: list[str] = []
        seen: set[str] = set()
        while node and node in mapping and node not in seen:
            seen.add(node)
            path.append(node)
            node = mapping[node].get("parent")
        path.reverse()
        key = c.get("conversation_id") or c.get("id") or _digest(c.get("title"), c.get("create_time"))
        conv = Conversation(key=str(key), title=(c.get("title") or "").strip(),
                            created_at=iso(c.get("create_time")), updated_at=iso(c.get("update_time")))
        branches = sum(1 for v in mapping.values() if len((v or {}).get("children") or []) > 1)
        for nid in path:
            msg = (mapping[nid] or {}).get("message")
            if not msg:
                continue
            meta = msg.get("metadata") or {}
            if meta.get("is_visually_hidden_from_conversation"):
                hidden += 1
                continue
            role = ((msg.get("author") or {}).get("role") or "").lower()
            content = msg.get("content") or {}
            ctype = content.get("content_type")
            at = msg.get("create_time")
            if ctype in ("thoughts", "reasoning_recap"):
                thoughts += 1
                continue
            if ctype == "user_editable_context":
                ctx = "\n\n".join(x for x in (content.get("user_profile"), content.get("user_instructions")) if x)
                conv.add("system", f"[custom instructions]\n{ctx}", at)
                continue
            if "parts" in content:
                text = "\n\n".join(p if isinstance(p, str) else text_of(p) for p in content.get("parts") or [])
            else:
                text = content.get("text") or text_of(content)
            if role == "assistant":
                recipient = msg.get("recipient") or "all"
                if recipient != "all":
                    conv.add("tool", f"→ {recipient}: {clip(text)}", at)
                    continue
                conv.add("assistant", text, at)
                if meta.get("model_slug"):
                    conv.model = meta["model_slug"]
            elif role == "user":
                conv.add("user", text, at)
            elif role == "tool":
                who = (msg.get("author") or {}).get("name") or "tool"
                conv.add("tool", f"← {who}: {clip(text)}", at)
            elif role == "system" and text.strip():
                conv.add("system", text, at)
        conv.model = conv.model or c.get("default_model_slug")
        conv.meta = {"branches_not_shown": branches, "gizmo_id": c.get("gizmo_id"),
                     "archived": bool(c.get("is_archived"))}
        if _useful(conv):
            b.conversations.append(_finish(conv))
        else:
            b.skipped += 1
    if hidden:
        b.notes.append(f"{hidden:,} hidden system message(s) left out")
    if thoughts:
        b.notes.append(f"{thoughts:,} reasoning trace(s) left out — only what was shown is analysed")
    return b


# --------------------------------------------------------------------------- Claude.ai


def _claude_export(name: str, convs: Iterable[dict[str, Any]]) -> Batch:
    b = Batch("claude_export", name)
    attachments = 0
    for c in convs:
        key = c.get("uuid") or _digest(c.get("name"), c.get("created_at"))
        conv = Conversation(key=str(key), title=(c.get("name") or "").strip(),
                            created_at=iso(c.get("created_at")), updated_at=iso(c.get("updated_at")))
        for m in c.get("chat_messages") or []:
            role = "user" if (m.get("sender") or "").lower() in ("human", "user") else "assistant"
            at = m.get("created_at")
            blocks = m.get("content") if isinstance(m.get("content"), list) else []
            if blocks:
                text_parts = []
                for bl in blocks:
                    kind = (bl or {}).get("type")
                    if kind == "text":
                        text_parts.append(bl.get("text") or "")
                    elif kind == "tool_use":
                        if text_parts:
                            conv.add(role, "\n\n".join(text_parts), at)
                            text_parts = []
                        conv.add("tool", f"→ {bl.get('name') or 'tool'}: {clip(json.dumps(bl.get('input') or {}, ensure_ascii=False))}", at)
                    elif kind == "tool_result":
                        if text_parts:
                            conv.add(role, "\n\n".join(text_parts), at)
                            text_parts = []
                        conv.add("tool", f"← {clip(text_of(bl.get('content')))}", at)
                text = "\n\n".join(text_parts)
            else:
                text = m.get("text") or ""
            files = [a.get("file_name") for a in (m.get("attachments") or []) + (m.get("files") or [])
                     if isinstance(a, dict) and a.get("file_name")]
            if files:
                attachments += len(files)
                text = (text + "\n\n" if text else "") + " ".join(f"[attached: {f}]" for f in files)
            conv.add(role, text, at)
        if _useful(conv):
            b.conversations.append(_finish(conv))
        else:
            b.skipped += 1
    if attachments:
        b.notes.append(f"{attachments:,} attachment(s) noted by name; their contents are not analysed")
    return b


# --------------------------------------------------------------------------- Claude Code


def _claude_code(name: str, rows: Iterable[dict[str, Any]]) -> Batch:
    """A Claude Code session log: one JSON object per line. An assistant message arrives
    split across lines (one content block each, same message id) and is put back together;
    a user line holding tool results is the tool speaking, not the user; meta lines,
    slash-command echoes and sub-agent sidechains are left out."""
    b = Batch("claude_code", name)
    convs: dict[str, Conversation] = {}
    state: dict[str, dict[str, Any]] = {}
    counts = {"thinking": 0, "sidechain": 0, "meta": 0}
    # A summary line names a conversation by the uuid of its last message, not by session.
    summaries: dict[str, str] = {}
    uuids: dict[str, set[str]] = {}
    for r in rows:
        typ = r.get("type")
        if typ == "summary":
            if r.get("summary") and r.get("leafUuid"):
                summaries[str(r["leafUuid"])] = str(r["summary"]).strip()
            continue
        if typ not in ("user", "assistant"):
            continue
        sid = str(r.get("sessionId") or Path(name).stem)
        if sid not in convs:
            convs[sid] = Conversation(key=sid, meta={"cwd": r.get("cwd"), "git_branch": r.get("gitBranch")})
            state[sid] = {"msg_id": None, "open": None}
            uuids[sid] = set()
        conv, st_ = convs[sid], state[sid]
        if r.get("uuid"):
            uuids[sid].add(str(r["uuid"]))
        if r.get("isSidechain"):
            counts["sidechain"] += 1
            continue
        if r.get("isMeta") or r.get("isVisibleInTranscriptOnly"):
            counts["meta"] += 1
            continue
        msg = r.get("message") or {}
        at = r.get("timestamp")
        content = msg.get("content")
        if conv.meta.get("cwd") is None and r.get("cwd"):
            conv.meta["cwd"] = r.get("cwd")
        if typ == "assistant":
            mid = msg.get("id")
            if msg.get("model") and not str(msg["model"]).startswith("<"):
                conv.model = msg["model"]
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for bl in blocks:
                kind = (bl or {}).get("type")
                if kind == "text" and (bl.get("text") or "").strip():
                    if st_["open"] is not None and st_["msg_id"] == mid and conv.turns and conv.turns[-1] is st_["open"]:
                        st_["open"]["text"] += "\n\n" + bl["text"].strip()
                    else:
                        conv.add("assistant", bl["text"], at)
                        st_["open"] = conv.turns[-1] if conv.turns else None
                elif kind == "tool_use":
                    conv.add("tool", f"→ {bl.get('name') or 'tool'}: {clip(json.dumps(bl.get('input') or {}, ensure_ascii=False))}", at)
                    st_["open"] = None
                elif kind in ("thinking", "redacted_thinking"):
                    counts["thinking"] += 1
            st_["msg_id"] = mid
            continue
        # A user line.
        st_["open"], st_["msg_id"] = None, None
        if r.get("isCompactSummary"):
            conv.add("system", f"[context summary]\n{clip(text_of(content), 4000)}", at)
            continue
        if isinstance(content, str):
            if content.lstrip().startswith(("<command-", "<local-command", "Caveat:")):
                counts["meta"] += 1
                continue
            conv.add("user", content, at)
            continue
        text_parts = []
        for bl in content or []:
            kind = (bl or {}).get("type")
            if kind == "tool_result":
                label = "error" if bl.get("is_error") else "result"
                conv.add("tool", f"← {label}: {clip(text_of(bl.get('content')))}", at)
            elif kind == "text":
                text_parts.append(bl.get("text") or "")
            elif kind == "image":
                text_parts.append("[image]")
        if text_parts:
            conv.add("user", "\n\n".join(text_parts), at)
    for sid, conv in convs.items():
        named = next((summaries[u] for u in uuids.get(sid, ()) if u in summaries), None)
        if named and not conv.title:
            conv.title = named
        cwd = conv.meta.get("cwd")
        conv.meta["project"] = Path(cwd).name if cwd else None
        conv.meta["tool_turns"] = sum(1 for t in conv.turns if t["role"] == "tool")
        if _useful(conv):
            _finish(conv)
            if conv.meta.get("project") and not conv.title.startswith(conv.meta["project"]):
                conv.title = f"{conv.meta['project']} — {conv.title}"
            b.conversations.append(conv)
        else:
            b.skipped += 1
    if counts["thinking"]:
        b.notes.append(f"{counts['thinking']:,} thinking block(s) left out — only what was shown is analysed")
    if counts["sidechain"]:
        b.notes.append(f"{counts['sidechain']:,} sub-agent line(s) left out")
    return b


# --------------------------------------------------------------------------- Codex CLI


def _codex(name: str, rows: Iterable[dict[str, Any]]) -> Batch:
    b = Batch("codex_cli", name)
    conv = Conversation(key=Path(name).stem)
    reasoning = 0
    for r in rows:
        typ = r.get("type")
        at = r.get("timestamp")
        if typ == "session_meta":
            p = r.get("payload") or {}
            conv.key = str(p.get("id") or conv.key)
            conv.meta["cwd"] = p.get("cwd")
            continue
        if typ == "turn_context":
            conv.model = (r.get("payload") or {}).get("model") or conv.model
            continue
        item = r.get("payload") if typ == "response_item" else (r if typ in (
            "message", "function_call", "function_call_output", "reasoning", "local_shell_call") else None)
        if not isinstance(item, dict):
            if "id" in r and "instructions" in r and not conv.meta.get("cwd"):
                conv.key = str(r.get("id") or conv.key)
            continue
        kind = item.get("type")
        if kind == "message":
            role = norm_role(item.get("role")) or "user"
            text = text_of(item.get("content"))
            if role == "user" and text.lstrip().startswith(("<environment_context>", "<user_instructions>")):
                role = "system"
            if role == "system" and text.lstrip().startswith("<environment_context>"):
                continue
            conv.add(role, text, at)
        elif kind in ("function_call", "local_shell_call"):
            args = item.get("arguments") or item.get("action") or {}
            args = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            conv.add("tool", f"→ {item.get('name') or 'shell'}: {clip(args)}", at)
        elif kind == "function_call_output":
            out = item.get("output")
            if isinstance(out, str):
                try:
                    out = json.loads(out).get("output", out)
                except (json.JSONDecodeError, AttributeError):
                    pass
            conv.add("tool", f"← result: {clip(text_of(out))}", at)
        elif kind == "reasoning":
            reasoning += 1
    cwd = conv.meta.get("cwd")
    conv.meta["project"] = Path(cwd).name if cwd else None
    if _useful(conv):
        _finish(conv)
        if conv.meta.get("project"):
            conv.title = f"{conv.meta['project']} — {conv.title}"
        b.conversations.append(conv)
    else:
        b.skipped += 1
    if reasoning:
        b.notes.append(f"{reasoning:,} reasoning item(s) left out")
    return b


# --------------------------------------------------------------------------- messages / ShareGPT


def _messages(name: str, items: Iterable[dict[str, Any]]) -> Batch:
    """One conversation per object carrying a `messages` array — the OpenAI and Anthropic
    request shape, a fine-tuning file, most eval harness logs. A `system` field and a
    trailing `response`/`output` are folded in when present."""
    b = Batch("messages", name)
    for i, obj in enumerate(items):
        msgs = obj.get("messages") or []
        conv = Conversation(key=str(obj.get("id") or obj.get("conversation_id") or f"{name}#{i}"),
                            title=str(obj.get("title") or obj.get("name") or "").strip(),
                            model=obj.get("model"), created_at=iso(obj.get("created_at") or obj.get("timestamp")))
        if obj.get("system"):
            conv.add("system", text_of(obj["system"]))
        for m in msgs:
            _add_message(conv, m)
        tail = next((obj.get(k) for k in ("response", "completion", "output") if obj.get(k)), None)
        if tail and (not conv.turns or conv.turns[-1]["role"] != "assistant"):
            conv.add("assistant", text_of(tail))
        if _useful(conv):
            b.conversations.append(_finish(conv))
        else:
            b.skipped += 1
    return b


def _add_message(conv: Conversation, m: Any) -> None:
    if not isinstance(m, dict):
        return
    if "role" not in m:
        # A message in some other app's shape (LM Studio's versions, a `sender` field…).
        got = _as_message(m)
        if got:
            conv.add(got["role"], got["text"], got["at"])
        return
    role = norm_role(m.get("role")) or "user"
    at = m.get("created_at") or m.get("timestamp")
    content = m.get("content")
    if isinstance(content, list) and any(isinstance(x, dict) and x.get("type") in ("tool_use", "tool_result")
                                         for x in content):
        texts = []
        for x in content:
            kind = (x or {}).get("type")
            if kind == "tool_use":
                if texts:
                    conv.add(role, "\n\n".join(texts), at)
                    texts = []
                conv.add("tool", f"→ {x.get('name') or 'tool'}: {clip(json.dumps(x.get('input') or {}, ensure_ascii=False))}", at)
            elif kind == "tool_result":
                if texts:
                    conv.add(role, "\n\n".join(texts), at)
                    texts = []
                conv.add("tool", f"← {clip(text_of(x.get('content')))}", at)
            else:
                texts.append(text_of(x))
        if texts:
            conv.add(role, "\n\n".join(t for t in texts if t), at)
        return
    if role == "tool":
        conv.add("tool", f"← {clip(text_of(content))}", at)
        return
    conv.add(role, text_of(content), at)
    for call in m.get("tool_calls") or []:
        fn = (call or {}).get("function") or {}
        conv.add("tool", f"→ {fn.get('name') or 'tool'}: {clip(str(fn.get('arguments') or ''))}", at)


def _sharegpt(name: str, items: Iterable[dict[str, Any]]) -> Batch:
    b = Batch("sharegpt", name)
    for i, obj in enumerate(items):
        conv = Conversation(key=str(obj.get("id") or f"{name}#{i}"), model=obj.get("model"),
                            title=str(obj.get("title") or "").strip())
        if obj.get("system"):
            conv.add("system", obj["system"])
        for m in obj.get("conversations") or []:
            if isinstance(m, dict):
                role = norm_role(m.get("from") or m.get("role")) or "user"
                text = m.get("value") if "value" in m else m.get("content")
                conv.add(role, text_of(text) if role != "tool" else f"← {clip(text_of(text))}")
        if _useful(conv):
            b.conversations.append(_finish(conv))
        else:
            b.skipped += 1
    return b


# --------------------------------------------------------------------------- pairs / CSV


def _pairs(name: str, rows: Iterable[dict[str, Any]]) -> Batch:
    """Single-turn rows. Stored as runs when the prompt matches a corpus prompt (so they
    count in Results), as two-turn conversations otherwise — see `intake_store`."""
    b = Batch("pairs", name)
    for i, r in enumerate(rows):
        keys = _pair_keys(r)
        if not keys:
            b.skipped += 1
            continue
        prompt, response = text_of(r[keys[0]]).strip(), text_of(r[keys[1]]).strip()
        if not prompt or not response:
            b.skipped += 1
            continue
        b.pairs.append({"prompt": prompt, "response": response,
                        "model": r.get("model") or r.get("model_id"),
                        "timestamp": iso(r.get("timestamp") or r.get("created_at")),
                        "key": str(r.get("id") or f"{name}#{i}"), "row": i})
    return b


def _looks_like_csv(s: str) -> bool:
    first = s.splitlines()[0] if s else ""
    if "," not in first and "\t" not in first:
        return False
    cols = {c.strip().strip('"').lower() for c in re.split(r"[,\t]", first)}
    return bool(cols & set(_PROMPT_KEYS) and cols & set(_RESPONSE_KEYS)) or \
        bool({"role", "content"} <= cols or {"speaker", "text"} <= cols)


def _from_csv(name: str, s: str) -> list[Batch]:
    try:
        dialect = csv.Sniffer().sniff(s[:4096], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(s), dialect=dialect))
    if not rows:
        return []
    cols = {c.lower(): c for c in rows[0] if c}
    if _pair_keys(rows[0]):
        return [_pairs(name, rows)]
    role_col = cols.get("role") or cols.get("speaker") or cols.get("sender") or cols.get("from")
    text_col = cols.get("content") or cols.get("text") or cols.get("message")
    if not (role_col and text_col):
        return []
    conv_col = next((cols[k] for k in ("conversation_id", "conversation", "thread_id", "session_id", "chat_id", "id")
                     if k in cols), None)
    b = Batch("csv_messages", name)
    groups: dict[str, Conversation] = {}
    for r in rows:
        key = str(r.get(conv_col) if conv_col else name)
        conv = groups.setdefault(key, Conversation(key=key))
        role = norm_role(r.get(role_col)) or "user"
        conv.add(role, r.get(text_col) or "", r.get(cols.get("timestamp", "")) if cols.get("timestamp") else None)
    for conv in groups.values():
        if _useful(conv):
            b.conversations.append(_finish(conv))
        else:
            b.skipped += 1
    return [b]


# --------------------------------------------------------------------------- transcripts


def _from_transcript(name: str, text: str) -> list[Batch]:
    from . import live

    split = live.split_turns(text)
    b = Batch("transcript", name)
    if not split["confident"] or split["convention"] is None:
        b.notes.append(
            f"{name}: no conversation found — the speakers could not be told apart. Label the "
            "turns (\"User:\" / \"Assistant:\"), or drop a structured export instead.")
        return [b]
    conv = Conversation(key=_digest(name, text[:2000]))
    for t in split["turns"]:
        role = t["role"] if t["role"] in ("user", "assistant", "system", "tool") else "user"
        conv.add(role, t["text"])
    conv.meta["split_convention"] = split["convention"]
    if _useful(conv):
        b.conversations.append(_finish(conv))
    return [b]


# --------------------------------------------------------------------------- anything else


_ROLE_KEYS = ("role", "sender", "from", "author", "speaker", "type", "user_type")
_TEXT_KEYS = ("content", "text", "value", "message", "parts", "body", "msg", "data")


def _as_message(el: Any) -> dict[str, Any] | None:
    """Read one element as a message if it plainly is one: a speaker we recognise and some
    text. Chosen conservatively — a list only counts as a conversation when most of it
    reads this way."""
    if not isinstance(el, dict):
        return None
    versions = el.get("versions")
    if isinstance(versions, list) and versions:
        pick = el.get("currentlySelected", len(versions) - 1)
        pick = pick if isinstance(pick, int) and 0 <= pick < len(versions) else len(versions) - 1
        return _as_message(versions[pick])
    if isinstance(el.get("message"), dict) and not any(k in el for k in ("role", "sender", "from", "author")):
        return _as_message(el["message"])
    role = None
    for k in _ROLE_KEYS:
        if k in el:
            role = norm_role(el[k])
            if role:
                break
    if role is None:
        return None
    text = ""
    for k in _TEXT_KEYS:
        if k in el and not isinstance(el[k], bool):
            text = text_of(el[k])
            if text.strip():
                break
    if not text.strip():
        return None
    return {"role": role, "text": text,
            "at": el.get("created_at") or el.get("timestamp") or el.get("create_time") or el.get("time")}


def _generic(name: str, obj: Any) -> Batch:
    """Walk an unknown JSON document for lists that read as conversations. Each such list
    becomes one conversation; a list is not searched inside once it has matched, and the
    title comes from its parent's `title`/`name` when there is one."""
    b = Batch("generic_json", name)
    found: list[tuple[list[Any], dict[str, Any] | None]] = []

    def walk(x: Any, parent: dict[str, Any] | None, depth: int) -> None:
        if depth > 8 or len(found) > 20000:
            return
        if isinstance(x, list):
            msgs = [m for m in (_as_message(e) for e in x) if m]
            if len(msgs) >= 2 and len(msgs) >= 0.6 * len(x):
                found.append((x, parent))
                return
            for e in x:
                walk(e, parent, depth + 1)
        elif isinstance(x, dict):
            for v in x.values():
                if isinstance(v, (list, dict)):
                    walk(v, x, depth + 1)

    walk(obj, obj if isinstance(obj, dict) else None, 0)
    for i, (lst, parent) in enumerate(found):
        title = ""
        if isinstance(parent, dict):
            title = str(parent.get("title") or parent.get("name") or "").strip()
        conv = Conversation(key=str((parent or {}).get("id") or f"{name}#{i}"), title=title)
        for e in lst:
            m = _as_message(e)
            if m:
                conv.add(m["role"], m["text"], m["at"])
        if _useful(conv):
            b.conversations.append(_finish(conv))
    if not b.conversations:
        b.notes.append(f"{name}: no conversation found in this JSON — no list of messages "
                       "with recognisable speakers (role / sender / from) and text")
    return b
