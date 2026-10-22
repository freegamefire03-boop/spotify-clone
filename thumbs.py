"""Local thumbnail cache: one image per video id, served by the WSGI app.

Covers used to point straight at ``i.ytimg.com``, so they vanished as soon as
the machine went offline. Every reachable thumbnail is now copied next to the
app and served from ``/thumb/<video_id>``; the remote host is only a one-time
source and a fallback for the very first render of an uncached item.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import urllib.request
from pathlib import Path
from typing import Optional

import settings

log = logging.getLogger("spotify")

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache" / "thumbs"

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_SEC = 6
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SpotifyClone/1.0"

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

_lock = threading.Lock()
_inflight: set[str] = set()
_pool = None


# --------------------------------------------------------------------------- #
# paths / urls
# --------------------------------------------------------------------------- #
def valid_id(video_id: str) -> bool:
    return bool(_ID_RE.fullmatch(str(video_id or "")))


def local_path(video_id: str) -> Optional[Path]:
    """Deterministic on-disk name for a video id (never user-controlled)."""
    vid = str(video_id or "")
    if not valid_id(vid):
        return None
    return CACHE_DIR / f"{vid}.jpg"


def exists(video_id: str) -> bool:
    path = local_path(video_id)
    try:
        return bool(path and path.is_file() and path.stat().st_size > 0)
    except OSError:
        return False


def remote_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def _candidate_urls(video_id: str, url: Optional[str] = None) -> list[str]:
    out: list[str] = []
    if url and str(url).startswith(("https://", "http://")):
        out.append(str(url))
    out.append(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg")
    out.append(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
    out.append(f"https://i.ytimg.com/vi/{video_id}/default.jpg")
    # Keep order but drop duplicates (the caller often passes mqdefault).
    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))]


# --------------------------------------------------------------------------- #
# image handling
# --------------------------------------------------------------------------- #
def _is_image(blob: bytes) -> bool:
    if len(blob) < 64:
        return False
    if blob[:3] == b"\xff\xd8\xff":          # JPEG
        return True
    if blob[:8] == b"\x89PNG\r\n\x1a\n":     # PNG
        return True
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return True
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return True
    return False


def content_type(blob: bytes) -> str:
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def offline() -> bool:
    try:
        return bool(settings.get("offline_mode"))
    except Exception:  # noqa: BLE001 - settings must never break artwork
        return False


def _fetch(url: str) -> Optional[bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
        if getattr(response, "status", 200) != 200:
            return None
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > MAX_BYTES:
            return None
        blob = response.read(MAX_BYTES + 1)
    if not blob or len(blob) > MAX_BYTES or not _is_image(blob):
        return None
    return blob


# --------------------------------------------------------------------------- #
# public api
# --------------------------------------------------------------------------- #
def ensure(video_id: str, url: Optional[str] = None) -> Optional[Path]:
    """Return the cached path, downloading it first when missing."""
    path = local_path(video_id)
    if path is None:
        return None
    if exists(video_id):
        return path
    if offline():
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    for candidate in _candidate_urls(str(video_id), url):
        try:
            blob = _fetch(candidate)
        except Exception as exc:  # noqa: BLE001 - artwork is optional
            log.debug("thumb fetch failed %s: %s", candidate, exc)
            continue
        if blob is None:
            continue
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_bytes(blob)
            os.replace(tmp, path)
            return path
        except OSError as exc:
            log.debug("thumb write failed %s: %s", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return None


def _executor():
    global _pool
    if _pool is None:
        from concurrent.futures import ThreadPoolExecutor

        _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="thumb")
    return _pool


def ensure_async(video_id: str, url: Optional[str] = None) -> None:
    """Warm the cache in the background; never blocks the caller."""
    if not valid_id(video_id) or exists(video_id) or offline():
        return
    vid = str(video_id)
    with _lock:
        if vid in _inflight:
            return
        _inflight.add(vid)

    def _job() -> None:
        try:
            ensure(vid, url)
        except Exception as exc:  # noqa: BLE001 - a thread must never die
            log.debug("thumb warm failed %s: %s", vid, exc)
        finally:
            with _lock:
                _inflight.discard(vid)

    try:
        _executor().submit(_job)
    except RuntimeError:
        with _lock:
            _inflight.discard(vid)


def remove(video_id: str) -> bool:
    """Drop a cached cover (called when the track itself is deleted)."""
    path = local_path(video_id)
    if path is None:
        return False
    removed = False
    for target in (path, path.with_suffix(".tmp")):
        try:
            target.unlink(missing_ok=True)
            removed = True
        except OSError:
            pass
    return removed


def cache_stats() -> dict[str, int]:
    """Small observability helper: number of covers and total bytes."""
    count = 0
    total = 0
    try:
        for item in CACHE_DIR.glob("*.jpg"):
            try:
                if item.is_file():
                    count += 1
                    total += item.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return {"count": count, "bytes": total}
