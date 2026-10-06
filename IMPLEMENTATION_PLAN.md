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
| B2 | Seek beyond the written edge | **Implemented 2026-10-06.** Growing stream answers `Accept-Ranges: bytes`; seeks past the edge hold the Range open (30 s budget) then serve `206 bytes N-/*` on the same channel, else `416`. Details in `to-do.md` item 5. |
| B3 | Avoid duplicate downloads | **Partially implemented.** An active download is registered before the slow URL resolve and reused by concurrent requests. Byte-level resume after a failed/interrupted encode is not implemented. |
| B4 | 10s-ahead model | **Implemented, then removed.** Playhead-aware throttling shipped, then wedged the single FG lane on stalls — deleted by the playback-lane fix. Downloads are now uncapped with a stall watchdog (10 s no-new-bytes, 30 s first-byte grace, pause-exempt) + ffmpeg `-rw_timeout`. |
| B5 | Encode lanes | **Implemented.** 1 FG + 2 BG persistent worker threads pull `AudioJob`s (priority-routed); ffmpeg stays per-track. Generation tokens drop stale jobs and stop superseded workers poisoning state. |
| C | Database | SQLite (built into Python, no server, no install). |
| — | MP3 quality | Bitrate setting is read at encode time; **128/192/320 tri-position switch in the player extras area** (default 320). |
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
    status          TEXT NOT NULL,      -- 'downloading' | 'mp3' | 'failed' | 'cancelled' | 'memory'
    bitrate         INTEGER,            -- 128 / 192 / 320
    duration        INTEGER,            -- seconds
    file_size       INTEGER,            -- added by migration
    completed_at    TEXT,               -- added by migration
    play_count      INTEGER NOT NULL DEFAULT 0,  -- added by migration (watch history)
    last_played_at  TEXT,               -- added by migration (watch history)
    created_at      TEXT                -- ISO timestamp
);
```

Migrations are idempotent (`PRAGMA table_info` existence checks in `_migrate_schema`).

Operations: `INSERT` (add), `SELECT WHERE video_id=?` (lookup → offline check),
`UPDATE` (mark mp3 ready), `DELETE` (remove).

## Phases

### Phase 1 — Library foundation
- [x] `library_db.py` — SQLite schema + CRUD (add, get, update, delete, list)
- [x] `library.py` — library-folder resolution (default = Music), filename sanitization, path helpers
- [x] `settings.py` — JSON-backed settings for bitrate plus `library_dir`, `memory_mode`, and `queue_depth` keys (`startup_buffer` removed 2026-10-04; `continue_current` retired with pacing; added `download_workers`, `session_queue`, `session_qindex`)
- [x] `downloader.py` — one ffmpeg process per download resolves the direct audio URL with yt-dlp, then encodes to stdout; a Python pump writes the growing MP3 (CBR-enforced libmp3lame, ID3v2.3 header, `-flush_packets 1`, `-rw_timeout` backstop), ungated, with a stall watchdog (10 s no-new-bytes, 30 s first-byte grace, pause-exempt) and generation tokens against stale jobs
- [x] Hidden ffmpeg subprocess (`CREATE_NO_WINDOW`)
- [x] `/audio/<id>` serves a complete file from disk with `Content-Length` and Range seeking
- [x] `/audio/<id>` streams the growing MP3 with no `Content-Length` and `Accept-Ranges: none`
- [x] Valid-frame gate plus adaptive ~300ms-audio burst (replaces the old 64KB probe floor + empty-flush; first chunk is always a decodable MP3 prefix)
- [x] ~~Startup pre-buffer setting (0 = instant, 0–30s) with a scrubber input box~~ — removed 2026-10-04, replaced by auto-calibration
- [x] `prepare_track` bridge endpoint: GUI awaits a servable MP3 prefix before assigning `audio.src`
- [x] Hover prefetch (150ms debounce) warms the download on the BG lane (`prefetch_only`: no history/playhead disturbance) before the click
- [x] Windows file-locking handling: per-read handles, reader registry, and retried `.part` → `.mp3` rename
- [x] Reader follows the `.part` → `.mp3` rename mid-stream; idle downloads do not leave HTTP responses open forever
- [x] GUI waits for `canplay`/`loadeddata` (`readyState >= 2`, 3s timeout), retries play, and retries once per track after a media error
- [x] User-visible search/network failure messages
- [x] `library.db` initialization on startup and in `Api.play`
- [x] Active-download registration before slow URL resolution to avoid duplicate ffmpeg work
- [x] First live playback reported working by the user
- [x] Long-lived background encode lanes: 1 FG + 2 BG persistent workers pull `AudioJob`s (priority-routed) from import time; ffmpeg stays per-track (CLI is one-input-one-run)
- [x] Download-progress display wired to backend progress data (never shown for cache hits)
- [x] Startup orphan `.part` cleanup in `reconcile_stale_downloads` (+ `*.part` sweep, stats logged) plus a mid-session reaper in `Api.play`
- [x] Skipped/interrupted downloads: no resume (CBR re-encode can't resume); skip cancels + deletes, failures delete, cancel paths self-clean
- [x] MP3 quality tri-position switch (128/192/320, default 320) in the player extras
- [x] Settings panel (memory mode, queue depth, download folder, cookies, session) + search focus/`Ctrl+L` + download-progress UI
- [x] Memory mode backend (ffmpeg→stdout→RAM buffer, Range support, DB purge on restart)
- [ ] Confirm one full offline replay explicitly (complete file, zero network)

### Phase 1 wiring notes
- GUI calls `api.play(id, title, url, upcoming, prevId)` on play → registers the track + starts the FG-lane download, cancels the previous track's download, prefetches the upcoming window on the BG lane.
- `/audio/<id>` order: complete file on disk → growing partial file (memory buffer if memory-mode) → proxy YouTube when no known title/URL.
- `_serve_partial` waits for a valid MP3 prefix (sync-word check), then for the adaptive burst size, then blocks at the written edge; an inactive download stops the wait; a zero-byte deadline expiry answers `503` (never an empty 200).
- `playTrack` awaits `api.prepare_track(id)` before setting `audio.src`; hover prefetch warms the BG lane early.
- Downloads are uncapped (10s pacing removed, see B4); pause freezes in place; prefetch follows `queue_depth` (default 3, 0–10).

### Phase 2 — Smart download policy
- [x] 10s-ahead pacing (compare playhead to written edge) — **built, then removed** by the playback-lane fix (wedged the FG lane); pump is now uncapped with a stall watchdog
- [x] Pause stops download once 10s secured — **replaced** by plain freeze-in-place
- [x] ~~Option 1: continue downloading current track~~ — **retired** (uncapped is the only mode)
- [x] Option 2: download queue as well — **implemented, no longer gated on option 1**; `queue_depth` 0–10, default 3

### Phase 3 — Settings UI
- [x] Settings panel in the GUI
- [x] Download folder picker (default Music) with validation + copy-prompt flow
- [x] Cookie management UI: paste-install with validation, status, remove, cache reset
- [x] Memory mode toggle (default OFF)
- [x] MP3 quality tri-position switch (128/192/320, default 320) in the player extras
- [x] Queue depth control (0–10, enabled, default 3)

### Phase 4 — Path-change prompt
- [x] On folder change: "Copy existing audios to the new path?"
- [x] Yes → copy all + update DB paths (collision suffix, failures reported, dir switches last); No → keep old paths, new downloads go to new folder; blocked while downloads are active

## v1 extras
- [x] Conditional main page: cookies on shows YouTube `:ytrecommended` recommendations (trending/library fallbacks, 10-min cache); cookies off shows recently played + offline tracks; any search replaces the main-page view; clear/`Esc` restores home
- [x] Personalized landing: local watch history (`play_count`, `last_played_at`) + home sections ("Recently played" distinct from recommendations/offline); YouTube-side history/playlists and playlist CRUD explicitly deferred
- [x] YouTube-style sidebar with a persistent hamburger-toggle mode and a hover-triggered overlay mode (Home/Queue/Settings items, all wired)
- [x] Session restore: queue + index persist debounced to `settings.json`; paused restore (no autoplay/download) with cold-start play
- [x] Search focus: focus the search box on startup and add `Ctrl+L` to return focus for typing
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
- **Orphaned `.part` files**: cleaned on startup (`reconcile_stale_downloads` + `*.part` sweep) and mid-session (reaper in `Api.play`); cancel/failure paths delete synchronously.

## Status — 2026-10-04 (docs updated post-implementation)

- **v1 complete**: search, play, queue, scrubber, native pywebview bridge, clean error handling, URL search.
- **Phase 1 complete** (minus explicit zero-network replay confirmation): persistent library, instant-start playback, Windows locking safeguards, playback-readiness behavior, orphan cleanup, progress display, encode lanes.
- **Phase 2–4 complete** (with pivots): pacing built then removed in favor of uncapped + watchdog; `continue_current` retired; prefetch gated only on `queue_depth`; settings panel, folder flow, cookie UI all shipped.
- **v1 extras complete**: home view, watch history, sidebar, session restore, search focus.
- **Playback-lane fix (committed, documented in CHANGELOG):** 10s pacing deleted (wedged the FG lane), stall watchdog + `-rw_timeout` + bounded resolve + generation tokens, hover moved to BG lane, zero-byte serves answer `503`, file logging to `spotify-clone.log`, lane depths in diagnostics.
- **2026-10-06 (mass-implementation loop):** all §8 round-2 items done (panel exclusivity + dismiss, Home fix, progress-bar suppression, queue add-links **[untested — dedupe re-test needed]**, Downloads page, offline mode, settings-modal swap) and the §5 seek design implemented (hold-open + 416).
- **Still pending:** explicit zero-network offline-replay confirmation in the live app, queue add-links dedupe re-test in a clean interpreter, and GUI confirmation of the 2026-10-06 items while running.
- **Data constraint**: no audio downloads without asking first. Note: current tree downloads uncapped (full track + up to 3 prefetches per play) — watch data usage on limited connections.

## Phase 1 architecture (as built)

```
yt-dlp  -> resolve direct audio URL only (no download, 60 s bound)
              |
              v
long-lived lanes: 1 FG (current) + 2 BG (prefetch), priority-routed AudioJobs
              |  stalled jobs killed by watchdog (10 s no-new-bytes)
              v
ffmpeg -i <direct_url> -vn -c:a libmp3lame -b:a ${bitrate}k ... -rw_timeout ... -f mp3 -
              |  stdout pumped by Python into <title>.mp3.part (uncapped)
              |  grows on disk while Chromium plays it (or into RAM in memory mode)
      +-------+-------+
      |               |
 /audio/<id>      SQLite (status=mp3, path)
/complete: Content-Length + Range /
/growing: no Content-Length, Accept-Ranges: none; zero-byte deadline -> 503/
      |
   <audio>

ffmpeg exits -> ffprobe-validate -> _publish() renames .part -> .mp3 -> DB updated
```

- While growing: no `Content-Length`, `Accept-Ranges: none` → Chromium treats it as a
  non-seekable stream and plays it immediately (MP3 has no trailing index).
- On replay: the complete file is served with `Content-Length` + `Accept-Ranges: bytes`
  → full seeking, zero network.
- Seeks during the first play are refused (see to-do.md item 5).
