"""Background audio downloader using yt-dlp.

Downloads the best audio (m4a/AAC) to the library folder, resuming partial
files, and reports progress. When a download finishes it calls `on_finished`
so the caller can transcode to MP3 in the background.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp

import library
import library_db

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"


class DownloadManager:
    def __init__(self, on_finished: Optional[Callable[[str, str, Path], None]] = None):
        self._on_finished = on_finished
        self._lock = threading.Lock()
        self._downloads: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ public
    def start(self, video_id: str, title: str, url: str, bitrate: int = 320) -> Path:
        """Start (or resume) a download. Returns the partial file path."""
        library_db.add_track(video_id, title, url, bitrate)
        path = self._reserve_path(video_id, title, "m4a")
        expected = self._expected_size(url)

        with self._lock:
            self._downloads[video_id] = {
                "status": "downloading",
                "written": 0,
                "total": expected,
                "path": str(path),
                "cancel": False,
            }
        library_db.update_track(video_id, file_path=str(path), status="downloading")

        thread = threading.Thread(
            target=self._run, args=(video_id, title, url, path, expected), daemon=True, name=f"dl-{video_id}"
        )
        thread.start()
        return path

    def get_progress(self, video_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._downloads.get(video_id, {"status": "unknown"}))

    def pause(self, video_id: str) -> None:
        with self._lock:
            if video_id in self._downloads:
                self._downloads[video_id]["cancel"] = True

    def cancel(self, video_id: str) -> None:
        self.pause(video_id)

    def is_active(self, video_id: str) -> bool:
        with self._lock:
            return self._downloads.get(video_id, {}).get("status") == "downloading"

    # ----------------------------------------------------------------- private
    def _reserve_path(self, video_id: str, title: str, ext: str) -> Path:
        """Pick a free path, disambiguating collisions with the video id."""
        base = library.get_library_dir()
        name = library.sanitize_filename(title)
        candidate = base / f"{name}.{ext}"
        if self._path_taken_by_other(candidate, video_id):
            candidate = base / f"{name} [{video_id}].{ext}"
        return candidate

    def _path_taken_by_other(self, candidate: Path, video_id: str) -> bool:
        for track in library_db.list_tracks():
            if track["video_id"] == video_id:
                continue
            if track.get("file_path") and Path(track["file_path"]).name == candidate.name:
                return True
        return candidate.exists()

    def _expected_size(self, url: str) -> int:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 30}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            return int(info.get("filesize") or info.get("filesize_approx") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _run(self, video_id: str, title: str, url: str, path: Path, expected: int) -> None:
        def hook(d: dict[str, Any]) -> None:
            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is None:
                    return
                if rec.get("cancel"):
                    raise _Cancelled
                if d.get("status") == "downloading":
                    rec["written"] = int(d.get("bytes_written") or 0)
                    rec["total"] = int(d.get("total_bytes") or expected or rec["total"])

        opts = {
            "format": AUDIO_FORMAT,
            "outtmpl": str(path),
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "continuedl": True,
            "progress_hooks": [hook],
            "cookiefile": _cookies(),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            with self._lock:
                if video_id in self._downloads:
                    self._downloads[video_id]["status"] = "done"
            library_db.set_status(video_id, "m4a", str(path))
            if self._on_finished:
                self._on_finished(video_id, title, path)
        except _Cancelled:
            with self._lock:
                if video_id in self._downloads:
                    self._downloads[video_id]["status"] = "paused"
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if video_id in self._downloads:
                    self._downloads[video_id]["status"] = "failed"
            library_db.set_status(video_id, "failed", str(path))
            library_db.update_track(video_id, status="failed")
            print(f"[downloader] {video_id} failed: {exc}")


class _Cancelled(Exception):
    pass


def _cookies() -> Optional[str]:
    try:
        from core import cookies_path

        return cookies_path()
    except Exception:  # noqa: BLE001
        return None
