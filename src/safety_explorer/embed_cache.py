"""A persistent cache in front of an embedding backend.

An embedding is a pure function of (model, text), and a remote one costs a round trip —
and, for a cloud provider, money and the text leaving the machine again. So every vector is
stored the first time it is computed, keyed by the backend's fingerprint and a hash of the
text, in a SQLite file of its own beside the database (so it never bloats or locks the
research data, and deleting it loses nothing but time). Re-reading a history, restarting
the app, or switching back to a backend used before costs nothing.

Vectors are stored as float32 — well below the precision that could move a register level.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from array import array
from pathlib import Path
from typing import Any, Sequence

MEMORY_MAX = 20_000


def text_key(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:32]


class CachedBackend:
    """Wraps a backend; looks every text up before asking it."""

    def __init__(self, inner: Any, path: str | Path | None = None) -> None:
        self.inner = inner
        self.key = inner.fingerprint() if hasattr(inner, "fingerprint") else inner.name
        self.hits = 0
        self.misses = 0
        self._mem: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS embedding (
                backend TEXT NOT NULL, text_key TEXT NOT NULL, dim INTEGER NOT NULL,
                vec BLOB NOT NULL, PRIMARY KEY (backend, text_key))""")
            self._db.commit()

    # The backend protocol, delegated.
    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def semantic(self) -> bool:
        return bool(self.inner.semantic)

    @property
    def dim(self) -> int:
        return self.inner.dim

    def fingerprint(self) -> str:
        return self.key

    def __getattr__(self, item: str) -> Any:        # destination, model, calls, …
        return getattr(self.inner, item)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        keys = [text_key(t) for t in texts]
        found: dict[str, list[float]] = {}
        with self._lock:
            for k in keys:
                if k in self._mem:
                    found[k] = self._mem[k]
            missing = [k for k in dict.fromkeys(keys) if k not in found]
            if missing and self._db is not None:
                for i in range(0, len(missing), 500):
                    chunk = missing[i:i + 500]
                    rows = self._db.execute(
                        f"SELECT text_key, vec FROM embedding WHERE backend = ? AND text_key IN "
                        f"({','.join('?' * len(chunk))})", (self.key, *chunk)).fetchall()
                    for k, blob in rows:
                        v = array("f")
                        v.frombytes(blob)
                        found[k] = v.tolist()
                        self._remember(k, found[k])
        todo_idx = {}
        for i, k in enumerate(keys):
            if k not in found and k not in todo_idx:
                todo_idx[k] = i
        self.hits += len(keys) - len(todo_idx)
        if todo_idx:
            self.misses += len(todo_idx)
            vecs = self.inner.embed([texts[i] for i in todo_idx.values()])
            with self._lock:
                rows = []
                for k, v in zip(todo_idx, vecs):
                    found[k] = list(v)
                    self._remember(k, found[k])
                    rows.append((self.key, k, len(v), array("f", v).tobytes()))
                if self._db is not None and rows:
                    self._db.executemany("INSERT OR REPLACE INTO embedding (backend, text_key, dim, vec) "
                                         "VALUES (?, ?, ?, ?)", rows)
                    self._db.commit()
        return [found[k] for k in keys]

    def _remember(self, k: str, v: list[float]) -> None:
        if len(self._mem) >= MEMORY_MAX:
            self._mem.pop(next(iter(self._mem)))
        self._mem[k] = v

    def stored(self) -> int:
        if self._db is None:
            return len(self._mem)
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM embedding WHERE backend = ?", (self.key,)).fetchone()[0]
