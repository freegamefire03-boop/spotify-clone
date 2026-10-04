"""Core of the desktop app: yt-dlp logic + a WSGI callable for the webview.

No web framework, no uvicorn, no port we manage. `application` is handed to
pywebview, which runs it in a daemon thread inside this same process and
picks its own random port. The GUI reaches Python through:
  * window.pywebview.api.*  (the js_api bridge) for search / control
  * GET /audio/<id>         (this module) for streamed audio bytes, because an
                            HTML <audio> element cannot read from the bridge
"""

from __future__ import annotations

import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import yt_dlp

import downloader
import library
import library_db
import settings

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
COOKIES_FILE = ROOT / "cookies.txt"

SEARCH_POOL = 50
SEARCH_TTL = 600.0
STREAM_TTL = 240.0
CHUNK = 65536
# Minimum bytes for a valid MP3 prefix (ID3v2 header + first frame sync).
# Chromium's demuxer needs a complete frame to lock onto the format.
MIN_VALID_MP3_PREFIX = 4 * 1024
# MP3 sync word: 0xFF 0xEx or 0xFF 0xFx (MPEG audio frame sync)
MP3_SYNC_WORDS = (b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2")
# m4a/AAC first: progressive <audio> playback and seeking are far more reliable
# in WebView2 than webm/opus, which can stall near the end of a stream.
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"

_search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_search_lock = threading.Lock()
_stream_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_stream_lock = threading.Lock()

# Windows will not let a file be renamed while a reader still holds it open.
# Readers register here so the downloader can drain them before it renames the
# finished .part file into place.
_open_readers: dict[str, int] = {}
_readers_lock = threading.Lock()


def _on_download_finished(video_id: str, title: str, mp3_path: Path) -> None:
    """The file is already a valid MP3 — nothing left to transcode."""
    print(f"[library] saved {mp3_path.name}")


download_manager = downloader.DownloadManager(on_finished=_on_download_finished)


# --------------------------------------------------------------------------- #
# cookies
# --------------------------------------------------------------------------- #
def cookies_path() -> Optional[str]:
    """Return cookies.txt if the user dropped one in the project root."""
    try:
        if COOKIES_FILE.is_file() and COOKIES_FILE.stat().st_size > 0:
            return str(COOKIES_FILE)
    except OSError:
        pass
    return None


def base_opts(**extra: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "socket_timeout": 20,
        "cachedir": False,
    }
    cookie_file = cookies_path()
    if cookie_file:
        opts["cookiefile"] = cookie_file
    opts.update(extra)
    return opts


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
def _thumbnail(entry: dict[str, Any]) -> str:
    for name in ("mqdefault", "hqdefault", "sddefault", "default"):
        for thumb in entry.get("thumbnails") or []:
            if thumb.get("id") == name and thumb.get("url"):
                return thumb["url"]
    video_id = entry.get("id")
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg" if video_id else ""


def _normalize(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry.get("id"),
        "title": entry.get("title") or "Unknown title",
        "channel": entry.get("channel") or entry.get("uploader") or "",
        "duration": int(entry.get("duration") or 0),
        "thumbnail": _thumbnail(entry),
    }


def friendly_network_error(exc: Exception) -> str:
    """Turn a raw network/yt-dlp exception into a short, user-friendly message."""
    text = str(exc).lower()
    if "getaddrinfo" in text or "failed to resolve" in text or "name or service not known" in text:
        return "Can't reach YouTube - no internet or DNS problem."
    if "timed out" in text or "timeout" in text:
        return "Connection to YouTube timed out - the network is slow."
    if "connection refused" in text or "connection reset" in text or "connection aborted" in text:
        return "Connection to YouTube was interrupted."
    if "ssl" in text or "certificate" in text:
        return "Secure connection to YouTube failed."
    if "network is unreachable" in text or "no route to host" in text:
        return "No network connection."
    return "Couldn't reach YouTube. Check your connection and try again."


YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com", "youtu.be", "www.youtu.be",
}
_VIDEO_ID = r"[A-Za-z0-9_-]{11}"
_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")


def parse_youtube_url(text: str) -> Optional[dict[str, str]]:
    """Recognise a YouTube video/playlist URL or a bare video id.

    Returns {"kind": "video"|"playlist", "id": ..., "url": ...} or None.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # bare video id
    if re.fullmatch(_VIDEO_ID, raw):
        return {"kind": "video", "id": raw, "url": f"https://www.youtube.com/watch?v={raw}"}

    candidate = raw if "//" in raw else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return None

    path = parsed.path or ""
    query = parse_qs(parsed.query or "")

    def video(vid: str) -> Optional[dict[str, str]]:
        if vid and re.fullmatch(_VIDEO_ID, vid):
            return {"kind": "video", "id": vid, "url": f"https://www.youtube.com/watch?v={vid}"}
        return None

    if host in ("youtu.be", "www.youtu.be"):
        return video(path.strip("/").split("/")[0])

    if path.rstrip("/") == "/playlist":
        plist = (query.get("list") or [""])[0]
        if plist:
            return {"kind": "playlist", "id": plist,
                    "url": f"https://www.youtube.com/playlist?list={plist}"}
        return None

    if path.rstrip("/") == "/watch":
        # a watch?v=ID&list=PL... link is treated as the single video
        return video((query.get("v") or [""])[0])

    for prefix in _PATH_PREFIXES:
        if path.startswith(prefix):
            return video(path[len(prefix):].strip("/").split("/")[0])

    return None


def _fetch_url_items(parsed: dict[str, str]) -> list[dict[str, Any]]:
    """Resolve a YouTube URL (or bare id) into result rows."""
    opts = base_opts(skip_download=True, ignoreerrors=False, retries=2, socket_timeout=30)
    if parsed["kind"] == "playlist":
        opts["extract_flat"] = "in_playlist"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(parsed["url"], download=False)
    if not info:
        return []
    if parsed["kind"] == "playlist":
        entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
        return [_normalize(e) for e in entries[:SEARCH_POOL]]
    return [_normalize(info)]


def _fetch_search(query: str) -> list[dict[str, Any]]:
    opts = base_opts(extract_flat="in_playlist", skip_download=True, ignoreerrors=False, retries=2, socket_timeout=30)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_POOL}:{query}", download=False)
    items: list[dict[str, Any]] = []
    for entry in (info or {}).get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        items.append(_normalize(entry))
    return items


def search(query: str, offset: int = 0, limit: int = 10) -> dict[str, Any]:
    """Flat YouTube search, cached per query. Returns one page of results."""
    query = (query or "").strip()
    if not query:
        return {"items": [], "total": 0, "offset": 0, "query": query, "cookies": bool(cookies_path())}

    key = query.lower()
    now = time.time()
    with _search_lock:
        cached = _search_cache.get(key)
        if cached and now - cached[0] < SEARCH_TTL:
            items = cached[1]
        else:
            try:
                parsed = parse_youtube_url(query)
                items = _fetch_url_items(parsed) if parsed else _fetch_search(query)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(friendly_network_error(exc)) from exc
            _search_cache[key] = (time.time(), items)

    start = max(0, min(int(offset), len(items)))
    size = max(1, min(int(limit), SEARCH_POOL))
    return {
        "items": items[start : start + size],
        "total": len(items),
        "offset": start,
        "query": query,
        "cookies": bool(cookies_path()),
    }


# --------------------------------------------------------------------------- #
# audio stream resolution
# --------------------------------------------------------------------------- #
def _resolve_direct(video_id: str, client: Optional[str]) -> dict[str, Any]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = base_opts(format=AUDIO_FORMAT, skip_download=True)
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the GUI
        raise RuntimeError(friendly_network_error(exc)) from exc
    if not info:
        raise RuntimeError("no metadata returned")
    direct = info.get("url")
    if not direct or ".m3u8" in direct:
        raise RuntimeError("no direct audio stream available for this client")
    ext = info.get("ext") or "mp4"
    return {
        "url": direct,
        "headers": info.get("http_headers") or {},
        "mime": "audio/mp4" if ext in ("mp4", "m4a") else f"audio/{ext}",
    }


def _get_stream_entry(video_id: str, force: bool = False) -> dict[str, Any]:
    now = time.time()
    with _stream_lock:
        cached = _stream_cache.get(video_id)
        if cached and not force and now - cached[0] < STREAM_TTL:
            return cached[1]

    errors: list[str] = []
    for client in (None, "web_safari", "web_embedded"):
        try:
            entry = _resolve_direct(video_id, client)
        except Exception as exc:  # noqa: BLE001 - client fallback chain
            errors.append(f"{client or 'default'}: {exc}")
            continue
        with _stream_lock:
            _stream_cache[video_id] = (time.time(), entry)
        return entry
    raise RuntimeError("; ".join(errors) or "unable to resolve audio")


def _open_range(url: str, headers: dict[str, str], range_header: Optional[str]):
    forward = {
        k: v
        for k, v in (headers or {}).items()
        if k.lower() not in ("host", "origin", "referer", "range")
    }
    if range_header:
        forward["Range"] = range_header
    return urlopen(UrlRequest(url, headers=forward), timeout=30)


def _iter_file(handle, length: int) -> Iterator[bytes]:
    remaining = length
    while remaining > 0:
        chunk = handle.read(min(CHUNK, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        yield chunk
    handle.close()


def _pipe_yt_dlp(video_id: str) -> Iterator[bytes]:
    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-f",
        AUDIO_FORMAT,
        "-o",
        "-",
        "--quiet",
        "--no-warnings",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    cookie_file = cookies_path()
    if cookie_file:
        args.extend(["--cookies", cookie_file])
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            chunk = proc.stdout.read(CHUNK) if proc.stdout else b""
            if not chunk:
                break
            yield chunk
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.poll() is None:
            proc.terminate()


# --------------------------------------------------------------------------- #
# WSGI application (serves the GUI files + the audio byte-pipe)
# --------------------------------------------------------------------------- #
def _parse_range(header: Optional[str], total: int) -> Optional[tuple[int, int]]:
    if not header or not header.startswith("bytes=") or total <= 0:
        return None
    spec = header[6:].split(",")[0].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:
            length = int(end_text)
            start = max(0, total - length)
            end = total - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else total - 1
    except ValueError:
        return None
    if start >= total or start < 0:
        return None
    return start, min(end, total - 1)


def _upstream_total(headers: Any, fallback_length: int) -> int:
    """Total resource size.

    For a 206 the Content-Length is only the length of the returned slice, so the
    total has to come from Content-Range ("bytes 0-99/4025466"). For a 200 the
    Content-Length is the full size.
    """
    content_range = headers.get("Content-Range")
    if content_range and "/" in content_range:
        total_text = content_range.rsplit("/", 1)[-1].strip()
        if total_text.isdigit():
            return int(total_text)
    return fallback_length


def _stream_from_youtube(start_response: Callable, video_id: str, range_header: Optional[str]):
    """Fallback: proxy audio straight from YouTube (no local file)."""
    try:
        entry = _get_stream_entry(video_id)
    except Exception:  # noqa: BLE001 - last resort: pipe yt-dlp stdout
        start_response(
            "200 OK",
            [("Content-Type", "audio/mpeg"), ("Accept-Ranges", "none"), ("Cache-Control", "no-store")],
        )
        return _pipe_yt_dlp(video_id)

    try:
        upstream = _open_range(entry["url"], entry["headers"], range_header)
    except Exception:  # noqa: BLE001
        try:
            entry = _get_stream_entry(video_id, force=True)
            upstream = _open_range(entry["url"], entry["headers"], range_header)
        except Exception:  # noqa: BLE001
            start_response(
                "200 OK",
                [("Content-Type", "audio/mpeg"), ("Accept-Ranges", "none"), ("Cache-Control", "no-store")],
            )
            return _pipe_yt_dlp(video_id)

    def build(retry: bool = False) -> Optional[tuple[str, list[tuple[str, str]], Iterator[bytes]]]:
        nonlocal upstream
        status = getattr(upstream, "status", None) or upstream.getcode()
        if status == 403 and not retry:
            with _stream_lock:
                _stream_cache.pop(video_id, None)
            try:
                _get_stream_entry(video_id, force=True)
            except Exception:  # noqa: BLE001
                return None
            return None
        if status and int(status) >= 400:
            return None

        upstream_headers = upstream.headers
        content_type = upstream_headers.get("Content-Type") or entry["mime"]
        slice_length = int(upstream_headers.get("Content-Length") or 0)

        if status == 206 and upstream_headers.get("Content-Range"):
            # Upstream already honoured our Range: pass it straight through.
            headers = [
                ("Content-Type", content_type),
                ("Content-Range", upstream_headers["Content-Range"]),
                ("Accept-Ranges", "bytes"),
                ("Cache-Control", "no-store"),
            ]
            if slice_length:
                headers.append(("Content-Length", str(slice_length)))
            return "206 Partial Content", headers, _iter_file(upstream, slice_length or (1 << 62))

        total = slice_length
        window = _parse_range(range_header, total) if total else None
        if window:
            start, end = window
            span = end - start + 1
            handle = _skip(upstream, start)
            headers = [
                ("Content-Type", content_type),
                ("Content-Length", str(span)),
                ("Content-Range", f"bytes {start}-{end}/{total}"),
                ("Accept-Ranges", "bytes"),
                ("Cache-Control", "no-store"),
            ]
            return "206 Partial Content", headers, _iter_file(handle, span)

        headers = [
            ("Content-Type", content_type),
            ("Accept-Ranges", "bytes"),
            ("Cache-Control", "no-store"),
        ]
        if total:
            headers.append(("Content-Length", str(total)))
        return "200 OK", headers, _iter_file(upstream, total or (1 << 62))

    result = build()
    if result is None:
        result = build(retry=True)
    if result is None:
        start_response(
            "200 OK",
            [("Content-Type", "audio/mpeg"), ("Accept-Ranges", "none"), ("Cache-Control", "no-store")],
        )
        return _pipe_yt_dlp(video_id)

    status, headers, body = result
    start_response(status, headers)
    return body


# --------------------------------------------------------------------------- #
# local library: serve from disk, or stream a partial file while downloading
# --------------------------------------------------------------------------- #
def _audio_response(start_response: Callable, video_id: str, range_header: Optional[str]):
    track = library_db.get_track(video_id)

    # 1. A complete file on disk -> serve it directly.
    if track and track.get("file_path"):
        existing = Path(track["file_path"])
        if existing.is_file():
            return _serve_file(start_response, existing, range_header)

    # 2. Otherwise make sure a download is running, then stream the partial.
    partial = _ensure_downloading(video_id, track)
    if partial is not None:
        total = int(download_manager.get_progress(video_id).get("total") or 0)
        return _serve_partial(start_response, video_id, partial, total, range_header)

    # 3. No title/url known -> fall back to proxying YouTube.
    return _stream_from_youtube(start_response, video_id, range_header)


def _ensure_downloading(video_id: str, track: Optional[dict[str, Any]]) -> Optional[Path]:
    """Return the partial file path, starting/resuming the download if needed."""
    if track is None:
        return None
    if download_manager.is_active(video_id):
        return download_manager.part_path(video_id)
    title = track.get("title") or video_id
    url = track.get("url") or f"https://www.youtube.com/watch?v={video_id}"
    bitrate = _current_bitrate()
    return download_manager.start(video_id, title, url, bitrate)


def _open_shared(path: Path, mode: str = "rb"):
    """Open a file that another thread may be writing (Windows file locking)."""
    for _ in range(20):
        try:
            return open(path, mode)
        except PermissionError:
            time.sleep(0.1)
    return open(path, mode)


def _register_reader(key: str) -> None:
    with _readers_lock:
        _open_readers[key] = _open_readers.get(key, 0) + 1


def _release_reader(key: str) -> None:
    with _readers_lock:
        if key in _open_readers:
            _open_readers[key] -= 1
            if _open_readers[key] <= 0:
                _open_readers.pop(key, None)


def reader_count(key: str) -> int:
    with _readers_lock:
        return _open_readers.get(key, 0)


def _readable_path(path: Path) -> Path:
    """yt-dlp downloads to `<name>.part` and renames to `<name>` when finished.

    While a download is in flight the real bytes live in the `.part` file, so
    that is what we must read from.
    """
    part = path.with_name(path.name + ".part")
    return part if part.exists() else path


def _serve_file(start_response: Callable, path: Path, range_header: Optional[str]):
    """Serve a complete local file with Range support."""
    total = path.stat().st_size
    ctype = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    window = _parse_range(range_header, total) if range_header else None
    if window:
        start, end = window
        span = end - start + 1
        start_response("206 Partial Content", [
            ("Content-Type", ctype),
            ("Content-Range", f"bytes {start}-{end}/{total}"),
            ("Content-Length", str(span)),
            ("Accept-Ranges", "bytes"),
            ("Cache-Control", "no-store"),
        ])
        handle = _open_shared(path)
        handle.seek(start)
        return _iter_file(handle, span)
    start_response("200 OK", [
        ("Content-Type", ctype),
        ("Content-Length", str(total)),
        ("Accept-Ranges", "bytes"),
        ("Cache-Control", "no-store"),
    ])
    return _iter_file(_open_shared(path), total)


def _find_mp3_sync(data: bytes) -> int:
    """Return offset of first MP3 sync word in data, or -1."""
    for sync in MP3_SYNC_WORDS:
        idx = data.find(sync)
        if idx != -1:
            return idx
    return -1


def _has_valid_mp3_prefix(path: Path) -> bool:
    """Check if file starts with a valid MP3 frame (ID3v2 or sync word)."""
    try:
        if not path.exists() or path.stat().st_size < MIN_VALID_MP3_PREFIX:
            return False
        with open(path, "rb") as f:
            head = f.read(MIN_VALID_MP3_PREFIX)
        # Skip ID3v2 tag if present
        if head[:3] == b"ID3":
            # ID3v2 size is syncsafe integer (7 bits per byte)
            size = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) | \
                   ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
            offset = 10 + size
            if offset >= len(head):
                return False
            return _find_mp3_sync(head[offset:offset + 4]) != -1
        return _find_mp3_sync(head[:4]) != -1
    except OSError:
        return False


class _AdaptiveGate:
    """Auto-calibrates initial burst size from measured ffmpeg throughput."""

    def __init__(self, bitrate_bps: int):
        self.bitrate = bitrate_bps
        self.observed_bps: float | None = None
        self._t0: float | None = None
        self._bytes_at_t0: int = 0

    def want(self) -> int:
        """Target bytes: enough for ~300ms audio + jitter, capped by throughput."""
        base = max(MIN_VALID_MP3_PREFIX, int(self.bitrate * 0.3 / 8))
        if self.observed_bps:
            max_wait = int(self.observed_bps * 0.4 / 8)
            base = min(base, max_wait)
        return base

    def tick(self, part: Path):
        """Call periodically to measure throughput."""
        now = time.monotonic()
        try:
            size = part.stat().st_size if part.exists() else 0
        except OSError:
            return
        if self._t0 is None:
            self._t0, self._bytes_at_t0 = now, size
        elif now - self._t0 >= 0.5:
            dt = now - self._t0
            if dt > 0 and size > self._bytes_at_t0:
                self.observed_bps = (size - self._bytes_at_t0) * 8 / dt
            self._t0, self._bytes_at_t0 = now, size


def _serve_partial(
    start_response: Callable, video_id: str, path: Path, total: int, range_header: Optional[str]
):
    """Stream the MP3 that ffmpeg is still writing (one channel: play + save).

    No Content-Length is sent while the file grows — advertising a length the
    body cannot reach would abort playback. The response simply stays open until
    ffmpeg finishes, which is what Chromium expects from a growing stream.
    Seeks are refused while growing (see to-do.md item 5).
    """
    part = download_manager.part_path(video_id) or path
    bitrate = _current_bitrate()
    gate = _AdaptiveGate(bitrate)

    start_response("200 OK", [
        ("Content-Type", "audio/mpeg"),
        ("Cache-Control", "no-store"),
        ("Accept-Ranges", "none"),
    ])

    def generate() -> Iterator[bytes]:
        deadline = time.monotonic() + 30.0

        # Wait for valid MP3 prefix (ID3v2 + frame sync), not arbitrary size
        while time.monotonic() < deadline:
            if _has_valid_mp3_prefix(part):
                break
            if not download_manager.is_active(video_id) and part.exists():
                break
            gate.tick(part)
            time.sleep(0.02)
        else:
            return

        # Adaptive gate: wait for measured throughput to produce enough bytes
        want = gate.want()
        while part.stat().st_size < want:
            if time.monotonic() > deadline or not download_manager.is_active(video_id):
                break
            gate.tick(part)
            time.sleep(0.02)

        # Handles are opened per read and closed immediately, so ffmpeg is never
        # blocked by us when it renames the finished file (Windows: WinError 32).
        # When that rename happens mid-stream we follow the file to its new name.
        source = part
        sent = 0
        last_size = -1
        idle_since = time.monotonic()
        try:
            while True:
                if not source.exists() and source != path and path.exists():
                    source = path          # the .part was published as the final file
                try:
                    size = source.stat().st_size
                except OSError:
                    size = last_size
                available = size - sent
                if available > 0:
                    _register_reader(str(source))
                    try:
                        with open(source, "rb") as handle:
                            handle.seek(sent)
                            block = handle.read(min(CHUNK, available))
                    finally:
                        _release_reader(str(source))
                    if not block:
                        time.sleep(0.05)
                        continue
                    sent += len(block)
                    last_size = size
                    idle_since = time.monotonic()
                    yield block
                elif download_manager.is_active(video_id):
                    # safety net: a download that died without reporting must not
                    # keep this response open forever
                    if size != last_size:
                        last_size = size
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since > 30.0:
                        return
                    time.sleep(0.05)
                else:
                    break
        except (PermissionError, FileNotFoundError):
            return

    return generate()


def _prepare_track(video_id: str) -> dict[str, Any]:
    """Resolve a track and wait until a valid MP3 prefix is ready to stream."""
    track = library_db.get_track(video_id)
    if track is None:
        return {"ready": False, "reason": "unknown track"}
    if track.get("file_path"):
        existing = Path(track["file_path"])
        if existing.is_file():
            return {"ready": True, "bytes": existing.stat().st_size}
    part = download_manager.part_path(video_id)
    if part and _has_valid_mp3_prefix(part):
        return {"ready": True, "bytes": part.stat().st_size}
    if not download_manager.is_active(video_id):
        _ensure_downloading(video_id, track)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if part and _has_valid_mp3_prefix(part):
            return {"ready": True, "bytes": part.stat().st_size}
        if not download_manager.is_active(video_id):
            break
        time.sleep(0.02)
    return {"ready": False, "reason": "timeout"}


def _current_bitrate() -> int:
    try:
        return int(settings.get("bitrate") or 320)
    except (TypeError, ValueError):
        return 320


def _skip(upstream, amount: int):
    """Position an open HTTP response at `amount` bytes (for Range requests)."""
    handle = upstream
    while amount > 0:
        block = handle.read(min(CHUNK, amount))
        if not block:
            break
        amount -= len(block)
    return handle


def _static_response(start_response: Callable, path: str):
    rel = path.lstrip("/") or "index.html"
    target = (STATIC_DIR / rel).resolve()
    try:
        target.relative_to(STATIC_DIR.resolve())
    except ValueError:
        target = (STATIC_DIR / "index.html").resolve()
    if not target.is_file():
        target = STATIC_DIR / "index.html"
    if not target.is_file():
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not found"]

    data = target.read_bytes()
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    start_response(
        "200 OK",
        [
            ("Content-Type", ctype),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "no-cache, no-store, must-revalidate"),
        ],
    )
    return [data]


def application(environ: dict, start_response: Callable) -> Iterable[bytes]:
    """WSGI entry point used by pywebview for the page files and the audio pipe."""
    path = environ.get("PATH_INFO", "/") or "/"
    if path.startswith("/audio/"):
        video_id = path[len("/audio/") :].split("?")[0].strip("/")
        if not video_id:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"missing id"]
        return _audio_response(start_response, video_id, environ.get("HTTP_RANGE"))
    return _static_response(start_response, path)


# --------------------------------------------------------------------------- #
# js_api bridge (what the GUI calls directly, no HTTP)
# --------------------------------------------------------------------------- #
class Api:
    """Exposed to the GUI as window.pywebview.api"""

    def search(self, query: str, offset: int = 0, limit: int = 10) -> dict[str, Any]:
        return search(query, offset, limit)

    def status(self) -> dict[str, Any]:
        return {
            "cookies": bool(cookies_path()),
            "cookies_path": str(COOKIES_FILE),
            "bitrate": _current_bitrate(),
        }

    def prepare_track(self, video_id: str) -> dict[str, Any]:
        """Wait until /audio/<id> can serve a valid MP3 prefix."""
        return _prepare_track(video_id)

    def download_progress(self, video_id: str) -> dict[str, Any]:
        return download_manager.get_progress(video_id)

    def play(self, video_id: str, title: str, url: str) -> dict[str, Any]:
        """Register a track and start downloading it (called by the GUI on play)."""
        library_db.init_db()
        track = library_db.get_or_create_track(video_id, title, url, int(settings.get("bitrate") or 320))
        if track.get("status") == "mp3" and track.get("file_path"):
            existing = Path(track["file_path"])
            if existing.is_file() and existing.stat().st_size > 0:
                return {"status": "mp3", "path": str(existing)}
        _ensure_downloading(video_id, track)
        return download_manager.get_progress(video_id)

    def resolve(self, video_id: str) -> dict[str, Any]:
        """Check that audio can be resolved (used for diagnostics)."""
        try:
            entry = _get_stream_entry(video_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "mime": entry["mime"]}
