"""Background audio downloader — ONE network connection, play + save at once.

yt-dlp is used only to *resolve* the direct audio URL. A single ffmpeg process
then reads that URL and encodes straight to a growing MP3 in the library folder.
Because MP3 has no trailing index atom, Chromium can play the file while ffmpeg
is still writing it. When ffmpeg exits successfully the `.part` file is renamed
into place, so the stored file is always a complete, valid MP3.
"""

from __future__ import annotations

import io
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp

import library
import library_db

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"


def cookies_file() -> Optional[str]:
    try:
        from core import cookies_path

        return cookies_path()
    except Exception:  # noqa: BLE001
        return None


def _publish(part_path: Path, final_path: Path, attempts: int = 25) -> None:
    """Rename the finished .part into place.

    Windows refuses to rename a file that anybody still holds open (WinError
    32): the WebView2 audio element and our own reader both may. Wait for the
    downloader to go idle and for our readers to drain, then retry.
    """
    last: Optional[OSError] = None
    for attempt in range(attempts):
        try:
            part_path.replace(final_path)
            return
        except OSError as exc:  # WinError 32 = file in use
            last = exc
            try:
                import core

                if core.reader_count(str(part_path)) > 0:
                    time.sleep(0.2)
                    continue
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2 * min(attempt + 1, 5))
    if last is not None:
        raise last


class DownloadManager:
    def __init__(self, on_finished: Optional[Callable[[str, str, Path], None]] = None):
        self._on_finished = on_finished
        self._lock = threading.Lock()
        self._downloads: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ public
    def start(self, video_id: str, title: str, url: str, bitrate: int = 320, memory_mode: bool = False) -> Optional[Path]:
        """Resolve the audio URL and stream it into a growing MP3.

        Returns the path of the partial file the player should read from.
        If a completed file already exists, returns it immediately (cache hit).
        """
        library_db.init_db()
        track = library_db.get_or_create_track(video_id, title, url, bitrate)

        # Cache hit: completed file exists on disk
        if not memory_mode and track.get("status") == "mp3" and track.get("file_path"):
            existing = Path(track["file_path"])
            if existing.is_file() and existing.stat().st_size > 0:
                return existing

        # Check if download is already active (per-video lock)
        with self._lock:
            if video_id in self._downloads and self._downloads[video_id].get("status") == "downloading":
                if memory_mode or self._downloads[video_id].get("memory"):
                    return None
                return Path(self._downloads[video_id].get("part", ""))

        if memory_mode:
            with self._lock:
                self._downloads[video_id] = {
                    "status": "downloading",
                    "written": 0,
                    "total": 0,
                    "duration": 0,
                    "bitrate": bitrate,
                    "path": "",
                    "part": "",
                    "cancel": False,
                    "proc": None,
                    "started": time.time(),
                    "memory": True,
                    "buffer": io.BytesIO(),
                }
            library_db.update_track(video_id, status="downloading")
            thread = threading.Thread(
                target=self._run_memory,
                args=(video_id, title, url, bitrate),
                daemon=True,
                name=f"dl-{video_id}",
            )
            thread.start()
            return None

        final_path = self._reserve_path(video_id, title, "mp3")
        part_path = final_path.with_name(final_path.name + ".part")

        # Register the download BEFORE the slow network resolve. Otherwise a
        # concurrent /audio/<id> request sees is_active() == False and starts a
        # second download of the same file (two ffmpeg processes, one .part).
        with self._lock:
            self._downloads[video_id] = {
                "status": "downloading",
                "written": 0,
                "total": 0,
                "duration": 0,
                "bitrate": bitrate,
                "path": str(final_path),
                "part": str(part_path),
                "cancel": False,
                "proc": None,
                "started": time.time(),
            }
        library_db.update_track(video_id, file_path=str(final_path), status="downloading")

        thread = threading.Thread(
            target=self._run,
            args=(video_id, title, url, part_path, final_path, bitrate),
            daemon=True,
            name=f"dl-{video_id}",
        )
        thread.start()
        return part_path

    def get_progress(self, video_id: str) -> dict[str, Any]:
        with self._lock:
            rec = dict(self._downloads.get(video_id) or {"status": "unknown"})
        rec.pop("proc", None)
        rec.pop("buffer", None)
        if rec.get("memory"):
            # _run_memory maintains rec["written"] under lock as bytes arrive.
            pass
        elif rec.get("part"):
            try:
                rec["written"] = Path(rec["part"]).stat().st_size
            except OSError:
                pass
        return rec

    def part_path(self, video_id: str) -> Optional[Path]:
        with self._lock:
            rec = self._downloads.get(video_id)
        if not rec:
            return None
        if rec.get("memory"):
            return None
        return Path(rec["part"])

    def is_memory(self, video_id: str) -> bool:
        with self._lock:
            rec = self._downloads.get(video_id)
        return bool(rec) and rec.get("memory", False)

    def get_buffer(self, video_id: str) -> Optional[io.BytesIO]:
        with self._lock:
            rec = self._downloads.get(video_id)
        if rec and rec.get("buffer"):
            return rec["buffer"]
        return None

    def _buffer_bytes(self, video_id: str) -> Optional[bytes]:
        """Thread-safe snapshot of the in-memory buffer (copy, position untouched)."""
        with self._lock:
            rec = self._downloads.get(video_id)
            buf = rec.get("buffer") if rec else None
            snapshot = buf.getvalue() if buf is not None else None
        return snapshot

    def buffer_size(self, video_id: str) -> int:
        snapshot = self._buffer_bytes(video_id)
        return len(snapshot) if snapshot else 0

    def buffer_head(self, video_id: str, n: int) -> bytes:
        snapshot = self._buffer_bytes(video_id)
        return (snapshot or b"")[:n]

    def buffer_slice(self, video_id: str, offset: int, length: int) -> bytes:
        snapshot = self._buffer_bytes(video_id)
        if not snapshot:
            return b""
        return snapshot[offset:offset + length]

    def is_active(self, video_id: str) -> bool:
        with self._lock:
            rec = self._downloads.get(video_id)
        return bool(rec) and rec.get("status") == "downloading"

    def cancel(self, video_id: str) -> None:
        with self._lock:
            rec = self._downloads.get(video_id)
            if not rec:
                return
            rec["cancel"] = True
            proc = rec.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()

    def pause(self, video_id: str) -> None:
        self.cancel(video_id)

    # ----------------------------------------------------------------- private
    def _validate_output(self, path: Path) -> bool:
        """Validate that the output file is a complete, playable MP3 using ffprobe."""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode != 0:
                return False
            duration_str = result.stdout.decode("utf-8", "replace").strip()
            duration = float(duration_str) if duration_str else 0.0
            return duration > 0
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return False

    def _reserve_path(self, video_id: str, title: str, ext: str) -> Path:
        base = library.get_library_dir()
        name = library.sanitize_filename(title)
        candidate = base / f"{name}.{ext}"
        if self._taken_by_other(candidate, video_id):
            candidate = base / f"{name} [{video_id}].{ext}"
        return candidate

    def _taken_by_other(self, candidate: Path, video_id: str) -> bool:
        for track in library_db.list_tracks():
            if track["video_id"] == video_id:
                continue
            if track.get("file_path") and Path(track["file_path"]).name == candidate.name:
                return True
        return candidate.exists()

    def _resolve(self, url: str) -> dict[str, Any]:
        opts = {
            "format": AUDIO_FORMAT,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 30,
            "retries": 2,
        }
        cookie_file = cookies_file()
        if cookie_file:
            opts["cookiefile"] = cookie_file
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}

    def _build_cmd(
        self, direct: str, headers: dict[str, str], part_path: Path, bitrate: int
    ) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        cmd += ["-fflags", "nobuffer"]
        header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if v)
        if header_blob:
            cmd += ["-headers", header_blob]
        cookie_file = cookies_file()
        if cookie_file:
            try:
                cmd += ["-cookies", cookie_file]
            except Exception:  # noqa: BLE001
                pass
        cmd += [
            "-i", direct,
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", f"{bitrate}k",
            "-minrate", f"{bitrate}k",
            "-maxrate", f"{bitrate}k",
            "-bufsize", f"{bitrate * 2}k",
            "-write_id3v2", "1",
            "-id3v2_version", "3",
            "-flush_packets", "1",
            "-f", "mp3",
            str(part_path),
        ]
        return cmd

    def _build_cmd_memory(self, direct: str, headers: dict[str, str], bitrate: int) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        cmd += ["-fflags", "nobuffer"]
        header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if v)
        if header_blob:
            cmd += ["-headers", header_blob]
        cookie_file = cookies_file()
        if cookie_file:
            try:
                cmd += ["-cookies", cookie_file]
            except Exception:  # noqa: BLE001
                pass
        cmd += [
            "-i", direct,
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", f"{bitrate}k",
            "-minrate", f"{bitrate}k",
            "-maxrate", f"{bitrate}k",
            "-bufsize", f"{bitrate * 2}k",
            "-write_id3v2", "1",
            "-id3v2_version", "3",
            "-flush_packets", "1",
            "-f", "mp3",
            "-",
        ]
        return cmd

    def _validate_buffer(self, buffer: io.BytesIO) -> bool:
        import tempfile
        buffer.seek(0)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(buffer.read())
            tmp_path = tmp.name
        buffer.seek(0)
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", tmp_path],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode != 0:
                return False
            duration_str = result.stdout.decode("utf-8", "replace").strip()
            return float(duration_str) > 0 if duration_str else False
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return False
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _run_memory(self, video_id: str, title: str, url: str, bitrate: int) -> None:
        proc: Optional[subprocess.Popen] = None
        try:
            info = self._resolve(url)
            direct = info.get("url") or ""
            headers = info.get("http_headers") or {}
            duration = int(info.get("duration") or 0)

            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None:
                    rec["duration"] = duration
                    rec["total"] = int(duration * bitrate * 1000 / 8) if duration else 0

            if not direct:
                raise RuntimeError("could not resolve a direct audio URL")

            cmd = self._build_cmd_memory(direct, headers, bitrate)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None:
                    rec["proc"] = proc

            buffer = io.BytesIO()
            stream = proc.stdout
            if stream is None:
                raise RuntimeError("ffmpeg stdout not available")
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                buffer.write(chunk)
                with self._lock:
                    rec = self._downloads.get(video_id)
                    if rec is not None:
                        rec["written"] = buffer.tell()

            stderr = proc.communicate()[1]
            code = proc.returncode

            with self._lock:
                rec = self._downloads.get(video_id)
                cancelled = bool(rec and rec.get("cancel"))
            if cancelled:
                with self._lock:
                    if video_id in self._downloads:
                        self._downloads[video_id]["status"] = "cancelled"
                return

            if code != 0 or buffer.tell() == 0:
                detail = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
                raise RuntimeError(detail or f"ffmpeg exited with code {code}")

            if not self._validate_buffer(buffer):
                raise RuntimeError("ffprobe validation failed: output is not a valid MP3")

            final_size = len(buffer.getvalue())
            buffer.seek(0)
            with self._lock:
                if video_id in self._downloads:
                    self._downloads[video_id]["status"] = "done"
                    self._downloads[video_id]["buffer"] = buffer
                    self._downloads[video_id]["written"] = final_size

            library_db.set_status(video_id, "memory", file_size=buffer.tell())
            if self._on_finished:
                self._on_finished(video_id, title, None)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if video_id in self._downloads:
                    self._downloads[video_id]["status"] = "failed"
                    self._downloads[video_id]["error"] = str(exc)[:300]
            library_db.update_track(video_id, status="failed")
            print(f"[downloader] {video_id} failed: {exc}")
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None:
                    rec["proc"] = None

    def _run(
        self,
        video_id: str,
        title: str,
        url: str,
        part_path: Path,
        final_path: Path,
        bitrate: int,
    ) -> None:
        proc: Optional[subprocess.Popen] = None
        try:
            info = self._resolve(url)
            direct = info.get("url") or ""
            headers = info.get("http_headers") or {}
            duration = int(info.get("duration") or 0)

            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None:
                    rec["duration"] = duration
                    rec["total"] = int(duration * bitrate * 1000 / 8) if duration else 0

            if not direct:
                raise RuntimeError("could not resolve a direct audio URL")
            cmd = self._build_cmd(direct, headers, part_path, bitrate)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None:
                    rec["proc"] = proc

            stderr = proc.communicate()[1]
            code = proc.returncode

            with self._lock:
                rec = self._downloads.get(video_id)
                cancelled = bool(rec and rec.get("cancel"))
            if cancelled:
                with self._lock:
                    if video_id in self._downloads:
                        self._downloads[video_id]["status"] = "cancelled"
                return

            if code != 0 or not part_path.exists() or part_path.stat().st_size == 0:
                detail = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
                raise RuntimeError(detail or f"ffmpeg exited with code {code}")

            if not self._validate_output(part_path):
                raise RuntimeError("ffprobe validation failed: output is not a valid MP3")

            _publish(part_path, final_path)
            with self._lock:
                if video_id in self._downloads:
                    self._downloads[video_id]["status"] = "done"
            library_db.set_status(video_id, "mp3", str(final_path), file_size=final_path.stat().st_size)
            if self._on_finished:
                self._on_finished(video_id, title, final_path)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if video_id in self._downloads:
                    self._downloads[video_id]["status"] = "failed"
                    self._downloads[video_id]["error"] = str(exc)[:300]
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass
            library_db.update_track(video_id, status="failed")
            print(f"[downloader] {video_id} failed: {exc}")
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None:
                    rec["proc"] = None