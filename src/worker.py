"""Doing the work: watermarking, filing, and sharing the gallery.

One item at a time, claimed from the database so two workers cannot take the
same file. Every step is ordered so that a crash or a dropped connection
costs a retry and never a photo:

1. The original stays where the volunteer put it until the archive copy is
   verified. Nothing here deletes anything, ever.
2. A watermarked copy is uploaded, then its checksum is compared against the
   bytes on disk. Only a verified upload counts as done.
3. The original is filed last, by moving it within the same Drive — no
   bytes, no second copy, and the file exists throughout.

Everything the worker does is safe to repeat, which is what lets a killed
process be recovered by putting its half-finished rows back in the queue.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src import db, media, watermark
from src.drive import Drive, DriveError
from src.watermark import WatermarkError, WatermarkStyle

log = logging.getLogger(__name__)


@dataclass
class WorkResult:
    """What one pass through the queue achieved."""

    done: int = 0
    failed: int = 0
    links: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.done} filed"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        return ", ".join(parts)


class Worker:
    """Holds the things worth setting up once: the Drive, the logo, the paths."""

    def __init__(
        self,
        drive: Drive,
        cfg: dict[str, Any],
        project_root: Path,
        staging: Path,
    ) -> None:
        self.drive = drive
        self.cfg = cfg
        self.staging = Path(staging)
        self.staging.mkdir(parents=True, exist_ok=True)

        self.archive_id = str(cfg.get("archive_folder_id", "")).strip()
        self.style = WatermarkStyle.from_config(cfg, project_root)
        self._mark: Any | None = None

    @property
    def mark(self) -> Any:
        """The club logo, opened once and reused for every photo."""
        if self._mark is None:
            self._mark = watermark.load_mark(self.style)
        return self._mark

    # --- the queue --------------------------------------------------------

    def run(self, limit: int = 10_000) -> WorkResult:
        """Work the queue until it is empty.

        Recovers anything a previous run left mid-flight first: those rows
        are claimed but unfinished, and every step is repeatable.
        """
        result = WorkResult()
        if not self.archive_id:
            result.errors.append(
                "archive_folder_id is not set in settings.yaml; nothing can be filed"
            )
            return result

        recovered = db.release_stuck_uploads()
        if recovered:
            log.info("Recovered %d item(s) left mid-flight by a previous run", recovered)

        for _ in range(limit):
            row = db.claim_next_approved()
            if row is None:
                break
            self._handle(row, result)

        self._share_galleries(result)
        return result

    def _handle(self, row: sqlite3.Row, result: WorkResult) -> None:
        """Process one claimed item, recording success or failure."""
        name = str(row["orig_name"])
        try:
            drive_id, original_id = self.process(row)
            db.mark_uploaded(int(row["id"]), drive_id, original_id)
            result.done += 1
            log.info("Filed %s", name)
        except (DriveError, WatermarkError, OSError) as exc:
            # The original is still in the drop folder: nothing has been
            # removed at any point, so a failure is only ever a delay.
            db.mark_failed(int(row["id"]), str(exc))
            result.failed += 1
            result.errors.append(f"{name}: {exc}")
            log.exception("Failed to file %s", name)

    # --- one item ---------------------------------------------------------

    def process(self, row: sqlite3.Row) -> tuple[str, str | None]:
        """File one item. Returns (id a viewer sees, id of the filed original)."""
        kind = str(row["kind"])
        if kind == media.KIND_PHOTO:
            return self._process_photo(row)
        return self._process_untouched(row)

    def _process_untouched(self, row: sqlite3.Row) -> tuple[str, str | None]:
        """RAW and video: filed as they are, never opened, never marked."""
        folder_id = self.drive.ensure_path(self.archive_id, self._destination(row))
        filed_id = self.drive.file_into(str(row["src_id"]), folder_id)
        return filed_id, filed_id

    def _process_photo(self, row: sqlite3.Row) -> tuple[str, str | None]:
        """A photo: watermarked copy into the gallery, original filed aside.

        The order matters. The marked copy is uploaded and verified before
        the original is touched, so at no point does the archive hold
        neither one nor the other.
        """
        src_id = str(row["src_id"])
        name = str(row["orig_name"])
        local = self.staging / f"{row['id']}-{name}"
        marked = self.staging / f"{row['id']}-marked-{watermark.output_name(name)}"

        try:
            self.drive.download(src_id, local)
            watermark.apply(local, marked, self.mark, self.style)

            gallery_folder = self.drive.ensure_path(
                self.archive_id, self._destination(row)
            )
            uploaded = self.drive.upload(
                marked, gallery_folder, name=watermark.output_name(name)
            )
            self._verify(marked, uploaded.md5, name)

            # Only now is the source safe to move.
            originals_folder = self.drive.ensure_path(
                self.archive_id, self._destination(row, original=True)
            )
            original_id = self.drive.file_into(src_id, originals_folder)

            self._remember_gallery(row, gallery_folder)
            return uploaded.id, original_id
        finally:
            # Local copies are working files; the archive has both versions.
            local.unlink(missing_ok=True)
            marked.unlink(missing_ok=True)

    def _verify(self, path: Path, remote_md5: str | None, name: str) -> None:
        """Refuse to call an upload done unless the bytes match.

        Drive returns a checksum for anything it stores. If it does not
        match what we sent, the upload is not finished, whatever the API
        said — and the original has not been moved yet, so raising here is
        entirely safe.
        """
        if not remote_md5:
            log.warning("Drive returned no checksum for %s; accepting the upload", name)
            return

        digest = hashlib.md5()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)

        if digest.hexdigest() != remote_md5:
            raise DriveError(
                f"upload of {name} did not match: sent {digest.hexdigest()}, "
                f"Drive stored {remote_md5}"
            )

    def _destination(self, row: sqlite3.Row, *, original: bool = False) -> tuple[str, ...]:
        """Where this item belongs, as folder names under the archive root."""
        return media.destination(
            event=str(row["event"]),
            year=int(row["year"] or 0),
            kind=str(row["kind"]),
            camera=row["camera"],
            orientation=row["orientation"],
            original=original,
        )

    # --- gallery links ----------------------------------------------------

    def _remember_gallery(self, row: sqlite3.Row, gallery_folder: str) -> None:
        """Note the event's gallery folder, so it can be shared once at the end.

        The folder recorded is the event's ``photos`` folder, not the camera
        or orientation folder inside it — one link per event is what gets
        pasted into WhatsApp.
        """
        event, year = str(row["event"]), int(row["year"] or 0)
        if db.get_gallery(event, year) is not None:
            return

        photos_folder = self.drive.ensure_path(
            self.archive_id, (str(year), event, media.GALLERY_FOLDER)
        )
        db.set_gallery(event, year, photos_folder, "")

    def _share_galleries(self, result: WorkResult) -> None:
        """Make each new event's gallery readable by link.

        Done once per event at the end of a run rather than per photo: the
        link is the point of the whole tool, and it should appear the moment
        the event's photos are in place.
        """
        for gallery in db.list_galleries():
            if gallery["link"]:
                continue
            event, year = str(gallery["event"]), int(gallery["year"])
            try:
                link = self.drive.share_anyone_reader(str(gallery["folder_id"]))
            except DriveError as exc:
                result.errors.append(f"could not share {event}: {exc}")
                log.warning("Could not share the %s gallery: %s", event, exc)
                continue

            db.set_gallery(event, year, str(gallery["folder_id"]), link)
            result.links[f"{event} {year}"] = link
            log.info("Gallery for %s %d: %s", event, year, link)
