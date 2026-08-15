"""Tests for the SQLite layer. Each test gets its own database under tmp_path."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from src import db


@pytest.fixture()
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point db at a throwaway file and create the schema."""
    path = tmp_path / "photocircle.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path


def _insert(h: str, **overrides: Any) -> int | None:
    """Insert a plausible item, overriding whichever fields the test cares about."""
    fields: dict[str, Any] = {
        "hash": h,
        "orig_name": f"{h}.jpg",
        "staged_path": f"/staging/{h}.jpg",
        "kind": "photo",
        "event": "Sangeet",
        "camera": "CanonR6",
        "orientation": "landscape",
    }
    fields.update(overrides)
    return db.insert_item(**fields)


def test_insert_item_returns_id(database: Path) -> None:
    item_id = _insert("aaa")
    assert isinstance(item_id, int)
    assert item_id > 0


def test_insert_item_duplicate_hash_returns_none(database: Path) -> None:
    first = _insert("dup")
    second = _insert("dup", orig_name="different_name.jpg")

    assert first is not None
    assert second is None
    assert db.seen_hash("dup") is True
    assert db.seen_hash("never-ingested") is False
    assert len(db.list_items(db.STATUS_REVIEW)) == 1


def test_claim_next_approved_flips_status(database: Path) -> None:
    item_id = _insert("approved-one", status=db.STATUS_APPROVED)

    assert item_id is not None

    claimed = db.claim_next_approved()

    assert claimed is not None
    assert claimed["id"] == item_id
    assert claimed["status"] == db.STATUS_UPLOADING

    stored = db.get_item(item_id)
    assert stored is not None
    assert stored["status"] == db.STATUS_UPLOADING


def test_claim_next_approved_returns_none_when_nothing_approved(
    database: Path,
) -> None:
    _insert("in-review")
    _insert("already-up", status=db.STATUS_UPLOADED)

    assert db.claim_next_approved() is None


def test_two_claims_never_return_the_same_row(database: Path) -> None:
    _insert("first", status=db.STATUS_APPROVED)
    _insert("second", status=db.STATUS_APPROVED)

    one = db.claim_next_approved()
    two = db.claim_next_approved()

    assert one is not None and two is not None
    assert one["id"] != two["id"]
    assert db.claim_next_approved() is None


def test_mark_uploaded_sets_status_and_timestamp(database: Path) -> None:
    item_id = _insert("to-upload", status=db.STATUS_APPROVED)
    assert item_id is not None
    before = db.get_item(item_id)
    assert before is not None

    db.mark_uploaded(item_id, "drive-abc123")

    after = db.get_item(item_id)
    assert after is not None
    assert after["status"] == db.STATUS_UPLOADED
    assert after["drive_id"] == "drive-abc123"
    assert after["error"] is None
    assert after["updated_at"] >= before["updated_at"]


def test_mark_failed_sets_status_and_error(database: Path) -> None:
    item_id = _insert("to-fail", status=db.STATUS_UPLOADING)
    assert item_id is not None
    before = db.get_item(item_id)
    assert before is not None

    db.mark_failed(item_id, "quota exceeded")

    after = db.get_item(item_id)
    assert after is not None
    assert after["status"] == db.STATUS_FAILED
    assert after["error"] == "quota exceeded"
    assert after["updated_at"] >= before["updated_at"]


def test_update_items_with_approve_clears_needs_review(database: Path) -> None:
    first = _insert("flagged-1", needs_review=1)
    second = _insert("flagged-2", needs_review=1)
    assert first is not None and second is not None

    changed = db.update_items(
        [first, second], {"event": "Reception", "camera": "SonyA7"}, approve=True
    )

    assert changed == 2
    for item_id in (first, second):
        row = db.get_item(item_id)
        assert row is not None
        assert row["status"] == db.STATUS_APPROVED
        assert row["needs_review"] == 0
        assert row["event"] == "Reception"
        assert row["camera"] == "SonyA7"


def test_update_items_without_approve_leaves_status_alone(database: Path) -> None:
    item_id = _insert("edit-only", needs_review=1)
    assert item_id is not None

    changed = db.update_items([item_id], {"orientation": "portrait"}, approve=False)

    row = db.get_item(item_id)
    assert changed == 1
    assert row is not None
    assert row["status"] == db.STATUS_REVIEW
    assert row["needs_review"] == 1
    assert row["orientation"] == "portrait"


def test_update_items_rejects_unknown_field(database: Path) -> None:
    item_id = _insert("guarded")
    assert item_id is not None

    with pytest.raises(ValueError):
        db.update_items([item_id], {"drive_id": "nope"}, approve=False)


def test_resolve_alias_falls_back_to_raw(database: Path) -> None:
    assert db.resolve_alias("IMG_CANON_R6") == "IMG_CANON_R6"

    db.upsert_alias("IMG_CANON_R6", "CanonR6")
    assert db.resolve_alias("IMG_CANON_R6") == "CanonR6"

    db.upsert_alias("IMG_CANON_R6", "Canon R6 Mk II")
    assert db.resolve_alias("IMG_CANON_R6") == "Canon R6 Mk II"
    assert [dict(row) for row in db.list_aliases()] == [
        {"raw": "IMG_CANON_R6", "clean": "Canon R6 Mk II"}
    ]


def test_events_roundtrip(database: Path) -> None:
    db.add_event("Sangeet")
    db.add_event("Reception")
    db.add_event("Sangeet")

    assert db.list_events() == ["Reception", "Sangeet"]


def test_stats_counts_by_status(database: Path) -> None:
    _insert("s1")
    _insert("s2", needs_review=1)
    _insert("s3", status=db.STATUS_APPROVED)
    _insert("s4", status=db.STATUS_UPLOADED)

    summary = db.stats()

    assert summary["review"] == 2
    assert summary["approved"] == 1
    assert summary["uploaded"] == 1
    assert summary["failed"] == 0
    assert summary["total"] == 4
    assert summary["needs_review"] == 1


def test_init_db_is_idempotent(database: Path) -> None:
    _insert("survivor")
    db.init_db()

    assert db.seen_hash("survivor") is True


def test_wal_mode_is_enabled(database: Path) -> None:
    conn: sqlite3.Connection = db.get_conn()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()

    assert mode.lower() == "wal"
    assert timeout == db.BUSY_TIMEOUT_MS
