# Spotify Clone — Implementation Plan

> Cross-session tracker. Update the **Status** section as work progresses.

## Goal

Turn the working v1 (search + play + queue + scrubber, pywebview bridge) into a
**local music library**: every played track is downloaded, saved as a real MP3,
recorded in a local DB, and served from disk on subsequent plays (offline-capable).

## Agreed decisions

| # | Decision | Choice |
|---|---|---|
| A | MP3 conversion timing | Download m4a → play instantly → transcode to MP3 in background. Play button never waits on ffmpeg. |
| B | Partial-file playback | `/audio/<id>` reads the partial file, sends `Content-Length: <expected size>`, blocks at the written edge until more bytes arrive. Browser buffers naturally. |
| B2 | Seek beyond written edge | Hold the request open up to ~30s, then `416`. |
| B3 | No re-download | Keep the partial file; resume with HTTP Range. |
| B4 | 10s-ahead model | Keep ~10s of audio buffered ahead of the playhead; pause stops downloading once 10s is secured. |
| C | Database | SQLite (built into Python, no server, no install). |
| — | MP3 quality | **320k default**, tri-position switch (128/192/320) in the playback scrubber. |
| — | Filename | YouTube title, sanitized for Windows; disambiguate collisions with the video id. |

## Architecture

```
WebView2 <audio>  ──HTTP──>  /audio/<id>  (in-process WSGI, pywebview random port)
                                   │
                                   ▼
        reads partial file ──> blocks at written edge
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
      DownloadManager      Library DB (SQLite)    Transcode worker
      (yt-dlp, thread)     library.db             (ffmpeg, thread)
              │                                         │
              ▼                                         ▼
        <id>.m4a (partial)                        <id>.mp3 (final)
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
    status      TEXT NOT NULL,      -- 'downloading' | 'm4a' | 'mp3' | 'failed'
    bitrate     INTEGER,            -- 128 / 192 / 320
    duration    INTEGER,            -- seconds
    created_at  TEXT                -- ISO timestamp
);
```

Operations: `INSERT` (add), `SELECT WHERE video_id=?` (lookup → offline check),
`UPDATE` (mark mp3 ready), `DELETE` (remove).

## Phases

### Phase 1 — Library foundation
- [x] `library_db.py` — SQLite schema + CRUD (add, get, update, delete, list) — tested
- [x] `library.py` — library folder resolution (default = Music), filename sanitization, path helpers — tested
- [x] `settings.py` — JSON-backed settings (bitrate, library_dir, memory_mode, options) — used by Phase 3
- [x] `downloader.py` — DownloadManager wrapping yt-dlp (progress hook, resume, partial file) — non-network logic tested
- [x] `/audio/<id>` serves from disk when the file exists — tested (200 + 206 + tail range)
- [x] Playback reads partial file (blocks at written edge) — tested with a fake writer
- [x] Background transcode: m4a → mp3 (320k) when download finishes; updates DB
- [x] Serve yt-dlp's `.part` file while downloading (renamed to final on completion) — simulated + tested
- [ ] End-to-end with a real download (needs user OK — data budget)
- [ ] Orphaned `.part` cleanup on startup
- [ ] Download progress surfaced in the GUI

### Phase 1 wiring notes
- GUI calls `api.play(id, title, url)` on play → registers the track + starts the download.
- `/audio/<id>` order: complete file on disk → partial file (stream) → proxy YouTube (fallback).
- `_serve_partial` blocks at the written edge; `is_active(video_id)` decides wait vs. stop.

### Phase 2 — Smart download policy
- [ ] 10s-ahead pacing (compare playhead to written edge)
- [ ] Pause stops download once 10s secured
- [ ] Option 1: continue downloading current track (uncapped, even when paused) — default OFF
- [ ] Option 2: download queue as well (integer ≥ 1, gated behind option 1) — default OFF

### Phase 3 — Settings UI
- [ ] Settings panel in the GUI
- [ ] Download folder picker (default Music)
- [ ] Memory mode toggle (default OFF)
- [ ] MP3 quality tri-position switch (128/192/320, default 320) in the scrubber
- [ ] Options 1 & 2 controls

### Phase 4 — Path-change prompt
- [ ] On folder change: "Copy existing audios to the new path?"
- [ ] Yes → copy all + update DB paths; No → keep old paths, new downloads go to new folder

## v1 extras
- [x] Search bar accepts YouTube URLs and bare video ids (`core.parse_youtube_url`) — 22 offline cases pass
  - video: `watch?v=`, `youtu.be`, `/shorts/`, `/embed/`, `/live/`, music/nocookie/mobile hosts, bare 11-char id
  - playlist: `/playlist?list=` → up to 50 rows (reuses Show more)
  - a `watch?v=ID&list=PL...` link resolves as the single video
  - non-YouTube URLs (vimeo, google) and plain keywords fall through to normal search

## Pitfalls (from research)

- **Windows file locking**: downloader writes + HTTP reader reads the same file → use one shared handle with a `threading.Lock`.
- **Browser caching**: always send `Cache-Control: no-store` on `/audio/<id>`.
- **ffmpeg can't read partial m4a**: only transcode after download completes.
- **Filename sanitization**: strip `/ \ : * ? " < > |`, cap length, disambiguate with video id.
- **Content-Length**: use the *expected* final size from yt-dlp, not the current file size.
- **Seek timeout**: 30s, then `416`.
- **Orphaned `.part` files**: clean up on startup.

## Status

- **v1 complete**: search, play, queue, scrubber, bridge, clean error handling.
- **Phase 1**: in progress — DB + storage layer done and tested.
- **Phase 2–4**: not started.
- **Data constraint**: no audio downloads without asking first.
