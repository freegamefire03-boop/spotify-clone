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
# m4a/AAC first: progressive <audio> playback and seeking are far more reliable
# in WebView2 than webm/opus, which can stall near the end of a stream.
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"

_search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_search_lock = threading.Lock()
_stream_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_stream_lock = threading.Lock()


def _on_download_finished(video_id: str, title: str, m4a_path: Path) -> None:
    """Transcode a finished m4a to MP3 in the background (decision A)."""
    if settings.get("memory_mode"):
        return
    threading.Thread(
        target=_transcode_worker, args=(video_id, title, m4a_path), daemon=True, name=f"tc-{video_id}"
    ).start()


def _transcode_worker(video_id: str, title: str, m4a_path: Path) -> None:
    bitrate = int(settings.get("bitrate") or 320)
    mp3_path = m4a_path.with_suffix(".mp3")
    cmd = [
        "ffmpeg", "-y", "-i", str(m4a_path),
        "-vn", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
        str(mp3_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        library_db.update_track(video_id, status="mp3", file_path=str(mp3_path))
        print(f"[transcode] {video_id} -> {mp3_path.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"[transcode] {video_id} failed: {exc}")


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
        return Path(download_manager.get_progress(video_id).get("path", ""))
    title = track.get("title") or video_id
    url = track.get("url") or f"https://www.youtube.com/watch?v={video_id}"
    bitrate = int(settings.get("bitrate") or 320)
    return download_manager.start(video_id, title, url, bitrate)


def _open_shared(path: Path, mode: str = "rb"):
    """Open a file that another thread may be writing (Windows file locking)."""
    for _ in range(20):
        try:
            return open(path, mode)
        except PermissionError:
            time.sleep(0.1)
    return open(path, mode)


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


def _serve_partial(
    start_response: Callable, video_id: str, path: Path, total: int, range_header: Optional[str]
):
    """Stream a file that is still being written, blocking at the written edge."""
    start = 0
    if range_header and total:
        window = _parse_range(range_header, total)
        if window:
            start = window[0]
    ctype = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    headers = [
        ("Content-Type", ctype),
        ("Accept-Ranges", "bytes"),
        ("Cache-Control", "no-store"),
    ]
    if total:
        headers.append(("Content-Length", str(total)))
    start_response("200 OK", headers)

    def generate() -> Iterator[bytes]:
        # Wait for the downloader to create its `.part` file before reading.
        deadline = time.time() + 30.0
        readable = _readable_path(path)
        while not readable.exists():
            if time.time() > deadline or not download_manager.is_active(video_id):
                return
            time.sleep(0.1)
            readable = _readable_path(path)

        handle = _open_shared(readable)
        handle.seek(start)
        sent = 0
        while total == 0 or sent < total:
            # The `.part` file is renamed to the final name on completion.
            if not readable.exists():
                moved = _readable_path(path)
                if moved != readable and moved.exists():
                    handle.close()
                    readable = moved
                    handle = _open_shared(readable)
                    handle.seek(start + sent)
                    continue
            available = readable.stat().st_size - start - sent
            if available > 0:
                block = handle.read(min(CHUNK, available))
                if not block:
                    break
                sent += len(block)
                yield block
            elif download_manager.is_active(video_id):
                time.sleep(0.2)
            else:
                break
        handle.close()

    return generate()


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
        return {"cookies": bool(cookies_path()), "cookies_path": str(COOKIES_FILE)}

    def play(self, video_id: str, title: str, url: str) -> dict[str, Any]:
        """Register a track and start downloading it (called by the GUI on play)."""
        library_db.add_track(video_id, title, url, int(settings.get("bitrate") or 320))
        track = library_db.get_track(video_id)
        _ensure_downloading(video_id, track)
        return download_manager.get_progress(video_id)

    def resolve(self, video_id: str) -> dict[str, Any]:
        """Check that audio can be resolved (used for diagnostics)."""
        try:
            entry = _get_stream_entry(video_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "mime": entry["mime"]}
