"""Durable outbound spool.

Every capture is written to disk *before* anyone tries to upload it. That single
decision is what makes the workflow safe to automate: if the network drops, the
backend restarts, or the capture PC loses power mid-shoot, the images are still
on disk and the queue drains when the link returns. Nothing depends on the
process staying alive.

Deduplication is by SHA-256 of the file bytes, in two places:

* `queue`  - the same file cannot be queued twice (a re-scanned SD card is a
             no-op rather than a second upload).
* `seen`   - once bytes have been accepted upstream they are never sent again,
             even across bridge restarts.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .config import config
from .logging_setup import setup_logging
from .models import CapturedImage, utcnow_iso

log = setup_logging().getChild("spool")

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id              TEXT PRIMARY KEY,
    content_hash    TEXT NOT NULL UNIQUE,
    filename        TEXT NOT NULL,
    blob_path       TEXT NOT NULL,
    meta_json       TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('pending', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    queued_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_ready ON queue(state, next_attempt_at);

CREATE TABLE IF NOT EXISTS seen (
    content_hash    TEXT PRIMARY KEY,
    first_seen      TEXT NOT NULL,
    disposition     TEXT NOT NULL
);
"""

# Exponential backoff, capped so a long outage still retries every few minutes.
BACKOFF_CAP_SECONDS = 300.0


@dataclass
class SpoolItem:
    id: str
    content_hash: str
    filename: str
    blob_path: Path
    meta: dict[str, Any]
    attempts: int

    def read_bytes(self) -> bytes:
        return self.blob_path.read_bytes()


class Spool:
    def __init__(self, directory: Path | None = None, max_attempts: int | None = None) -> None:
        self.dir = directory or config.spool_dir
        self.blob_dir = self.dir / "blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "spool.db"
        self.max_attempts = max_attempts if max_attempts is not None else config.max_upload_attempts
        # One writer at a time: capture threads enqueue while the uploader
        # updates attempt counts, and SQLite would otherwise raise 'db locked'.
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """Open, commit and always close - `sqlite3.Connection` as a context
        manager commits but leaves the handle open, which leaks over a clinic day."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            conn.executescript(SCHEMA)

    # -- writing -----------------------------------------------------------

    def enqueue(self, captured: CapturedImage) -> str | None:
        """Persist a capture and queue it for upload.

        Returns the spool id, or None when these exact bytes are already known.
        """
        content_hash = hashlib.sha256(captured.data).hexdigest()

        with self._lock, self._db() as conn:
            queued_row = conn.execute(
                "SELECT id, meta_json FROM queue WHERE content_hash=?", (content_hash,)
            ).fetchone()

            if conn.execute(
                "SELECT 1 FROM seen WHERE content_hash=?", (content_hash,)
            ).fetchone():
                log.debug("skip %s - already delivered", captured.filename)
                return None
            if queued_row is not None:
                log.debug("skip %s - already queued", captured.filename)
                return None

            suffix = Path(captured.filename).suffix or ".jpg"
            blob_path = self.blob_dir / f"{content_hash}{suffix}"
            if not blob_path.exists():
                # Write to a temp name then rename: a crash mid-write can never
                # leave a truncated blob that would later upload as a corrupt image.
                tmp_path = blob_path.with_suffix(blob_path.suffix + ".part")
                tmp_path.write_bytes(captured.data)
                tmp_path.replace(blob_path)

            item_id = str(uuid.uuid4())
            meta = {
                "source": captured.source,
                "operatory": config.operatory,
                "captured_at": captured.captured_at,
                "camera_make": captured.camera_make,
                "camera_model": captured.camera_model,
                "content_hash": content_hash,
                "origin": captured.origin,
                **captured.extra,
            }
            conn.execute(
                "INSERT INTO queue (id, content_hash, filename, blob_path, meta_json,"
                " state, attempts, next_attempt_at, queued_at)"
                " VALUES (?,?,?,?,?,'pending',0,0,?)",
                (item_id, content_hash, captured.filename, str(blob_path),
                 json.dumps(meta), utcnow_iso()),
            )
        log.info("queued %s from %s (%d bytes)", captured.filename,
                 captured.source, len(captured.data))
        return item_id

    # -- reading -----------------------------------------------------------

    def due_items(self, limit: int = 8) -> list[SpoolItem]:
        now = time.time()
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM queue WHERE state='pending' AND next_attempt_at<=?"
                " ORDER BY queued_at LIMIT ?",
                (now, limit),
            ).fetchall()
        return [
            SpoolItem(
                id=row["id"],
                content_hash=row["content_hash"],
                filename=row["filename"],
                blob_path=Path(row["blob_path"]),
                meta=json.loads(row["meta_json"]),
                attempts=row["attempts"],
            )
            for row in rows
        ]

    def stats(self) -> dict[str, int]:
        with self._lock, self._db() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM queue WHERE state='pending'"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM queue WHERE state='failed'"
            ).fetchone()[0]
            delivered = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        return {"pending": pending, "failed": failed, "delivered": delivered}

    # -- state transitions -------------------------------------------------

    def mark_sent(self, item: SpoolItem, disposition: str = "stored") -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO seen (content_hash, first_seen, disposition)"
                " VALUES (?,?,?)",
                (item.content_hash, utcnow_iso(), disposition),
            )
            conn.execute("DELETE FROM queue WHERE id=?", (item.id,))
        item.blob_path.unlink(missing_ok=True)
        log.info("delivered %s (%s)", item.filename, disposition)

    def mark_retry(self, item: SpoolItem, error: str) -> None:
        attempts = item.attempts + 1
        if attempts >= self.max_attempts:
            self.mark_failed(item, f"gave up after {attempts} attempts: {error}")
            return
        # Jitter keeps a fleet of bridges from retrying in lockstep after an
        # outage; the blob stays on disk the whole time.
        delay = min(2.0 ** attempts, BACKOFF_CAP_SECONDS) * (0.8 + random.random() * 0.4)
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE queue SET attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
                (attempts, time.time() + delay, error[:500], item.id),
            )
        log.warning("retry %s in %.0fs (attempt %d/%d): %s",
                    item.filename, delay, attempts, self.max_attempts, error)

    def mark_failed(self, item: SpoolItem, error: str) -> None:
        """Park an item for human attention. The bytes are deliberately kept."""
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE queue SET state='failed', last_error=? WHERE id=?",
                (error[:500], item.id),
            )
        log.error("failed %s: %s", item.filename, error)

    def retry_failed(self) -> int:
        """Re-arm everything in the failed bucket (tray menu / support action)."""
        with self._lock, self._db() as conn:
            cursor = conn.execute(
                "UPDATE queue SET state='pending', attempts=0, next_attempt_at=0"
                " WHERE state='failed'"
            )
            count = cursor.rowcount
        if count:
            log.info("re-queued %d failed item(s)", count)
        return count

    def failures(self) -> list[dict[str, Any]]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT filename, last_error, queued_at FROM queue WHERE state='failed'"
                " ORDER BY queued_at DESC LIMIT 20"
            ).fetchall()
        return [dict(row) for row in rows]
