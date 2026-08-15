"""The manager's screen: what came in, what needs a decision, what broke.

Deliberately small. The tool's job is to need no human, so this exists for
the cases where it could not decide — and the measure of it working is that
after a 400-file event the manager opens it, sees eight rows, fixes them in
one action and closes it again.

Everything is a plain server-rendered form. No build step, no JavaScript
framework, nothing to install: next year's maintainer can read the whole
thing and change a column without learning anything first.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask, flash, redirect, render_template, request, url_for

from src import config, db, media

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).resolve().parent / "templates"

# Fields the manager may set on held items. Deliberately the four that
# decide where a file goes, and nothing else — this is not a photo editor.
EDITABLE = ("event", "camera", "orientation")


def create_app(cfg: dict[str, Any] | None = None) -> Flask:
    """Build the dashboard. Passing cfg is what the tests use."""
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config["SETTINGS"] = cfg if cfg is not None else config.load_config()
    # Only used to show one-off confirmation messages.
    app.secret_key = "photo-circle-dashboard"

    @app.route("/")
    def home() -> str:
        return render_template(
            "index.html",
            counts=db.stats(),
            events=db.event_summary(),
            galleries=db.list_galleries(),
        )

    @app.route("/review")
    def review() -> str:
        return render_template(
            "review.html",
            items=db.review_queue(),
            events=db.list_events(),
            cameras=_known_cameras(),
            orientations=(media.ORIENT_LANDSCAPE, media.ORIENT_PORTRAIT, media.ORIENT_SQUARE),
        )

    @app.post("/review/apply")
    def apply_review() -> Any:
        """Set fields on the selected items, and optionally approve them.

        One action for the whole selection: the point of the queue is that
        eight wrong files are fixed together, not one at a time.
        """
        ids = _selected_ids()
        if not ids:
            flash("Nothing was selected.")
            return redirect(url_for("review"))

        fields = {
            name: request.form[name].strip()
            for name in EDITABLE
            if request.form.get(name, "").strip()
        }
        if "event" in fields:
            fields["event"] = media.normalise_event(fields["event"])

        approve = request.form.get("action") == "approve"
        if not fields and not approve:
            flash("Nothing to change: fill in a field or choose Approve.")
            return redirect(url_for("review"))

        changed = db.update_items(ids, fields, approve=approve)
        for event in {fields["event"]} if "event" in fields else set():
            db.add_event(event)

        flash(
            f"{changed} item(s) updated"
            + (" and queued for filing." if approve else ".")
        )
        return redirect(url_for("review"))

    @app.route("/failed")
    def failed() -> str:
        return render_template("failed.html", items=db.failed_items())

    @app.post("/failed/retry")
    def retry() -> Any:
        """Put failed items back in the queue.

        Safe at any time: nothing was deleted when they failed, so a retry
        picks up exactly where it stopped.
        """
        ids = _selected_ids()
        if not ids:
            flash("Nothing was selected.")
            return redirect(url_for("failed"))

        flash(f"{db.requeue(ids)} item(s) queued to try again.")
        return redirect(url_for("failed"))

    @app.route("/cameras")
    def cameras() -> str:
        return render_template(
            "cameras.html", aliases=db.list_aliases(), cameras=_known_cameras()
        )

    @app.post("/cameras/add")
    def add_alias() -> Any:
        """Teach the tool that two camera names are the same body."""
        raw = request.form.get("raw", "").strip()
        clean = request.form.get("clean", "").strip()
        if not raw or not clean:
            flash("Both names are needed.")
            return redirect(url_for("cameras"))

        db.upsert_alias(raw, clean)
        flash(
            f"'{raw}' will be filed as '{clean}' from now on. "
            "Files already in the archive are not moved."
        )
        return redirect(url_for("cameras"))

    return app


def _selected_ids() -> list[int]:
    """The checked rows, as integers, ignoring anything malformed."""
    ids: list[int] = []
    for value in request.form.getlist("ids"):
        try:
            ids.append(int(value))
        except ValueError:
            continue
    return ids


def _known_cameras() -> list[str]:
    """Camera names already in the archive, to offer as suggestions.

    Suggesting what already exists is most of what stops one body ending up
    under three names.
    """
    names = {
        str(row["camera"])
        for row in db.list_items(db.STATUS_UPLOADED, limit=2000)
        if row["camera"]
    }
    names.update(str(row["clean"]) for row in db.list_aliases() if row["clean"])
    return sorted(names)
