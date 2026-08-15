"""Run the pipeline: look for new files, file them, print the gallery links.

    py run.py            keep running, checking every minute
    py run.py --once     one pass and stop, which is what the tests do

One process rather than three. The database is built to allow more, but a
club laptop running a single loop is easier to start, easier to see, and
easier for next year's maintainer to reason about than a set of services
that can each be separately dead.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from src import config, db, drive as drive_module, poller, worker

LOG_FORMAT = "%(asctime)s  %(levelname)-7s %(message)s"


def setup_logging(verbose: bool = False) -> None:
    """Log to the console and to a file next to the database."""
    config.BASE_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.BASE_DIR / "photocircle.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        handlers=handlers,
    )
    # The Google client logs a wall of noise at INFO.
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)


def one_pass(drive: drive_module.Drive, cfg: dict, project_root: Path) -> int:
    """Scan, then work the queue. Returns the number of problems worth seeing."""
    log = logging.getLogger("run")

    scan = poller.scan(drive, cfg, staging=config.STAGING)
    log.info("Scan: %s", scan.summary())
    for problem in scan.errors:
        log.error("Scan problem: %s", problem)

    work = worker.Worker(drive, cfg, project_root, config.STAGING).run()
    log.info("Work: %s", work.summary())
    for problem in work.errors:
        log.error("Work problem: %s", problem)

    for event, link in work.links.items():
        log.info("GALLERY LINK  %s  ->  %s", event, link)

    if scan.held:
        log.info(
            "%d file(s) held for review. Open the dashboard to sort them out.",
            scan.held,
        )

    return len(scan.errors) + len(work.errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Photo Circle media sorter")
    parser.add_argument("--once", action="store_true", help="one pass, then stop")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    log = logging.getLogger("run")

    project_root = Path(__file__).resolve().parent
    cfg = config.load_config()
    config.ensure_dirs()
    db.init_db()

    drive = drive_module.open_drive(cfg, project_root)
    interval = float(cfg.get("poll_interval_secs", 60))

    if args.once:
        return 1 if one_pass(drive, cfg, project_root) else 0

    log.info("Watching the drop folder every %.0f seconds. Ctrl-C to stop.", interval)
    while True:
        try:
            one_pass(drive, cfg, project_root)
        except KeyboardInterrupt:
            log.info("Stopped.")
            return 0
        except Exception:
            # A loop that dies at 10pm is the failure this tool exists to
            # prevent. Log it and try again on the next tick.
            log.exception("Unexpected error; carrying on")

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("Stopped.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
