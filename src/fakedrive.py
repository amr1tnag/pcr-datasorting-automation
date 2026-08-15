"""A Drive stand-in backed by ordinary folders on disk.

It exists for two reasons. The tests need to run the whole pipeline without
credentials or a network, and next year's maintainer needs to be able to see
what the tool does before pointing it at the club's real archive: set
``drive_mode: fake`` and the sorted tree appears in a local folder they can
open in Explorer.

File ids are the SHA-1 of the file's path inside the fake drive rather than
a counter, so they survive a restart and a human can drop files straight
into the inbox folder without registering them anywhere. The one place this
differs from Google is that filing a file changes its id, because its path
changed — callers already use the id returned by :meth:`file_into`, which is
what Drive's own behaviour requires anyway.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from src.drive import FOLDER_MIME, DriveError, DriveFile

ROOT_ID: str = "root"

# Extensions Pillow can measure. Anything else gets no dimensions from the
# fake, exactly as Drive gives none for formats it cannot parse.
_MEASURABLE = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"}


def _id_for(relative: Path) -> str:
    """Stable id for a path inside the fake drive."""
    if str(relative) in (".", ""):
        return ROOT_ID
    digest = hashlib.sha1(str(relative).replace("\\", "/").encode("utf-8"))
    return digest.hexdigest()[:16]


class FakeDrive:
    """Implements the :class:`src.drive.Drive` protocol against a folder."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

        # Metadata a test wants a file to report, keyed by file id. Real
        # cameras write this into the file; here it is injected.
        self.metadata: dict[str, dict[str, Any]] = {}

        # Failure injection: map of operation name to the number of times it
        # should still fail, e.g. {"upload": 2}. Used to test retries.
        self.failures: dict[str, int] = {}

        # Every operation performed, for tests that assert what was *not*
        # done — nothing in this project may delete an original.
        self.calls: list[tuple[str, str]] = []

    # --- helpers ----------------------------------------------------------

    def _path(self, file_id: str) -> Path:
        """Find the path a file id refers to, by searching the tree.

        Linear rather than indexed. The fake drive holds a test fixture or a
        volunteer's dry run, not a real archive, and an index would be one
        more thing to keep honest.
        """
        if file_id == ROOT_ID:
            return self.root
        for candidate in self.root.rglob("*"):
            if _id_for(candidate.relative_to(self.root)) == file_id:
                return candidate
        raise DriveError(f"no such file: {file_id}")

    def _maybe_fail(self, operation: str) -> None:
        remaining = self.failures.get(operation, 0)
        if remaining > 0:
            self.failures[operation] = remaining - 1
            raise DriveError(f"injected {operation} failure")

    def _to_file(self, path: Path) -> DriveFile:
        relative = path.relative_to(self.root)
        file_id = _id_for(relative)

        if path.is_dir():
            return DriveFile(
                id=file_id,
                name=path.name,
                mime_type=FOLDER_MIME,
                parents=(_id_for(relative.parent),),
            )

        data = path.read_bytes()
        injected = self.metadata.get(file_id, {})
        return DriveFile(
            id=file_id,
            name=path.name,
            mime_type="application/octet-stream",
            size=len(data),
            md5=hashlib.md5(data).hexdigest(),
            parents=(_id_for(relative.parent),),
            created_time=None,
            image_metadata=injected.get("imageMediaMetadata", self._measure(path)),
            video_metadata=injected.get("videoMediaMetadata", {}),
            web_link=path.as_uri(),
        )

    def _measure(self, path: Path) -> dict[str, Any]:
        """Read width, height, make and model the way Drive would."""
        if path.suffix.lower() not in _MEASURABLE:
            return {}
        try:
            from PIL import Image

            with Image.open(path) as image:
                width, height = image.size
                exif = image.getexif()
        except Exception:
            return {}

        found: dict[str, Any] = {"width": width, "height": height}
        make, model = exif.get(0x010F), exif.get(0x0110)
        if make:
            found["cameraMake"] = str(make)
        if model:
            found["cameraModel"] = str(model)
        orientation = exif.get(0x0112)
        if orientation in (5, 6, 7, 8):
            found["rotation"] = 90
        return found

    # --- the Drive protocol ----------------------------------------------

    def list_children(self, folder_id: str) -> list[DriveFile]:
        self._maybe_fail("list_children")
        folder = self._path(folder_id)
        if not folder.is_dir():
            return []
        return [self._to_file(child) for child in sorted(folder.iterdir())]

    def get_file(self, file_id: str) -> DriveFile:
        self._maybe_fail("get_file")
        return self._to_file(self._path(file_id))

    def ensure_folder(self, parent_id: str, name: str) -> str:
        self._maybe_fail("ensure_folder")
        folder = self._path(parent_id) / name
        folder.mkdir(parents=True, exist_ok=True)
        return _id_for(folder.relative_to(self.root))

    def ensure_path(self, root_id: str, parts: tuple[str, ...]) -> str:
        current = root_id
        for part in parts:
            current = self.ensure_folder(current, part)
        return current

    def file_into(self, file_id: str, destination_id: str) -> str:
        self._maybe_fail("file_into")
        source = self._path(file_id)
        folder = self._path(destination_id)
        folder.mkdir(parents=True, exist_ok=True)

        target = folder / source.name
        if target.resolve() == source.resolve():
            return file_id  # already filed
        target = _free_name(target)

        shutil.move(str(source), str(target))
        self.calls.append(("file_into", source.name))
        return _id_for(target.relative_to(self.root))

    def download(self, file_id: str, destination: Path) -> Path:
        self._maybe_fail("download")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._path(file_id), destination)
        self.calls.append(("download", destination.name))
        return destination

    def upload(self, path: Path, parent_id: str, name: str | None = None) -> DriveFile:
        self._maybe_fail("upload")
        path = Path(path)
        target = _free_name(self._path(parent_id) / (name or path.name))
        shutil.copy2(path, target)
        self.calls.append(("upload", target.name))
        return self._to_file(target)

    def share_anyone_reader(self, file_id: str) -> str:
        self._maybe_fail("share_anyone_reader")
        path = self._path(file_id)
        self.calls.append(("share", path.name))
        return path.as_uri()

    # --- test conveniences ------------------------------------------------

    def add_file(
        self,
        parent_id: str,
        name: str,
        data: bytes = b"pretend photo",
        source: Path | None = None,
        image_metadata: dict[str, Any] | None = None,
        video_metadata: dict[str, Any] | None = None,
    ) -> DriveFile:
        """Put a file in the fake drive the way a volunteer's upload would."""
        target = _free_name(self._path(parent_id) / name)
        if source is not None:
            shutil.copy2(source, target)
        else:
            target.write_bytes(data)

        entry = self._to_file(target)
        injected: dict[str, Any] = {}
        if image_metadata is not None:
            injected["imageMediaMetadata"] = image_metadata
        if video_metadata is not None:
            injected["videoMediaMetadata"] = video_metadata
        if injected:
            self.metadata[entry.id] = injected
            entry = self._to_file(target)
        return entry

    def fail_next(self, operation: str, times: int = 1) -> None:
        """Make the next ``times`` calls to ``operation`` raise."""
        self.failures[operation] = times


def _free_name(target: Path) -> Path:
    """A path that does not collide, adding ' (2)', ' (3)' as Drive does."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    counter = 2
    while True:
        candidate = target.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
