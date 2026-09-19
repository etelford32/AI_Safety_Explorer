"""SQLite storage.

One file per instrument instance. SQLite is the right call here: the whole point of
a longitudinal study is that the data outlives the code, and a single portable file
that any tool can read in ten years beats a service that needs to be running.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB = Path("data/explorer.db")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


#: Columns added after the first release. `CREATE TABLE IF NOT EXISTS` will not add a
#: column to an existing table, so an instrument whose whole purpose is longitudinal
#: data must migrate additively rather than ask the user to start over.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("run", "stop_details", "TEXT NOT NULL DEFAULT '{}'"),
    ("prompt", "sub_arm", "TEXT NOT NULL DEFAULT 'ladder'"),
    ("ground_truth", "null_accuracy", "REAL"),
    ("prompt", "language", "TEXT NOT NULL DEFAULT 'en'"),
    ("run", "cue_id", "TEXT NOT NULL DEFAULT 'none'"),
    ("run", "cue_level", "INTEGER NOT NULL DEFAULT 0"),
    ("run", "cue_arm", "TEXT NOT NULL DEFAULT 'none'"),
    ("ground_truth", "graded_accuracy", "REAL"),
    ("ground_truth", "weighted_accuracy", "REAL"),
    ("ground_truth", "error_classes", "TEXT NOT NULL DEFAULT '{}'"),
    ("ground_truth", "consistency", "REAL"),
    ("ground_truth", "consistency_coverage", "REAL"),
    ("ground_truth", "relations_checked", "INTEGER NOT NULL DEFAULT 0"),
    ("ground_truth", "relations_satisfied", "INTEGER NOT NULL DEFAULT 0"),
    ("ground_truth", "relation_details", "TEXT NOT NULL DEFAULT '[]'"),
    ("ground_truth", "stated_values", "TEXT NOT NULL DEFAULT '{}'"),
    ("prompt", "answer_key", "TEXT NOT NULL DEFAULT 'full'"),
    ("ground_truth", "answer_key_cover", "TEXT NOT NULL DEFAULT 'full'"),
]


def migrate(conn: sqlite3.Connection) -> list[str]:
    applied = []
    for table, column, decl in MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table not created yet; the schema script will include it
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"{table}.{column}")
    if applied:
        conn.commit()
    return applied


def init_db(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA_PATH.read_text())
    migrate(conn)
    conn.commit()
    return conn


def insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    """Insert a row, JSON-encoding any dict/list values."""
    clean = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
    cols = ", ".join(clean)
    marks = ", ".join("?" for _ in clean)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(clean.values()))


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], key: str) -> None:
    clean = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
    cols = ", ".join(clean)
    marks = ", ".join("?" for _ in clean)
    updates = ", ".join(f"{c}=excluded.{c}" for c in clean if c != key)
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}",
        tuple(clean.values()),
    )


def query(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def query_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def loads(value: Any, default: Any = None) -> Any:
    """Decode a JSON column that may be NULL or already decoded."""
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default
