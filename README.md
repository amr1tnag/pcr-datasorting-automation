# Photo Circle media sorter

Volunteers drop a card into a Google Drive folder named after the event.
A laptop in the club room notices, sorts everything, watermarks the photos,
files the RAW and video, and produces a link to share. Nobody has to decide
to make that happen.

**If you have just inherited this, read [Taking this over](#taking-this-over)
at the bottom first.**

---

## What it does with a night's shoot

A volunteer uploads into the drop folder:

```
DROP/
  horizon/                     <- they type the event name, that is all
    IMG_0001.JPG  IMG_0001.CR2  C0001.MP4  ... 400 more
```

Within a minute the archive looks like this:

```
ARCHIVE/2026/Horizon/
  photos/                      <- the only folder that gets shared
    CanonEOSR6/    landscape/ portrait/ square/
    NikonZ6/       landscape/ portrait/ square/
    Phone-iPhone13Pro/ portrait/
  raw/       CanonEOSR6/       <- kept, never watermarked
  video/     CanonEOSR6/       <- kept, never watermarked
  _originals/                  <- unmarked copies of every photo
```

and the link to `photos/` is in the log, ready to paste into WhatsApp.

Note the volunteer typed `horizon` and the archive says `Horizon`. Casing,
spacing and stray punctuation are normalised so one event does not become
three folders.

## The two rules everything else follows

**1. No original is ever deleted or modified.** There is no delete call
anywhere in this project, and a test fails if someone adds one. Photos are
watermarked into a *new* file; the unmarked original is kept in
`_originals/`. Emptying the drop folder is a human action, taken after the
archive copy has been checksum-verified.

**2. The normal path needs no human.** Files the tool is sure about publish
themselves. Only the doubtful ones wait, and they wait *in the drop folder*,
untouched, until someone decides.

## What gets held for review

Everything else is published automatically. A file is held only when:

| Reason | What happened |
|---|---|
| `no camera metadata` | The camera wrote nothing usable, and reading the file's own EXIF did not help either |
| `no usable dimensions` | Nothing could tell us whether it is landscape or portrait |
| `no event folder` | It was dropped loose instead of into a folder named after the event |
| an upload failed | Campus wifi, a full Drive, a dropped connection |

Duplicates are *not* held — the same card copied twice is recognised and
skipped. Phone footage is not held either; it files under `Phone-<model>`.
Sidecars and card litter (`Thumbs.db`, `.XMP`, `.DS_Store`) are ignored
silently.

---

## Running it

Two programs. Both are started by double-clicking a `.bat` file in this
folder.

| | |
|---|---|
| `start-sorter.bat` | The sorter. Leave it running. This is the one that matters. |
| `start-dashboard.bat` | The manager's screen, at <http://127.0.0.1:5000> |
| `install.bat` | One-time setup: installs the Python packages |

The sorter checks the drop folder every 60 seconds, logs what it did, and
keeps going if something goes wrong. Closing the window stops it; nothing is
lost, it picks up where it left off.

### First-time setup on a new laptop

1. **Install Python 3.11 or newer** from <https://python.org>, ticking
   *"Add Python to PATH"* during the install.

2. **Get the code**: download this repository as a ZIP and unzip it, or
   `git clone` it if you know how.

3. **Install what it needs.** Open the folder, type `cmd` in the address
   bar, press Enter, and run:

   ```
   py -m pip install -r requirements.txt
   ```

4. **Put the club logo** at `assets/watermark.png` — a transparent PNG.

5. **Connect Google Drive** (see below).

6. **Fill in `settings.yaml`** — it appears the first time you run anything,
   with every option commented.

7. Double-click `start-sorter.bat`.

### Connecting Google Drive

The tool signs in once, as the club account, and remembers.

1. Go to <https://console.cloud.google.com>, make a project (any name).
2. **APIs & Services → Library → Google Drive API → Enable.**
3. **APIs & Services → Credentials → Create credentials → OAuth client ID →
   Desktop app.**
4. Download the JSON, rename it `client_secret.json`, put it in this folder.
5. Run `start-sorter.bat`. A browser opens; sign in as the club account and
   allow access. It writes `token.json` and never asks again.

`client_secret.json` and `token.json` are secrets. They are already in
`.gitignore` — do not commit them, and do not put them in the club's shared
Drive.

### The two folder ids

Open each folder in Drive and copy the id out of its address bar:

```
https://drive.google.com/drive/folders/1aB2cD3eF4gH5iJ6kL
                                       ^^^^^^^^^^^^^^^^^^ this
```

Put them in `settings.yaml` as `drop_folder_id` and `archive_folder_id`.

> **The archive folder must not be inside the drop folder.** If it is, the
> tool finds its own output on the next pass and sorts it again, forever.

Share the *drop* folder as **Anyone with the link → Editor** so volunteers
need no account. Never share the archive folder that way — the tool shares
each event's `photos/` folder by itself, and that is the only link that
should be handed out.

### Trying it without touching the real Drive

Set `drive_mode: fake` in `settings.yaml`. The tool then treats a local
folder as if it were Drive, so you can drop files in, watch them sort, and
open the result in Explorer. Nothing touches the club's archive and no
credentials are needed. This is the safe way to learn what it does.

### Starting automatically when the laptop boots

1. Press Start, type **Task Scheduler**, open it.
2. **Create Basic Task** → name it *Photo Circle sorter*.
3. Trigger: **When the computer starts**.
4. Action: **Start a program** → browse to `start-sorter.bat`.
5. Finish, then find the task in the list, right-click → **Properties** →
   tick **Run whether user is logged on or not**.

Repeat for `start-dashboard.bat` if the manager wants it always available.

---

## Settings worth knowing

Everything lives in `settings.yaml`, which is written with comments the
first time the tool runs. The ones people actually change:

| Setting | Does what |
|---|---|
| `watermark_scale` | Mark width as a fraction of the photo's long edge. `0.10` suits a square logo, `0.16` a wide one. |
| `watermark_opacity` | `1.0` puts the logo on exactly as drawn. |
| `watermark_corner` | `bottom-right` by default. |
| `watermark_shadow` | Off. Turn it on if the white logo disappears against bright skies. |
| `poll_interval_secs` | How often the drop folder is checked. |
| `jpeg_quality` | 92. Higher means bigger files. |

> **`settings.yaml` beats the built-in defaults.** Updating the code does
> *not* change a setting on a laptop that already has the file. If a default
> moves and you want it, edit the file — or delete it and let it be written
> again.

---

## When something goes wrong

**Nothing is happening.** Is `start-sorter.bat` still running? Check
`photocircle.log`, next to the database in the folder named by `base_dir`.

**Everything is being held for review.** Almost always the logo is missing
or `assets/watermark.png` is misnamed — the tool refuses to publish photos
unmarked. The log says so explicitly.

**"Google Drive is out of space."** Someone has to free space or buy more.
Nothing is lost: every file stays in the drop folder and retries from the
dashboard once there is room.

**Files failed overnight.** Open the dashboard → **Failed** → tick them →
**Try these again**. Retrying is always safe.

**A camera made a new folder for itself after a firmware update.** Dashboard
→ **Cameras** → map the new name onto the old one. Future files follow it;
files already filed stay put.

**The tool sorted something into the wrong place.** Move it in Drive. The
tool never revisits a file it has already filed.

---

## How the code is laid out

Nine small files. Read them in this order:

| File | What it is |
|---|---|
| `src/config.py` | Settings and where things live on disk |
| `src/db.py` | Every SQL statement in the project, in one place |
| `src/media.py` | Pure decisions: what a file is, which camera, which way up |
| `src/drive.py` | The only code that talks to Google |
| `src/fakedrive.py` | The same interface backed by folders, for tests and dry runs |
| `src/watermark.py` | Putting the mark on |
| `src/poller.py` | Finds new files, decides, records. Changes nothing. |
| `src/worker.py` | The only thing that moves or uploads anything |
| `src/dashboard.py` | The manager's screen |

`run.py` and `dashboard.py` at the top level are the two entry points.

### Running the tests

```
py -m pytest
```

They need no credentials, no network and no Drive — the whole suite runs
against `fakedrive.py` in a couple of seconds. **If you change anything, run these first.**
They encode the rules that matter, including the ones that would otherwise
be learned by losing an event's photos.

---

## Taking this over

Some things that will not be obvious:

**The tests are the specification.** `tests/test_pipeline.py` runs the whole
tool against a fake Drive and asserts the behaviour the club actually needs.
If you are unsure whether something is deliberate, look for the test — most
awkward-looking decisions have one explaining themselves.

**Resist adding features.** This is not a photo editor, and the review queue
is not a place to browse photos. If it becomes pleasant to sort files by
hand in the dashboard, people will, and the automation dies. Every manual
step has to justify itself.

**Do not add a delete.** Not to tidy the drop folder, not to clean up
duplicates, not "just for the failed ones". The one unrecoverable failure
this project can have is losing an event's photos. Everything else is a
delay.

**Keep it boring.** No async, no queue broker, no framework. Someone who has
written one Python script should be able to read all of it in an afternoon.
That is a feature, and it is the reason it will still work next year.
