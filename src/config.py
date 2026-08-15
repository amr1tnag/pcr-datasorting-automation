"""Settings loading and derived paths for Photo Circle.

A single ``settings.yaml`` at the project root drives every process. It is
written with commented defaults the first time anything imports this module,
so a fresh checkout is editable without hunting for keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
SETTINGS_PATH: Path = PROJECT_ROOT / "settings.yaml"

DEFAULTS: dict[str, Any] = {
    "watermark_path": "assets/watermark.png",
    "watermark_scale": 0.10,
    "watermark_opacity": 1.0,
    "watermark_margin": 0.025,
    "watermark_corner": "bottom-right",
    "watermark_shadow": False,
    "jpeg_quality": 92,
    "drive_root": "PhotoCircle",
    "drive_mode": "google",
    "drop_folder_id": "",
    "archive_folder_id": "",
    "oauth_client_secret": "client_secret.json",
    "oauth_token": "token.json",
    "fake_drive_dir": "~/PhotoCircle/FAKEDRIVE",
    "poll_interval_secs": 60,
    "stable_wait_secs": 2.0,
    "upload_workers": 3,
    "base_dir": "~/PhotoCircle",
}

DEFAULT_SETTINGS_YAML: str = """\
# Photo Circle settings. Delete a key to fall back to its built-in default.

# The club logo, as a transparent PNG. Drop it in at assets/watermark.png
# and this works as-is; point elsewhere if you keep it somewhere else.
watermark_path: assets/watermark.png

# Watermark width as a fraction of the photo's long edge. The club logo is
# square, so it covers far more of the frame than a wide logo would at the
# same number — 0.10 is about right for it, 0.16 suits a wordmark.
watermark_scale: 0.10

# Watermark alpha, 0.0 (invisible) to 1.0 (opaque). 1.0 puts the logo on
# exactly as drawn; lower it if the mark feels heavy over a busy photo.
watermark_opacity: 1.0

# Gap from the edge of the frame, same fraction of the long edge.
watermark_margin: 0.025

# bottom-right, bottom-left, top-right, top-left or bottom-centre.
watermark_corner: bottom-right

# Soft dark halo behind the mark. Off: the logo goes on exactly as drawn.
# Turn it on if a white mark starts disappearing against bright skies.
watermark_shadow: false

# JPEG quality for watermarked output, 1-95.
jpeg_quality: 92

# Name of the top-level folder created in Google Drive.
drive_root: PhotoCircle

# 'google' for the real Drive, 'fake' to sort into a local folder instead.
# Fake mode needs no credentials and is the safe way to try the tool out.
drive_mode: google

# The folder volunteers upload into. Copy the id out of its Drive URL:
# https://drive.google.com/drive/folders/<this bit>
drop_folder_id: ""

# The folder the sorted archive is built in. Must NOT be inside the drop
# folder, or the tool would find its own output and sort it again.
archive_folder_id: ""

# Google OAuth files, relative to the project folder. Both are secrets and
# are kept out of git.
oauth_client_secret: client_secret.json
oauth_token: token.json

# Where fake mode builds its tree.
fake_drive_dir: ~/PhotoCircle/FAKEDRIVE

# Seconds between checks of the drop folder.
poll_interval_secs: 60

# Seconds a file's size must stay unchanged before it counts as fully written.
stable_wait_secs: 2.0

# Parallel Drive upload threads.
upload_workers: 3

# Root for INBOX, STAGING, ORIGINALS, FAILED and the SQLite database.
base_dir: ~/PhotoCircle
"""


def _write_default_settings(path: Path) -> None:
    """Create a commented settings file. Never overwrites an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_SETTINGS_YAML, encoding="utf-8")


def load_config(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    """Return defaults merged with the user's settings file.

    Writes the default file first if it is missing. Unknown keys in the file
    are kept, so later stages can read their own settings without touching
    this module.
    """
    if not path.exists():
        _write_default_settings(path)

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        loaded = {}

    merged: dict[str, Any] = {**DEFAULTS, **loaded}
    return merged


CFG: dict[str, Any] = load_config()

BASE_DIR: Path = Path(str(CFG["base_dir"])).expanduser()
INBOX: Path = BASE_DIR / "INBOX"
STAGING: Path = BASE_DIR / "STAGING"
ORIGINALS: Path = BASE_DIR / "ORIGINALS"
FAILED: Path = BASE_DIR / "FAILED"
DB_PATH: Path = BASE_DIR / "photocircle.db"


def ensure_dirs() -> None:
    """Create every working directory. Safe to call repeatedly."""
    for directory in (BASE_DIR, INBOX, STAGING, ORIGINALS, FAILED):
        directory.mkdir(parents=True, exist_ok=True)


ensure_dirs()
