"""Tests for the watermark step.

The assertions that matter: the source file is never touched, the mark
actually lands where it was asked to, and a failure leaves nothing
half-written behind.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from src import watermark
from src.watermark import WatermarkError, WatermarkStyle

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _photo(path: Path, size: tuple[int, int] = (1200, 800), colour: str = "white") -> Path:
    Image.new("RGB", size, colour).save(path, "JPEG")
    return path


def _mark_file(path: Path, size: tuple[int, int] = (400, 100)) -> Path:
    """An opaque white block, easy to detect against a known background."""
    Image.new("RGBA", size, (255, 255, 255, 255)).save(path, "PNG")
    return path


@pytest.fixture()
def style(tmp_path: Path) -> WatermarkStyle:
    return WatermarkStyle(path=_mark_file(tmp_path / "mark.png"), shadow=False)


@pytest.fixture()
def mark(style: WatermarkStyle) -> Image.Image:
    return watermark.load_mark(style)


# --- the placeholder ------------------------------------------------------


def test_placeholder_is_committed_and_loadable() -> None:
    """Until the club logo arrives, the pipeline must still run."""
    placeholder = PROJECT_ROOT / "assets" / watermark.PLACEHOLDER_NAME
    assert placeholder.exists()
    style = WatermarkStyle(path=placeholder)
    assert style.is_placeholder
    assert watermark.load_mark(style).mode == "RGBA"


def test_a_real_logo_is_not_reported_as_placeholder(tmp_path: Path) -> None:
    style = WatermarkStyle(path=_mark_file(tmp_path / "watermark.png"))
    assert not style.is_placeholder


def test_placeholder_use_is_warned_about(
    style: WatermarkStyle, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    placeholder = tmp_path / watermark.PLACEHOLDER_NAME
    _mark_file(placeholder)
    with caplog.at_level("WARNING"):
        watermark.load_mark(WatermarkStyle(path=placeholder))
    assert "placeholder" in caplog.text.lower()


def test_missing_logo_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(WatermarkError, match="settings.yaml"):
        watermark.load_mark(WatermarkStyle(path=tmp_path / "nope.png"))


def test_a_logo_that_is_not_an_image_is_rejected(tmp_path: Path) -> None:
    broken = tmp_path / "watermark.png"
    broken.write_text("this is not a png")
    with pytest.raises(WatermarkError):
        watermark.load_mark(WatermarkStyle(path=broken))


# --- the original is untouched -------------------------------------------


def test_the_source_photo_is_never_modified(
    tmp_path: Path, style: WatermarkStyle, mark: Image.Image
) -> None:
    """First non-negotiable, at the one step that opens an original."""
    source = _photo(tmp_path / "IMG_0001.JPG")
    before = hashlib.md5(source.read_bytes()).hexdigest()

    watermark.apply(source, tmp_path / "out" / "IMG_0001.jpg", mark, style)

    assert hashlib.md5(source.read_bytes()).hexdigest() == before


def test_output_goes_where_it_was_asked(
    tmp_path: Path, style: WatermarkStyle, mark: Image.Image
) -> None:
    source = _photo(tmp_path / "IMG_0001.JPG")
    result = watermark.apply(source, tmp_path / "deep" / "er" / "out.jpg", mark, style)
    assert result.exists() and result.parent.name == "er"


def test_no_part_file_is_left_behind(
    tmp_path: Path, style: WatermarkStyle, mark: Image.Image
) -> None:
    source = _photo(tmp_path / "IMG_0001.JPG")
    destination = tmp_path / "out.jpg"
    watermark.apply(source, destination, mark, style)
    assert list(tmp_path.glob("*.part")) == []


# --- the mark lands ------------------------------------------------------


def _corner_pixel(path: Path, corner: str) -> tuple[int, int, int]:
    with Image.open(path) as image:
        width, height = image.size
        spots = {
            "bottom-right": (width - 60, height - 40),
            "bottom-left": (60, height - 40),
            "top-right": (width - 60, 40),
            "top-left": (60, 40),
        }
        return image.convert("RGB").getpixel(spots[corner])  # type: ignore[return-value]


def test_the_mark_is_visible_in_the_chosen_corner(
    tmp_path: Path, mark: Image.Image
) -> None:
    """White mark on a black photo: the corner must stop being black."""
    source = _photo(tmp_path / "IMG_0001.JPG", colour="black")
    style = WatermarkStyle(path=tmp_path / "mark.png", shadow=False, opacity=1.0)
    out = watermark.apply(source, tmp_path / "out.jpg", mark, style)

    red, _, _ = _corner_pixel(out, "bottom-right")
    assert red > 100
    # And the opposite corner is untouched.
    assert _corner_pixel(out, "top-left")[0] < 40


@pytest.mark.parametrize("corner", ["bottom-right", "bottom-left", "top-right", "top-left"])
def test_every_corner_setting_works(tmp_path: Path, mark: Image.Image, corner: str) -> None:
    source = _photo(tmp_path / f"{corner}.JPG", colour="black")
    style = WatermarkStyle(path=tmp_path / "mark.png", corner=corner, shadow=False, opacity=1.0)
    out = watermark.apply(source, tmp_path / f"out-{corner}.jpg", mark, style)
    assert _corner_pixel(out, corner)[0] > 100


def test_opacity_is_applied(tmp_path: Path, mark: Image.Image) -> None:
    source = _photo(tmp_path / "IMG_0001.JPG", colour="black")
    faint = WatermarkStyle(path=tmp_path / "mark.png", opacity=0.2, shadow=False)
    solid = WatermarkStyle(path=tmp_path / "mark.png", opacity=1.0, shadow=False)

    faint_out = watermark.apply(source, tmp_path / "faint.jpg", mark, faint)
    solid_out = watermark.apply(source, tmp_path / "solid.jpg", mark, solid)

    assert _corner_pixel(faint_out, "bottom-right")[0] < _corner_pixel(solid_out, "bottom-right")[0]


def test_shadow_keeps_a_white_mark_readable_on_white(
    tmp_path: Path, mark: Image.Image
) -> None:
    """The club logo is white; a bright sky is the case that breaks it."""
    source = _photo(tmp_path / "sky.JPG", colour="white")
    without = WatermarkStyle(path=tmp_path / "mark.png", shadow=False)
    with_shadow = WatermarkStyle(path=tmp_path / "mark.png", shadow=True)

    plain = watermark.apply(source, tmp_path / "plain.jpg", mark, without)
    shadowed = watermark.apply(source, tmp_path / "shadowed.jpg", mark, with_shadow)

    def darkest(path: Path) -> int:
        with Image.open(path) as image:
            return image.convert("L").getextrema()[0]

    # Without a shadow, white-on-white leaves the frame essentially white.
    assert darkest(plain) > 200
    assert darkest(shadowed) < 200


# --- sizing ---------------------------------------------------------------


def test_mark_scales_to_the_long_edge_not_the_width(
    tmp_path: Path, mark: Image.Image
) -> None:
    """A portrait frame should not get a proportionally bigger mark."""
    style = WatermarkStyle(path=tmp_path / "mark.png", scale=0.25, shadow=False, opacity=1.0)

    landscape = watermark.apply(
        _photo(tmp_path / "l.JPG", (1200, 800), "black"), tmp_path / "l-out.jpg", mark, style
    )
    portrait = watermark.apply(
        _photo(tmp_path / "p.JPG", (800, 1200), "black"), tmp_path / "p-out.jpg", mark, style
    )

    def mark_width(path: Path) -> int:
        with Image.open(path) as image:
            grey = image.convert("L")
            bright_columns = [
                x for x in range(grey.width)
                if any(grey.getpixel((x, y)) > 100 for y in range(grey.height - 120, grey.height))
            ]
        return len(bright_columns)

    assert abs(mark_width(landscape) - mark_width(portrait)) <= 2


def test_a_tiny_photo_does_not_get_a_mark_bigger_than_itself(
    tmp_path: Path, mark: Image.Image
) -> None:
    source = _photo(tmp_path / "tiny.JPG", (120, 90), "black")
    style = WatermarkStyle(path=tmp_path / "mark.png", scale=2.0, shadow=False)
    out = watermark.apply(source, tmp_path / "tiny-out.jpg", mark, style)
    with Image.open(out) as image:
        assert image.size == (120, 90)


# --- what survives the round trip ----------------------------------------


def test_exif_is_carried_into_the_marked_copy(tmp_path: Path, mark: Image.Image, style: WatermarkStyle) -> None:
    source = tmp_path / "IMG_0001.JPG"
    image = Image.new("RGB", (1200, 800), "grey")
    exif = Image.Exif()
    exif[0x010F] = "Canon"
    exif[0x0110] = "Canon EOS R6"
    exif[0x0132] = "2026:08:15 21:04:11"
    image.save(source, "JPEG", exif=exif)

    out = watermark.apply(source, tmp_path / "out.jpg", mark, style)
    with Image.open(out) as marked:
        carried = marked.getexif()
    assert carried.get(0x0110) == "Canon EOS R6"
    assert carried.get(0x0132) == "2026:08:15 21:04:11"


def test_a_rotated_photo_comes_out_upright(tmp_path: Path, mark: Image.Image, style: WatermarkStyle) -> None:
    """EXIF says 'turn me'; the marked copy has it already turned."""
    source = tmp_path / "rotated.JPG"
    image = Image.new("RGB", (1200, 800), "grey")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90
    image.save(source, "JPEG", exif=exif)

    out = watermark.apply(source, tmp_path / "out.jpg", mark, style)
    with Image.open(out) as marked:
        assert marked.size == (800, 1200)


def test_a_greyscale_photo_still_works(tmp_path: Path, mark: Image.Image, style: WatermarkStyle) -> None:
    source = tmp_path / "grey.JPG"
    Image.new("L", (1200, 800), 40).save(source, "JPEG")
    assert watermark.apply(source, tmp_path / "out.jpg", mark, style).exists()


# --- failure --------------------------------------------------------------


def test_a_corrupt_photo_raises_rather_than_writing_rubbish(
    tmp_path: Path, mark: Image.Image, style: WatermarkStyle
) -> None:
    source = tmp_path / "truncated.jpg"
    source.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    destination = tmp_path / "out.jpg"

    with pytest.raises(WatermarkError):
        watermark.apply(source, destination, mark, style)

    assert not destination.exists()
    assert list(tmp_path.glob("*.part")) == []


# --- naming and settings --------------------------------------------------


@pytest.mark.parametrize(
    ("original", "expected"),
    [("IMG_0001.JPG", "IMG_0001.jpg"), ("clip.HEIC", "clip.jpg"), ("a.b.jpeg", "a.b.jpg")],
)
def test_marked_copies_are_always_jpg(original: str, expected: str) -> None:
    assert watermark.output_name(original) == expected


def test_style_reads_settings(tmp_path: Path) -> None:
    style = WatermarkStyle.from_config(
        {
            "watermark_path": "assets/watermark.png",
            "watermark_scale": 0.2,
            "watermark_opacity": 0.5,
            "watermark_corner": "top-left",
            "jpeg_quality": 80,
        },
        tmp_path,
    )
    assert style.path == tmp_path / "assets" / "watermark.png"
    assert (style.scale, style.opacity, style.corner, style.jpeg_quality) == (
        0.2, 0.5, "top-left", 80,
    )


def test_an_unknown_corner_falls_back_instead_of_crashing(tmp_path: Path) -> None:
    style = WatermarkStyle.from_config({"watermark_corner": "middle-of-the-face"}, tmp_path)
    assert style.corner == "bottom-right"


def test_defaults_point_at_the_placeholder(tmp_path: Path) -> None:
    assert WatermarkStyle.from_config({}, tmp_path).is_placeholder
