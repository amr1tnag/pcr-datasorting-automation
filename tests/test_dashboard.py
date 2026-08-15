"""Tests for the manager's screen.

The behaviour worth protecting is the shape of the job: after a big event
the queue holds only the doubtful files, and fixing them is one action for
the whole selection rather than one click per file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from src import db, media
from src.dashboard import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "photocircle.db")
    db.init_db()
    app = create_app({})
    app.config.update(TESTING=True)
    return app.test_client()


def _add(name: str, **overrides: Any) -> int:
    """An item in the database, held for review unless told otherwise."""
    fields: dict[str, Any] = {
        "hash": name,
        "orig_name": name,
        "staged_path": "",
        "kind": media.KIND_PHOTO,
        "event": "Horizon",
        "camera": "CanonEOSR6",
        "orientation": media.ORIENT_LANDSCAPE,
        "status": db.STATUS_REVIEW,
        "needs_review": 1,
        "src_id": f"src-{name}",
        "year": 2026,
        "reasons": media.REVIEW_NO_CAMERA,
    }
    fields.update(overrides)
    item_id = db.insert_item(**fields)
    assert item_id is not None
    return item_id


# --- the pages load -------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/review", "/failed", "/cameras"])
def test_every_page_loads_on_an_empty_database(client: FlaskClient, path: str) -> None:
    """A fresh install must not greet the manager with a stack trace."""
    assert client.get(path).status_code == 200


def test_an_empty_queue_says_so_plainly(client: FlaskClient) -> None:
    body = client.get("/review").get_data(as_text=True)
    assert "Nothing needs a decision" in body


# --- the review queue -----------------------------------------------------


def test_the_queue_shows_only_the_held_items(client: FlaskClient) -> None:
    """The whole design: 400 in, a handful on screen."""
    for index in range(20):
        _add(f"done-{index}.jpg", status=db.STATUS_UPLOADED, needs_review=0, reasons="")
    _add("mystery.CR2")

    body = client.get("/review").get_data(as_text=True)

    assert "mystery.CR2" in body
    assert "done-0.jpg" not in body


def test_the_reason_is_shown_next_to_the_file(client: FlaskClient) -> None:
    _add("mystery.CR2", reasons=media.REVIEW_NO_CAMERA)
    body = client.get("/review").get_data(as_text=True)
    assert media.REVIEW_NO_CAMERA in body


def test_bulk_approve_clears_the_queue_in_one_action(client: FlaskClient) -> None:
    ids = [_add(f"orphan-{index}.jpg", event=media.UNSORTED_EVENT) for index in range(8)]

    response = client.post(
        "/review/apply",
        data={"ids": [str(i) for i in ids], "event": "navratri", "action": "approve"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert db.review_queue() == []
    for item_id in ids:
        item = db.get_item(item_id)
        assert item["status"] == db.STATUS_APPROVED
        assert item["event"] == "Navratri", "the typed event name is normalised"


def test_saving_without_approving_leaves_the_item_held(client: FlaskClient) -> None:
    """Half-finished triage should not publish anything."""
    item_id = _add("mystery.CR2")

    client.post(
        "/review/apply",
        data={"ids": [str(item_id)], "camera": "NikonZ6", "action": "save"},
        follow_redirects=True,
    )

    item = db.get_item(item_id)
    assert item["camera"] == "NikonZ6"
    assert item["status"] == db.STATUS_REVIEW


def test_approving_nothing_selected_is_harmless(client: FlaskClient) -> None:
    _add("mystery.CR2")
    response = client.post(
        "/review/apply", data={"action": "approve"}, follow_redirects=True
    )
    assert "Nothing was selected" in response.get_data(as_text=True)
    assert len(db.review_queue()) == 1


def test_a_malformed_id_does_not_crash_the_page(client: FlaskClient) -> None:
    _add("mystery.CR2")
    response = client.post(
        "/review/apply",
        data={"ids": ["not-a-number"], "action": "approve"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert len(db.review_queue()) == 1


def test_the_manager_cannot_edit_fields_that_are_not_theirs(client: FlaskClient) -> None:
    """A stray form field must not reach the database."""
    item_id = _add("mystery.CR2")

    client.post(
        "/review/apply",
        data={"ids": [str(item_id)], "drive_id": "tampered", "action": "save"},
        follow_redirects=True,
    )

    assert db.get_item(item_id)["drive_id"] is None


# --- failures -------------------------------------------------------------


def test_failed_items_are_listed_with_their_error(client: FlaskClient) -> None:
    item_id = _add("broken.jpg", status=db.STATUS_APPROVED, needs_review=0)
    db.mark_failed(item_id, "Drive said no")

    body = client.get("/failed").get_data(as_text=True)
    assert "broken.jpg" in body
    assert "Drive said no" in body


def test_retry_puts_an_item_back_in_the_queue(client: FlaskClient) -> None:
    item_id = _add("broken.jpg", status=db.STATUS_APPROVED, needs_review=0)
    db.mark_failed(item_id, "campus wifi")

    client.post("/failed/retry", data={"ids": [str(item_id)]}, follow_redirects=True)

    item = db.get_item(item_id)
    assert item["status"] == db.STATUS_APPROVED
    assert item["error"] is None


# --- camera aliases -------------------------------------------------------


def test_adding_an_alias_teaches_the_tool(client: FlaskClient) -> None:
    client.post(
        "/cameras/add",
        data={"raw": "EOS R6m2", "clean": "CanonR6II"},
        follow_redirects=True,
    )
    assert db.resolve_alias("EOS R6m2") == "CanonR6II"


def test_an_incomplete_alias_is_rejected(client: FlaskClient) -> None:
    response = client.post(
        "/cameras/add", data={"raw": "EOS R6m2", "clean": ""}, follow_redirects=True
    )
    assert "Both names are needed" in response.get_data(as_text=True)
    assert db.list_aliases() == []


# --- the overview ---------------------------------------------------------


def test_the_overview_shows_the_gallery_link(client: FlaskClient) -> None:
    db.set_gallery("Horizon", 2026, "folder123", "https://drive.example/horizon")
    body = client.get("/").get_data(as_text=True)
    assert "https://drive.example/horizon" in body


def test_the_overview_counts_what_came_in(client: FlaskClient) -> None:
    _add("done.jpg", status=db.STATUS_UPLOADED, needs_review=0, reasons="")
    _add("held.CR2")

    body = client.get("/").get_data(as_text=True)
    assert "Horizon" in body
    assert "waiting on you" in body
