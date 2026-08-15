"""End-to-end tests: a volunteer uploads, the archive ends up correct.

These run the real poller and the real worker against the fake Drive, so
they cover the decisions the tool makes on its own at 10pm with nobody
watching. Each of the failures named in the brief has a test here:

* a camera reporting no usable metadata
* the same card copied twice
* files dumped without an event name
* an upload that fails partway
* phone footage mixed in with camera footage
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from src import config, db, media, poller, worker
from src.drive import DriveError
from src.fakedrive import ROOT_ID, FakeDrive

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CANON = {"cameraMake": "Canon", "cameraModel": "Canon EOS R6"}
NIKON = {"cameraMake": "NIKON CORPORATION", "cameraModel": "NIKON Z 6"}
IPHONE = {"cameraMake": "Apple", "cameraModel": "iPhone 13 Pro"}
LANDSCAPE = {"width": 6000, "height": 4000}
PORTRAIT = {"width": 4000, "height": 6000}


class Rig:
    """A whole installation: a fake Drive, a database, a logo, a worker."""

    def __init__(self, tmp_path: Path) -> None:
        self.drive = FakeDrive(tmp_path / "drive")
        self.drop = self.drive.ensure_folder(ROOT_ID, "DROP")
        self.archive = self.drive.ensure_folder(ROOT_ID, "ARCHIVE")
        self.staging = tmp_path / "staging"
        self.staging.mkdir()

        self.cfg: dict[str, Any] = {
            "drop_folder_id": self.drop,
            "archive_folder_id": self.archive,
            "watermark_path": str(PROJECT_ROOT / "assets" / "watermark.png"),
            "watermark_shadow": False,
        }

    # --- acting like a volunteer -----------------------------------------

    def event_folder(self, name: str) -> str:
        return self.drive.ensure_folder(self.drop, name)

    def photo(
        self,
        parent: str,
        name: str = "IMG_0001.JPG",
        camera: dict[str, Any] | None = None,
        shape: dict[str, Any] | None = None,
        colour: str = "grey",
    ) -> Any:
        """A real JPEG, with the metadata Drive would have reported.

        Each one is made genuinely unique: two photos with identical bytes
        are a duplicate card as far as the tool is concerned, and rightly
        deduplicated, which would quietly hollow out these tests.
        """
        source = self.staging / f"src-{name}"
        image = Image.new("RGB", (600, 400), colour)
        # A block, not a pixel: JPEG quantisation would flatten anything
        # smaller and hand us two identical files again.
        digest = hashlib.md5(name.encode()).digest()
        ImageDraw.Draw(image).rectangle(
            (10, 10, 210, 210), fill=(digest[0], digest[1], digest[2])
        )
        image.save(source, "JPEG")
        metadata = {**(camera or CANON), **(shape or LANDSCAPE)}
        entry = self.drive.add_file(parent, name, source=source, image_metadata=metadata)
        source.unlink()
        return entry

    def raw(self, parent: str, name: str = "IMG_0001.CR2", camera: dict | None = None) -> Any:
        return self.drive.add_file(
            parent, name, data=b"raw bytes " + name.encode(),
            image_metadata={**(camera or CANON), **LANDSCAPE},
        )

    def video(self, parent: str, name: str = "C0001.MP4", camera: dict | None = None) -> Any:
        return self.drive.add_file(
            parent, name, data=b"video bytes " + name.encode(),
            image_metadata=dict(camera or CANON),
            video_metadata={"width": 1920, "height": 1080},
        )

    # --- running the tool -------------------------------------------------

    def scan(self) -> poller.ScanResult:
        return poller.scan(self.drive, self.cfg, staging=self.staging)

    def work(self) -> worker.WorkResult:
        return worker.Worker(self.drive, self.cfg, PROJECT_ROOT, self.staging).run()

    def run(self) -> tuple[poller.ScanResult, worker.WorkResult]:
        return self.scan(), self.work()

    # --- inspecting the archive ------------------------------------------

    def paths(self) -> set[str]:
        """Every file in the archive, as a path relative to its root."""
        root = self.drive.root / "ARCHIVE"
        return {
            str(p.relative_to(root)).replace("\\", "/")
            for p in root.rglob("*")
            if p.is_file()
        }

    def drop_paths(self) -> set[str]:
        root = self.drive.root / "DROP"
        return {
            str(p.relative_to(root)).replace("\\", "/")
            for p in root.rglob("*")
            if p.is_file()
        }


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    """A clean installation with its own database."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "photocircle.db")
    db.init_db()
    return Rig(tmp_path)


# --- the normal night -----------------------------------------------------


def test_a_whole_shoot_sorts_itself(rig: Rig) -> None:
    """The case the tool exists for: nobody touches anything."""
    horizon = rig.event_folder("Horizon")
    rig.photo(horizon, "IMG_0001.JPG", CANON, LANDSCAPE)
    rig.photo(horizon, "IMG_0002.JPG", CANON, PORTRAIT)
    rig.photo(horizon, "DSC_0001.JPG", NIKON, LANDSCAPE)
    rig.raw(horizon, "IMG_0001.CR2", CANON)
    rig.video(horizon, "C0001.MP4", CANON)

    scan, work = rig.run()

    assert scan.added == 5
    assert scan.held == 0, "a clean shoot must need no human at all"
    assert work.failed == 0
    assert work.done == 5

    year = str(next(iter(db.list_galleries()))["year"])
    assert rig.paths() == {
        f"{year}/Horizon/photos/CanonEOSR6/landscape/IMG_0001.jpg",
        f"{year}/Horizon/photos/CanonEOSR6/portrait/IMG_0002.jpg",
        f"{year}/Horizon/photos/NikonZ6/landscape/DSC_0001.jpg",
        f"{year}/Horizon/_originals/CanonEOSR6/landscape/IMG_0001.JPG",
        f"{year}/Horizon/_originals/CanonEOSR6/portrait/IMG_0002.JPG",
        f"{year}/Horizon/_originals/NikonZ6/landscape/DSC_0001.JPG",
        f"{year}/Horizon/raw/CanonEOSR6/IMG_0001.CR2",
        f"{year}/Horizon/video/CanonEOSR6/C0001.MP4",
    }


def test_a_gallery_link_comes_out_of_a_clean_run(rig: Rig) -> None:
    """The success condition from the brief: a link, with nobody deciding to make one."""
    rig.photo(rig.event_folder("Horizon"))
    _, work = rig.run()

    assert len(work.links) == 1
    event, link = next(iter(work.links.items()))
    assert event.startswith("Horizon")
    assert link


def test_the_link_points_at_photos_only(rig: Rig) -> None:
    """RAW, unmarked originals and held items must not be inside the share."""
    horizon = rig.event_folder("Horizon")
    rig.photo(horizon)
    rig.raw(horizon)
    rig.run()

    gallery = db.list_galleries()[0]
    shared = rig.drive.get_file(str(gallery["folder_id"]))
    assert shared.name == media.GALLERY_FOLDER

    inside = {f.name for f in rig.drive.list_children(shared.id)}
    assert media.RAW_FOLDER not in inside
    assert media.ORIGINALS_FOLDER not in inside


def test_raw_and_video_are_never_watermarked(rig: Rig) -> None:
    """They get edited later; a burned-in mark would ruin them."""
    horizon = rig.event_folder("Horizon")
    raw = rig.raw(horizon, "IMG_0009.CR2")
    video = rig.video(horizon, "C0009.MP4")
    before = {raw.md5, video.md5}

    rig.run()

    after = set()
    for path in rig.paths():
        if path.endswith((".CR2", ".MP4")):
            full = rig.drive.root / "ARCHIVE" / path
            import hashlib
            after.add(hashlib.md5(full.read_bytes()).hexdigest())
    assert after == before


def test_photos_come_out_watermarked(rig: Rig) -> None:
    entry = rig.photo(rig.event_folder("Horizon"), colour="black")
    rig.run()

    marked = next(p for p in rig.paths() if "/photos/" in p)
    original = next(p for p in rig.paths() if media.ORIGINALS_FOLDER in p)

    root = rig.drive.root / "ARCHIVE"
    with Image.open(root / marked) as image:
        grey = image.convert("L")
        # The logo is mostly transparent, so look at the brightest pixel in
        # the corner it occupies rather than one arbitrary point.
        corner = grey.crop((grey.width - 120, grey.height - 120, grey.width, grey.height))
    assert corner.getextrema()[1] > 100, "the white mark should show on a black frame"

    # The unmarked original is kept, byte for byte.
    import hashlib
    assert hashlib.md5((root / original).read_bytes()).hexdigest() == entry.md5


# --- what the automation gets wrong --------------------------------------


def test_a_camera_with_no_metadata_is_held_not_guessed(rig: Rig) -> None:
    rig.drive.add_file(rig.event_folder("Horizon"), "IMG_0001.CR2", data=b"mystery raw")

    scan, work = rig.run()

    assert scan.held == 1
    assert work.done == 0, "held items must not be published"
    assert rig.paths() == set(), "nothing should reach the archive yet"

    held = db.review_queue()[0]
    assert media.REVIEW_NO_CAMERA in str(held["reasons"])


def test_files_dumped_without_an_event_name_are_held(rig: Rig) -> None:
    rig.photo(rig.drop, "IMG_0001.JPG")  # loose at the top, no event folder

    scan, _ = rig.run()

    assert scan.held == 1
    held = db.review_queue()[0]
    assert held["event"] == media.UNSORTED_EVENT
    assert media.REVIEW_NO_EVENT in str(held["reasons"])


def test_the_same_card_copied_twice_is_ingested_once(rig: Rig) -> None:
    """Identical bytes under a second name: one row, one archive copy."""
    horizon = rig.event_folder("Horizon")
    source = rig.staging / "same.jpg"
    Image.new("RGB", (600, 400), "grey").save(source, "JPEG")
    rig.drive.add_file(horizon, "IMG_0001.JPG", source=source, image_metadata={**CANON, **LANDSCAPE})
    rig.drive.add_file(horizon, "COPY_0001.JPG", source=source, image_metadata={**CANON, **LANDSCAPE})

    scan, work = rig.run()

    assert scan.added == 1
    assert scan.duplicates == 1
    assert work.done == 1


def test_rescanning_does_not_re_ingest(rig: Rig) -> None:
    """The poller runs every minute; it must not pile up rows."""
    rig.photo(rig.event_folder("Horizon"))
    first, _ = rig.run()
    second = rig.scan()

    assert first.added == 1
    assert second.added == 0
    assert db.stats()["total"] == 1


def test_phone_footage_sorts_beside_camera_footage(rig: Rig) -> None:
    """Mixed in on the same card, and neither one confuses the other."""
    horizon = rig.event_folder("Horizon")
    rig.video(horizon, "C0001.MP4", CANON)
    rig.video(horizon, "IMG_4321.MOV", IPHONE)

    scan, work = rig.run()

    assert scan.held == 0, "a phone is not a problem, just a different body"
    assert work.done == 2
    assert any("video/Phone-iPhone13Pro/IMG_4321.MOV" in p for p in rig.paths())
    assert any("video/CanonEOSR6/C0001.MP4" in p for p in rig.paths())


def test_one_firmware_spelling_does_not_split_a_camera(rig: Rig) -> None:
    horizon = rig.event_folder("Horizon")
    rig.photo(horizon, "A.JPG", {"cameraMake": "Canon", "cameraModel": "Canon EOS R6"})
    rig.photo(horizon, "B.JPG", {"cameraMake": "Canon Inc.", "cameraModel": " Canon  EOS-R6 "})

    rig.run()

    cameras = {p.split("/")[3] for p in rig.paths() if "/photos/" in p}
    assert cameras == {"CanonEOSR6"}


def test_a_camera_alias_is_obeyed(rig: Rig) -> None:
    """What the manager teaches the tool once, it remembers."""
    db.upsert_alias("CanonEOSR6", "CanonR6-Body1")
    rig.photo(rig.event_folder("Horizon"))

    rig.run()

    assert any("photos/CanonR6-Body1/" in p for p in rig.paths())


def test_card_litter_is_ignored_silently(rig: Rig) -> None:
    horizon = rig.event_folder("Horizon")
    rig.drive.add_file(horizon, "Thumbs.db", data=b"junk")
    rig.drive.add_file(horizon, "IMG_0001.XMP", data=b"sidecar")
    rig.photo(horizon)

    scan, _ = rig.run()

    assert scan.added == 1
    assert scan.ignored == 2
    assert scan.held == 0, "litter must not reach the manager"


def test_a_whole_dcim_tree_still_finds_the_event(rig: Rig) -> None:
    """Volunteers drag the card in, not the folder inside it."""
    horizon = rig.event_folder("Horizon")
    nested = rig.drive.ensure_path(horizon, ("DCIM", "100CANON"))
    rig.photo(nested, "IMG_0001.JPG")

    scan, _ = rig.run()

    assert scan.held == 0
    assert db.get_item(1)["event"] == "Horizon"


def test_lowercase_event_folders_are_one_event(rig: Rig) -> None:
    rig.photo(rig.event_folder("horizon"), "A.JPG")
    rig.photo(rig.event_folder("HORIZON "), "B.JPG")

    rig.run()

    events = {p.split("/")[1] for p in rig.paths()}
    assert events == {"Horizon"}


# --- failures -------------------------------------------------------------


def test_an_upload_that_fails_leaves_the_original_untouched(rig: Rig) -> None:
    """The non-negotiable, at the step most likely to break."""
    entry = rig.photo(rig.event_folder("Horizon"))
    rig.drive.fail_next("upload", times=99)

    scan, work = rig.run()

    assert work.failed == 1
    assert rig.drop_paths() == {"Horizon/IMG_0001.JPG"}, "original must still be in the drop folder"
    assert rig.drive.get_file(entry.id).md5 == entry.md5

    item = db.get_item(1)
    assert item["status"] == db.STATUS_FAILED
    assert item["error"]


def test_a_failed_item_can_be_retried_and_succeeds(rig: Rig) -> None:
    """Campus wifi drops; the manager clicks retry and the night is saved."""
    rig.photo(rig.event_folder("Horizon"))
    rig.drive.fail_next("upload", times=1)

    rig.run()
    assert db.get_item(1)["status"] == db.STATUS_FAILED

    assert db.requeue([1]) == 1
    work = rig.work()

    assert work.done == 1
    assert db.get_item(1)["status"] == db.STATUS_UPLOADED
    assert any("/photos/" in p for p in rig.paths())


def test_a_failure_partway_does_not_half_file_a_photo(rig: Rig) -> None:
    """If the move fails after the upload, the original stays put."""
    rig.photo(rig.event_folder("Horizon"))
    rig.drive.fail_next("file_into", times=99)

    _, work = rig.run()

    assert work.failed == 1
    assert rig.drop_paths() == {"Horizon/IMG_0001.JPG"}


def test_a_crashed_run_is_recovered_next_time(rig: Rig) -> None:
    """Rows left mid-flight go back in the queue rather than being stranded."""
    rig.photo(rig.event_folder("Horizon"))
    rig.scan()

    claimed = db.claim_next_approved()          # pretend the process died here
    assert claimed is not None
    assert db.get_item(1)["status"] == db.STATUS_UPLOADING

    work = rig.work()

    assert work.done == 1
    assert db.get_item(1)["status"] == db.STATUS_UPLOADED


def test_a_missing_archive_folder_is_reported_not_crashed(rig: Rig) -> None:
    rig.cfg["archive_folder_id"] = ""
    rig.photo(rig.event_folder("Horizon"))

    _, work = rig.run()

    assert work.done == 0
    assert any("archive_folder_id" in problem for problem in work.errors)


def test_a_missing_drop_folder_is_reported_not_crashed(rig: Rig) -> None:
    rig.cfg["drop_folder_id"] = ""
    scan = rig.scan()
    assert any("drop_folder_id" in problem for problem in scan.errors)


def test_one_unreadable_file_does_not_stop_the_others(rig: Rig) -> None:
    horizon = rig.event_folder("Horizon")
    rig.photo(horizon, "A.JPG")
    rig.photo(horizon, "B.JPG")
    rig.photo(horizon, "C.JPG")
    rig.drive.fail_next("upload", times=1)   # B fails, A and C must not

    _, work = rig.run()

    assert work.done == 2
    assert work.failed == 1


def test_a_broken_watermark_holds_the_photo_rather_than_publishing_it_bare(
    rig: Rig, tmp_path: Path
) -> None:
    """An unmarked gallery is harder to undo than a delayed one."""
    rig.cfg["watermark_path"] = str(tmp_path / "no-such-logo.png")
    rig.photo(rig.event_folder("Horizon"))

    _, work = rig.run()

    assert work.failed == 1
    assert rig.paths() == set()
    assert rig.drop_paths() == {"Horizon/IMG_0001.JPG"}


# --- the manager's workflow ----------------------------------------------


def test_the_review_queue_holds_only_the_doubtful_ones(rig: Rig) -> None:
    """400 files in, 8 on screen: the shape the brief asked for."""
    horizon = rig.event_folder("Horizon")
    for index in range(12):
        rig.photo(horizon, f"IMG_{index:04d}.JPG")
    for index in range(3):
        rig.drive.add_file(horizon, f"MYSTERY_{index}.CR2", data=f"raw {index}".encode())

    scan, work = rig.run()

    assert scan.added == 15
    assert work.done == 12, "the good ones publish themselves"
    assert len(db.review_queue()) == 3, "only the doubtful ones wait for a human"


def test_approving_a_held_item_files_it(rig: Rig) -> None:
    """The manager fills in the camera and the tool takes it from there."""
    rig.drive.add_file(rig.event_folder("Horizon"), "IMG_0001.CR2", data=b"mystery raw")
    rig.run()

    held = db.review_queue()[0]
    db.update_items([int(held["id"])], {"camera": "CanonEOSR6"}, approve=True)
    work = rig.work()

    assert work.done == 1
    assert any("raw/CanonEOSR6/IMG_0001.CR2" in p for p in rig.paths())
    assert db.review_queue() == []


def test_bulk_assigning_an_event_clears_the_queue(rig: Rig) -> None:
    """Eight orphaned files, one action."""
    for index in range(8):
        rig.photo(rig.drop, f"IMG_{index:04d}.JPG")
    rig.run()

    ids = [int(row["id"]) for row in db.review_queue()]
    assert len(ids) == 8

    assert db.update_items(ids, {"event": "Navratri"}, approve=True) == 8
    work = rig.work()

    assert work.done == 8
    assert db.review_queue() == []
    assert all("/Navratri/" in p for p in rig.paths())


def test_event_summary_reports_what_came_in(rig: Rig) -> None:
    horizon = rig.event_folder("Horizon")
    rig.photo(horizon, "A.JPG")
    rig.drive.add_file(horizon, "MYSTERY.CR2", data=b"mystery")
    rig.run()

    summary = db.event_summary()
    horizon_row = next(row for row in summary if row["event"] == "Horizon")
    assert horizon_row["total"] == 2
    assert horizon_row["done"] == 1
    assert horizon_row["held"] == 1
