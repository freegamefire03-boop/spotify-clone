"""Background audio downloader — ONE network connection, play + save at once.

yt-dlp is used only to *resolve* the direct audio URL. A single ffmpeg process
then reads that URL and encodes straight to a growing MP3 in the library folder.
Because MP3 has no trailing index atom, Chromium can play the file while ffmpeg
is still writing it. When ffmpeg exits successfully the `.part` file is renamed
into place, so the stored file is always a complete, valid MP3.
"""

from __future__ import annotations

import io
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp

import library
import library_db
import thumbs

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"

log = logging.getLogger("spotify.downloader")

FG_PRIORITY = 0  # foreground lane: current track, never waits behind prefetch
BG_PRIORITY = 1  # background lane: queue prefetch


@dataclass
class AudioJob:
    """One encode job for the long-lived worker lanes."""

    video_id: str
    title: str
    url: str
    bitrate: int
    memory_mode: bool
    part_path: Optional[Path]  # None for memory jobs
    final_path: Optional[Path]  # None for memory jobs
    priority: int  # FG_PRIORITY or BG_PRIORITY
    gen: int = 0  # download generation: stale queued jobs drop at dispatch

# Uncapped downloading (wifi-first): the pump reads ffmpeg stdout as fast as
# the connection allows until EOF. No ahead-of-playhead cap.
# Stall watchdog: first-byte grace after spawn, then any inter-byte gap this
# long with zero new bytes (while unpaused/uncancelled) fails the job so a
# stuck ffmpeg can never wedge a worker lane forever.
FIRST_BYTE_GRACE_SEC = 30.0
STALL_TIMEOUT_SEC = 10.0
PAUSE_POLL_SEC = 0.2
# Upper bound for the (blocking) direct-URL resolve step. The resolve runs in
# a short-lived daemon thread per job; exceeding this frees the lane while a
# generation token stops the late result from spawning ffmpeg afterwards.
RESOLVE_TIMEOUT_SEC = 60.0


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
    def __init__(self, on_finished: Optional[Callable[[str, str, Path], None]] = None, max_workers: int = 3):
        self._on_finished = on_finished
        self._lock = threading.Lock()
        self._downloads: dict[str, dict[str, Any]] = {}
        # Tombstones for ids deleted by the user: a job already sitting in a
        # lane queue must be dropped, and an encoder that finishes right after
        # the delete must not publish the file or re-create the DB row.
        self._purged: dict[str, float] = {}
        self._max_workers = max(1, int(max_workers or 3))
        self._fg_queue: queue.Queue[AudioJob] = queue.Queue()
        self._bg_queue: queue.Queue[AudioJob] = queue.Queue()
        self._workers: list[threading.Thread] = []
        # Lane split: 1 foreground worker, remainder background. Current-track
        # jobs never queue behind prefetches. Total <= max_workers.
        n_bg = max(1, self._max_workers - 1)
        self._start_lane(self._fg_queue, 1, "fg")
        self._start_lane(self._bg_queue, n_bg, "bg")

    def _start_lane(self, q: queue.Queue[AudioJob], count: int, lane: str) -> None:
        for i in range(count):
            t = threading.Thread(
                target=self._worker_loop, args=(q,), daemon=True,
                name=f"dl-worker-{lane}-{i}",
            )
            t.start()
            self._workers.append(t)

    def _worker_loop(self, q: queue.Queue[AudioJob]) -> None:
        """Long-lived worker: blocks on the queue when idle, never respawns."""
        while True:
            job = q.get()
            try:
                self._dispatch(job)
            except Exception as exc:  # noqa: BLE001 - a worker must never die
                log.warning("worker error %s: %s", job.video_id, exc)
            finally:
                q.task_done()

    def _dispatch(self, job: AudioJob) -> None:
        """Run one job on a worker thread (existing pump code, verbatim)."""
        if self.is_purged(job.video_id):
            # Deleted while queued: nothing to cancel, nothing to write.
            return
        with self._lock:
            rec = self._downloads.get(job.video_id)
            if rec is None or rec.get("cancel"):
                if rec is not None:
                    rec["status"] = "cancelled"
                try:
                    library_db.set_status(job.video_id, "cancelled")
                except Exception:  # noqa: BLE001 - DB write must not kill a worker
                    pass
                return
            if int(rec.get("gen") or 0) != job.gen:
                # Superseded while queued (a newer start() re-registered this
                # id): drop quietly. The newer generation has its own job.
                return
        if job.memory_mode:
            self._run_memory(job.video_id, job.title, job.url, job.bitrate, job.gen)
        else:
            assert job.part_path is not None and job.final_path is not None
            self._run(job.video_id, job.title, job.url, job.part_path, job.final_path, job.bitrate, job.gen)

    def _enqueue(self, job: AudioJob) -> None:
        (self._fg_queue if job.priority == FG_PRIORITY else self._bg_queue).put(job)

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def lane_status(self) -> dict[str, int]:
        """Queue depths per lane (observability: wedged lanes are visible)."""
        try:
            fg = self._fg_queue.qsize()
        except NotImplementedError:
            fg = -1
        try:
            bg = self._bg_queue.qsize()
        except NotImplementedError:
            bg = -1
        return {"fg_queued": fg, "bg_queued": bg, "workers": len(self._workers)}

    # ------------------------------------------------------------------ public
    def start(
        self,
        video_id: str,
        title: str,
        url: str,
        bitrate: int = 320,
        memory_mode: bool = False,
        priority: int = FG_PRIORITY,
        channel: str = "",
    ) -> Optional[Path]:
        """Resolve the audio URL and stream it into a growing MP3.

        Returns the path of the partial file the player should read from.
        If a completed file already exists, returns it immediately (cache hit).
        Work runs on the long-lived worker lanes (priority routes to the
        foreground or background lane); registration still happens here so
        concurrent duplicate starts are absorbed before any job is queued.
        """
        library_db.init_db()
        with self._lock:
            # A real re-play clears the tombstone: same id, new intent.
            self._purged.pop(video_id, None)
        try:
            thumbs.ensure_async(video_id)
        except Exception:  # noqa: BLE001 - artwork is optional
            pass
        track = library_db.get_or_create_track(
            video_id, title, url, bitrate, channel=str(channel or "")[:200])

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
                prev = self._downloads.get(video_id)
                gen = int((prev.get("gen") if prev else 0) or 0) + 1
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
                    "playhead": 0.0,
                    "gated": False,
                    "paused": False,
                    "gen": gen,
                }
            library_db.update_track(video_id, status="downloading")
            self._enqueue(AudioJob(video_id, title, url, bitrate, True, None, None, priority, gen))
            return None

        final_path = self._reserve_path(video_id, title, "mp3")
        part_path = final_path.with_name(final_path.name + ".part")

        # Register the download BEFORE the slow network resolve. Otherwise a
        # concurrent /audio/<id> request sees is_active() == False and starts a
        # second download of the same file (two ffmpeg processes, one .part).
        with self._lock:
            prev = self._downloads.get(video_id)
            gen = int((prev.get("gen") if prev else 0) or 0) + 1
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
                "playhead": 0.0,
                    "gated": False,
                    "paused": False,
                    "gen": gen,
                }
        library_db.update_track(video_id, file_path=str(final_path), status="downloading")

        self._enqueue(AudioJob(video_id, title, url, bitrate, False, part_path, final_path, priority, gen))
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
        bitrate = int(rec.get("bitrate") or 0)
        bps = bitrate * 1000 / 8 if bitrate > 0 else 0
        rec["playhead"] = float(rec.get("playhead") or 0.0)
        rec["buffered_sec"] = (int(rec.get("written") or 0) / bps) if bps > 0 else 0.0
        rec["gated"] = False  # pacing removed; key kept for GUI compat
        rec["paused"] = bool(rec.get("paused"))
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

    def report_playhead(self, video_id: str, seconds: float) -> dict[str, Any]:
        """Record the player's playhead (stored only; downloads are uncapped)."""
        try:
            seconds = max(0.0, float(seconds))
        except (TypeError, ValueError):
            seconds = 0.0
        with self._lock:
            rec = self._downloads.get(video_id)
            if rec is None:
                return {"gated": False}
            rec["playhead"] = seconds
            written = int(rec.get("written") or 0)
            bitrate = int(rec.get("bitrate") or 0)
        bps = bitrate * 1000 / 8 if bitrate > 0 else 0
        secured = written / bps if bps > 0 else 0.0
        return {"gated": False, "secured": secured, "target": seconds}

    def _job_live(self, video_id: str, token: int) -> bool:
        """True if this job generation is still current and uncancelled."""
        with self._lock:
            rec = self._downloads.get(video_id)
            if rec is None or rec.get("cancel"):
                return False
            return int(rec.get("gen") or 0) == token

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

    def purge(self, video_id: str) -> None:
        """Forget a permanently deleted track.

        Drops a job that is still waiting in a lane queue, terminates the
        encoder, and frees memory-mode buffers. The tombstone makes a worker
        that is already mid-download stop before it publishes the file or
        writes the DB row again.
        """
        vid = str(video_id or "")
        if not vid:
            return
        now = time.time()
        with self._lock:
            self._purged[vid] = now
            if len(self._purged) > 4096:
                cutoff = now - 6 * 3600
                self._purged = {k: t for k, t in self._purged.items() if t >= cutoff}
            rec = self._downloads.pop(vid, None)
            if rec is not None:
                rec["cancel"] = True
                rec["status"] = "cancelled"
        if rec is None:
            return
        proc = rec.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001 - deletion must never fail here
                pass
        buffer = rec.get("buffer")
        if buffer is not None:
            try:
                buffer.close()
            except Exception:  # noqa: BLE001
                pass

    def is_purged(self, video_id: str) -> bool:
        with self._lock:
            return str(video_id or "") in self._purged

    def set_paused(self, video_id: str, paused: bool) -> dict[str, Any]:
        """Freeze/unfreeze a download in place (never abandons progress).

        The pump loops notice within PAUSE_POLL_SEC and stop reading ffmpeg
        stdout (pipe backpressure stalls ffmpeg) until resumed. Partial file,
        buffer, DB row, and process are left untouched. Paused time never
        trips the stall watchdog.
        """
        with self._lock:
            rec = self._downloads.get(video_id)
            if rec is None or rec.get("status") != "downloading":
                return {"ok": False, "reason": "not-downloading"}
            rec["paused"] = bool(paused)
            return {"ok": True, "paused": bool(paused), "gated": False}

    def pause_download(self, video_id: str) -> dict[str, Any]:
        return self.set_paused(video_id, True)

    def resume_download(self, video_id: str) -> dict[str, Any]:
        return self.set_paused(video_id, False)

    def is_paused(self, video_id: str) -> bool:
        with self._lock:
            rec = self._downloads.get(video_id)
        return bool(rec) and rec.get("paused", False)

    def has_active_downloads(self) -> bool:
        with self._lock:
            return any(
                r.get("status") == "downloading" for r in self._downloads.values()
            )

    def pause(self, video_id: str) -> None:
        self.set_paused(video_id, True)

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

    def _resolve_guarded(self, video_id: str, token: int, url: str) -> dict[str, Any]:
        """Run _resolve with a hard deadline so a hung extractor can't pin a lane.

        The blocking yt-dlp call runs in a disposable daemon thread; if it
        exceeds RESOLVE_TIMEOUT_SEC the lane is freed and the job fails. The
        generation token ensures a late-returning resolve (or a resolve that
        finished after cancel) can never spawn ffmpeg afterwards — the worker
        re-checks liveness before Popen.
        """
        box: dict[str, Any] = {}
        done = threading.Event()

        def _target() -> None:
            try:
                box["info"] = self._resolve(url)
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        t = threading.Thread(target=_target, daemon=True,
                             name=f"dl-resolve-{video_id}")
        t.start()
        if not done.wait(RESOLVE_TIMEOUT_SEC):
            raise RuntimeError(
                f"audio resolve timed out after {int(RESOLVE_TIMEOUT_SEC)}s")
        if not self._job_live(video_id, token):
            raise RuntimeError("download superseded before resolve completed")
        if "error" in box:
            raise box["error"]
        return box.get("info") or {}

    def _watch_stall(self, video_id: str, token: int,
                     stop: threading.Event) -> None:
        """Kill a stalled ffmpeg so the pump's blocking read unblocks.

        Runs alongside the pump loop (which a monitor cannot replace: on
        Windows, select() doesn't work on pipes). Watches `rec["written"]`:
        zero bytes past FIRST_BYTE_GRACE_SEC, or no new bytes for
        STALL_TIMEOUT_SEC afterwards, terminates ffmpeg and flags
        `rec["stalled"]` for the pump to classify. Paused/cancelled time and
        superseded generations never trip it. Daemon thread; exits on `stop`
        or when the job leaves `downloading`.
        """
        start = time.monotonic()
        last_new = start
        last_written = 0
        stalled = False
        while not stop.wait(1.0):
            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is None or int(rec.get("gen") or 0) != token:
                    return
                if rec.get("cancel") or rec.get("status") != "downloading":
                    return
                paused = bool(rec.get("paused"))
                written = int(rec.get("written") or 0)
                proc = rec.get("proc")
            if paused:
                start = time.monotonic()
                last_new = start
                last_written = written
                continue
            if written != last_written:
                last_written = written
                last_new = time.monotonic()
                continue
            now = time.monotonic()
            if written == 0:
                if now - start > FIRST_BYTE_GRACE_SEC:
                    stalled = True
                    break
            elif now - last_new > STALL_TIMEOUT_SEC:
                stalled = True
                break
        if not stalled:
            return  # stopped normally (job finished/cancelled/superseded)
        with self._lock:
            rec = self._downloads.get(video_id)
            if rec is None or int(rec.get("gen") or 0) != token:
                return
            if rec.get("status") != "downloading" or rec.get("cancel"):
                return
            rec["stalled"] = True
            proc = rec.get("proc")
        if proc is not None and proc.poll() is None:
            proc.terminate()

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
        # Backstop for a dead CDN socket mid-read: abort network reads stuck
        # longer than this (microseconds). The pump watchdog is the enforcer;
        # this keeps ffmpeg itself from hanging forever underneath it.
        cmd += ["-rw_timeout", str(10 * 1_000_000)]
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

    def _run_memory(self, video_id: str, title: str, url: str, bitrate: int, token: int) -> None:
        proc: Optional[subprocess.Popen] = None
        try:
            info = self._resolve_guarded(video_id, token, url)
            direct = info.get("url") or ""
            headers = info.get("http_headers") or {}
            duration = int(info.get("duration") or 0)

            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None:
                    rec["duration"] = duration
                    rec["total"] = int(duration * bitrate * 1000 / 8) if duration else 0

            _ch = str(info.get("channel") or info.get("uploader") or "").strip()[:200]
            if _ch:
                try:
                    library_db.update_track(video_id, channel=_ch)
                except Exception:
                    pass

            if not direct:
                raise RuntimeError("could not resolve a direct audio URL")
            if not self._job_live(video_id, token):
                return  # superseded/cancelled while resolving: never spawn ffmpeg

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
            # Uncapped pump: read as fast as ffmpeg produces. A watchdog
            # thread (_watch_stall) kills a stalled ffmpeg so the blocking
            # read below unblocks; the pump then classifies the outcome.
            # Pause freezes in place (backpressure stalls ffmpeg); paused
            # time never trips the watchdog.
            stop_watch = threading.Event()
            watch = threading.Thread(
                target=self._watch_stall, args=(video_id, token, stop_watch),
                daemon=True, name=f"dl-watch-{video_id}",
            )
            watch.start()
            stale = False
            try:
                while True:
                    with self._lock:
                        rec = self._downloads.get(video_id)
                        if rec is None:
                            break
                        if int(rec.get("gen") or 0) != token:
                            stale = True
                            break
                        if rec.get("cancel"):
                            break
                        paused = bool(rec.get("paused"))
                    if paused:
                        time.sleep(PAUSE_POLL_SEC)
                        continue
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    buffer.write(chunk)
                    with self._lock:
                        rec = self._downloads.get(video_id)
                        if rec is not None:
                            rec["written"] = buffer.tell()
            finally:
                stop_watch.set()
            if stale:
                return  # superseded by a newer generation: leave it alone

            with self._lock:
                rec = self._downloads.get(video_id)
                stalled = bool(rec and rec.get("stalled"))
            if stalled:
                try:
                    proc.communicate(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(
                    f"ffmpeg stalled: no new bytes for {int(STALL_TIMEOUT_SEC)}s")

            stderr = proc.communicate()[1]
            code = proc.returncode

            if self.is_purged(video_id):
                # Deleted while buffering: drop the buffer, write nothing.
                try:
                    buffer.close()
                except Exception:  # noqa: BLE001
                    pass
                return

            with self._lock:
                rec = self._downloads.get(video_id)
                cancelled = bool(rec and rec.get("cancel")
                                 and int(rec.get("gen") or 0) == token)
            if cancelled:
                with self._lock:
                    if video_id in self._downloads:
                        self._downloads[video_id]["status"] = "cancelled"
                        self._downloads[video_id].pop("buffer", None)
                try:
                    library_db.set_status(video_id, "cancelled")
                except Exception:  # noqa: BLE001
                    pass
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
            # Only fail the CURRENT generation: a superseded worker (old
            # token) must never poison the newer job's state.
            current = False
            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None and int(rec.get("gen") or 0) == token:
                    rec["status"] = "failed"
                    rec["error"] = str(exc)[:300]
                    current = True
            if current:
                library_db.update_track(video_id, status="failed")
                log.warning("%s failed: %s", video_id, exc)
            else:
                log.info("%s superseded: %s", video_id, exc)
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None and int(rec.get("gen") or 0) == token:
                    rec["proc"] = None

    def _run(
        self,
        video_id: str,
        title: str,
        url: str,
        part_path: Path,
        final_path: Path,
        bitrate: int,
        token: int,
    ) -> None:
        proc: Optional[subprocess.Popen] = None
        try:
            info = self._resolve_guarded(video_id, token, url)
            direct = info.get("url") or ""
            headers = info.get("http_headers") or {}
            duration = int(info.get("duration") or 0)

            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None:
                    rec["duration"] = duration
                    rec["total"] = int(duration * bitrate * 1000 / 8) if duration else 0

            _ch = str(info.get("channel") or info.get("uploader") or "").strip()[:200]
            if _ch:
                try:
                    library_db.update_track(video_id, channel=_ch)
                except Exception:
                    pass

            if not direct:
                raise RuntimeError("could not resolve a direct audio URL")
            if not self._job_live(video_id, token):
                return  # superseded/cancelled while resolving: never spawn ffmpeg
            # Pumped mode: ffmpeg writes MP3 to stdout, Python writes the
            # .part file. Not reading (paused) lets pipe backpressure stall
            # ffmpeg — a pause that works on Windows with stdlib only
            # (no SIGSTOP, no psutil). No ahead-of-playhead cap: the pump
            # reads as fast as ffmpeg produces until EOF.
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

            stream = proc.stdout
            if stream is None:
                raise RuntimeError("ffmpeg stdout not available")
            # Uncapped pump with stall watchdog (see _run_memory): the
            # _watch_stall thread kills a stalled ffmpeg so this blocking
            # read unblocks; the outcome is classified below.
            stop_watch = threading.Event()
            watch = threading.Thread(
                target=self._watch_stall, args=(video_id, token, stop_watch),
                daemon=True, name=f"dl-watch-{video_id}",
            )
            watch.start()
            stale = False
            try:
                with open(part_path, "wb") as out:
                    while True:
                        with self._lock:
                            rec = self._downloads.get(video_id)
                            if rec is None:
                                break
                            if int(rec.get("gen") or 0) != token:
                                stale = True
                                break
                            if rec.get("cancel"):
                                break
                            paused = bool(rec.get("paused"))
                        if paused:
                            time.sleep(PAUSE_POLL_SEC)
                            continue
                        chunk = stream.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        out.flush()
                        with self._lock:
                            rec = self._downloads.get(video_id)
                            if rec is not None:
                                rec["written"] = int(rec.get("written") or 0) + len(chunk)
            finally:
                stop_watch.set()
            if stale:
                return  # superseded by a newer generation: leave it alone

            with self._lock:
                rec = self._downloads.get(video_id)
                stalled = bool(rec and rec.get("stalled"))
            if stalled:
                try:
                    proc.communicate(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(
                    f"ffmpeg stalled: no new bytes for {int(STALL_TIMEOUT_SEC)}s")

            stderr = proc.communicate()[1]
            code = proc.returncode

            if self.is_purged(video_id):
                # Deleted while encoding: never publish the bytes.
                try:
                    part_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return

            with self._lock:
                rec = self._downloads.get(video_id)
                cancelled = bool(rec and rec.get("cancel")
                                 and int(rec.get("gen") or 0) == token)
            if cancelled:
                # File handle is closed (the `with open(...)` block exited),
                # so delete the abandoned .part now; the startup sweep is
                # the backstop if Windows still holds the file locked.
                with self._lock:
                    if video_id in self._downloads:
                        self._downloads[video_id]["status"] = "cancelled"
                try:
                    part_path.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    library_db.set_status(video_id, "cancelled")
                except Exception:  # noqa: BLE001
                    pass
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
            # Only fail the CURRENT generation: a superseded worker (old
            # token) must never poison the newer job's state.
            current = False
            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None and int(rec.get("gen") or 0) == token:
                    rec["status"] = "failed"
                    rec["error"] = str(exc)[:300]
                    current = True
            if current:
                try:
                    part_path.unlink(missing_ok=True)
                except OSError:
                    pass
                library_db.update_track(video_id, status="failed")
                log.warning("%s failed: %s", video_id, exc)
            else:
                log.info("%s superseded: %s", video_id, exc)
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
            with self._lock:
                rec = self._downloads.get(video_id)
                if rec is not None and int(rec.get("gen") or 0) == token:
                    rec["proc"] = None