"""SQLite access layer. Every statement in the project lives in this module.

Three processes (watcher, worker, dashboard) share one database file, so the
connection is opened in WAL mode with a busy timeout: readers never block the
writer, and a writer that collides with another writer waits instead of
raising "database is locked".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import config

DB_PATH: Path = config.DB_PATH

STATUS_REVIEW: str = "review"
STATUS_APPROVED: str = "approved"
STATUS_UPLOADING: str = "uploading"
STATUS_UPLOADED: str = "uploaded"
STATUS_FAILED: str = "failed"

STATUSES: tuple[str, ...] = (
    STATUS_REVIEW,
    STATUS_APPROVED,
    STATUS_UPLOADING,
    STATUS_UPLOADED,
    STATUS_FAILED,
)

# Columns a caller may hand to update_items(). Anything else is rejected
# rather than interpolated into SQL.
EDITABLE_FIELDS: frozenset[str] = frozenset(
    {"event", "camera", "orientation", "kind", "needs_review", "status"}
)

BUSY_TIMEOUT_MS: int = 10_000


def now_iso() -> str:
    """UTC timestamp used for every created_at / updated_at value."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    """Open a connection in WAL mode with row access by column name.

    ``isolation_level=None`` puts the connection in autocommit mode so that
    transactions are started explicitly where they matter (claim_next_approved).
    """
    conn = sqlite3.connect(
        DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Open a connection for one unit of work and always close it.

    sqlite3's own context manager commits but leaves the handle open; three
    long-running processes cannot afford to leak file handles.
    """
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create tables and indexes if they are not already there."""
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id           INTEGER PRIMARY KEY,
                hash         TEXT UNIQUE,
                orig_name    TEXT,
                staged_path  TEXT,
                kind         TEXT,
                event        TEXT,
                camera       TEXT,
                orientation  TEXT,
                status       TEXT,
                needs_review INTEGER DEFAULT 0,
                drive_id     TEXT,
                error        TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS aliases (
                raw   TEXT PRIMARY KEY,
                clean TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                name       TEXT PRIMARY KEY,
                created_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
            CREATE INDEX IF NOT EXISTS idx_items_hash   ON items(hash);
            """
        )


def insert_item(
    hash: str,
    orig_name: str,
    staged_path: str,
    kind: str,
    event: str,
    camera: str,
    orientation: str,
    status: str = STATUS_REVIEW,
    needs_review: int = 0,
) -> int | None:
    """Insert a new item, or return None if the hash was already ingested."""
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status!r}")

    ts = now_iso()
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO items
                (hash, orig_name, staged_path, kind, event, camera,
                 orientation, status, needs_review, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hash,
                orig_name,
                staged_path,
                kind,
                event,
                camera,
                orientation,
                status,
                int(needs_review),
                ts,
                ts,
            ),
        )
        if cur.rowcount == 0:
            return None
        return int(cur.lastrowid)


def seen_hash(h: str) -> bool:
    """True if an item with this content hash already exists."""
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM items WHERE hash = ?", (h,)).fetchone()
    return row is not None


def claim_next_approved() -> sqlite3.Row | None:
    """Atomically take the oldest approved item and mark it 'uploading'.

    BEGIN IMMEDIATE takes the write lock before the SELECT, so two uploader
    threads racing here cannot read the same row: the loser waits, then sees
    the row already flipped and moves to the next one.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM items WHERE status = ? ORDER BY id LIMIT 1",
            (STATUS_APPROVED,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None

        item_id = int(row["id"])
        conn.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
            (STATUS_UPLOADING, now_iso(), item_id),
        )
        claimed = conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        conn.execute("COMMIT")
        return claimed
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def mark_uploaded(item_id: int, drive_id: str) -> None:
    """Record a successful upload and clear any previous error."""
    with _conn() as conn:
        conn.execute(
            """
            UPDATE items
               SET status = ?, drive_id = ?, error = NULL, updated_at = ?
             WHERE id = ?
            """,
            (STATUS_UPLOADED, drive_id, now_iso(), item_id),
        )


def mark_failed(item_id: int, error: str) -> None:
    """Park an item as failed with the reason attached."""
    with _conn() as conn:
        conn.execute(
            "UPDATE items SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (STATUS_FAILED, error, now_iso(), item_id),
        )


def list_items(
    status: str, event: str | None = None, limit: int = 300
) -> list[sqlite3.Row]:
    """Newest-first items in one status, optionally narrowed to one event."""
    sql = "SELECT * FROM items WHERE status = ?"
    params: list[Any] = [status]
    if event is not None:
        sql += " AND event = ?"
        params.append(event)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with _conn() as conn:
        return conn.execute(sql, params).fetchall()


def get_item(item_id: int) -> sqlite3.Row | None:
    """One item by id."""
    with _conn() as conn:
        return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def update_items(ids: list[int], fields: dict[str, Any], approve: bool) -> int:
    """Apply the same field edits to several items; return rows changed.

    With ``approve=True`` the items also move to 'approved' and lose their
    needs_review flag — that is the dashboard's one-click path out of the
    review queue.
    """
    if not ids:
        return 0

    payload: dict[str, Any] = dict(fields)
    if approve:
        payload["status"] = STATUS_APPROVED
        payload["needs_review"] = 0

    unknown = set(payload) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    if "status" in payload and payload["status"] not in STATUSES:
        raise ValueError(f"unknown status: {payload['status']!r}")
    if not payload:
        return 0

    columns = sorted(payload)
    assignments = ", ".join(f"{col} = ?" for col in columns)
    placeholders = ", ".join("?" for _ in ids)
    params: list[Any] = [payload[col] for col in columns]
    params.append(now_iso())
    params.extend(ids)

    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE items SET {assignments}, updated_at = ? "
            f"WHERE id IN ({placeholders})",
            params,
        )
        return cur.rowcount


def stats() -> dict[str, int]:
    """Item counts per status, plus totals the dashboard header shows."""
    counts: dict[str, int] = {status: 0 for status in STATUSES}
    with _conn() as conn:
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status"):
            counts[str(row["status"])] = int(row["n"])
        total = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        flagged = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE needs_review = 1"
        ).fetchone()["n"]

    counts["total"] = int(total)
    counts["needs_review"] = int(flagged)
    return counts


def upsert_alias(raw: str, clean: str) -> None:
    """Map a messy folder/camera string onto its canonical name."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO aliases (raw, clean) VALUES (?, ?)
            ON CONFLICT(raw) DO UPDATE SET clean = excluded.clean
            """,
            (raw, clean),
        )


def resolve_alias(raw: str) -> str:
    """Canonical name for ``raw``, or ``raw`` itself when nothing maps."""
    with _conn() as conn:
        row = conn.execute("SELECT clean FROM aliases WHERE raw = ?", (raw,)).fetchone()
    if row is None or row["clean"] is None:
        return raw
    return str(row["clean"])


def list_aliases() -> list[sqlite3.Row]:
    """Every alias mapping, alphabetical by raw string."""
    with _conn() as conn:
        return conn.execute("SELECT raw, clean FROM aliases ORDER BY raw").fetchall()


def add_event(name: str) -> None:
    """Register an event name; existing names are left untouched."""
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO events (name, created_at) VALUES (?, ?)",
            (name, now_iso()),
        )


def list_events() -> list[str]:
    """Known event names, alphabetical."""
    with _conn() as conn:
        rows = conn.execute("SELECT name FROM events ORDER BY name").fetchall()
    return [str(row["name"]) for row in rows]
