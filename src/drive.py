"""Everything that talks to Google Drive.

The rest of the project uses Drive only through the small surface defined
here: list a folder, make sure a folder path exists, move a file, upload a
file, share a folder. Keeping it narrow is what lets the pipeline be tested
against :mod:`src.fakedrive` without credentials or a network.

Two rules this module exists to enforce:

* **Filing is a move, not a copy.** Inbox and archive live in the same Drive,
  so putting a 40MB RAW in its folder is a parent swap: no bytes transferred
  and no second copy of the file. :meth:`Drive.file_into` falls back to a
  server-side copy only if a file somehow lives in a different drive.
* **Nothing is deleted, ever.** There is no delete or trash call in this
  module, and there should not be one. Clearing the inbox is a human action
  taken after the archive copy is verified.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

FOLDER_MIME: str = "application/vnd.google-apps.folder"

# Everything the pipeline reads about a file. Requested explicitly because
# Drive returns a minimal set otherwise.
FILE_FIELDS: str = (
    "id, name, mimeType, size, md5Checksum, parents, trashed, "
    "createdTime, modifiedTime, imageMediaMetadata, videoMediaMetadata, "
    "webViewLink"
)

MAX_ATTEMPTS: int = 5
BACKOFF_BASE_SECS: float = 2.0


class DriveError(Exception):
    """Any Drive failure the pipeline should record against an item."""


class TransientDriveError(DriveError):
    """A failure worth retrying: rate limits, 5xx, dropped connections."""


class QuotaExceeded(DriveError):
    """The Drive is full. Retrying will not help; a human must free space."""


@dataclass(frozen=True)
class DriveFile:
    """One file or folder as the pipeline cares about it."""

    id: str
    name: str
    mime_type: str = ""
    size: int = 0
    md5: str | None = None
    parents: tuple[str, ...] = ()
    trashed: bool = False
    created_time: str | None = None
    modified_time: str | None = None
    image_metadata: dict[str, Any] = field(default_factory=dict)
    video_metadata: dict[str, Any] = field(default_factory=dict)
    web_link: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    def as_metadata(self) -> dict[str, Any]:
        """Shape :func:`src.media.facts_from_drive` expects."""
        return {
            "imageMediaMetadata": self.image_metadata,
            "videoMediaMetadata": self.video_metadata,
            "createdTime": self.created_time,
        }


class Drive(Protocol):
    """What the pipeline needs from a Drive. Implemented twice: real, fake."""

    def list_children(self, folder_id: str) -> list[DriveFile]: ...

    def get_file(self, file_id: str) -> DriveFile: ...

    def ensure_folder(self, parent_id: str, name: str) -> str: ...

    def ensure_path(self, root_id: str, parts: tuple[str, ...]) -> str: ...

    def file_into(self, file_id: str, destination_id: str) -> str: ...

    def download(self, file_id: str, destination: Path) -> Path: ...

    def upload(self, path: Path, parent_id: str, name: str | None = None) -> DriveFile: ...

    def share_anyone_reader(self, file_id: str) -> str: ...


def walk_files(drive: Drive, folder_id: str) -> list[tuple[tuple[str, ...], DriveFile]]:
    """Every non-folder file under ``folder_id``, with its folder path.

    The path is relative to ``folder_id``, so a file dropped in a subfolder
    named after the event arrives as ``(("Horizon",), file)`` and a file
    dumped loose at the top arrives as ``((), file)`` — which is how the
    pipeline spots a card copied without an event name.

    Depth is bounded because a volunteer occasionally drags in a whole card
    with its DCIM tree, and we would rather walk it than ask them not to.
    """
    found: list[tuple[tuple[str, ...], DriveFile]] = []
    queue: list[tuple[tuple[str, ...], str]] = [((), folder_id)]
    seen: set[str] = {folder_id}

    while queue:
        path, current = queue.pop(0)
        for entry in drive.list_children(current):
            if entry.trashed:
                continue
            if entry.is_folder:
                if entry.id not in seen and len(path) < 8:
                    seen.add(entry.id)
                    queue.append((path + (entry.name,), entry.id))
            else:
                found.append((path, entry))
    return found


def _sleep_for_attempt(attempt: int) -> None:
    """Exponential backoff with jitter, so retries do not march in step."""
    delay = BACKOFF_BASE_SECS * (2 ** (attempt - 1))
    time.sleep(delay + random.uniform(0, delay * 0.1))


class GoogleDrive:
    """The real client.

    Constructed lazily: importing this module must not require the Google
    libraries, because the poller's tests and the dashboard's dev mode run
    without them.
    """

    # Full drive scope: the tool moves files it did not upload, which the
    # narrower drive.file scope does not permit.
    SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/drive",)

    def __init__(self, client_secret: Path, token_path: Path) -> None:
        self.client_secret = Path(client_secret)
        self.token_path = Path(token_path)
        self._service: Any | None = None
        self._folder_cache: dict[tuple[str, str], str] = {}

    # --- authentication ---------------------------------------------------

    def _credentials(self) -> Any:
        """Load the saved token, refreshing or re-authorising as needed.

        The browser sign-in happens once, on the club laptop, and writes
        token.json. After that the refresh token keeps it alive unattended.
        """
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self.token_path), list(self.SCOPES)
            )

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not self.client_secret.exists():
                raise DriveError(
                    f"No Google credentials at {self.client_secret}. "
                    "See the README section 'Connecting Google Drive'."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secret), list(self.SCOPES)
            )
            creds = flow.run_local_server(port=0)

        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    @property
    def service(self) -> Any:
        if self._service is None:
            from googleapiclient.discovery import build

            self._service = build(
                "drive", "v3", credentials=self._credentials(), cache_discovery=False
            )
        return self._service

    # --- retries ----------------------------------------------------------

    def _execute(self, request: Any) -> Any:
        """Run a Drive request, retrying the failures that are worth retrying.

        Rate limits and 5xx are transient and back off. A full Drive is not
        transient and raises immediately so the manager sees a useful error
        instead of the pipeline quietly stalling for twenty minutes.
        """
        from googleapiclient.errors import HttpError

        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return request.execute()
            except HttpError as exc:
                status = getattr(exc.resp, "status", 0)
                reason = str(exc)
                # A full Drive is not transient; a rate limit is.
                if "storageQuotaExceeded" in reason:
                    raise QuotaExceeded(
                        "Google Drive is out of space; free space and retry."
                    ) from exc
                if status in (403, 429) or status >= 500:
                    last = exc
                    log.warning(
                        "Drive %s on attempt %d/%d, backing off", status, attempt, MAX_ATTEMPTS
                    )
                    if attempt < MAX_ATTEMPTS:
                        _sleep_for_attempt(attempt)
                        continue
                    raise TransientDriveError(reason) from exc
                raise DriveError(reason) from exc
            except (TimeoutError, ConnectionError, OSError) as exc:
                last = exc
                if attempt < MAX_ATTEMPTS:
                    _sleep_for_attempt(attempt)
                    continue
                raise TransientDriveError(str(exc)) from exc

        raise TransientDriveError(str(last))

    # Every call needs this so the client works in a Shared Drive as well as
    # in My Drive; without it Drive pretends Shared Drive files do not exist.
    _ALL_DRIVES: dict[str, Any] = {"supportsAllDrives": True}

    # --- reading ----------------------------------------------------------

    def _to_file(self, payload: dict[str, Any]) -> DriveFile:
        return DriveFile(
            id=str(payload["id"]),
            name=str(payload.get("name", "")),
            mime_type=str(payload.get("mimeType", "")),
            size=int(payload.get("size") or 0),
            md5=payload.get("md5Checksum"),
            parents=tuple(payload.get("parents") or ()),
            trashed=bool(payload.get("trashed", False)),
            created_time=payload.get("createdTime"),
            modified_time=payload.get("modifiedTime"),
            image_metadata=dict(payload.get("imageMediaMetadata") or {}),
            video_metadata=dict(payload.get("videoMediaMetadata") or {}),
            web_link=payload.get("webViewLink"),
        )

    def list_children(self, folder_id: str) -> list[DriveFile]:
        """Direct children of a folder, following pagination to the end."""
        files: list[DriveFile] = []
        page_token: str | None = None
        while True:
            response = self._execute(
                self.service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields=f"nextPageToken, files({FILE_FIELDS})",
                    pageSize=200,
                    pageToken=page_token,
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                )
            )
            files.extend(self._to_file(item) for item in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return files

    def get_file(self, file_id: str) -> DriveFile:
        return self._to_file(
            self._execute(
                self.service.files().get(
                    fileId=file_id, fields=FILE_FIELDS, **self._ALL_DRIVES
                )
            )
        )

    # --- folders ----------------------------------------------------------

    def ensure_folder(self, parent_id: str, name: str) -> str:
        """Return the id of ``name`` under ``parent_id``, creating it if absent.

        Cached, because building the archive tree asks for the same handful
        of folders once per file and a 400-file event would otherwise spend
        most of its time re-asking Drive the same question.
        """
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]

        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        response = self._execute(
            self.service.files().list(
                q=(
                    f"'{parent_id}' in parents and name = '{escaped}' "
                    f"and mimeType = '{FOLDER_MIME}' and trashed = false"
                ),
                fields="files(id, name)",
                pageSize=1,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
        )
        existing = response.get("files", [])
        if existing:
            folder_id = str(existing[0]["id"])
        else:
            created = self._execute(
                self.service.files().create(
                    body={
                        "name": name,
                        "mimeType": FOLDER_MIME,
                        "parents": [parent_id],
                    },
                    fields="id",
                    **self._ALL_DRIVES,
                )
            )
            folder_id = str(created["id"])

        self._folder_cache[key] = folder_id
        return folder_id

    def ensure_path(self, root_id: str, parts: tuple[str, ...]) -> str:
        """Create a whole folder path, returning the id of the deepest folder."""
        current = root_id
        for part in parts:
            current = self.ensure_folder(current, part)
        return current

    # --- filing -----------------------------------------------------------

    def file_into(self, file_id: str, destination_id: str) -> str:
        """Put a file in its destination folder and return its id.

        Within one Drive this is a parent swap: Drive rewrites the parent
        list and no bytes move, which is why a night of RAW files can be
        filed in seconds. If the file turns out to live in another drive the
        swap is refused, and we fall back to a server-side copy — still no
        download, but it does consume storage.
        """
        current = self.get_file(file_id)
        if destination_id in current.parents:
            return file_id  # already filed; a retry after a partial run

        previous = ",".join(current.parents)
        try:
            self._execute(
                self.service.files().update(
                    fileId=file_id,
                    addParents=destination_id,
                    removeParents=previous,
                    fields="id, parents",
                    **self._ALL_DRIVES,
                )
            )
            return file_id
        except DriveError as exc:
            log.warning("Move refused for %s (%s); copying instead", current.name, exc)
            copied = self._execute(
                self.service.files().copy(
                    fileId=file_id,
                    body={"name": current.name, "parents": [destination_id]},
                    fields="id",
                    **self._ALL_DRIVES,
                )
            )
            return str(copied["id"])

    # --- bytes ------------------------------------------------------------

    def download(self, file_id: str, destination: Path) -> Path:
        """Fetch a file to disk. Only JPEGs take this path, for watermarking."""
        from googleapiclient.http import MediaIoBaseDownload

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")

        request = self.service.files().get_media(
            fileId=file_id, supportsAllDrives=True
        )
        with partial.open("wb") as handle:
            downloader = MediaIoBaseDownload(handle, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        # Rename only once the bytes are all there, so an interrupted
        # download can never be mistaken for a finished file.
        partial.replace(destination)
        return destination

    def upload(self, path: Path, parent_id: str, name: str | None = None) -> DriveFile:
        """Upload a local file, resumably, and return it as Drive sees it."""
        from googleapiclient.http import MediaFileUpload

        path = Path(path)
        media = MediaFileUpload(str(path), resumable=True, chunksize=8 * 1024 * 1024)
        request = self.service.files().create(
            body={"name": name or path.name, "parents": [parent_id]},
            media_body=media,
            fields=FILE_FIELDS,
            **self._ALL_DRIVES,
        )

        response = None
        attempt = 0
        while response is None:
            try:
                _, response = request.next_chunk()
            except Exception as exc:  # chunk failures are normal on campus wifi
                attempt += 1
                if attempt >= MAX_ATTEMPTS:
                    raise TransientDriveError(f"upload of {path.name} failed: {exc}") from exc
                _sleep_for_attempt(attempt)

        return self._to_file(response)

    # --- sharing ----------------------------------------------------------

    def share_anyone_reader(self, file_id: str) -> str:
        """Make a folder readable by link and return that link.

        Applied to a single event's gallery folder, never to the archive
        root: RAW, unmarked originals and held items sit outside it.
        """
        self._execute(
            self.service.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                **self._ALL_DRIVES,
            )
        )
        return self.get_file(file_id).web_link or (
            f"https://drive.google.com/drive/folders/{file_id}"
        )


def open_drive(cfg: dict[str, Any], project_root: Path) -> Drive:
    """Build the Drive client the settings ask for.

    ``drive_mode: fake`` swaps in the local-folder stand-in, which is how the
    whole pipeline can be run on a laptop with no credentials — useful when
    next year's maintainer wants to see what it does before touching the
    club's real archive.
    """
    if str(cfg.get("drive_mode", "google")).lower() == "fake":
        from src.fakedrive import FakeDrive

        root = Path(str(cfg.get("fake_drive_dir", "~/PhotoCircle/FAKEDRIVE"))).expanduser()
        return FakeDrive(root)

    return GoogleDrive(
        client_secret=project_root / str(cfg.get("oauth_client_secret", "client_secret.json")),
        token_path=project_root / str(cfg.get("oauth_token", "token.json")),
    )
