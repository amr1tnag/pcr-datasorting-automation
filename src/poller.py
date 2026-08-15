"""Finding new files in the drop folder and deciding what they are.

The poller reads. It creates a database row for every file a volunteer has
uploaded, works out where that file belongs, and decides whether it can be
published without a human looking at it. It never moves, uploads or changes
anything in Drive — that is the worker's job, and keeping the split means a
crash in the poller can only ever cost us a re-scan.

The decision it exists to make is the one from the brief: four hundred files
come in, eight are doubtful, and only those eight should reach the manager.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src import db, drive as drive_module, media
from src.drive import Drive, DriveError, DriveFile

log = logging.getLogger(__name__)

# Only files below this are pulled down for an EXIF second opinion. A RAW
# that Drive could not parse is usually 40MB and rarely tells us more than
# Drive already did; a JPEG is small and often does.
EXIF_RESCUE_MAX_BYTES: int = 40 * 1024 * 1024


@dataclass
class ScanResult:
    """What one pass over the drop folder did. Printed by the runner."""

    seen: int = 0
    added: int = 0
    duplicates: int = 0
    ignored: int = 0
    held: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def published(self) -> int:
        """Files that will go out with nobody looking at them."""
        return self.added - self.held

    def summary(self) -> str:
        return (
            f"{self.seen} files: {self.published} auto, {self.held} held, "
            f"{self.duplicates} duplicate, {self.ignored} ignored"
        )


def scan(drive: Drive, cfg: dict[str, Any], staging: Path | None = None) -> ScanResult:
    """Look at the drop folder once and record everything new in it.

    Safe to run as often as you like: a file already ingested is recognised
    by its Drive id or its checksum and skipped, so a volunteer who uploads
    the same card twice does not get two copies in the archive.
    """
    result = ScanResult()
    drop_id = str(cfg.get("drop_folder_id", "")).strip()
    if not drop_id:
        result.errors.append(
            "drop_folder_id is not set in settings.yaml; nothing to scan"
        )
        return result

    try:
        found = drive_module.walk_files(drive, drop_id)
    except DriveError as exc:
        result.errors.append(f"could not read the drop folder: {exc}")
        return result

    for folders, entry in found:
        result.seen += 1
        try:
            _ingest(drive, cfg, folders, entry, result, staging)
        except DriveError as exc:
            # One unreadable file must not stop the other 399.
            result.errors.append(f"{entry.name}: {exc}")
            log.exception("Failed to ingest %s", entry.name)

    return result


def _ingest(
    drive: Drive,
    cfg: dict[str, Any],
    folders: tuple[str, ...],
    entry: DriveFile,
    result: ScanResult,
    staging: Path | None,
) -> None:
    """Record one file, or explain why it was skipped."""
    if media.is_ignorable(entry.name):
        result.ignored += 1
        return

    if db.seen_source(entry.id):
        result.duplicates += 1
        return

    facts = media.facts_from_drive(entry.name, entry.as_metadata())

    if facts.kind == media.KIND_OTHER:
        # Not a photo, RAW or video. Left alone rather than filed somewhere
        # arbitrary; the manager sees it in the queue and decides.
        result.ignored += 1
        return

    if not facts.confident:
        facts = _second_opinion(drive, entry, facts, staging)

    event = _event_from(folders)
    year = facts.captured_at.year if facts.captured_at else _fallback_year(entry)
    camera = db.resolve_alias(facts.camera) if facts.camera else None

    reasons = list(facts.reasons)
    if event == media.UNSORTED_EVENT:
        reasons.append(media.REVIEW_NO_EVENT)

    held = bool(reasons)
    status = db.STATUS_REVIEW if held else db.STATUS_APPROVED

    item_id = db.insert_item(
        hash=entry.md5 or f"nomd5:{entry.id}",
        orig_name=entry.name,
        staged_path="",
        kind=facts.kind,
        event=event,
        camera=camera,
        orientation=facts.orientation,
        status=status,
        needs_review=int(held),
        src_id=entry.id,
        year=year,
        reasons="; ".join(reasons),
    )

    if item_id is None:
        # Same bytes already ingested under another name: the classic
        # "someone copied the card twice" case.
        result.duplicates += 1
        return

    db.add_event(event)
    result.added += 1
    if held:
        result.held += 1
        log.info("Holding %s: %s", entry.name, "; ".join(reasons))


def _second_opinion(
    drive: Drive, entry: DriveFile, facts: media.Facts, staging: Path | None
) -> media.Facts:
    """Read the file's own EXIF when Drive's metadata came up short.

    Drive parses most formats, but not all of them, and a photo it could not
    read would otherwise land in the review queue for no better reason than
    the file format. Downloading it to look is cheaper than a human's
    attention.
    """
    if staging is None or facts.kind != media.KIND_PHOTO:
        return facts
    if entry.size and entry.size > EXIF_RESCUE_MAX_BYTES:
        return facts

    local = Path(staging) / f"peek-{entry.id}-{entry.name}"
    try:
        drive.download(entry.id, local)
        rescued = media.merge_facts(facts, media.facts_from_exif(local))
    except (DriveError, OSError) as exc:
        log.warning("Could not read %s for a second opinion: %s", entry.name, exc)
        return facts
    finally:
        local.unlink(missing_ok=True)

    if rescued.confident and not facts.confident:
        log.info("EXIF rescued %s from the review queue", entry.name)
    return rescued


def _event_from(folders: tuple[str, ...]) -> str:
    """The event name a file's folder path implies.

    The first folder under the drop root is the event, which is the whole of
    what a volunteer has to get right. Anything deeper is the card's own
    structure — DCIM, 100CANON — and is ignored. A file sitting loose at the
    top has no event, and is held.
    """
    if not folders:
        return media.UNSORTED_EVENT
    return media.normalise_event(folders[0])


def _fallback_year(entry: DriveFile) -> int:
    """Year to file under when the photo does not say when it was taken.

    Falls back to when Drive first saw the file, which for a card copied the
    same night is the right answer anyway.
    """
    created = media.parse_capture_time(entry.created_time)
    if created:
        return created.year

    from datetime import datetime, timezone

    return datetime.now(timezone.utc).year
