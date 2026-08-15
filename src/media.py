"""Deciding what a file is, which camera shot it, and which way up it is.

Everything here is a pure function over filenames, numbers and metadata
dictionaries. Nothing in this module opens a network connection or writes to
the database, which is what makes the sorting rules cheap to test: the whole
module can be exercised without Drive credentials or a real camera.

Two sources of truth feed it. Google Drive parses most photo and RAW formats
on upload and hands back an ``imageMediaMetadata`` block, so the usual path
costs no download at all. When Drive returns nothing useful the worker pulls
the file down and calls :func:`facts_from_exif` instead. If both come back
empty the item is flagged for review rather than guessed into a folder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

# --- what kind of file is this -------------------------------------------

KIND_PHOTO: str = "photo"
KIND_RAW: str = "raw"
KIND_VIDEO: str = "video"
KIND_OTHER: str = "other"

# Finished images the club can share. These are the only files watermarked.
PHOTO_EXTS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".jpe", ".heic", ".heif", ".png"}
)

# Camera negatives. Kept and sorted, never opened, never watermarked.
RAW_EXTS: frozenset[str] = frozenset(
    {
        ".cr2", ".cr3", ".crw",   # Canon
        ".nef", ".nrw",           # Nikon
        ".arw", ".srf", ".sr2",   # Sony
        ".raf",                   # Fujifilm
        ".rw2",                   # Panasonic
        ".orf",                   # Olympus / OM System
        ".pef", ".dng",           # Pentax, Adobe / DJI / phones
        ".3fr", ".iiq",           # Hasselblad, Phase One
    }
)

VIDEO_EXTS: frozenset[str] = frozenset(
    {
        ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv",
        ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".mxf", ".insv",
    }
)

# Sidecars and card housekeeping. Recognised so they can be ignored quietly
# instead of landing in the review queue every single shoot.
IGNORED_EXTS: frozenset[str] = frozenset(
    {".xmp", ".thm", ".ctg", ".ini", ".db", ".ds_store", ".lrv", ".sec"}
)

IGNORED_NAMES: frozenset[str] = frozenset({"thumbs.db", ".ds_store", "desktop.ini"})


def classify(filename: str) -> str:
    """Return the KIND_* bucket for a filename, ignoring case.

    Only the extension is consulted. Reading file headers would be more
    rigorous but would mean downloading every file to find out what it is,
    and cameras do not lie about their own extensions.
    """
    name = Path(filename).name
    if name.lower() in IGNORED_NAMES:
        return KIND_OTHER

    ext = Path(name).suffix.lower()
    if ext in PHOTO_EXTS:
        return KIND_PHOTO
    if ext in RAW_EXTS:
        return KIND_RAW
    if ext in VIDEO_EXTS:
        return KIND_VIDEO
    return KIND_OTHER


def is_ignorable(filename: str) -> bool:
    """True for sidecars and card litter that should never reach the queue."""
    name = Path(filename).name
    if name.lower() in IGNORED_NAMES:
        return True
    if name.startswith("._"):  # macOS resource forks left on shared cards
        return True
    return Path(name).suffix.lower() in IGNORED_EXTS


# --- which way up ---------------------------------------------------------

ORIENT_LANDSCAPE: str = "landscape"
ORIENT_PORTRAIT: str = "portrait"
ORIENT_SQUARE: str = "square"

# Sensors are never exactly square, and a 3:2 frame cropped by hand often
# lands a pixel or two off. Anything within this fraction of 1:1 is square.
SQUARE_TOLERANCE: float = 0.02


def orientation_of(
    width: int | None, height: int | None, rotation: int = 0
) -> str | None:
    """Classify a frame's shape, or None when the dimensions are unusable.

    ``rotation`` is degrees clockwise as Drive reports it. A quarter turn
    swaps the axes, so a portrait shot recorded as 6000x4000 with rotation 90
    is correctly filed as portrait.
    """
    if not width or not height or width <= 0 or height <= 0:
        return None

    if rotation % 180 == 90:
        width, height = height, width

    ratio = width / height
    if abs(ratio - 1.0) <= SQUARE_TOLERANCE:
        return ORIENT_SQUARE
    return ORIENT_LANDSCAPE if ratio > 1.0 else ORIENT_PORTRAIT


# EXIF orientation tag values that mean "the camera was held sideways".
_EXIF_ROTATED = {5, 6, 7, 8}


def rotation_from_exif_orientation(value: int | None) -> int:
    """Degrees of rotation implied by the EXIF orientation tag (1-8)."""
    if value in _EXIF_ROTATED:
        return 90
    return 0


# --- which camera ---------------------------------------------------------

# Manufacturers write their own names inconsistently across firmware
# versions; these collapse the common spellings before the alias table gets
# a look in. Anything not listed here still works, it just arrives with
# whatever capitalisation the camera used.
_MAKE_CANON: dict[str, str] = {
    "nikon corporation": "Nikon",
    "nikon": "Nikon",
    "canon inc.": "Canon",
    "canon inc": "Canon",
    "canon": "Canon",
    "sony": "Sony",
    "fujifilm": "Fujifilm",
    "panasonic": "Panasonic",
    "olympus imaging corp.": "Olympus",
    "olympus corporation": "Olympus",
    "om digital solutions": "OMSystem",
    "eastman kodak company": "Kodak",
    "gopro": "GoPro",
    "dji": "DJI",
    "apple": "Apple",
    "samsung": "Samsung",
    "samsung electronics": "Samsung",
    "xiaomi": "Xiaomi",
    "oneplus": "OnePlus",
    "realme": "Realme",
    "oppo": "Oppo",
    "vivo": "Vivo",
    "motorola": "Motorola",
    "google": "Google",
    "nothing": "Nothing",
}

# Makes whose files are phone footage. They still get sorted like any other
# body, just into an obviously-labelled bucket so the manager can tell at a
# glance which folders came off a real camera.
PHONE_MAKES: frozenset[str] = frozenset(
    {
        "Apple", "Samsung", "Xiaomi", "OnePlus", "Realme", "Oppo",
        "Vivo", "Motorola", "Google", "Nothing", "Huawei", "Honor",
    }
)

# Firmware and card-tool leftovers that appear glued to model names.
_MODEL_NOISE = re.compile(
    r"\b(digital|camera|corporation|corp|company|co|inc|ltd)\b\.?",
    re.IGNORECASE,
)
_NON_NAME_CHARS = re.compile(r"[^A-Za-z0-9]+")


def _tidy(text: str | None) -> str:
    """Collapse whitespace and drop surrounding junk from a metadata string."""
    if not text:
        return ""
    return " ".join(str(text).replace("\x00", " ").split()).strip()


def canonical_make(make: str | None) -> str:
    """Normalise a manufacturer string, e.g. 'NIKON CORPORATION' -> 'Nikon'."""
    tidy = _tidy(make)
    if not tidy:
        return ""
    return _MAKE_CANON.get(tidy.lower(), tidy)


def normalise_camera(make: str | None, model: str | None) -> str | None:
    """Build one stable folder name for a camera body, or None if unknowable.

    The result is the make and model with punctuation and spaces removed, so
    that 'Canon EOS R6' and 'Canon  EOS-R6 ' land in the same folder. Phones
    are prefixed 'Phone-' so club bodies and volunteer handsets never mix.

    This is a best-effort tidy, not a database. When a body still reports two
    different names across firmware versions, the manager maps one onto the
    other once in the dashboard and the alias table takes over from there.
    """
    make_name = canonical_make(make)
    model_name = _tidy(model)
    model_name = _MODEL_NOISE.sub(" ", model_name)
    model_name = _tidy(model_name)

    # Most models already repeat the make ('Canon' + 'Canon EOS R6').
    if model_name and make_name:
        if model_name.lower().startswith(make_name.lower()):
            model_name = _tidy(model_name[len(make_name):])

    # A phone's model already names the handset ('iPhone 13', 'SM-G991B'),
    # so repeating the make would only make the folder harder to read.
    if make_name in PHONE_MAKES:
        parts = [model_name or make_name]
        prefix = "Phone-"
    else:
        parts = [part for part in (make_name, model_name) if part]
        prefix = ""

    joined = _NON_NAME_CHARS.sub("", "".join(parts))
    if not joined:
        return None
    return prefix + joined


def is_phone(make: str | None) -> bool:
    """True when the manufacturer is a handset maker rather than a camera one."""
    return canonical_make(make) in PHONE_MAKES


# --- when was it shot -----------------------------------------------------

# EXIF spells it '2026:08:15 21:04:11'; Drive hands back RFC 3339.
_EXIF_DATE_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d")


def parse_capture_time(text: str | None) -> datetime | None:
    """Parse a capture timestamp from EXIF or Drive, or None if unreadable."""
    tidy = _tidy(text)
    if not tidy:
        return None

    # Drive's RFC 3339, including the trailing Z that fromisoformat dislikes
    # on older Pythons.
    iso_candidate = tidy.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate)
    except ValueError:
        pass

    for fmt in _EXIF_DATE_FORMATS:
        try:
            return datetime.strptime(tidy, fmt)
        except ValueError:
            continue
    return None


# --- events ---------------------------------------------------------------

UNSORTED_EVENT: str = "_UNSORTED"

_EVENT_SAFE = re.compile(r"[^A-Za-z0-9 &()\-_]+")


def normalise_event(raw: str | None) -> str:
    """Turn a volunteer's folder name into a consistent event name.

    Case and spacing are normalised so 'horizon', 'Horizon ' and 'HORIZON'
    are one event rather than three. Characters Drive or Windows would
    object to are dropped. An empty or unusable name becomes ``_UNSORTED``,
    which the dashboard surfaces for the manager to assign.
    """
    tidy = _tidy(raw)
    if not tidy:
        return UNSORTED_EVENT
    if tidy.startswith("_"):  # our own internal folders, left alone
        return tidy

    cleaned = _tidy(_EVENT_SAFE.sub(" ", tidy))
    if not cleaned:
        return UNSORTED_EVENT

    # Title-case only the words that are not already deliberately capitalised,
    # so 'EPL' and 'DYDT20' survive intact but 'ganpati day 2' becomes
    # 'Ganpati Day 2'.
    words = [word if any(ch.isupper() for ch in word) else word.title() for word in cleaned.split()]
    return " ".join(words)


def event_key(name: str | None) -> str:
    """Comparison key for event names: lowercase, letters and digits only."""
    return _NON_NAME_CHARS.sub("", _tidy(name)).lower()


# --- the facts a file carries --------------------------------------------

REVIEW_NO_CAMERA: str = "no camera metadata"
REVIEW_NO_ORIENTATION: str = "no usable dimensions"
REVIEW_NO_EVENT: str = "no event folder"
REVIEW_UNKNOWN_KIND: str = "unrecognised file type"

UNKNOWN_CAMERA: str = "UnknownCamera"


@dataclass(frozen=True)
class Facts:
    """What we managed to work out about one file.

    ``reasons`` is empty for a file that can be filed with confidence. Any
    entry in it means the item is held for review instead of published, and
    the text is what the manager reads in the dashboard.
    """

    kind: str
    camera: str | None = None
    orientation: str | None = None
    captured_at: datetime | None = None
    is_phone: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def confident(self) -> bool:
        """True when nothing about this file needs a human decision."""
        return not self.reasons


def facts_from_drive(filename: str, metadata: dict[str, Any] | None) -> Facts:
    """Read what Drive already knows about an uploaded file.

    ``metadata`` is a Drive ``files.get`` response. Drive fills in
    ``imageMediaMetadata`` for JPEG and most RAW formats and
    ``videoMediaMetadata`` for video, so in the common case this is all the
    inspection a file ever needs.
    """
    meta = metadata or {}
    kind = classify(filename)

    image = meta.get("imageMediaMetadata") or {}
    video = meta.get("videoMediaMetadata") or {}
    block = image or video

    camera = normalise_camera(image.get("cameraMake"), image.get("cameraModel"))
    orientation = orientation_of(
        block.get("width"), block.get("height"), int(image.get("rotation") or 0)
    )
    captured = parse_capture_time(image.get("time") or meta.get("createdTime"))

    return _with_reasons(
        Facts(
            kind=kind,
            camera=camera,
            orientation=orientation,
            captured_at=captured,
            is_phone=is_phone(image.get("cameraMake")),
        )
    )


def facts_from_exif(path: Path) -> Facts:
    """Fallback for files Drive could not parse: read the EXIF ourselves.

    Import is local because the poller never needs it — only the worker, and
    only for the handful of files that arrive without Drive metadata.
    """
    import exifread

    kind = classify(path.name)
    try:
        with path.open("rb") as handle:
            tags = exifread.process_file(handle, details=False)
    except (OSError, ValueError):
        tags = {}

    def tag(name: str) -> str | None:
        value = tags.get(name)
        return str(value) if value is not None else None

    def number(name: str) -> int | None:
        value = tags.get(name)
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return None

    make = tag("Image Make")
    exif_orientation = tags.get("Image Orientation")
    rotation = 0
    if exif_orientation is not None:
        values = getattr(exif_orientation, "values", None)
        if values:
            rotation = rotation_from_exif_orientation(int(values[0]))

    orientation = orientation_of(
        number("EXIF ExifImageWidth") or number("Image ImageWidth"),
        number("EXIF ExifImageLength") or number("Image ImageLength"),
        rotation,
    )
    if orientation is None and kind == KIND_PHOTO:
        orientation = _orientation_from_pixels(path, rotation)

    return _with_reasons(
        Facts(
            kind=kind,
            camera=normalise_camera(make, tag("Image Model")),
            orientation=orientation,
            captured_at=parse_capture_time(
                tag("EXIF DateTimeOriginal") or tag("Image DateTime")
            ),
            is_phone=is_phone(make),
        )
    )


def _orientation_from_pixels(path: Path, rotation: int) -> str | None:
    """Last resort for a photo: decode the header and measure it.

    A JPEG records its real size in the SOF marker rather than in EXIF, and
    plenty of bodies never write the EXIF dimension tags at all, so without
    this a perfectly ordinary photo would be held for review. RAW is not
    attempted — Pillow cannot open it, and the EXIF tags are reliable there.
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except Exception:  # unreadable, truncated, or a format Pillow lacks
        return None
    return orientation_of(width, height, rotation)


def merge_facts(primary: Facts, fallback: Facts) -> Facts:
    """Fill the gaps in ``primary`` from ``fallback``.

    Used when Drive gave a partial answer and the EXIF read filled in the
    rest; the reasons are recalculated from the combined result so a file
    rescued by the fallback is no longer flagged.
    """
    merged = replace(
        primary,
        camera=primary.camera or fallback.camera,
        orientation=primary.orientation or fallback.orientation,
        captured_at=primary.captured_at or fallback.captured_at,
        is_phone=primary.is_phone or fallback.is_phone,
        reasons=(),
    )
    return _with_reasons(merged)


def _with_reasons(facts: Facts) -> Facts:
    """Attach the review reasons implied by what is missing."""
    reasons: list[str] = []

    if facts.kind == KIND_OTHER:
        reasons.append(REVIEW_UNKNOWN_KIND)
    if facts.camera is None:
        reasons.append(REVIEW_NO_CAMERA)
    # Video is filed by camera alone, so its shape does not matter.
    if facts.kind in (KIND_PHOTO, KIND_RAW) and facts.orientation is None:
        reasons.append(REVIEW_NO_ORIENTATION)

    return replace(facts, reasons=tuple(reasons))


# --- where it goes in Drive ----------------------------------------------

REVIEW_FOLDER: str = "_review"
ORIGINALS_FOLDER: str = "_originals"

# Only this subtree is ever shared. RAW, unmarked originals and held items
# sit outside it, so the link pasted into WhatsApp cannot leak them.
GALLERY_FOLDER: str = "photos"
RAW_FOLDER: str = "raw"
VIDEO_FOLDER: str = "video"


def destination(
    event: str,
    year: int,
    kind: str,
    camera: str | None,
    orientation: str | None,
    *,
    held: bool = False,
    original: bool = False,
) -> tuple[str, ...]:
    """Folder path, relative to the Drive root, for one sorted file.

    ``held`` puts the file in the event's review area instead of the gallery.
    ``original`` is the unmarked JPEG kept alongside its watermarked copy.
    The layout is deliberately spelled out in one place: the folder tree is
    the part of this tool everybody else has to understand.
    """
    base: tuple[str, ...] = (str(year), event)
    body = camera or UNKNOWN_CAMERA

    if held:
        return base + (REVIEW_FOLDER,)
    if kind == KIND_VIDEO:
        return base + (VIDEO_FOLDER, body)
    if kind == KIND_RAW:
        return base + (RAW_FOLDER, body)
    if kind == KIND_PHOTO:
        top = ORIGINALS_FOLDER if original else GALLERY_FOLDER
        return base + (top, body, orientation or ORIENT_LANDSCAPE)
    return base + (REVIEW_FOLDER,)


def should_watermark(kind: str) -> bool:
    """Only finished photos are watermarked; RAW and video are edited later."""
    return kind == KIND_PHOTO
