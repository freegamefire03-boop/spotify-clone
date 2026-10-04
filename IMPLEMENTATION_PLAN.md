# Spotify Clone — Implementation Plan

> Cross-session tracker. Update the **Status** section as work progresses.

## Goal

Turn the working v1 (search + play + queue + scrubber, pywebview bridge) into a
**local music library**: every played track is downloaded, saved as a real MP3,
recorded in a local DB, and served from disk on subsequent plays (offline-capable).

## Architecture decisions (current)

| # | Decision | Status / choice |
|---|---|---|
| A | MP3 production | **Implemented as direct streaming transcode.** yt-dlp resolves the direct audio URL; one ffmpeg process encodes straight to a growing MP3. This supersedes the older two-stage m4a-then-transcode plan. |
| B | Growing-file playback | **Implemented (instant-start rework 2026-10-04).** `/audio/<id>` sends no `Content-Length` while the MP3 grows, uses `Accept-Ranges: none`, waits for a valid MP3 prefix (ID3v2-aware sync-word detection, 4KB minimum) plus an adaptive ~300ms-audio burst calibrated from measured ffmpeg throughput, then streams from disk as ffmpeg writes. The manual startup-buffer setting was removed. |
| B2 | Seek beyond the written edge | **Not implemented.** Seeks are refused for growing audio for now; the deferred design is in `to-do.md` item 5. |
| B3 | Avoid duplicate downloads | **Partially implemented.** An active download is registered before the slow URL resolve and reused by concurrent requests. Byte-level resume after a failed/interrupted encode is not implemented. |
| B4 | 10s-ahead model | **Not implemented.** No playhead-aware throttling yet. |
| C | Database | SQLite (built into Python, no server, no install). |
| — | MP3 quality | Bitrate setting is read at encode time; **no 128/192/320 UI switch yet**. |
| — | Filename | YouTube title, sanitized for Windows; collisions are disambiguated with the video id. |

## Architecture

```
WebView2 <audio>  ──HTTP──>  /audio/<id>  (in-process WSGI, pywebview random port)
                                   │
                     ┌─────────────┴──────────────┐
                     │                           │
                     ▼                           ▼
         growing <title>.mp3.part       complete <title>.mp3
         (no Content-Length,          (Content-Length + Range)
          Accept-Ranges: none)
                     │                           │
                     └─────────────┬─────────────┘
                                   ▼
                         SQLite (library.db)
```

- **Bridge**: GUI ↔ Python via `window.pywebview.api.*` (search, status, download control).
- **Audio bytes**: via the `/audio/<id>` route only (an `<audio>` element cannot read the bridge).
- **One process**: pywebview runs the WSGI app in a daemon thread; no port we manage.

## Database schema

File: `library.db` (SQLite). One table:

```sql
CREATE TABLE IF NOT EXISTS tracks (
    video_id    TEXT PRIMARY KEY,   -- YouTube id, e.g. "0RpTykaCVNQ"
    title       TEXT NOT NULL,      -- YouTube title
    url         TEXT NOT NULL,      -- watch URL
    file_path   TEXT,               -- local audio path (NULL until downloaded)
    status      TEXT NOT NULL,      -- 'downloading' | 'mp3' | 'failed' (see also transient downloader states: 'done', 'cancelled')
    bitrate     INTEGER,            -- 128 / 192 / 320
    duration    INTEGER,            -- seconds
    created_at  TEXT                -- ISO timestamp
);
```

Operations: `INSERT` (add), `SELECT WHERE video_id=?` (lookup → offline check),
`UPDATE` (mark mp3 ready), `DELETE` (remove).

## Phases

### Phase 1 — Library foundation
- [x] `library_db.py` — SQLite schema + CRUD (add, get, update, delete, list)
- [x] `library.py` — library-folder resolution (default = Music), filename sanitization, path helpers
- [x] `settings.py` — JSON-backed settings for bitrate plus reserved `library_dir`, `memory_mode`, `continue_current`, and `queue_depth` keys (`startup_buffer` removed 2026-10-04)
- [x] `downloader.py` — one ffmpeg process resolves the direct audio URL with yt-dlp, then encodes straight to a growing MP3 (CBR-enforced libmp3lame, ID3v2.3 header, `-flush_packets 1`)
- [x] Hidden ffmpeg subprocess (`CREATE_NO_WINDOW`)
- [x] `/audio/<id>` serves a complete file from disk with `Content-Length` and Range seeking
- [x] `/audio/<id>` streams the growing MP3 with no `Content-Length` and `Accept-Ranges: none`
- [x] Valid-frame gate plus adaptive ~300ms-audio burst (replaces the old 64KB probe floor + empty-flush; first chunk is always a decodable MP3 prefix)
- [x] ~~Startup pre-buffer setting (0 = instant, 0–30s) with a scrubber input box~~ — removed 2026-10-04, replaced by auto-calibration
- [x] `prepare_track` bridge endpoint: GUI awaits a servable MP3 prefix before assigning `audio.src`
- [x] Hover prefetch (150ms debounce) warms the ffmpeg pipe before the click
- [x] Windows file-locking handling: per-read handles, reader registry, and retried `.part` → `.mp3` rename
- [x] Reader follows the `.part` → `.mp3` rename mid-stream; idle downloads do not leave HTTP responses open forever
- [x] GUI waits for `canplay`/`loadeddata` (`readyState >= 2`, 3s timeout), retries play, and retries once per track after a media error
- [x] User-visible search/network failure messages
- [x] `library.db` initialization on startup and in `Api.play`
- [x] Active-download registration before slow URL resolution to avoid duplicate ffmpeg work
- [x] First live playback reported working by the user
- [ ] Long-lived background ffmpeg worker: start with the app, keep it alive across tracks, and expose a successive-job interface instead of cold-starting one short-lived process per download
- [ ] Confirm one full offline replay explicitly (complete file, zero network)

### Phase 1 wiring notes
- GUI calls `api.play(id, title, url)` on play → registers the track + starts the download.
- `/audio/<id>` order: complete file on disk → growing partial file → proxy YouTube when no known title/URL.
- `_serve_partial` waits for a valid MP3 prefix (sync-word check), then for the adaptive burst size, then blocks at the written edge; an inactive download stops the wait.
- `playTrack` awaits `api.prepare_track(id)` before setting `audio.src`; hover prefetch starts the download early.
- There is no playhead-aware 10-second pacing yet, and pause does not stop or throttle downloads.

### Phase 2 — Smart download policy
- [ ] 10s-ahead pacing (compare playhead to written edge)
- [ ] Pause stops download once 10s secured
- [ ] Option 1: continue downloading current track (uncapped, even when paused) — default OFF
- [ ] Option 2: download queue as well (integer ≥ 1, gated behind option 1) — default OFF

### Phase 3 — Settings UI
- [ ] Settings panel in the GUI
- [ ] Download folder picker (default Music)
- [ ] Cookie management UI; currently only manual `cookies.txt` plus status display
- [ ] Memory mode toggle (default OFF)
- [ ] MP3 quality tri-position switch (128/192/320, default 320) in the scrubber
- [ ] Options 1 & 2 controls

### Phase 4 — Path-change prompt
- [ ] On folder change: "Copy existing audios to the new path?"
- [ ] Yes → copy all + update DB paths; No → keep old paths, new downloads go to new folder

## v1 extras
- [ ] Conditional main page: cookies on shows the same YouTube recommendations yt-dlp can provide; cookies off shows recently played plus offline tracks; any search replaces the main-page view
- [ ] Personalized landing sources: YouTube history, recommendations, preferences, and playlists
- [ ] YouTube-style sidebar with a persistent hamburger-toggle mode and a hover-triggered overlay mode
- [ ] Session restore: remember and restore the last playing track plus the queue on app startup
- [ ] Search focus: focus the search box on startup and add `Ctrl+L` to return focus for typing
- [x] Search bar accepts YouTube URLs and bare video ids (`core.parse_youtube_url`) — 22 offline cases pass
  - video: `watch?v=`, `youtu.be`, `/shorts/`, `/embed/`, `/live/`, music/nocookie/mobile hosts, bare 11-char id
  - playlist: `/playlist?list=` → up to 50 rows (reuses Show more)
  - a `watch?v=ID&list=PL...` link resolves as the single video
  - non-YouTube URLs (vimeo, google) and plain keywords fall through to normal search

## Pitfalls and current safeguards

- **Windows file locking**: playback uses short-lived per-read handles plus a reader registry. Publishing waits for readers to drain and retries `.part` → `.mp3` rename.
- **Browser caching**: always send `Cache-Control: no-store` on `/audio/<id>`.
- **Partial MP4/m4a is not playable in Chromium**: the app encodes straight to MP3 so first-play bytes are usable while the file grows.
- **First-play header/frame race (fixed 2026-10-04)**: yielding headers plus an empty chunk before ffmpeg flushed a complete frame caused intermittent `MEDIA_ERR_SRC_NOT_SUPPORTED`. Fixed by gating on a valid MP3 prefix and removing the empty flush — never gate on wall-clock seconds alone.
- **Filename sanitization**: strip `/ \ : * ? " < > |`, cap length, disambiguate with video id.
- **Unknown final length**: do not send `Content-Length` for growing audio; complete files use `Content-Length` plus Range seeking.
- **Future seek timeout design**: hold a seek up to 30s, then `416`; not implemented yet.
- **Orphaned `.part` files**: not yet cleaned up on startup.

## Status — 2026-10-04

- **v1 complete**: search, play, queue, scrubber, native pywebview bridge, clean error handling, URL search.
- **Phase 1 mostly complete**: one-channel MP3 downloading, persistent library, offline replay, instant-start playback (auto-calibrated, no manual buffer), Windows locking safeguards, and playback-readiness behavior.
- **Verified live by the user:** first playback after the streaming-transcode fix.
- **Instant-start rework (2026-10-04):** fixed the first-play `MEDIA_ERR_SRC_NOT_SUPPORTED` race; removed the `buf` spinner and `startup_buffer` setting; syntax-verified (`py_compile` + `node --check`), awaiting live user confirmation.
- **Still missing from Phase 1:** explicit offline second-play confirmation, orphaned-part cleanup, download-progress display, and the requested long-lived background ffmpeg worker.
- **Phase 2–4**: not started.
- **Data constraint**: no audio downloads without asking first.

## Phase 1 architecture (as built)

```
yt-dlp  -> resolve direct audio URL only (no download)
              |  ONE HTTP connection
              v
ffmpeg -i <direct_url> -vn -c:a libmp3lame -b:a 320k -minrate 320k -maxrate 320k -bufsize 640k -write_id3v2 1 -id3v2_version 3 -flush_packets 1  <title>.mp3.part
              |  grows on disk while Chromium plays it
      +-------+-------+
      |               |
 /audio/<id>      SQLite (status=mp3, path)
 (no Content-Length,
  Accept-Ranges: none)
      |
   <audio>

ffmpeg exits -> _publish() renames .part -> .mp3 -> DB updated
```

- While growing: no `Content-Length`, `Accept-Ranges: none` → Chromium treats it as a
  non-seekable stream and plays it immediately (MP3 has no trailing index).
- On replay: the complete file is served with `Content-Length` + `Accept-Ranges: bytes`
  → full seeking, zero network.
- Seeks during the first play are refused (see to-do.md item 5).
