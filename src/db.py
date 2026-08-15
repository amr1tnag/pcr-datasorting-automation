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
                updated_at   TEXT,
                src_id       TEXT,
                year         INTEGER,
                reasons      TEXT,
                attempts     INTEGER DEFAULT 0,
                original_id  TEXT
            );

            -- One row per event and year: the folder the gallery link points
            -- at, and the link itself once it has been shared.
            CREATE TABLE IF NOT EXISTS galleries (
                event      TEXT,
                year       INTEGER,
                folder_id  TEXT,
                link       TEXT,
                created_at TEXT,
                PRIMARY KEY (event, year)
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
            CREATE INDEX IF NOT EXISTS idx_items_src    ON items(src_id);
            """
        )
        _add_missing_columns(conn)


# Columns added after the first release. A database already sitting on the
# club laptop is upgraded in place rather than rebuilt: the archive's record
# of what has been ingested is not worth losing to a schema change.
LATER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("src_id", "TEXT"),
    ("year", "INTEGER"),
    ("reasons", "TEXT"),
    ("attempts", "INTEGER DEFAULT 0"),
    ("original_id", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring an older items table up to date, one ALTER at a time."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    for column, declaration in LATER_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE items ADD COLUMN {column} {declaration}")


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
    src_id: str | None = None,
    year: int | None = None,
    reasons: str = "",
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
                 orientation, status, needs_review, created_at, updated_at,
                 src_id, year, reasons)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                src_id,
                year,
                reasons,
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


def mark_uploaded(item_id: int, drive_id: str, original_id: str | None = None) -> None:
    """Record a finished item and clear any previous error.

    ``drive_id`` is what a viewer sees: the watermarked copy for a photo, or
    the file itself for RAW and video. ``original_id`` is the unmarked
    original once it has been filed away, which is what proves the source is
    safely in the archive.
    """
    with _conn() as conn:
        conn.execute(
            """
            UPDATE items
               SET status = ?, drive_id = ?, original_id = COALESCE(?, original_id),
                   error = NULL, updated_at = ?
             WHERE id = ?
            """,
            (STATUS_UPLOADED, drive_id, original_id, now_iso(), item_id),
        )


def mark_failed(item_id: int, error: str) -> None:
    """Park an item as failed with the reason attached.

    The attempt count goes up so a file that fails every night is visible as
    such in the dashboard rather than looking like a fresh problem.
    """
    with _conn() as conn:
        conn.execute(
            """
            UPDATE items
               SET status = ?, error = ?, attempts = attempts + 1, updated_at = ?
             WHERE id = ?
            """,
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


# --- galleries ------------------------------------------------------------


def set_gallery(event: str, year: int, folder_id: str, link: str) -> None:
    """Remember the shared folder for one event, and the link to it."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO galleries (event, year, folder_id, link, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(event, year) DO UPDATE SET
                folder_id = excluded.folder_id,
                link      = excluded.link
            """,
            (event, year, folder_id, link, now_iso()),
        )


def get_gallery(event: str, year: int) -> sqlite3.Row | None:
    """The gallery row for one event, or None if it has not been shared."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM galleries WHERE event = ? AND year = ?", (event, year)
        ).fetchone()


def list_galleries() -> list[sqlite3.Row]:
    """Every shared gallery, newest year first."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM galleries ORDER BY year DESC, event"
        ).fetchall()


# --- queries the dashboard and the runner ask for ------------------------


def seen_source(src_id: str) -> bool:
    """True if this Drive file has already been ingested.

    Checked alongside the content hash: the hash catches the same card
    copied twice under different names, this catches the same file seen
    again on the next poll before anyone has moved it.
    """
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM items WHERE src_id = ?", (src_id,)).fetchone()
    return row is not None


def review_queue(limit: int = 300) -> list[sqlite3.Row]:
    """What the manager actually looks at: held items, oldest first.

    Oldest first on purpose. The review queue is a to-do list, and the file
    that has been waiting longest is the one most likely to be forgotten.
    """
    with _conn() as conn:
        return conn.execute(
            """
            SELECT * FROM items
             WHERE status = ? OR needs_review = 1
             ORDER BY id ASC LIMIT ?
            """,
            (STATUS_REVIEW, limit),
        ).fetchall()


def failed_items(limit: int = 300) -> list[sqlite3.Row]:
    """Items that errored, so the manager can retry them in bulk."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM items WHERE status = ? ORDER BY id DESC LIMIT ?",
            (STATUS_FAILED, limit),
        ).fetchall()


def requeue(ids: list[int]) -> int:
    """Send failed items back to the worker. Returns rows changed."""
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    with _conn() as conn:
        cur = conn.execute(
            f"""
            UPDATE items
               SET status = ?, error = NULL, updated_at = ?
             WHERE id IN ({placeholders})
            """,
            [STATUS_APPROVED, now_iso(), *ids],
        )
        return cur.rowcount


def release_stuck_uploads() -> int:
    """Return half-processed items to the queue after a crash.

    A killed worker leaves rows marked 'uploading' that nothing will ever
    pick up again. Every step it performs is safe to repeat, so the honest
    recovery is to put them back rather than have a human hunt for them.
    """
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE status = ?",
            (STATUS_APPROVED, now_iso(), STATUS_UPLOADING),
        )
        return cur.rowcount


def event_summary() -> list[sqlite3.Row]:
    """Per-event counts for the dashboard's front page."""
    with _conn() as conn:
        return conn.execute(
            """
            SELECT event,
                   year,
                   COUNT(*)                                   AS total,
                   SUM(status = 'uploaded')                   AS done,
                   SUM(status = 'failed')                     AS failed,
                   SUM(status = 'review' OR needs_review = 1) AS held
              FROM items
             GROUP BY event, year
             ORDER BY year DESC, event
            """
        ).fetchall()
