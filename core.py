"""Core of the desktop app: yt-dlp logic + a WSGI callable for the webview.

No web framework, no uvicorn, no port we manage. `application` is handed to
pywebview, which runs it in a daemon thread inside this same process and
picks its own random port. The GUI reaches Python through:
  * window.pywebview.api.*  (the js_api bridge) for search / control
  * GET /audio/<id>         (this module) for streamed audio bytes, because an
                            HTML <audio> element cannot read from the bridge
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import shutil
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
import thumbs

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
COOKIES_FILE = ROOT / "cookies.txt"
LOG_FILE = ROOT / "spotify-clone.log"

log = logging.getLogger("spotify.core")


def setup_logging() -> None:
    """Timestamped file + console logging (idempotent).

    pythonw.exe has no console, so the log FILE is the only persistent
    record of downloader failures — previously they went to print() and
    vanished. Console runs still see everything via the stream handler.
    """
    root = logging.getLogger()
    if getattr(root, "_spotify_setup", False):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s")
    try:
        fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    root._spotify_setup = True  # type: ignore[attr-defined]

SEARCH_POOL = 50
SEARCH_TTL = 600.0
STREAM_TTL = 240.0
HOME_LIMIT = 20
HOME_TTL = 600.0
MAX_CHANNEL_RESULTS = 5
MAX_CHANNEL_VIDEOS = 50
# Staggered bulk downloads: wait before the first start, then one start per
# gap. Keeps channel downloads off the foreground lane and off burst-flooding.
CHANNEL_DL_INITIAL_DELAY = 10.0
CHANNEL_DL_GAP = 30.0
_home_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_home_lock = threading.Lock()
CHUNK = 65536
# Minimum bytes for a valid MP3 prefix (ID3v2 header + first frame sync).
# Chromium's demuxer needs a complete frame to lock onto the format.
MIN_VALID_MP3_PREFIX = 4 * 1024
# MP3 sync word: 0xFF 0xEx or 0xFF 0xFx (MPEG audio frame sync)
MP3_SYNC_WORDS = (b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2")
# m4a/AAC first: progressive <audio> playback and seeking are far more reliable
# in WebView2 than webm/opus, which can stall near the end of a stream.
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"

_search_cache: dict[str, tuple[float, tuple[list[dict[str, Any]], list[dict[str, Any]]]]] = {}
_search_lock = threading.Lock()
_stream_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_stream_lock = threading.Lock()

# Windows will not let a file be renamed while a reader still holds it open.
# Readers register here so the downloader can drain them before it renames the
# finished .part file into place.
_open_readers: dict[str, int] = {}
_readers_lock = threading.Lock()


def _on_download_finished(video_id: str, title: str, mp3_path: Optional[Path]) -> None:
    if mp3_path:
        log.info("saved %s", mp3_path.name)
    else:
        log.info("%s ready in memory", video_id)


def _worker_count() -> int:
    try:
        return max(1, min(4, int(settings.get("download_workers") or 3)))
    except (TypeError, ValueError):
        return 3


def is_offline() -> bool:
    """True when the persisted offline_mode setting is on."""
    try:
        return bool(settings.get("offline_mode"))
    except Exception:
        return False


# Long-lived encode lanes (1 foreground + rest background) start here at
# import time, before the window opens. Daemon threads: never block exit.
download_manager = downloader.DownloadManager(
    on_finished=_on_download_finished, max_workers=_worker_count()
)


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


def validate_cookies_text(text: str) -> tuple[bool, str]:
    """Check pasted text looks like a Netscape cookies.txt (never logs it)."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if len(raw) > 1_000_000:
        return False, "Pasted content is too large (max 1 MB)."
    lines = [ln for ln in raw.split("\n") if ln.strip()]
    if not lines:
        return False, "Paste cookies.txt content first."
    head = lines[:5]
    if any("# HTTP Cookie File" in ln for ln in head):
        return True, ""
    for ln in lines:
        if ln.startswith("#"):
            continue
        parts = ln.split("\t")
        if len(parts) == 7 and parts[0] and parts[1] in ("TRUE", "FALSE") \
                and parts[3] in ("TRUE", "FALSE") and parts[4].isdigit():
            return True, ""
    return False, ("Not a Netscape cookies.txt: need the '# HTTP Cookie File' "
                   "header or tab-separated domain-flag-path-secure-expiry-name-value lines.")


def get_cookie_status() -> dict[str, Any]:
    """Presence + metadata only — never the contents."""
    try:
        if cookies_path():
            st = COOKIES_FILE.stat()
            return {"present": True, "path": str(COOKIES_FILE),
                    "size": st.st_size, "mtime": st.st_mtime}
    except OSError:
        pass
    return {"present": False, "path": str(COOKIES_FILE), "size": 0, "mtime": None}


def _invalidate_cookie_caches() -> None:
    with _home_lock:
        _home_cache.pop("rec", None)
        _home_cache.pop("lib", None)
    with _search_lock:
        _search_cache.clear()


def set_cookies_from_text(text: str, overwrite: bool = False) -> dict[str, Any]:
    ok, err = validate_cookies_text(text)
    if not ok:
        return {"ok": False, "error": err}
    try:
        if COOKIES_FILE.is_file() and COOKIES_FILE.stat().st_size > 0 and not overwrite:
            return {"ok": False, "need_confirm": True,
                    "error": "cookies.txt already exists. Confirm overwrite."}
    except OSError:
        pass
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    try:
        COOKIES_FILE.write_text(raw if raw.endswith("\n") else raw + "\n", encoding="utf-8")
        try:
            os.chmod(COOKIES_FILE, 0o600)  # best-effort; no-op on Windows ACLs
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "error": f"Could not write cookies.txt: {exc}"}
    log.info("cookies installed (%d bytes)", COOKIES_FILE.stat().st_size)
    _invalidate_cookie_caches()
    return {"ok": True, **get_cookie_status()}


def remove_cookies() -> dict[str, Any]:
    try:
        COOKIES_FILE.unlink(missing_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"Could not remove cookies.txt: {exc}"}
    log.info("cookies removed")
    _invalidate_cookie_caches()
    return {"ok": True, **get_cookie_status()}


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
    video_id = entry.get("id")
    thumbnail = _thumbnail(entry)
    if video_id:
        # Warm the on-disk cover cache as soon as a row is produced, so the
        # artwork survives the next offline session. Fire-and-forget.
        try:
            thumbs.ensure_async(str(video_id), thumbnail or None)
        except Exception:  # noqa: BLE001 - artwork must never break search
            pass
    return {
        "id": video_id,
        "title": entry.get("title") or "Unknown title",
        "channel": str(entry.get("channel") or entry.get("uploader") or "").strip()[:200],
        "duration": int(entry.get("duration") or 0),
        "thumbnail": thumbnail,
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
    row = _normalize(info)
    _ch = row.get("channel") or ""
    if _ch and info.get("id"):
        try:
            library_db.update_track(info["id"], channel=_ch)
        except Exception:
            pass
    return [row]


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


def _is_channel_entry(entry: dict[str, Any]) -> bool:
    """True when a flat search entry is a channel, not a video."""
    if str(entry.get("ie_key") or "") == "YoutubeChannel":
        return True
    url = str(entry.get("url") or entry.get("webpage_url") or "")
    for pat in ("/channel/", "/@", "/c/", "/user/"):
        if pat in url:
            return True
    cid = str(entry.get("channel_id") or entry.get("id") or "")
    if cid.startswith("UC") and len(cid) >= 22 and not re.fullmatch(_VIDEO_ID, cid):
        if not entry.get("duration") and entry.get("_type") in ("url", None):
            return True
    return False


def _normalize_channel(entry: dict[str, Any]) -> dict[str, Any]:
    cid = str(entry.get("channel_id") or "")
    if not cid and str(entry.get("id") or "").startswith("UC"):
        cid = str(entry.get("id"))
    name = str(entry.get("channel") or entry.get("uploader") or entry.get("title") or "").strip()[:200]
    url = str(entry.get("webpage_url") or entry.get("url") or "")
    if not url.startswith("http") and cid:
        url = f"https://www.youtube.com/channel/{cid}"
    return {
        "kind": "channel",
        "channel_id": cid,
        "name": name or "Unknown channel",
        "channel_url": url,
        "thumbnail": _thumbnail(entry),
    }


def _fetch_search_split(query: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One ytsearch fetch partitioned into (video rows, channel cards)."""
    opts = base_opts(extract_flat="in_playlist", skip_download=True, ignoreerrors=False, retries=2, socket_timeout=30)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_POOL}:{query}", download=False)
    videos: list[dict[str, Any]] = []
    channels: list[dict[str, Any]] = []
    for entry in (info or {}).get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        if _is_channel_entry(entry):
            if len(channels) < MAX_CHANNEL_RESULTS:
                channels.append(_normalize_channel(entry))
        else:
            if len(videos) < SEARCH_POOL:
                videos.append(_normalize(entry))
    return videos, channels


def resolve_channel_videos(channel_url: str, limit: int = MAX_CHANNEL_VIDEOS) -> list[dict[str, Any]]:
    """Resolve a channel URL/handle into video rows (capped)."""
    url = (channel_url or "").strip()
    if not url:
        return []
    if not url.startswith("http"):
        url = "https://www.youtube.com/" + url.lstrip("/")
    if not url.rstrip("/").endswith("/videos"):
        url = url.rstrip("/") + "/videos"
    try:
        rows = _fetch_tab_items(url, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(friendly_network_error(exc)) from exc
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        vid = row.get("id") or ""
        if not re.fullmatch(_VIDEO_ID, vid) or vid in seen:
            continue
        seen.add(vid)
        out.append(row)
    return out[:limit]


def _register_resolved(rows: list[dict[str, Any]]) -> None:
    """Register resolved rows as DB tracks (metadata only, no downloads)."""
    try:
        bitrate = int(settings.get("bitrate") or 320)
    except (TypeError, ValueError):
        bitrate = 320
    for row in rows:
        try:
            vid = row.get("id") or ""
            if not vid:
                continue
            library_db.get_or_create_track(
                vid, row.get("title") or vid,
                f"https://www.youtube.com/watch?v={vid}", bitrate,
                channel=str(row.get("channel") or "")[:200])
        except Exception:  # noqa: BLE001 - one bad row must not break the batch
            continue


def _disk_complete(video_id: str) -> bool:
    """True when the track already has a complete file on disk."""
    try:
        track = library_db.get_track(video_id)
    except Exception:  # noqa: BLE001
        return False
    return bool(track and track.get("file_path") and Path(track["file_path"]).is_file())


def _fetch_tab_items(url_or_key: str, limit: int = HOME_LIMIT) -> list[dict[str, Any]]:
    opts = base_opts(extract_flat="in_playlist", skip_download=True, ignoreerrors=False, retries=2, socket_timeout=30)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url_or_key, download=False)
    items: list[dict[str, Any]] = []
    for entry in (info or {}).get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        items.append(_normalize(entry))
        if len(items) >= limit:
            break
    return items


def _library_home_items(limit: int = HOME_LIMIT) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in library_db.list_home_library(limit):
        vid = t.get("video_id") or ""
        out.append({
            "id": vid,
            "title": t.get("title") or "Unknown title",
            "channel": t.get("channel") or "",
            "duration": int(t.get("duration") or 0),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else "",
        })
    return out


def _recent_home_items(limit: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in library_db.list_recently_played(limit):
        vid = t.get("video_id") or ""
        out.append({
            "id": vid,
            "title": t.get("title") or "Unknown title",
            "channel": t.get("channel") or "",
            "duration": int(t.get("duration") or 0),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else "",
            "play_count": int(t.get("play_count") or 0),
        })
    return out


def get_downloads() -> dict[str, Any]:
    """Downloaded tracks only, most recently completed first.

    Excludes memory-only ('memory'), in-flight ('downloading'), and
    failed/cancelled rows, plus rows whose file is gone from disk.
    """
    rows: list[tuple[str, dict[str, Any]]] = []
    for t in library_db.list_tracks():
        if t.get("status") != "mp3" or not t.get("file_path"):
            continue
        if not Path(t["file_path"]).is_file():
            continue
        vid = t.get("video_id") or ""
        rows.append((
            # completed_at is "%Y-%m-%dT%H:%M:%S", lexicographically sortable;
            # fall back to created_at for pre-migration rows missing it.
            t.get("completed_at") or t.get("created_at") or "",
            {
                "id": vid,
                "title": t.get("title") or "Unknown title",
                "channel": t.get("channel") or "",
                "duration": int(t.get("duration") or 0),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else "",
                "file_size": int(t.get("file_size") or 0),
            },
        ))
    rows.sort(key=lambda r: r[0], reverse=True)
    items = [row for _, row in rows]
    return {"items": items, "total": len(items)}


def delete_tracks(ids: list[str]) -> dict[str, Any]:
    """Permanently delete tracks: queued jobs, DB row, audio files, cover.

    Deletion is idempotent (an id with no row still counts as deleted, so the
    GUI can drop it) and never claims success when a file could not be removed.
    """
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in ids or []:
        vid = str(raw or "").strip()
        if not vid or vid in seen:
            continue
        seen.add(vid)
        try:
            # Tombstone first: drops a job already sitting in a lane queue and
            # stops the encoder, so nothing can re-create the row or the file
            # after this point.
            download_manager.purge(vid)
            result = library_db.delete_track_file(vid)
            if not result.get("ok"):
                skipped.append({"id": vid, "reason": str(result.get("error") or "delete-failed")})
                continue
            thumbs.remove(vid)
            deleted.append(vid)
        except Exception as exc:  # noqa: BLE001 - one bad id must not break the batch
            skipped.append({"id": vid, "reason": str(exc)[:160] or "delete-failed"})
    if deleted:
        # The local home feed is cached for HOME_TTL; otherwise a deleted
        # track keeps showing up under "Recently played" until it expires.
        with _home_lock:
            _home_cache.pop("lib", None)
            _home_cache.pop("rec", None)
    return {
        "ok": True,
        "deleted": len(deleted),
        "skipped": len(skipped),
        "deleted_ids": deleted,
        "skipped_items": skipped,
    }


def get_home() -> dict[str, Any]:
    """Home feed: YouTube recommendations with cookies, else local library.

    Returns sections (Recently played + main feed) plus a flat items list
    for backward compatibility.
    """
    has_cookies = bool(cookies_path())
    now = time.time()
    if has_cookies and not is_offline():
        with _home_lock:
            cached = _home_cache.get("rec")
            if cached and now - cached[0] < HOME_TTL:
                return cached[1]
        items: list[dict[str, Any]] = []
        try:
            items = _fetch_tab_items(":ytrecommended")
        except Exception:  # noqa: BLE001
            items = []
        if not items:
            try:
                items = _fetch_tab_items("https://www.youtube.com/feed/trending")
            except Exception:  # noqa: BLE001
                items = []
        if items:
            items = items[:HOME_LIMIT]
            recent = _recent_home_items()
            seen = {i["id"] for i in recent}
            sections = []
            if recent:
                sections.append({"title": "Recently played", "items": recent})
            sections.append({"title": "Recommended for you",
                             "items": [i for i in items if i["id"] not in seen]})
            res = {"mode": "recommendations", "items": items,
                   "sections": sections, "cookies": True}
            with _home_lock:
                _home_cache["rec"] = (time.time(), res)
            return res
    else:
        with _home_lock:
            cached = _home_cache.get("lib")
            if cached and now - cached[0] < HOME_TTL:
                return cached[1]
    items = _library_home_items()
    recent = _recent_home_items()
    seen = {i["id"] for i in recent}
    lib_rest = [i for i in items if i["id"] not in seen]
    sections = []
    if recent:
        sections.append({"title": "Recently played", "items": recent})
    sections.append({"title": "Recently played & offline", "items": lib_rest})
    flat = recent + lib_rest
    res = {"mode": "library", "items": flat or items,
           "sections": sections, "cookies": has_cookies}
    if not has_cookies:
        with _home_lock:
            _home_cache["lib"] = (time.time(), res)
    return res


def search(query: str, offset: int = 0, limit: int = 10) -> dict[str, Any]:
    """Flat YouTube search, cached per query. Returns one page of results."""
    query = (query or "").strip()
    if not query:
        return {"items": [], "total": 0, "offset": 0, "query": query, "cookies": bool(cookies_path()), "channels": []}

    if is_offline():
        needle = query.lower()
        try:
            rows = library_db.list_tracks()
        except Exception:
            rows = []
        matches: list[dict[str, Any]] = []
        for t in rows:
            if t.get("status") != "mp3" or not t.get("file_path"):
                continue
            try:
                if not Path(t["file_path"]).is_file():
                    continue
            except OSError:
                continue
            title = t.get("title") or ""
            if needle not in title.lower():
                continue
            vid = t.get("video_id") or ""
            matches.append({
                "id": vid,
                "title": title or "Unknown title",
                "channel": t.get("channel") or "",
                "duration": int(t.get("duration") or 0),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else "",
            })
        start = max(0, min(int(offset), len(matches)))
        size = max(1, min(int(limit), SEARCH_POOL))
        return {
            "items": matches[start:start + size],
            "total": len(matches),
            "offset": start,
            "query": query,
            "cookies": bool(cookies_path()),
            "offline": True,
            "channels": [],
        }

    key = query.lower()
    now = time.time()
    with _search_lock:
        cached = _search_cache.get(key)
        if cached and now - cached[0] < SEARCH_TTL:
            items, channels = cached[1]
        else:
            try:
                parsed = parse_youtube_url(query)
                if parsed:
                    items = _fetch_url_items(parsed)
                    channels = []
                else:
                    items, channels = _fetch_search_split(query)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(friendly_network_error(exc)) from exc
            _search_cache[key] = (time.time(), (items, channels))

    start = max(0, min(int(offset), len(items)))
    size = max(1, min(int(limit), SEARCH_POOL))
    return {
        "items": items[start : start + size],
        "total": len(items),
        "offset": start,
        "query": query,
        "cookies": bool(cookies_path()),
        "channels": channels if start == 0 else [],
    }


MAX_ADD_LINKS = 50


def add_links(text: str) -> dict[str, Any]:
    """Resolve a pasted batch of YouTube links into queue rows."""
    tokens = (text or "").split()
    if not tokens:
        return {"ok": False, "error": "Paste at least one YouTube link."}

    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    skipped = 0
    errors: list[str] = []

    for token in tokens[:MAX_ADD_LINKS]:
        parsed = parse_youtube_url(token)
        if parsed is None:
            skipped += 1
            continue
        if parsed["url"] in seen_urls:
            continue
        seen_urls.add(parsed["url"])
        try:
            rows = _fetch_url_items(parsed)
        except Exception as exc:  # noqa: BLE001 - keep going through the batch
            errors.append(friendly_network_error(exc))
            continue
        for row in rows:
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            items.append(row)

    if not items:
        if errors:
            return {"ok": False, "error": errors[0]}
        return {"ok": False, "error": "No YouTube links found in that text."}

    return {"ok": True, "items": items, "skipped": skipped, "errors": errors}


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
def _has_valid_mp3_prefix_bytes(head: bytes) -> bool:
    if len(head) < MIN_VALID_MP3_PREFIX:
        return False
    if head[:3] == b"ID3":
        size = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) | \
               ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
        offset = 10 + size
        if offset >= len(head):
            return False
        return _find_mp3_sync(head[offset:offset + 4]) != -1
    return _find_mp3_sync(head[:4]) != -1


def _unavailable(start_response: Callable, video_id: str):
    """Honest error for an unservable track (nothing decodable in time).

    Must be used INSTEAD of a 200 with an empty body: Chromium reports an
    empty audio/mpeg response as "no supported source", hiding the real
    cause. A 503 lets the GUI's retry path surface "Stream unavailable".
    """
    body = b"audio unavailable: download stalled or failed, try again"
    start_response("503 Service Unavailable", [
        ("Content-Type", "text/plain"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ])
    return [body]


def _serve_memory_partial(
    start_response: Callable, video_id: str, range_header: Optional[str]
):
    # Wait for a valid prefix BEFORE start_response so an unservable track
    # gets a 503 (see _unavailable) instead of an empty 200.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if download_manager.buffer_size(video_id) >= MIN_VALID_MP3_PREFIX:
            head = download_manager.buffer_head(video_id, MIN_VALID_MP3_PREFIX)
            if _has_valid_mp3_prefix_bytes(head):
                break
        if not download_manager.is_active(video_id):
            break
        time.sleep(0.02)
    else:
        return _unavailable(start_response, video_id)

    if not download_manager.is_active(video_id):
        head = download_manager.buffer_head(video_id, MIN_VALID_MP3_PREFIX)
        if not _has_valid_mp3_prefix_bytes(head):
            return _unavailable(start_response, video_id)

    start_response("200 OK", [
        ("Content-Type", "audio/mpeg"),
        ("Cache-Control", "no-store"),
        ("Accept-Ranges", "none"),
    ])

    def generate() -> Iterator[bytes]:
        sent = 0

        last_size = -1
        idle_since = time.monotonic()
        while True:
            if not download_manager.is_memory(video_id):
                break
            size = download_manager.buffer_size(video_id)
            if size == 0 and not download_manager.is_active(video_id):
                break
            available = size - sent
            if available > 0:
                block = download_manager.buffer_slice(video_id, sent, min(CHUNK, available))
                if block:
                    sent += len(block)
                    last_size = size
                    idle_since = time.monotonic()
                    yield block
            elif download_manager.is_active(video_id):
                if size != last_size:
                    last_size = size
                    idle_since = time.monotonic()
                elif download_manager.is_paused(video_id):
                    # Frozen by the user, not stalled: keep the stream open.
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > 30.0:
                    return
                time.sleep(0.05)
            else:
                break

    return generate()


def _serve_memory_complete(
    start_response: Callable, video_id: str, range_header: Optional[str]
):
    total = download_manager.buffer_size(video_id)
    if total == 0:
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not in memory"]

    window = _parse_range(range_header, total) if range_header else None
    if window:
        start, end = window
        span = end - start + 1
        data = download_manager.buffer_slice(video_id, start, span)
        start_response("206 Partial Content", [
            ("Content-Type", "audio/mpeg"),
            ("Content-Range", f"bytes {start}-{end}/{total}"),
            ("Content-Length", str(span)),
            ("Accept-Ranges", "bytes"),
            ("Cache-Control", "no-store"),
        ])
        return [data]
    data = download_manager.buffer_slice(video_id, 0, total)
    start_response("200 OK", [
        ("Content-Type", "audio/mpeg"),
        ("Content-Length", str(total)),
        ("Accept-Ranges", "bytes"),
        ("Cache-Control", "no-store"),
    ])
    return [data]


def _audio_response(start_response: Callable, video_id: str, range_header: Optional[str]):
    track = library_db.get_track(video_id)

    # 1. A complete file on disk -> serve it directly.
    if track and track.get("file_path"):
        existing = Path(track["file_path"])
        if existing.is_file():
            return _serve_file(start_response, existing, range_header)

    if is_offline():
        # Offline: disk file or nothing. Skip memory re-download,
        # growing-partial serve, and the YouTube proxy fallback.
        return _unavailable(start_response, video_id)

    # 2. Memory mode: serve from in-memory buffer
    if download_manager.is_memory(video_id):
        if download_manager.is_active(video_id):
            return _serve_memory_partial(start_response, video_id, range_header)
        else:
            return _serve_memory_complete(start_response, video_id, range_header)

    # 3. Track is memory-only (status="memory") but buffer not loaded -> re-download
    if track and track.get("status") == "memory":
        _ensure_downloading(video_id, track)
        return _serve_memory_partial(start_response, video_id, range_header)

    # 4. Otherwise make sure a download is running, then stream the partial.
    partial = _ensure_downloading(video_id, track)
    if partial is not None:
        total = int(download_manager.get_progress(video_id).get("total") or 0)
        return _serve_partial(start_response, video_id, partial, total, range_header)

    # 5. No title/url known -> fall back to proxying YouTube.
    return _stream_from_youtube(start_response, video_id, range_header)


def _ensure_downloading(video_id: str, track: Optional[dict[str, Any]],
                        priority: int = downloader.FG_PRIORITY) -> Optional[Path]:
    """Return the partial file path, starting/resuming the download if needed."""
    if is_offline():
        return None
    if track is None:
        return None
    if download_manager.is_active(video_id):
        return download_manager.part_path(video_id)
    title = track.get("title") or video_id
    url = track.get("url") or f"https://www.youtube.com/watch?v={video_id}"
    bitrate = _current_bitrate()
    memory = bool(settings.get("memory_mode"))
    channel = str((track or {}).get("channel") or "")[:200]
    return download_manager.start(
        video_id, title, url, bitrate, memory_mode=memory,
        priority=priority, channel=channel,
    )


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


def _partial_range_start(range_header: Optional[str]) -> Optional[int]:
    """Start offset of a Range header, or None if no usable range.

    Only the start matters on a growing stream; the end is ignored (we
    stream to the growth edge). Suffix ranges (`bytes=-N`) are meaningless
    while the total is unknown -> None (caller does the 200 flow).
    Returns the raw int including 0; caller treats None *and* 0 as
    "no seek" (initial-load 200 flow) so `Range: bytes=0-` behaves
    exactly as today.
    """
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_text, _, _ = spec.partition("-")
    start_text = start_text.strip()
    if not start_text:
        return None
    try:
        start = int(start_text)
    except ValueError:
        return None
    if start < 0:
        return None
    return start


def _serve_partial(
    start_response: Callable, video_id: str, path: Path, total: int, range_header: Optional[str]
):
    """Stream the MP3 that ffmpeg is still writing (one channel: play + save).

    No Content-Length is sent while the file grows — advertising a length the
    body cannot reach would abort playback. The response simply stays open until
    ffmpeg finishes, which is what Chromium expects from a growing stream.

    Seeks past the written edge are held open (to-do.md item 5, approach 1):
    the Range request waits (bounded 30s) for the encoder to write past the
    requested start, then serves 206 from that offset on the same channel.
    `total` is an estimate, never used as a Content-Range total.
    """
    part = download_manager.part_path(video_id) or path
    bitrate = _current_bitrate()
    gate = _AdaptiveGate(bitrate)

    # Wait for a valid MP3 prefix BEFORE start_response so an unservable
    # track gets an honest 503 (see _unavailable) instead of a 200 with an
    # empty body (which Chromium reports as "no supported source").
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
        return _unavailable(start_response, video_id)

    if not _has_valid_mp3_prefix(part):
        # Download died before producing a decodable prefix.
        return _unavailable(start_response, video_id)

    # Adaptive gate: wait for measured throughput to produce enough bytes
    want = gate.want()
    while part.exists() and part.stat().st_size < want:
        if time.monotonic() > deadline or not download_manager.is_active(video_id):
            break
        gate.tick(part)
        time.sleep(0.02)

    seek_start = _partial_range_start(range_header)

    if not seek_start:
        start_response("200 OK", [
            ("Content-Type", "audio/mpeg"),
            ("Cache-Control", "no-store"),
            ("Accept-Ranges", "bytes"),
        ])

        def generate() -> Iterator[bytes]:
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
                        # keep this response open forever. A user-paused download
                        # legitimately shows no growth, so the watchdog is
                        # suspended while paused.
                        if size != last_size:
                            last_size = size
                            idle_since = time.monotonic()
                        elif download_manager.is_paused(video_id):
                            idle_since = time.monotonic()
                        elif time.monotonic() - idle_since > 30.0:
                            return
                        time.sleep(0.05)
                    else:
                        break
            except (PermissionError, FileNotFoundError):
                return

        return generate()

    def _grown_size() -> int:
        try:
            if part.exists():
                return part.stat().st_size
        except OSError:
            pass
        try:
            if path != part and path.exists():
                return path.stat().st_size
        except OSError:
            pass
        return 0

    def _range_unsatisfiable():
        body = b"range start beyond buffered bytes"
        start_response("416 Range Not Satisfiable", [
            ("Content-Type", "text/plain"),
            ("Content-Range", "bytes */*"),
            ("Content-Length", str(len(body))),
            ("Accept-Ranges", "bytes"),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    if download_manager.is_paused(video_id):
        return _range_unsatisfiable()
    if not download_manager.is_active(video_id):
        if _grown_size() <= seek_start:
            return _range_unsatisfiable()
    else:
        wait_until = time.monotonic() + 30.0
        while True:
            if _grown_size() > seek_start:
                break
            if download_manager.is_paused(video_id):
                return _range_unsatisfiable()
            if not download_manager.is_active(video_id):
                if _grown_size() > seek_start:
                    break
                return _range_unsatisfiable()
            if time.monotonic() >= wait_until:
                return _range_unsatisfiable()
            time.sleep(0.05)

    start_response("206 Partial Content", [
        ("Content-Type", "audio/mpeg"),
        ("Content-Range", f"bytes {seek_start}-/*"),
        ("Accept-Ranges", "bytes"),
        ("Cache-Control", "no-store"),
    ])

    def generate_from_offset() -> Iterator[bytes]:
        source = part
        sent = seek_start
        last_size = -1
        idle_since = time.monotonic()
        try:
            while True:
                if not source.exists() and source != path and path.exists():
                    source = path
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
                    if size != last_size:
                        last_size = size
                        idle_since = time.monotonic()
                    elif download_manager.is_paused(video_id):
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since > 30.0:
                        return
                    time.sleep(0.05)
                else:
                    break
        except (PermissionError, FileNotFoundError):
            return

    return generate_from_offset()


def _prepare_track(video_id: str) -> dict[str, Any]:
    """Resolve a track and wait until a valid MP3 prefix is ready to stream."""
    track = library_db.get_track(video_id)
    if track is None:
        return {"ready": False, "reason": "unknown track"}
    if track.get("file_path"):
        existing = Path(track["file_path"])
        if existing.is_file():
            return {"ready": True, "bytes": existing.stat().st_size}

    if download_manager.is_memory(video_id):
        size = download_manager.buffer_size(video_id)
        if size >= MIN_VALID_MP3_PREFIX:
            head = download_manager.buffer_head(video_id, MIN_VALID_MP3_PREFIX)
            if _has_valid_mp3_prefix_bytes(head):
                return {"ready": True, "bytes": size}
        if not download_manager.is_active(video_id):
            _ensure_downloading(video_id, track)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            size = download_manager.buffer_size(video_id)
            if size >= MIN_VALID_MP3_PREFIX:
                head = download_manager.buffer_head(video_id, MIN_VALID_MP3_PREFIX)
                if _has_valid_mp3_prefix_bytes(head):
                    return {"ready": True, "bytes": size}
            if not download_manager.is_active(video_id):
                break
            time.sleep(0.02)
        return {"ready": False, "reason": "timeout"}

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


def _thumb_response(start_response: Callable, video_id: str):
    """Serve a cached cover, or redirect to the remote one while it warms."""
    path = thumbs.local_path(video_id)
    if path is not None and path.is_file():
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        if data:
            start_response(
                "200 OK",
                [
                    ("Content-Type", thumbs.content_type(data)),
                    ("Content-Length", str(len(data))),
                    # Video covers never change for a given id.
                    ("Cache-Control", "public, max-age=31536000, immutable"),
                ],
            )
            return [data]

    if thumbs.valid_id(video_id) and not thumbs.offline():
        # Cold cache: let the browser paint the remote image immediately while
        # a background job stores a local copy for the next (possibly offline)
        # render. The redirect is not cacheable, so the local file wins later.
        thumbs.ensure_async(video_id)
        start_response(
            "302 Found",
            [
                ("Location", thumbs.remote_url(video_id)),
                ("Cache-Control", "no-store"),
            ],
        )
        return [b""]

    start_response(
        "404 Not Found",
        [("Content-Type", "text/plain"), ("Cache-Control", "no-store")],
    )
    return [b"no thumbnail"]


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
    if path.startswith("/thumb/"):
        video_id = path[len("/thumb/") :].split("?")[0].strip("/")
        return _thumb_response(start_response, video_id)
    return _static_response(start_response, path)


# --------------------------------------------------------------------------- #
# js_api bridge (what the GUI calls directly, no HTTP)
# --------------------------------------------------------------------------- #
class Api:
    """Exposed to the GUI as window.pywebview.api"""

    def search(self, query: str, offset: int = 0, limit: int = 10) -> dict[str, Any]:
        return search(query, offset, limit)

    def add_links(self, text: str) -> dict[str, Any]:
        return add_links(text)

    def add_channel_to_queue(self, channel_url: str) -> dict[str, Any]:
        """Resolve a channel into queue rows (registers tracks, no downloads)."""
        if is_offline():
            return {"ok": False, "skipped": True, "reason": "offline"}
        try:
            rows = resolve_channel_videos(channel_url)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if not rows:
            return {"ok": True, "items": [], "total": 0, "reason": "empty"}
        _register_resolved(rows)
        return {"ok": True, "items": rows, "total": len(rows),
                "channel_url": channel_url}

    def add_channel_to_downloads(self, channel_url: str) -> dict[str, Any]:
        """Queue a whole channel for staggered background downloading.

        Disk library-building: memory_mode is ignored (memory rows die on
        restart). One download starts per CHANNEL_DL_GAP after an initial
        CHANNEL_DL_INITIAL_DELAY; already-complete tracks are skipped.
        """
        if is_offline():
            return {"ok": False, "skipped": True, "reason": "offline"}
        try:
            rows = resolve_channel_videos(channel_url)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        rows = [r for r in rows if not _disk_complete(r.get("id") or "")]
        if not rows:
            return {"ok": True, "queued": 0, "total": 0, "reason": "empty-or-complete"}
        _register_resolved(rows)

        def _run() -> None:
            time.sleep(CHANNEL_DL_INITIAL_DELAY)
            bitrate = _current_bitrate()
            for row in rows:
                try:
                    if is_offline():
                        break
                    vid = row.get("id") or ""
                    if not vid:
                        continue
                    if download_manager.is_purged(vid):
                        # Deleted from the app while this channel was still
                        # staggering: never bring it back.
                        continue
                    download_manager.start(
                        vid, row.get("title") or vid,
                        f"https://www.youtube.com/watch?v={vid}",
                        bitrate, memory_mode=False,
                        priority=downloader.BG_PRIORITY,
                        channel=str(row.get("channel") or "")[:200],
                    )
                except Exception:  # noqa: BLE001 - one bad row must not stop the stagger
                    pass
                time.sleep(CHANNEL_DL_GAP)

        threading.Thread(target=_run, daemon=True, name="channel-stagger").start()
        return {"ok": True, "queued": len(rows), "total": len(rows),
                "initial_delay": CHANNEL_DL_INITIAL_DELAY, "gap": CHANNEL_DL_GAP}

    def status(self) -> dict[str, Any]:
        return {
            "cookies": bool(cookies_path()),
            "cookies_path": str(COOKIES_FILE),
            "bitrate": _current_bitrate(),
            "lanes": download_manager.lane_status(),
            "offline_mode": is_offline(),
        }

    def prepare_track(self, video_id: str) -> dict[str, Any]:
        """Wait until /audio/<id> can serve a valid MP3 prefix."""
        return _prepare_track(video_id)

    def download_progress(self, video_id: str) -> dict[str, Any]:
        progress = download_manager.get_progress(video_id)
        progress["lanes"] = download_manager.lane_status()
        return progress

    def report_playhead(self, video_id: str, seconds: float) -> dict[str, Any]:
        try:
            return download_manager.report_playhead(video_id, float(seconds))
        except (TypeError, ValueError):
            return {"gated": False}

    def pause_download(self, video_id: str) -> dict[str, Any]:
        return download_manager.pause_download(video_id)

    def resume_download(self, video_id: str) -> dict[str, Any]:
        return download_manager.resume_download(video_id)

    def set_paused(self, video_id: str, paused: bool) -> dict[str, Any]:
        return download_manager.set_paused(video_id, bool(paused))

    def set_bitrate(self, value: int) -> dict[str, Any]:
        settings.set("bitrate", int(value))
        return {"ok": True, "bitrate": int(value)}

    def get_settings(self) -> dict[str, Any]:
        return {
            "memory_mode": bool(settings.get("memory_mode")),
            "queue_depth": int(settings.get("queue_depth") or 0),
            "offline_mode": is_offline(),
        }

    def set_memory_mode(self, value: bool) -> dict[str, Any]:
        settings.set("memory_mode", bool(value))
        return {"ok": True}

    def set_queue_depth(self, value: int) -> dict[str, Any]:
        depth = max(0, min(10, int(value)))
        settings.set("queue_depth", depth)
        return {"ok": True, "queue_depth": depth}

    def set_offline_mode(self, value: Any) -> dict[str, Any]:
        flag = bool(value)
        settings.set("offline_mode", flag)
        return {"ok": True, "offline_mode": flag}

    def get_session(self) -> dict[str, Any]:
        try:
            raw = settings.get("session_queue") or []
            qi = int(settings.get("session_qindex") if settings.get("session_qindex") is not None else -1)
        except (TypeError, ValueError):
            raw, qi = [], -1
        clean = [t for t in raw if isinstance(t, dict)
                 and isinstance(t.get("id"), str) and t["id"]][:100]
        if not clean:
            qi = -1
        elif not 0 <= qi < len(clean):
            qi = 0
        return {"queue": clean, "qIndex": qi}

    def save_session(self, queue: Any, q_index: Any) -> dict[str, Any]:
        clean: list[dict[str, Any]] = []
        for t in (queue or [])[:100]:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            clean.append({
                "id": str(t["id"])[:32],
                "title": str(t.get("title") or "Unknown")[:300],
                "channel": str(t.get("channel") or "")[:200],
                "duration": int(t.get("duration") or 0),
                "thumbnail": str(t.get("thumbnail") or "")[:500],
            })
        try:
            qi = int(q_index)
        except (TypeError, ValueError):
            qi = -1
        if not clean:
            qi = -1
        elif not 0 <= qi < len(clean):
            qi = 0
        settings.set("session_queue", clean)
        settings.set("session_qindex", qi)
        return {"ok": True}

    def get_cookie_status(self) -> dict[str, Any]:
        return get_cookie_status()

    def set_cookies_from_text(self, text: str, overwrite: Any = False) -> dict[str, Any]:
        return set_cookies_from_text(text, bool(overwrite))

    def remove_cookies(self) -> dict[str, Any]:
        return remove_cookies()

    def get_library_dir(self) -> dict[str, Any]:
        return {"ok": True, "path": str(library.get_library_dir())}

    def set_library_dir(self, path: str, copy_existing: bool = False) -> dict[str, Any]:
        """Change the download folder, optionally copying existing audios.

        copy_existing=True copies finished .mp3 files to the new folder and
        updates their DB paths; in-flight downloads block the change (their
        .part paths were pinned at start time).
        """
        try:
            new_dir = library.validate_library_dir(path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            current = library.get_library_dir().resolve()
        except OSError:
            current = None
        if current is not None and os.path.normcase(str(current)) == os.path.normcase(str(new_dir)):
            return {"ok": True, "changed": False, "path": str(current)}
        if download_manager.has_active_downloads():
            return {"ok": False, "reason": "active-downloads",
                    "error": "Wait for active downloads to finish first."}
        if not copy_existing:
            library.set_library_dir(new_dir)
            return {"ok": True, "changed": True, "copied": 0, "path": str(new_dir)}
        copied = 0
        skipped: list[str] = []
        tracks = library_db.list_tracks()
        owner = {
            Path(t["file_path"]).name: t["video_id"]
            for t in tracks if t.get("file_path")
        }
        for track in tracks:
            if track.get("status") != "mp3" or not track.get("file_path"):
                continue
            src = Path(track["file_path"])
            if not src.is_file():
                skipped.append(track["video_id"])
                continue
            dest = new_dir / src.name
            if dest.exists() or owner.get(dest.name, track["video_id"]) != track["video_id"]:
                dest = new_dir / f"{src.stem} [{track['video_id']}].mp3"
            try:
                shutil.copy2(src, dest)
            except OSError:
                skipped.append(track["video_id"])
                continue
            library_db.update_track(track["video_id"], file_path=str(dest))
            owner[dest.name] = track["video_id"]
            copied += 1
        library.set_library_dir(new_dir)
        return {"ok": True, "changed": True, "copied": copied,
                "skipped": skipped, "total": len(tracks), "path": str(new_dir)}

    def _prefetch_upcoming(self, upcoming: Any, current_id: str) -> None:
        """Start background downloads for the next N queue items.

        Uncapped (wifi-first): depth comes from queue_depth alone (0 = off).
        Reuses DownloadManager.start() (dedupes active downloads, skips disk
        cache hits, respects memory_mode).
        """
        try:
            depth = int(settings.get("queue_depth") or 0)
        except (TypeError, ValueError):
            return
        if depth < 1 or not isinstance(upcoming, list):
            return
        bitrate = _current_bitrate()
        memory = bool(settings.get("memory_mode"))
        for item in upcoming[:depth][:10]:
            try:
                vid = item.get("id") if isinstance(item, dict) else None
                if not vid or vid == current_id:
                    continue
                title = item.get("title") or vid
                url = item.get("url") or f"https://www.youtube.com/watch?v={vid}"
                channel = str(item.get("channel") or "")[:200] if isinstance(item, dict) else ""
                track = library_db.get_track(vid)
                if track and track.get("file_path") and Path(track["file_path"]).is_file():
                    continue
                download_manager.start(
                    vid, title, url, bitrate, memory_mode=memory,
                    priority=downloader.BG_PRIORITY, channel=channel,
                )
            except Exception:  # noqa: BLE001 - one bad entry must not break play
                continue

    def abandon_track(self, video_id: str) -> dict[str, Any]:
        """Cancel an in-flight download and delete its partial file."""
        download_manager.cancel(video_id)
        return {"ok": True}

    def delete_tracks(self, ids: list[str]) -> dict[str, Any]:
        try:
            return delete_tracks(ids)
        except Exception:  # noqa: BLE001 - delete must never break the GUI
            return {"deleted": 0, "skipped": len(ids or [])}

    def get_home(self) -> dict[str, Any]:
        try:
            return get_home()
        except Exception:  # noqa: BLE001 - home must never break search/play
            return {"mode": "library", "items": [], "cookies": bool(cookies_path())}

    def get_downloads(self) -> dict[str, Any]:
        try:
            return get_downloads()
        except Exception:  # noqa: BLE001 - downloads must never break search/play
            return {"items": [], "total": 0}

    def _reap_orphan_part(self, video_id: str) -> None:
        """Delete a mid-session orphan .part (the startup sweep is startup-only).

        Only when no download is in flight for the id and the DB row is
        terminal (failed/cancelled): never touches a .part belonging to a
        queued or running job.
        """
        try:
            if download_manager.is_active(video_id):
                return
            track = library_db.get_track(video_id)
            if not track or track.get("status") not in ("failed", "cancelled"):
                return
            file_path = track.get("file_path")
            if not file_path:
                return
            candidate = Path(str(file_path) + ".part")
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
        except OSError:
            pass

    def play(
        self, video_id: str, title: str, url: str, upcoming: Any = None,
        prev_id: Any = None, priority: Any = None, prefetch_only: Any = False,
        channel: str = "",
    ) -> dict[str, Any]:
        """Register a track and start downloading it (called by the GUI on play).

        Hover prefetch passes priority=BG + prefetch_only=True: the download
        warms on a background lane without touching play history, the
        playhead, or the paused flag — so a hover can never disturb (or queue
        ahead of) real playback. Real clicks use the foreground lane.
        """
        library_db.init_db()
        with _home_lock:
            _home_cache.pop("lib", None)
            _home_cache.pop("rec", None)
        try:
            lane = int(priority) if priority is not None else downloader.FG_PRIORITY
        except (TypeError, ValueError):
            lane = downloader.FG_PRIORITY
        if lane != downloader.BG_PRIORITY:
            lane = downloader.FG_PRIORITY
        warming = bool(prefetch_only)
        if is_offline():
            try:
                existing_track = library_db.get_track(video_id)
            except Exception:
                existing_track = None
            if (existing_track and existing_track.get("status") == "mp3"
                    and existing_track.get("file_path")):
                try:
                    existing = Path(existing_track["file_path"])
                    if existing.is_file() and existing.stat().st_size > 0:
                        if not warming:
                            try:
                                library_db.record_play(video_id)
                            except Exception:  # noqa: BLE001 - history must never break playback
                                pass
                        return {"status": "mp3", "path": str(existing)}
                except OSError:
                    pass
            return {"ok": False, "skipped": True, "reason": "not-downloaded", "offline": True}
        # Skip path: cancel the previously-playing download so it stops
        # fetching and frees the foreground lane. Prefetch ids are never
        # cancelled (guarded below); completed files are left alone.
        if prev_id and prev_id != video_id:
            try:
                upcoming_ids = {
                    item.get("id") for item in (upcoming or [])
                    if isinstance(item, dict) and item.get("id")
                }
                if prev_id not in upcoming_ids:
                    old = library_db.get_track(str(prev_id))
                    if not (old and old.get("status") == "mp3" and old.get("file_path")
                            and Path(old["file_path"]).is_file()):
                        download_manager.cancel(str(prev_id))
            except Exception:  # noqa: BLE001 - cancel must never break play
                pass
        self._reap_orphan_part(video_id)
        if prev_id and prev_id != video_id:
            try:
                self._reap_orphan_part(str(prev_id))
            except Exception:  # noqa: BLE001 - reaping must never break play
                pass
        track = library_db.get_or_create_track(
            video_id, title, url, int(settings.get("bitrate") or 320),
            channel=str(channel or "")[:200])
        if not warming:
            try:
                library_db.record_play(video_id)
            except Exception:  # noqa: BLE001 - history must never break playback
                pass
        if track.get("status") == "mp3" and track.get("file_path"):
            existing = Path(track["file_path"])
            if existing.is_file() and existing.stat().st_size > 0:
                return {"status": "mp3", "path": str(existing)}
        _ensure_downloading(video_id, track, priority=lane)
        if not warming:
            # Fresh play from byte 0: drop any stale paused flag from a
            # previous listen. (Playhead no longer gates anything.)
            download_manager.set_paused(video_id, False)
        if not warming:
            self._prefetch_upcoming(upcoming, current_id=video_id)
        return download_manager.get_progress(video_id)

    def resolve(self, video_id: str) -> dict[str, Any]:
        """Check that audio can be resolved (used for diagnostics)."""
        try:
            entry = _get_stream_entry(video_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "mime": entry["mime"]}
