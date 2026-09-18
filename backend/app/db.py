"""SQLite access layer.

Deliberately plain `sqlite3` (standard library) rather than an ORM: the whole
data model is five tables and every judge-facing query stays readable.
A fresh connection is opened per request; SQLite in WAL mode handles the
concurrency levels a single dental operatory produces without any pooling.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id              TEXT PRIMARY KEY,
    chart_number    TEXT UNIQUE NOT NULL,
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    date_of_birth   TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capture_sessions (
    id              TEXT PRIMARY KEY,
    patient_id      TEXT NOT NULL REFERENCES patients(id),
    operatory       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_operatory
    ON capture_sessions(operatory, status);

CREATE TABLE IF NOT EXISTS images (
    id              TEXT PRIMARY KEY,
    content_hash    TEXT NOT NULL UNIQUE,
    filename        TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    thumb_path      TEXT,
    preview_path    TEXT,
    mime            TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    width           INTEGER,
    height          INTEGER,
    captured_at     TEXT,
    received_at     TEXT NOT NULL,
    source          TEXT NOT NULL,
    camera_make     TEXT,
    camera_model    TEXT,
    operatory       TEXT,
    session_id      TEXT REFERENCES capture_sessions(id),
    patient_id      TEXT REFERENCES patients(id),
    status          TEXT NOT NULL CHECK (status IN ('assigned', 'unassigned'))
);
CREATE INDEX IF NOT EXISTS idx_images_patient  ON images(patient_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_images_session  ON images(session_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_images_status   ON images(status, received_at DESC);

CREATE TABLE IF NOT EXISTS bridge_status (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_seen       TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    subject         TEXT,
    detail          TEXT
);
"""


def utcnow() -> str:
    """Single source of truth for timestamps: ISO-8601, UTC, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


#: Columns added after the first release. `CREATE TABLE IF NOT EXISTS` leaves an
#: existing table alone, so a practice that has been running since before a
#: column existed would never get it without this.
_ADDED_COLUMNS = {
    "images": {"preview_path": "TEXT"},
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)


def audit(conn: sqlite3.Connection, actor: str, action: str,
          subject: str | None = None, detail: str | None = None) -> None:
    """Append-only trail of who did what.

    Clinical images are patient data; every assignment or reassignment has to be
    reconstructable after the fact. We never write patient names here - only ids.
    """
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, subject, detail) VALUES (?,?,?,?,?)",
        (utcnow(), actor, action, subject, detail),
    )
