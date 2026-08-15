"""Tests for the Drive layer.

Written against the protocol rather than the implementation, so the same
suite can be pointed at the real client later by changing the fixture. The
important assertions here are the negative ones: filing a file must not
duplicate it, and nothing in this layer may remove an original.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import drive as drive_module
from src.drive import DriveError, walk_files
from src.fakedrive import ROOT_ID, FakeDrive


@pytest.fixture()
def drive(tmp_path: Path) -> FakeDrive:
    return FakeDrive(tmp_path / "drive")


# --- folders --------------------------------------------------------------


def test_ensure_folder_is_idempotent(drive: FakeDrive) -> None:
    first = drive.ensure_folder(ROOT_ID, "2026")
    second = drive.ensure_folder(ROOT_ID, "2026")
    assert first == second


def test_ensure_path_builds_the_whole_tree(drive: FakeDrive) -> None:
    folder_id = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "photos", "CanonEOSR6"))
    assert (drive.root / "2026" / "Horizon" / "photos" / "CanonEOSR6").is_dir()
    assert drive.get_file(folder_id).name == "CanonEOSR6"


def test_ensure_path_reuses_existing_folders(drive: FakeDrive) -> None:
    first = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "photos"))
    second = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "photos"))
    assert first == second
    assert len(list((drive.root / "2026").iterdir())) == 1


# --- listing --------------------------------------------------------------


def test_list_children_separates_files_from_folders(drive: FakeDrive) -> None:
    inbox = drive.ensure_folder(ROOT_ID, "DROP")
    drive.ensure_folder(inbox, "Horizon")
    drive.add_file(inbox, "loose.jpg")

    children = drive.list_children(inbox)
    assert {child.name for child in children} == {"Horizon", "loose.jpg"}
    assert [child.name for child in children if child.is_folder] == ["Horizon"]


def test_walk_finds_files_with_their_event_folder(drive: FakeDrive) -> None:
    inbox = drive.ensure_folder(ROOT_ID, "DROP")
    horizon = drive.ensure_folder(inbox, "Horizon")
    drive.add_file(horizon, "IMG_0001.JPG")
    drive.add_file(inbox, "orphan.jpg")

    found = {file.name: path for path, file in walk_files(drive, inbox)}
    assert found["IMG_0001.JPG"] == ("Horizon",)
    # A card dumped without an event name has no folder above it.
    assert found["orphan.jpg"] == ()


def test_walk_descends_into_a_whole_card_tree(drive: FakeDrive) -> None:
    """Volunteers sometimes drag in DCIM rather than the photos inside it."""
    inbox = drive.ensure_folder(ROOT_ID, "DROP")
    nested = drive.ensure_path(inbox, ("Horizon", "DCIM", "100CANON"))
    drive.add_file(nested, "IMG_0001.JPG")

    (path, file), = walk_files(drive, inbox)
    assert file.name == "IMG_0001.JPG"
    assert path == ("Horizon", "DCIM", "100CANON")


def test_walk_of_an_empty_inbox_is_empty(drive: FakeDrive) -> None:
    inbox = drive.ensure_folder(ROOT_ID, "DROP")
    assert walk_files(drive, inbox) == []


# --- filing ---------------------------------------------------------------


def test_filing_moves_rather_than_copies(drive: FakeDrive) -> None:
    """The whole storage argument for one Drive rests on this."""
    inbox = drive.ensure_folder(ROOT_ID, "DROP")
    entry = drive.add_file(inbox, "IMG_0001.CR2", data=b"raw bytes")
    destination = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "raw", "CanonEOSR6"))

    new_id = drive.file_into(entry.id, destination)

    assert drive.list_children(inbox) == []
    filed = drive.get_file(new_id)
    assert filed.name == "IMG_0001.CR2"
    assert (drive.root / "2026" / "Horizon" / "raw" / "CanonEOSR6" / "IMG_0001.CR2").exists()


def test_filing_preserves_the_bytes(drive: FakeDrive) -> None:
    inbox = drive.ensure_folder(ROOT_ID, "DROP")
    entry = drive.add_file(inbox, "IMG_0001.CR2", data=b"irreplaceable")
    destination = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "raw"))

    filed = drive.get_file(drive.file_into(entry.id, destination))
    assert filed.md5 == entry.md5
    assert filed.size == entry.size


def test_filing_twice_is_harmless(drive: FakeDrive) -> None:
    """A retry after a partial run must not error or duplicate."""
    inbox = drive.ensure_folder(ROOT_ID, "DROP")
    entry = drive.add_file(inbox, "IMG_0001.CR2")
    destination = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "raw"))

    first = drive.file_into(entry.id, destination)
    second = drive.file_into(first, destination)

    assert first == second
    assert len(drive.list_children(destination)) == 1


def test_two_cards_with_the_same_filename_both_survive(drive: FakeDrive) -> None:
    """Every camera writes IMG_0001. Neither file may overwrite the other."""
    inbox = drive.ensure_folder(ROOT_ID, "DROP")
    destination = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "raw"))

    first = drive.add_file(inbox, "IMG_0001.CR2", data=b"from the R6")
    drive.file_into(first.id, destination)
    second = drive.add_file(inbox, "IMG_0001.CR2", data=b"from the Z6")
    drive.file_into(second.id, destination)

    filed = drive.list_children(destination)
    assert len(filed) == 2
    assert {drive.download(f.id, drive.root.parent / f.name).read_bytes() for f in filed} == {
        b"from the R6",
        b"from the Z6",
    }


# --- bytes ----------------------------------------------------------------


def test_download_then_upload_roundtrip(tmp_path: Path, drive: FakeDrive) -> None:
    inbox = drive.ensure_folder(ROOT_ID, "DROP")
    entry = drive.add_file(inbox, "IMG_0001.JPG", data=b"pixels")

    local = drive.download(entry.id, tmp_path / "work" / "IMG_0001.JPG")
    assert local.read_bytes() == b"pixels"

    gallery = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "photos"))
    uploaded = drive.upload(local, gallery, name="IMG_0001.JPG")
    assert uploaded.md5 == entry.md5
    # The source is still in the inbox: uploading is not filing.
    assert [f.name for f in drive.list_children(inbox)] == ["IMG_0001.JPG"]


def test_download_creates_missing_directories(tmp_path: Path, drive: FakeDrive) -> None:
    entry = drive.add_file(ROOT_ID, "IMG_0001.JPG")
    target = tmp_path / "a" / "b" / "c" / "IMG_0001.JPG"
    assert drive.download(entry.id, target).exists()


# --- metadata -------------------------------------------------------------


def test_injected_metadata_reaches_the_sorting_rules(drive: FakeDrive) -> None:
    from src import media

    entry = drive.add_file(
        ROOT_ID,
        "IMG_0001.JPG",
        image_metadata={
            "cameraMake": "Canon",
            "cameraModel": "Canon EOS R6",
            "width": 6000,
            "height": 4000,
        },
    )
    facts = media.facts_from_drive(entry.name, entry.as_metadata())
    assert facts.camera == "CanonEOSR6"
    assert facts.orientation == media.ORIENT_LANDSCAPE
    assert facts.confident


def test_a_real_jpeg_is_measured_like_drive_would(tmp_path: Path, drive: FakeDrive) -> None:
    from PIL import Image

    source = tmp_path / "real.jpg"
    Image.new("RGB", (800, 600), "grey").save(source, "JPEG")

    entry = drive.add_file(ROOT_ID, "real.jpg", source=source)
    assert entry.image_metadata["width"] == 800
    assert entry.image_metadata["height"] == 600


def test_unparseable_formats_report_no_metadata(drive: FakeDrive) -> None:
    """Drive gives nothing for some RAW; the fake must behave the same."""
    entry = drive.add_file(ROOT_ID, "IMG_0001.CR3", data=b"not a real raw")
    assert entry.image_metadata == {}


def test_md5_distinguishes_two_different_files(drive: FakeDrive) -> None:
    first = drive.add_file(ROOT_ID, "a.jpg", data=b"one")
    second = drive.add_file(ROOT_ID, "b.jpg", data=b"two")
    assert first.md5 != second.md5


def test_md5_matches_for_the_same_card_copied_twice(drive: FakeDrive) -> None:
    """This is what makes a duplicate card copy cheap to detect."""
    first = drive.add_file(ROOT_ID, "IMG_0001.JPG", data=b"same pixels")
    second = drive.add_file(ROOT_ID, "IMG_0001.JPG", data=b"same pixels")
    assert first.id != second.id
    assert first.md5 == second.md5


# --- sharing --------------------------------------------------------------


def test_sharing_returns_a_link(drive: FakeDrive) -> None:
    gallery = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "photos"))
    assert drive.share_anyone_reader(gallery).startswith("file://")


# --- failure behaviour ----------------------------------------------------


def test_injected_failures_surface_as_drive_errors(drive: FakeDrive) -> None:
    drive.fail_next("upload", times=1)
    with pytest.raises(DriveError):
        drive.upload(Path(__file__), ROOT_ID)


def test_a_failed_move_leaves_the_original_where_it_was(drive: FakeDrive) -> None:
    """The non-negotiable: a failure never costs us the file."""
    inbox = drive.ensure_folder(ROOT_ID, "DROP")
    entry = drive.add_file(inbox, "IMG_0001.CR2", data=b"irreplaceable")
    destination = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "raw"))

    drive.fail_next("file_into", times=1)
    with pytest.raises(DriveError):
        drive.file_into(entry.id, destination)

    still_there = drive.list_children(inbox)
    assert [f.name for f in still_there] == ["IMG_0001.CR2"]
    assert still_there[0].md5 == entry.md5


def test_missing_file_ids_raise_rather_than_return_none(drive: FakeDrive) -> None:
    with pytest.raises(DriveError):
        drive.get_file("nosuchid")


# --- the layer cannot delete ---------------------------------------------


@pytest.mark.parametrize("forbidden", ["delete", "trash", "remove", "purge"])
def test_no_deletion_call_exists_in_the_drive_layer(forbidden: str) -> None:
    """Guards the first non-negotiable against a future well-meaning edit.

    Clearing the inbox is a human action taken after the archive copy is
    verified. If someone adds a delete here, this test should stop them and
    send them to read why.
    """
    source = Path(drive_module.__file__).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if f".{forbidden}(" in line and not line.strip().startswith(("#", "*"))
    ]
    assert offenders == []


def test_the_protocol_and_the_fake_have_not_drifted() -> None:
    """Every method the pipeline relies on exists on the stand-in."""
    required = [
        name
        for name in vars(drive_module.Drive)
        if not name.startswith("_")
    ]
    assert required, "protocol has no methods; the check would pass vacuously"
    for name in required:
        assert callable(getattr(FakeDrive, name, None)), f"FakeDrive lacks {name}"


def test_a_relative_root_still_produces_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """settings.yaml may hold a relative fake_drive_dir; links must still work."""
    monkeypatch.chdir(tmp_path)
    relative = FakeDrive(Path("fakedrive"))
    folder = relative.ensure_folder(ROOT_ID, "photos")
    assert relative.share_anyone_reader(folder).startswith("file://")


def test_injected_metadata_survives_a_restart(tmp_path: Path) -> None:
    """Fake mode is how someone tries the tool out; a restart must not
    forget every camera name and hold their whole test shoot."""
    first = FakeDrive(tmp_path / "drive")
    first.add_file(ROOT_ID, "IMG_0001.CR2", data=b"raw",
                   image_metadata={"cameraMake": "Canon", "cameraModel": "EOS R6"})

    reopened = FakeDrive(tmp_path / "drive")
    entry = reopened.list_children(ROOT_ID)[0]
    assert entry.image_metadata["cameraModel"] == "EOS R6"


def test_metadata_follows_a_file_when_it_is_filed(tmp_path: Path) -> None:
    drive = FakeDrive(tmp_path / "drive")
    entry = drive.add_file(ROOT_ID, "IMG_0001.CR2", data=b"raw",
                           image_metadata={"cameraMake": "Canon"})
    destination = drive.ensure_path(ROOT_ID, ("2026", "Horizon", "raw"))

    filed = drive.get_file(drive.file_into(entry.id, destination))
    assert filed.image_metadata["cameraMake"] == "Canon"
