"""Putting the club mark on a photo.

Only finished photos come through here. RAW and video are never opened, and
the unmarked original is kept in the archive alongside the marked copy, so a
watermark that turns out wrong costs a re-run rather than a reshoot.

The source file is opened read-only and the result is written somewhere
else. Nothing in this module writes to the file it was given.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger(__name__)

# Where the club logo is expected to be, relative to the project folder.
DEFAULT_MARK: str = "assets/watermark.png"

CORNERS: tuple[str, ...] = (
    "bottom-right",
    "bottom-left",
    "top-right",
    "top-left",
    "bottom-centre",
)


class WatermarkError(Exception):
    """The mark could not be applied. The item is held, never published."""


@dataclass(frozen=True)
class WatermarkStyle:
    """How the mark is drawn. Every value comes from settings.yaml."""

    path: Path
    scale: float = 0.16          # width, as a fraction of the photo's long edge
    opacity: float = 0.75
    margin: float = 0.025        # gap from the edges, same fraction
    corner: str = "bottom-right"
    shadow: bool = True          # keeps a white mark readable on a bright sky
    jpeg_quality: int = 92

    @classmethod
    def from_config(cls, cfg: dict[str, Any], project_root: Path) -> WatermarkStyle:
        raw_path = str(cfg.get("watermark_path", DEFAULT_MARK))
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = project_root / path

        corner = str(cfg.get("watermark_corner", "bottom-right")).lower()
        if corner not in CORNERS:
            log.warning("Unknown watermark_corner %r; using bottom-right", corner)
            corner = "bottom-right"

        return cls(
            path=path,
            scale=float(cfg.get("watermark_scale", 0.16)),
            opacity=float(cfg.get("watermark_opacity", 0.75)),
            margin=float(cfg.get("watermark_margin", 0.025)),
            corner=corner,
            shadow=bool(cfg.get("watermark_shadow", True)),
            jpeg_quality=int(cfg.get("jpeg_quality", 92)),
        )


def load_mark(style: WatermarkStyle) -> Image.Image:
    """Read the logo once, as RGBA.

    Loaded by the worker at startup and reused for every photo — opening a
    PNG four hundred times would be the slowest part of the run.
    """
    if not style.path.exists():
        raise WatermarkError(
            f"No watermark image at {style.path}. Put the club logo there as "
            "a transparent PNG, or point watermark_path at it in "
            "settings.yaml. Photos are held rather than published unmarked."
        )
    try:
        with Image.open(style.path) as opened:
            mark = opened.convert("RGBA")
    except OSError as exc:
        raise WatermarkError(f"{style.path} is not a readable image: {exc}") from exc

    return mark


def _scaled(mark: Image.Image, photo: Image.Image, style: WatermarkStyle) -> Image.Image:
    """Size the mark against the photo's long edge.

    Long edge rather than width, so the mark occupies the same share of a
    portrait frame as of a landscape one instead of ballooning.
    """
    long_edge = max(photo.size)
    target_width = max(1, int(long_edge * style.scale))
    ratio = target_width / mark.width
    target_height = max(1, int(mark.height * ratio))

    if target_width >= photo.width or target_height >= photo.height:
        # A thumbnail small enough that the mark would cover it. Shrink to
        # fit rather than refusing: the photo is still worth publishing.
        target_width = max(1, photo.width // 3)
        target_height = max(1, int(mark.height * (target_width / mark.width)))

    return mark.resize((target_width, target_height), Image.LANCZOS)


def _faded(mark: Image.Image, opacity: float) -> Image.Image:
    """Apply opacity to the mark's own alpha, preserving its transparency."""
    if opacity >= 1.0:
        return mark
    faded = mark.copy()
    alpha = faded.getchannel("A").point(lambda value: int(value * max(0.0, opacity)))
    faded.putalpha(alpha)
    return faded


# Tuned by eye against a white frame: enough of a halo that a white logo
# reads on a bright sky, not so much that it looks like a sticker.
_SHADOW_ALPHA: int = 200
_SHADOW_BLUR_DIVISOR: int = 60
_SHADOW_PASSES: int = 2


def _compose(sized: Image.Image, style: WatermarkStyle) -> Image.Image:
    """Fade the mark and, if asked, lay a soft dark blur underneath it.

    Club logos are usually white, and a white mark over a bright sky or a
    white kurta simply disappears. The shadow costs nothing on a dark
    background and rescues the mark on a light one.

    The shadow is built from the mark's *original* alpha, before the opacity
    setting is applied. A faint watermark is precisely the case that needs
    the halo most, so fading them together would defeat the point.
    """
    faded = _faded(sized, style.opacity)
    if not style.shadow:
        return faded

    pad = max(2, sized.width // _SHADOW_BLUR_DIVISOR)
    canvas = Image.new("RGBA", (sized.width + pad * 4, sized.height + pad * 4), (0, 0, 0, 0))

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, _SHADOW_ALPHA), (pad * 2, pad * 2), sized.getchannel("A"))
    shadow = shadow.filter(ImageFilter.GaussianBlur(pad))

    for _ in range(_SHADOW_PASSES):
        canvas.alpha_composite(shadow)
    canvas.alpha_composite(faded, (pad * 2, pad * 2))
    return canvas


def _position(photo: Image.Image, mark: Image.Image, style: WatermarkStyle) -> tuple[int, int]:
    """Top-left pixel for the mark, given the chosen corner and margin."""
    gap = int(max(photo.size) * style.margin)
    right = max(0, photo.width - mark.width - gap)
    bottom = max(0, photo.height - mark.height - gap)
    centre = max(0, (photo.width - mark.width) // 2)

    return {
        "bottom-right": (right, bottom),
        "bottom-left": (gap, bottom),
        "top-right": (right, gap),
        "top-left": (gap, gap),
        "bottom-centre": (centre, bottom),
    }[style.corner]


def apply(source: Path, destination: Path, mark: Image.Image, style: WatermarkStyle) -> Path:
    """Write a watermarked copy of ``source`` to ``destination``.

    ``source`` is only ever read. The result is written to a temporary file
    and renamed into place, so a crash mid-save cannot leave a half-written
    JPEG that looks finished to the next run.

    EXIF is carried across, because the archive is worth more with the
    capture time and camera still attached.
    """
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        with Image.open(source) as opened:
            # Honour the camera's rotation flag, then drop it: the pixels
            # are now the right way up, and a viewer that also honoured the
            # flag would otherwise rotate the photo a second time.
            photo = ImageOps.exif_transpose(opened)
            exif = opened.info.get("exif")
            icc = opened.info.get("icc_profile")

            if photo.mode != "RGB":
                photo = photo.convert("RGB")

            stamp = _compose(_scaled(mark, photo, style), style)
            photo.paste(stamp, _position(photo, stamp, style), stamp)

            partial = destination.with_suffix(destination.suffix + ".part")
            save_options: dict[str, Any] = {
                "quality": style.jpeg_quality,
                # 4:4:4. Chroma subsampling is cheap on file size but shows
                # on saturated stage lighting, which is most of what the
                # club shoots.
                "subsampling": 0,
                "optimize": True,
            }
            if exif:
                save_options["exif"] = exif
            if icc:
                save_options["icc_profile"] = icc

            photo.save(partial, "JPEG", **save_options)
            partial.replace(destination)
    except WatermarkError:
        raise
    except (OSError, ValueError) as exc:
        raise WatermarkError(f"could not watermark {source.name}: {exc}") from exc

    return destination


def output_name(original: str) -> str:
    """Name for the marked copy: same stem, always .jpg.

    HEIC off a phone becomes a JPEG here, because the point of the gallery
    is that anyone can open it.
    """
    return Path(original).with_suffix(".jpg").name
