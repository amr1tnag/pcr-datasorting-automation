# assets

The club watermark lives here, as **`watermark.png`** — a transparent PNG,
ideally at least 1000px wide so it stays sharp on a full-size photo.

Drop it in this folder with exactly that name and the tool picks it up. No
code change is needed. To use a different name or location, point
`watermark_path` in `settings.yaml` at it instead.

Until the file is here, the worker refuses to publish photos and holds them
for review rather than uploading them unmarked. That is deliberate: a
gallery of unwatermarked photos is harder to undo than a delayed one.

This README also keeps the folder alive in git, which does not track empty
directories — deleting it would take `assets/` with it.
