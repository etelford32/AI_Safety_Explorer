"""Sources — where conversations already live on this computer, and a worker that keeps
them flowing in without anyone clicking Import again.

Three ways in, all ending in `intake` + `intake_store`:

* **The inbox.** A folder in the data directory (or `EXPLORER_INBOX`). Anything saved or
  dragged into it — a ChatGPT export zip, a folder of agent logs, a CSV — is detected,
  imported, and moved to `inbox/imported/` (or `inbox/failed/`, with the reason logged).
  It is the one source that is always on, because putting a file there *is* the request.
* **Connected sources.** Folders that tools keep writing conversations to: Claude Code's
  session logs, the Codex CLI's, any folder a user names. Once connected, a source is
  re-scanned every few seconds and only files whose size or modification time changed are
  re-read — so an agent working in another window shows up here turn by turn, and a log
  that has not changed costs a `stat`.
* **Found exports.** A ChatGPT or Claude.ai data export sitting in Downloads, offered for a
  one-click import.

The consent rule is the push-never-pull boundary carried over to files. `discover` looks
only in a short list of well-known places and reads nothing but names, sizes and dates —
plus a zip's table of contents, to tell an export from any other archive. No file's
contents are read until the user connects its source or imports it, and a source can be
disconnected at any time. Everything stays on this machine.
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
import traceback
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import db, intake, intake_store, sessions
from .db import loads, new_id, now_iso, query, query_one

#: How often connected sources are re-scanned.
SCAN_INTERVAL = 8.0
#: A large, busy log (an agent's) is re-read at most this often, however often it changes.
BIG_FILE = 5 * 1024 * 1024
BIG_FILE_INTERVAL = 60.0
#: Uploads larger than this are refused (the zip of a ChatGPT export with images can be
#: several GB; only its JSON is read, from disk, but the upload still has to fit).
MAX_UPLOAD = 4 * 1024 ** 3


@dataclass
class Known:
    kind: str
    label: str
    note: str
    pattern: str
    roots: Callable[[], list[Path]]


def _env_path(var: str, *tail: str) -> list[Path]:
    v = os.environ.get(var)
    return [Path(v).expanduser().joinpath(*tail)] if v else []


KNOWN: list[Known] = [
    Known("claude_code", "Claude Code sessions",
          "Every Claude Code session, one log per session, per project. Tool calls and their "
          "results come in as tool turns; thinking is left out.",
          "*/*.jsonl",
          lambda: _env_path("CLAUDE_CONFIG_DIR", "projects") + [Path.home() / ".claude" / "projects"]),
    Known("codex", "Codex CLI sessions",
          "The OpenAI Codex CLI's session logs (one file per session, by date).",
          "**/rollout-*.jsonl",
          lambda: _env_path("CODEX_HOME", "sessions") + [Path.home() / ".codex" / "sessions"]),
]


def default_inbox(db_path: str | Path) -> Path:
    env = os.environ.get("EXPLORER_INBOX")
    return Path(env).expanduser() if env else Path(db_path).resolve().parent / "inbox"


def ensure(conn) -> None:
    """The intake tables, for a connection that has not run the schema."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS intake_source (id TEXT PRIMARY KEY, kind TEXT NOT NULL,
            path TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', watch INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, last_scan TEXT, n_files INTEGER NOT NULL DEFAULT 0,
            n_conversations INTEGER NOT NULL DEFAULT 0, error TEXT);
        CREATE TABLE IF NOT EXISTS intake_file (path TEXT PRIMARY KEY, source_id TEXT,
            size INTEGER NOT NULL, mtime REAL NOT NULL, format TEXT,
            n_conversations INTEGER NOT NULL DEFAULT 0, scanned_at TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS intake_event (id TEXT PRIMARY KEY, at TEXT NOT NULL,
            origin TEXT NOT NULL, source_id TEXT, formats TEXT NOT NULL DEFAULT '[]',
            stats TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'ok',
            message TEXT NOT NULL DEFAULT '');
    """)
    sessions.ensure(conn)


# --------------------------------------------------------------------------- discovery


def _files(root: Path, pattern: str, limit: int = 20000) -> list[Path]:
    out = []
    try:
        for f in root.glob(pattern):
            if f.is_file():
                out.append(f)
                if len(out) >= limit:
                    break
    except OSError:
        pass
    return out


def _export_kind(path: Path) -> str | None:
    """Tell a chat export from any other zip by its table of contents — no contents read."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = {Path(n).name for n in zf.namelist()}
    except (zipfile.BadZipFile, OSError):
        return None
    if "conversations.json" not in names:
        return None
    if "chat.html" in names or "message_feedback.json" in names:
        return "ChatGPT export"
    if "users.json" in names or "projects.json" in names:
        return "Claude.ai export"
    return "Chat export"


def discover(conn) -> dict[str, Any]:
    """What is on this computer, by name, size and date only."""
    ensure(conn)
    connected = {(r["kind"], r["path"]): r for r in list_sources(conn)}
    known = []
    for k in KNOWN:
        for root in k.roots():
            if not root.is_dir():
                continue
            files = _files(root, k.pattern)
            if not files:
                continue
            stats = [f.stat() for f in files]
            src = connected.get((k.kind, str(root)))
            known.append({
                "kind": k.kind, "label": k.label, "note": k.note, "path": str(root),
                "n_files": len(files), "bytes": sum(s.st_size for s in stats),
                "newest": intake.iso(max(s.st_mtime for s in stats)),
                "connected": bool(src), "source_id": src["id"] if src else None,
            })
            break   # the first root that exists is the one in use
    exports = []
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        try:
            entries = sorted(downloads.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:400]
        except OSError:
            entries = []
        for f in entries:
            if not f.is_file():
                continue
            kind = None
            if f.suffix.lower() == ".zip" and f.stat().st_size < MAX_UPLOAD:
                kind = _export_kind(f)
            elif f.name.lower().startswith("conversations") and f.suffix.lower() == ".json":
                kind = "Conversations file"
            if not kind:
                continue
            st = f.stat()
            seen = query_one(conn, "SELECT size, mtime FROM intake_file WHERE path = ?", (str(f),))
            exports.append({"path": str(f), "name": f.name, "kind": kind, "bytes": st.st_size,
                            "modified": intake.iso(st.st_mtime),
                            "imported": bool(seen and seen["size"] == st.st_size
                                             and abs(seen["mtime"] - st.st_mtime) < 1e-3)})
            if len(exports) >= 20:
                break
    return {"known": known, "exports": exports}


# --------------------------------------------------------------------------- sources


def list_sources(conn) -> list[dict[str, Any]]:
    ensure(conn)
    return query(conn, "SELECT * FROM intake_source ORDER BY created_at")


def connect_source(conn, kind: str, path: str, label: str = "") -> dict[str, Any]:
    ensure(conn)
    p = Path(path).expanduser()
    if not p.exists():
        raise ValueError(f"{path} does not exist")
    row = query_one(conn, "SELECT * FROM intake_source WHERE kind = ? AND path = ?", (kind, str(p)))
    if row:
        conn.execute("UPDATE intake_source SET watch = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        return dict(row, watch=1)
    known = next((k for k in KNOWN if k.kind == kind), None)
    sid = new_id("src")
    db.insert(conn, "intake_source", {
        "id": sid, "kind": kind, "path": str(p),
        "label": label or (known.label if known else p.name), "watch": 1,
        "created_at": now_iso()})
    conn.commit()
    return query_one(conn, "SELECT * FROM intake_source WHERE id = ?", (sid,))


def disconnect_source(conn, source_id: str) -> None:
    """Stop watching. What was imported stays; nothing further is read."""
    ensure(conn)
    conn.execute("UPDATE intake_source SET watch = 0 WHERE id = ? AND kind != 'inbox'", (source_id,))
    conn.commit()


def _pattern(kind: str) -> str:
    known = next((k for k in KNOWN if k.kind == kind), None)
    return known.pattern if known else "**/*"


# --------------------------------------------------------------------------- the worker


class IntakeWorker:
    """One background thread that does every import: uploads the user committed, files
    dropped in the inbox, and changes in connected sources. One at a time, so two imports
    never race for the same session; a campaign running meanwhile is unaffected (SQLite in
    WAL mode, each writer waiting its turn)."""

    def __init__(self, db_path: str, corpus, inbox: Path, interval: float = SCAN_INTERVAL):
        self.db_path = str(db_path)
        self.corpus = corpus
        self.inbox = Path(inbox)
        self.interval = interval
        self.tasks: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"busy": False, "current": None, "done": 0, "total": 0,
                                      "last_event": None, "last_error": None, "scans": 0}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_read: dict[str, float] = {}
        self.uploads = self.inbox.parent / ".intake-uploads"

    # -- lifecycle
    def start(self) -> "IntakeWorker":
        for d in (self.inbox, self.inbox / "imported", self.inbox / "failed", self.uploads):
            d.mkdir(parents=True, exist_ok=True)
        conn = db.connect(self.db_path)
        try:
            ensure(conn)
            if not query_one(conn, "SELECT 1 AS x FROM intake_source WHERE kind = 'inbox' AND path = ?",
                             (str(self.inbox),)):
                db.insert(conn, "intake_source", {
                    "id": new_id("src"), "kind": "inbox", "path": str(self.inbox),
                    "label": "Inbox", "watch": 1, "created_at": now_iso()})
                conn.commit()
        finally:
            conn.close()
        self._thread = threading.Thread(target=self._run, name="intake", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def brief(self) -> dict[str, Any]:
        with self.lock:
            return {k: self.state[k] for k in ("busy", "current", "done", "total", "last_event")} | {
                "queued": self.tasks.qsize(), "inbox": str(self.inbox)}

    # -- requests from the server
    def submit_file(self, path: Path, origin: str, *, delete_after: bool = False,
                    source_id: str | None = None) -> None:
        self.tasks.put(("file", {"path": Path(path), "origin": origin,
                                 "delete_after": delete_after, "source_id": source_id}))

    def submit_scan(self, source_id: str | None = None) -> None:
        self.tasks.put(("scan", {"source_id": source_id}))

    def save_upload(self, name: str, stream, length: int) -> Path:
        if length > MAX_UPLOAD:
            raise ValueError(f"upload too large ({length:,} bytes; the limit is {MAX_UPLOAD:,})")
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (name or "upload"))[-120:]
        dest = self.uploads / f"{new_id('up')}__{safe}"
        remaining = length
        with dest.open("wb") as fh:
            while remaining > 0:
                chunk = stream.read(min(1 << 20, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining:
            dest.unlink(missing_ok=True)
            raise ValueError("the upload was cut short")
        return dest

    def upload_path(self, token: str) -> Path:
        p = (self.uploads / token).resolve()
        if self.uploads.resolve() not in p.parents or not p.is_file():
            raise ValueError("unknown or expired upload")
        return p

    # -- the loop
    def _run(self) -> None:
        conn = db.connect(self.db_path)
        ensure(conn)
        next_scan = 0.0
        while not self._stop.is_set():
            timeout = max(0.2, next_scan - time.time())
            try:
                kind, args = self.tasks.get(timeout=timeout)
            except queue.Empty:
                kind, args = "scan", {"source_id": None, "periodic": True}
            try:
                if kind == "file":
                    self._import_file(conn, **args)
                elif kind == "scan":
                    self._scan(conn, args.get("source_id"))
                    if args.get("periodic") or args.get("source_id") is None:
                        next_scan = time.time() + self.interval
            except Exception as exc:  # noqa: BLE001 — logged and shown, never fatal
                traceback.print_exc()
                with self.lock:
                    self.state["last_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                with self.lock:
                    self.state.update(busy=False, current=None)

    def _progress(self, label: str) -> Callable[[int, int, str], None]:
        def cb(done: int, total: int, item: str) -> None:
            with self.lock:
                self.state.update(busy=True, current=label, done=done, total=total)
        return cb

    def _warm(self, conn, ids: list[str], label: str) -> None:
        """Compute the triage for what was just imported, so the list is instant."""
        for i, sid in enumerate(ids):
            if self._stop.is_set():
                return
            with self.lock:
                self.state.update(busy=True, current=f"reading {label}", done=i, total=len(ids))
            sessions.summary(conn, sid, self.corpus)

    def _import_file(self, conn, path: Path, origin: str, delete_after: bool = False,
                     source_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            self.state.update(busy=True, current=origin, done=0, total=0)
        try:
            batches = intake.detect_path(path)
            stats = intake_store.import_batches(conn, self.corpus, batches,
                                                on_progress=self._progress(origin),
                                                extra_meta={"source_id": source_id} if source_id else None)
        except Exception as exc:  # noqa: BLE001
            intake_store.record_event(conn, origin, None, source_id=source_id, status="error",
                                      message=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if delete_after:
                Path(path).unlink(missing_ok=True)
        status = "ok" if stats["conversations"] or stats["runs_added"] else "empty"
        msg = "" if status == "ok" else "; ".join(stats["notes"]) or "nothing recognisable in it"
        eid = intake_store.record_event(conn, origin, dict(stats), source_id=source_id,
                                        status=status, message=msg)
        with self.lock:
            self.state["last_event"] = {"id": eid, "origin": origin, "status": status,
                                        "conversations": stats["conversations"],
                                        "new": stats["new"], "turns_added": stats["turns_added"],
                                        "runs_added": stats["runs_added"], "at": now_iso()}
        self._warm(conn, stats["session_ids"], origin)
        return stats

    def _scan(self, conn, source_id: str | None) -> None:
        rows = query(conn, "SELECT * FROM intake_source WHERE watch = 1"
                     + (" AND id = ?" if source_id else ""), (source_id,) if source_id else ())
        with self.lock:
            self.state["scans"] += 1
        for src in rows:
            if self._stop.is_set():
                return
            try:
                if src["kind"] == "inbox":
                    self._scan_inbox(conn, src)
                else:
                    self._scan_source(conn, src, force=bool(source_id))
                conn.execute("UPDATE intake_source SET last_scan = ?, error = NULL WHERE id = ?",
                             (now_iso(), src["id"]))
            except Exception as exc:  # noqa: BLE001
                conn.execute("UPDATE intake_source SET last_scan = ?, error = ? WHERE id = ?",
                             (now_iso(), f"{type(exc).__name__}: {exc}", src["id"]))
            conn.commit()

    def _scan_inbox(self, conn, src: dict[str, Any]) -> None:
        inbox = Path(src["path"])
        if not inbox.is_dir():
            return
        for f in sorted(inbox.iterdir()):
            if f.name.startswith(".") or f.name in ("imported", "failed"):
                continue
            # A file still being written (a browser download in progress) is left for the
            # next pass: its size must hold still between two looks.
            try:
                size = f.stat().st_size if f.is_file() else None
            except OSError:
                continue
            if size is not None:
                time.sleep(0.2)
                if f.stat().st_size != size or f.suffix in (".crdownload", ".part", ".download"):
                    continue
            ok = True
            try:
                stats = self._import_file(conn, f, f"inbox/{f.name}", source_id=src["id"])
                ok = bool(stats["conversations"] or stats["runs_added"])
            except Exception:  # noqa: BLE001 — recorded as an event by _import_file
                ok = False
            dest_dir = inbox / ("imported" if ok else "failed")
            dest = dest_dir / f.name
            if dest.exists():
                dest = dest_dir / f"{f.stem}-{int(time.time())}{f.suffix}"
            try:
                shutil.move(str(f), str(dest))
            except OSError:
                pass

    def _scan_source(self, conn, src: dict[str, Any], force: bool = False) -> None:
        root = Path(src["path"])
        if not root.exists():
            raise FileNotFoundError(f"{root} no longer exists")
        files = [root] if root.is_file() else _files(root, _pattern(src["kind"]))
        changed = []
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            seen = query_one(conn, "SELECT size, mtime FROM intake_file WHERE path = ?", (str(f),))
            if seen and seen["size"] == st.st_size and abs(seen["mtime"] - st.st_mtime) < 1e-3:
                continue
            last = self._last_read.get(str(f), 0.0)
            if not force and st.st_size > BIG_FILE and time.time() - last < BIG_FILE_INTERVAL:
                continue
            changed.append((f, st))
        total: dict[str, Any] = {"files": 0, "conversations": 0, "new": 0, "updated": 0,
                                 "turns_added": 0, "runs_added": 0, "formats": set(), "session_ids": []}
        for i, (f, st) in enumerate(changed):
            if self._stop.is_set():
                return
            with self.lock:
                self.state.update(busy=True, current=f"{src['label']}: {f.name}", done=i, total=len(changed))
            self._last_read[str(f)] = time.time()
            err = None
            n = 0
            fmt = None
            try:
                batches = intake.detect_path(f)
                stats = intake_store.import_batches(conn, self.corpus, batches,
                                                    extra_meta={"source_id": src["id"]})
                n = stats["conversations"]
                fmt = ",".join(stats["formats"])
                total["files"] += 1
                for k in ("conversations", "new", "updated", "turns_added", "runs_added"):
                    total[k] += stats[k]
                total["formats"] |= set(stats["formats"])
                total["session_ids"] += stats["session_ids"]
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
            db.upsert(conn, "intake_file", {
                "path": str(f), "source_id": src["id"], "size": st.st_size, "mtime": st.st_mtime,
                "format": fmt, "n_conversations": n, "scanned_at": now_iso(), "error": err}, key="path")
            conn.commit()
        agg = query_one(conn, "SELECT COUNT(*) AS files, COALESCE(SUM(n_conversations), 0) AS convs "
                              "FROM intake_file WHERE source_id = ?", (src["id"],))
        conn.execute("UPDATE intake_source SET n_files = ?, n_conversations = ? WHERE id = ?",
                     (agg["files"], agg["convs"], src["id"]))
        conn.commit()
        if total["new"] or total["updated"] or total["runs_added"]:
            total["formats"] = sorted(total["formats"])
            eid = intake_store.record_event(conn, src["label"], dict(total), source_id=src["id"])
            with self.lock:
                self.state["last_event"] = {"id": eid, "origin": src["label"], "status": "ok",
                                            "conversations": total["conversations"], "new": total["new"],
                                            "turns_added": total["turns_added"],
                                            "runs_added": total["runs_added"], "at": now_iso()}
            self._warm(conn, total["session_ids"], src["label"])

    # -- a direct import of a file already on disk (a found export)
    def import_existing(self, conn, path: str) -> None:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ValueError(f"{path} is not a file")
        st = p.stat()
        db.upsert(conn, "intake_file", {"path": str(p), "source_id": None, "size": st.st_size,
                                        "mtime": st.st_mtime, "format": None, "n_conversations": 0,
                                        "scanned_at": now_iso(), "error": None}, key="path")
        conn.commit()
        self.submit_file(p, p.name)


def status(conn, worker: IntakeWorker | None) -> dict[str, Any]:
    ensure(conn)
    srcs = list_sources(conn)
    counts = query_one(conn, """SELECT COUNT(*) AS n,
            SUM(CASE WHEN source LIKE 'import:%' THEN 1 ELSE 0 END) AS imported
        FROM live_session""") or {}
    return {
        "worker": worker.brief() if worker else None,
        "inbox": str(worker.inbox) if worker else None,
        "sources": [dict(s, watch=bool(s["watch"])) for s in srcs],
        "events": intake_store.recent_events(conn, 15),
        "n_sessions": counts.get("n") or 0,
        "n_imported": counts.get("imported") or 0,
        "formats": [{"format": k, "label": v[0], "tier": v[1]} for k, v in intake.FORMATS.items()],
    }


__all__ = ["IntakeWorker", "discover", "connect_source", "disconnect_source", "list_sources",
           "status", "default_inbox", "loads"]
