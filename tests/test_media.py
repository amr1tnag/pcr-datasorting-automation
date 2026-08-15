"""Tests for the sorting rules.

These are the decisions that put a file in the wrong folder if they are
wrong, so the awkward real-world cases from the brief each get a test: a
camera with no metadata, a model name that changed across firmware, phone
footage on a card, a card dumped without an event name.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src import media


# --- classify -------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("IMG_0001.JPG", media.KIND_PHOTO),
        ("shot.jpeg", media.KIND_PHOTO),
        ("IMG_0001.HEIC", media.KIND_PHOTO),
        ("IMG_0001.CR2", media.KIND_RAW),
        ("DSC01234.arw", media.KIND_RAW),
        ("drone.DNG", media.KIND_RAW),
        ("C0012.MP4", media.KIND_VIDEO),
        ("clip.mov", media.KIND_VIDEO),
        ("AVCHD.mts", media.KIND_VIDEO),
        ("notes.txt", media.KIND_OTHER),
        ("no_extension", media.KIND_OTHER),
    ],
)
def test_classify_by_extension(filename: str, expected: str) -> None:
    assert media.classify(filename) == expected


def test_classify_uses_only_the_final_extension() -> None:
    assert media.classify("horizon.mp4.jpg") == media.KIND_PHOTO


@pytest.mark.parametrize(
    "filename",
    ["Thumbs.db", ".DS_Store", "IMG_0001.XMP", "._IMG_0001.JPG", "MISC.CTG"],
)
def test_card_litter_is_ignorable(filename: str) -> None:
    assert media.is_ignorable(filename)


def test_real_photos_are_not_ignorable() -> None:
    assert not media.is_ignorable("IMG_0001.JPG")


# --- orientation ----------------------------------------------------------


def test_landscape_and_portrait() -> None:
    assert media.orientation_of(6000, 4000) == media.ORIENT_LANDSCAPE
    assert media.orientation_of(4000, 6000) == media.ORIENT_PORTRAIT


def test_square_within_tolerance() -> None:
    assert media.orientation_of(4000, 4000) == media.ORIENT_SQUARE
    # A hand crop a few pixels off is still square.
    assert media.orientation_of(4000, 3970) == media.ORIENT_SQUARE


def test_just_outside_tolerance_is_not_square() -> None:
    assert media.orientation_of(4000, 3800) == media.ORIENT_LANDSCAPE


def test_rotation_swaps_the_axes() -> None:
    """A portrait frame recorded as 6000x4000 with a quarter turn."""
    assert media.orientation_of(6000, 4000, rotation=90) == media.ORIENT_PORTRAIT
    assert media.orientation_of(6000, 4000, rotation=270) == media.ORIENT_PORTRAIT
    assert media.orientation_of(6000, 4000, rotation=180) == media.ORIENT_LANDSCAPE


@pytest.mark.parametrize(
    ("width", "height"), [(None, 4000), (6000, None), (0, 4000), (-1, 10)]
)
def test_unusable_dimensions_give_no_answer(
    width: int | None, height: int | None
) -> None:
    assert media.orientation_of(width, height) is None


def test_exif_orientation_tag_to_degrees() -> None:
    assert media.rotation_from_exif_orientation(1) == 0
    assert media.rotation_from_exif_orientation(6) == 90
    assert media.rotation_from_exif_orientation(8) == 90
    assert media.rotation_from_exif_orientation(None) == 0


# --- camera names ---------------------------------------------------------


def test_make_is_repeated_in_the_model_only_once() -> None:
    assert media.normalise_camera("Canon", "Canon EOS R6") == "CanonEOSR6"


def test_spacing_and_punctuation_do_not_split_a_body() -> None:
    """The same body across two firmware versions must land in one folder."""
    first = media.normalise_camera("Canon", "Canon EOS R6")
    second = media.normalise_camera("Canon Inc.", " Canon  EOS-R6 ")
    assert first == second == "CanonEOSR6"


def test_nikon_corporation_is_just_nikon() -> None:
    assert media.normalise_camera("NIKON CORPORATION", "NIKON Z 6") == "NikonZ6"


def test_model_alone_still_gives_a_name() -> None:
    assert media.normalise_camera(None, "ILCE-7M3") == "ILCE7M3"


def test_make_alone_still_gives_a_name() -> None:
    assert media.normalise_camera("Fujifilm", None) == "Fujifilm"


@pytest.mark.parametrize(("make", "model"), [(None, None), ("", ""), ("  ", "\x00")])
def test_no_metadata_gives_no_camera(make: str | None, model: str | None) -> None:
    """The brief's 'camera that reports no usable metadata' case."""
    assert media.normalise_camera(make, model) is None


def test_phones_are_labelled_and_not_mixed_with_club_bodies() -> None:
    assert media.normalise_camera("Apple", "iPhone 13 Pro") == "Phone-iPhone13Pro"
    assert media.normalise_camera("samsung", "SM-G991B") == "Phone-SMG991B"
    assert media.is_phone("Apple")
    assert not media.is_phone("Canon Inc.")


# --- capture time ---------------------------------------------------------


def test_parses_exif_and_drive_timestamps() -> None:
    assert media.parse_capture_time("2026:08:15 21:04:11") == datetime(
        2026, 8, 15, 21, 4, 11
    )
    parsed = media.parse_capture_time("2026-08-15T21:04:11.000Z")
    assert parsed is not None and parsed.year == 2026


@pytest.mark.parametrize("text", [None, "", "not a date", "0000:00:00 00:00:00"])
def test_unreadable_timestamps_give_none(text: str | None) -> None:
    assert media.parse_capture_time(text) is None


# --- event names ----------------------------------------------------------


def test_casing_and_spacing_collapse_to_one_event() -> None:
    assert media.normalise_event("horizon") == "Horizon"
    assert media.normalise_event("  Horizon  ") == "Horizon"
    assert media.event_key("HORIZON") == media.event_key("horizon ")


def test_deliberate_capitals_survive() -> None:
    assert media.normalise_event("EPL finals") == "EPL Finals"
    assert media.normalise_event("DYDT20") == "DYDT20"


def test_missing_event_becomes_unsorted() -> None:
    assert media.normalise_event(None) == media.UNSORTED_EVENT
    assert media.normalise_event("   ") == media.UNSORTED_EVENT
    assert media.normalise_event("///") == media.UNSORTED_EVENT


def test_unsafe_characters_are_dropped() -> None:
    assert media.normalise_event("Ganpati/Day 2") == "Ganpati Day 2"


# --- facts from Drive metadata -------------------------------------------


def _drive_image(**image: object) -> dict[str, object]:
    return {"imageMediaMetadata": image}


def test_drive_metadata_is_enough_for_a_confident_photo() -> None:
    facts = media.facts_from_drive(
        "IMG_0001.JPG",
        _drive_image(
            cameraMake="Canon",
            cameraModel="Canon EOS R6",
            width=6000,
            height=4000,
            rotation=0,
            time="2026:08:15 21:04:11",
        ),
    )
    assert facts.kind == media.KIND_PHOTO
    assert facts.camera == "CanonEOSR6"
    assert facts.orientation == media.ORIENT_LANDSCAPE
    assert facts.captured_at == datetime(2026, 8, 15, 21, 4, 11)
    assert facts.confident


def test_photo_without_camera_metadata_is_held() -> None:
    facts = media.facts_from_drive("IMG_0002.JPG", _drive_image(width=6000, height=4000))
    assert facts.camera is None
    assert media.REVIEW_NO_CAMERA in facts.reasons
    assert not facts.confident


def test_video_needs_no_orientation() -> None:
    facts = media.facts_from_drive(
        "C0012.MP4",
        {
            "videoMediaMetadata": {"width": 1920, "height": 1080},
            "imageMediaMetadata": {"cameraMake": "Sony", "cameraModel": "ILME-FX3"},
        },
    )
    assert facts.kind == media.KIND_VIDEO
    assert facts.confident


def test_raw_without_dimensions_is_held() -> None:
    facts = media.facts_from_drive(
        "IMG_0003.CR2", _drive_image(cameraMake="Canon", cameraModel="EOS R6")
    )
    assert media.REVIEW_NO_ORIENTATION in facts.reasons


def test_missing_metadata_block_does_not_crash() -> None:
    for metadata in (None, {}, {"imageMediaMetadata": None}):
        facts = media.facts_from_drive("IMG_0004.JPG", metadata)  # type: ignore[arg-type]
        assert not facts.confident


def test_unrecognised_file_type_is_flagged() -> None:
    facts = media.facts_from_drive("notes.txt", None)
    assert media.REVIEW_UNKNOWN_KIND in facts.reasons


# --- EXIF fallback --------------------------------------------------------


def _write_jpeg(path: Path, size: tuple[int, int], exif: dict[int, object]) -> Path:
    """A real JPEG on disk, with EXIF tags a camera would have written."""
    from PIL import Image

    image = Image.new("RGB", size, "grey")
    exif_block = Image.Exif()
    for tag, value in exif.items():
        exif_block[tag] = value
    image.save(path, "JPEG", exif=exif_block)
    return path


# EXIF tag numbers: Make, Model, DateTimeOriginal, Orientation.
_MAKE, _MODEL, _DATETIME, _ORIENTATION = 0x010F, 0x0110, 0x0132, 0x0112


def test_exif_fallback_reads_a_real_file(tmp_path: Path) -> None:
    path = _write_jpeg(
        tmp_path / "IMG_0005.JPG",
        (600, 400),
        {
            _MAKE: "NIKON CORPORATION",
            _MODEL: "NIKON Z 6",
            _DATETIME: "2026:08:15 21:04:11",
        },
    )
    facts = media.facts_from_exif(path)
    assert facts.camera == "NikonZ6"
    assert facts.orientation == media.ORIENT_LANDSCAPE
    assert facts.captured_at == datetime(2026, 8, 15, 21, 4, 11)


def test_exif_fallback_on_a_file_with_nothing_in_it(tmp_path: Path) -> None:
    path = _write_jpeg(tmp_path / "IMG_0006.JPG", (600, 400), {})
    facts = media.facts_from_exif(path)
    assert facts.camera is None
    assert media.REVIEW_NO_CAMERA in facts.reasons


def test_exif_fallback_on_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "truncated.jpg"
    path.write_bytes(b"not really a jpeg")
    facts = media.facts_from_exif(path)
    assert not facts.confident


def test_merge_rescues_a_file_drive_could_not_parse(tmp_path: Path) -> None:
    """Drive knew nothing, EXIF knew everything: the item must not be held."""
    from_drive = media.facts_from_drive("IMG_0007.JPG", None)
    assert not from_drive.confident

    path = _write_jpeg(
        tmp_path / "IMG_0007.JPG",
        (400, 600),
        {_MAKE: "Canon", _MODEL: "Canon EOS R6", _DATETIME: "2026:08:15 21:04:11"},
    )
    merged = media.merge_facts(from_drive, media.facts_from_exif(path))
    assert merged.camera == "CanonEOSR6"
    assert merged.orientation == media.ORIENT_PORTRAIT
    assert merged.confident


def test_merge_keeps_what_the_primary_already_knew() -> None:
    primary = media.Facts(
        kind=media.KIND_PHOTO, camera="CanonEOSR6", orientation=media.ORIENT_LANDSCAPE
    )
    fallback = media.Facts(
        kind=media.KIND_PHOTO, camera="NikonZ6", orientation=media.ORIENT_PORTRAIT
    )
    merged = media.merge_facts(primary, fallback)
    assert merged.camera == "CanonEOSR6"
    assert merged.orientation == media.ORIENT_LANDSCAPE


# --- destinations ---------------------------------------------------------


def test_photo_goes_to_the_shared_gallery_tree() -> None:
    assert media.destination(
        "Horizon", 2026, media.KIND_PHOTO, "CanonEOSR6", media.ORIENT_LANDSCAPE
    ) == ("2026", "Horizon", "photos", "CanonEOSR6", "landscape")


def test_unmarked_original_sits_outside_the_gallery() -> None:
    path = media.destination(
        "Horizon", 2026, media.KIND_PHOTO, "CanonEOSR6", media.ORIENT_LANDSCAPE,
        original=True,
    )
    assert path[2] == media.ORIGINALS_FOLDER
    assert media.GALLERY_FOLDER not in path


def test_raw_and_video_skip_orientation() -> None:
    assert media.destination("Horizon", 2026, media.KIND_RAW, "CanonEOSR6", None) == (
        "2026", "Horizon", "raw", "CanonEOSR6",
    )
    assert media.destination("Horizon", 2026, media.KIND_VIDEO, "Phone-iPhone13", None) == (
        "2026", "Horizon", "video", "Phone-iPhone13",
    )


def test_held_items_never_land_in_the_gallery() -> None:
    path = media.destination(
        "Horizon", 2026, media.KIND_PHOTO, None, None, held=True
    )
    assert path == ("2026", "Horizon", media.REVIEW_FOLDER)


def test_watermark_applies_to_photos_only() -> None:
    assert media.should_watermark(media.KIND_PHOTO)
    assert not media.should_watermark(media.KIND_RAW)
    assert not media.should_watermark(media.KIND_VIDEO)
