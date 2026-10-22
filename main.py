"""Entry point: one process, one native window, no server we manage."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import webview

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import Api, application, setup_logging  # noqa: E402
import library_db  # noqa: E402
import thumbs  # noqa: E402
import logging

WINDOW_TITLE = "Spotify Clone"
PID_FILE = ROOT / "spotify-clone.pid"
ERROR_LOG = ROOT / "startup-error.log"


def _write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")


def _log_startup_error() -> None:
    """pythonw has no console, so failures must land in a file."""
    try:
        ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    _write_pid()
    setup_logging()
    log = logging.getLogger("spotify")
    try:
        # Create the library table before any request or bridge call can touch it.
        library_db.init_db()
        # Reconcile stale downloads + orphaned .part files from previous sessions
        stats = library_db.reconcile_stale_downloads()
        log.info("startup reconcile %s", stats)

        # Warm the cover cache for everything already in the library, so a
        # single online boot is enough to keep artwork after going offline.
        try:
            for row in library_db.list_tracks():
                if row.get("video_id"):
                    thumbs.ensure_async(str(row["video_id"]))
        except Exception as exc:  # noqa: BLE001 - artwork is never fatal
            log.debug("thumbnail warm-up skipped: %s", exc)

        # The WSGI app goes in as the `url` argument: pywebview detects a
        # callable, runs it in a daemon thread in THIS process on a random
        # port, and points the window at it. `js_api` exposes the Python
        # methods to the GUI. No uvicorn, no port we manage.
        webview.create_window(
            WINDOW_TITLE,
            application,
            js_api=Api(),
            width=1180,
            height=780,
            min_size=(880, 560),
            maximized=True,
            background_color="#121212",
            text_select=False,
        )
        webview.start()
    except Exception:
        _log_startup_error()
        raise
    finally:
        PID_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
