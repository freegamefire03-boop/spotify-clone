# TO-DO — mixed implemented and pending features

> Status snapshot: 2026-10-04 (docs updated post-implementation).
>
> - **Implemented:** persistent-library core, direct-to-MP3 downloading, offline replay from disk, instant-start playback (valid-frame gate + adaptive burst + `prepare_track` + hover prefetch; manual pre-buffer removed), hidden ffmpeg process, playback readiness/retry behavior, friendly network errors, YouTube URL search, search focus + `Ctrl+L`, MP3-quality switch (128/192/320), download-progress display, settings panel (memory mode, prefetch depth, download folder, cookies, session), memory-mode backend, queue prefetch, long-lived encode lanes (1 FG + 2 BG), orphan `.part` cleanup, skip/interrupt handling (cancel + delete, no resume), download-folder change flow + copy prompt, conditional home view, YouTube-style sidebar, watch history + home sections, session restore, cookie management UI.
> - **Implemented then superseded:** 10s-ahead pacing and pause-securing-10s were built, then removed by the playback-lane fix — downloads are now uncapped with a stall watchdog (see §1). `continue_current` setting retired.
> - **Still pending:** seeking into not-yet-downloaded bytes (§5, deferred) and the round-2 UI items in §8.

## 1. Playback-aware downloading

**Status: 10s-ahead pacing was implemented, then removed by the playback-lane
fix. Current behaviour is uncapped (wifi-first) downloading.**

**History**

- Built: 10s-ahead pacing (playhead reports from the GUI, pipe-backpressure gate
  with hysteresis) and pause-freeze-after-10s. Shipped across `599bcec`/`39bb534`.
- Removed: the gate wedged the single foreground encode lane on stalls, killing
  all subsequent plays. The lane fix deleted pacing (`_wants_pause`, gate
  constants, `is_gated`); the pump now reads flat-out until EOF.

**Current behaviour**

- Play = stream + download the whole track as fast as the connection allows.
- Pause = plain freeze-in-place (no more fetching until resume).
- A stall watchdog kills ffmpeg after 10 s with zero new bytes (30 s first-byte
  grace, pause-exempt) plus ffmpeg `-rw_timeout`; resolve is bounded at 60 s.
- Generation tokens: stale queued jobs drop at dispatch, late resolves never
  spawn ffmpeg, superseded workers can't poison newer state.

**Settings**

| # | Option | Default | Behaviour |
|---|--------|---------|-----------|
| 1 | ~~Continue downloading the current track~~ | — | **Retired** with pacing (uncapped is now the only mode). |
| 2 | Download the queue as well (`queue_depth`) | 3 | No longer gated on option 1. Accepts an integer 0–10 (0 = off). Re-checked whenever a new track starts; current playback has priority via the FG lane. |
| 3 | Persistent local library (always on) | ON | Every audio is downloaded and kept in persistent storage. **Implemented.** |

## 2. Local library, DB and storage

**Status: core + folder-change flow implemented.**

- **Filename** = the YouTube title. **Implemented**, with Windows-unsafe characters removed and filename collisions disambiguated.
- **Local DB** records, per audio: `title`, `url`, `id` (when the video has one),
  and the `audio path`. **Implemented.**
- Next time an audio is requested it is **fetched from storage** instead of the
  network, and it is **accessible offline**. **Implemented for complete files.**
- **Settings: download folder picker.** **Implemented** (settings-panel text
  input + backend validation, default = user's **Music** folder).
  - If the user changes the path, prompt:
    *"Do you want to copy the existing audios to the new path?"*
    - **Yes** → copy all existing audios to the new path and update the DB paths
      (name collisions get a `[video_id]` suffix; failures keep the old path and
      are reported).
    - **No** → leave them where they are. The DB keeps the **old** paths for those
      files (so they are still fetched from there) while **new** downloads go to
      the new folder.
  - Blocked while downloads are active (in-flight `.part` paths are pinned at start).

## 3. Real files, not DRM

**Status: implemented for successfully completed downloads.**

- The saved audios are **real, usable MP3 files** — not encrypted, not tied to
  the app, unlike Spotify.

## 4. Memory mode (GUI toggle, in the player)

**Status: implemented** (settings-panel toggle + downloader backend).

- Default **OFF**.
- When ON: new audios are **not saved to disk**. ffmpeg encodes to stdout into
  an in-memory buffer, served progressively and with full Range support once
  complete; discarded when finished (memory rows purged from the DB on restart).
- Audios already in local storage are still fetched from storage — this option
  only stops *saving new ones*.

## 5. Seeking beyond the downloaded portion (deferred)

**Status: deferred and still pending.**

**Current behaviour:** a seek past the written edge is **rejected and snaps
back**. Chromium is told the stream is not range-seekable while the file is
still growing, so the scrubber cannot drag into unwritten bytes.

**Wanted eventually:** serve the seeked part *while the download continues in
the same single channel*. Ideas to evaluate:

- Hold the Range request open and wait for the encoder to write those bytes
  (bounded timeout, then `416`). Feels frozen but is seamless.
- Re-position the ffmpeg process (`-ss`) and continue encoding from there.
- Once the file is complete, allow full Range seeking (already works today on
  replay from disk).

---

## 6. Conditional home view and sidebar

**Status: implemented.**

- If cookies are on, the main page shows YouTube recommendations via yt-dlp
  `:ytrecommended` (trending fallback, then library fallback), cached 10 min.
- If cookies are off, the main page shows recently played + offline tracks.
- Any search hides the main-page content and lets search results take over;
  clearing the box (or `Esc`) restores home from cache.
- Left sidebar: hamburger icon opens **push** mode (layout resizes around it,
  stays open until toggled); left-edge hover opens **overlay** mode (floats,
  closes on pointer-leave). Push degrades to overlay under 860 px. Items: Home,
  Queue, Settings — all wired, no dead buttons.

## 7. Other work captured during implementation (all implemented 2026-10-04)

- MP3 quality selector: tri-position 128/192/320 control, default 320, in the player extras area.
- Settings panel: memory mode, queue depth (0–10), download folder, cookies, session — plus bitrate (see above).
- Download-progress display wired to the existing backend progress data (bar + `written/total`, auto-hides; never shown for cache hits).
- Startup cleanup for orphaned unfinished download files (`reconcile_stale_downloads` + `*.part` sweep, stats logged).
- Skipped/interrupted-download handling: **no resume** (CBR re-encode can't resume — restart from 0 is correct and cheapest). Skip cancels the previous download + deletes its `.part`; failures delete the `.part`; cancel paths self-clean; quit relies on startup reconcile.
- Download-folder change flow and prompt described in section 2.
- Cookie management UI: paste-install with Netscape validation, status (size/date), remove, home/search cache reset on change.
- Personalized landing content: local watch history (`play_count`, `last_played_at`) + home sections ("Recently played" distinct from recommendations/offline). YouTube-side history/playlists and playlist CRUD explicitly deferred (login-only / separate milestone).
- Long-lived background encode lanes: 1 FG + 2 BG persistent workers pulling `AudioJob`s (priority-routed); ffmpeg stays per-track (CLI is one-input-one-run).
- Session restore: queue + index persist debounced to `settings.json`; restore is paused (no autoplay, no download) with cold-start play on first gesture.
- Search focus: search box focused on first open; `Ctrl+L` refocuses; `Esc` clears search and restores home.

## 8. Round-2 UI work (noted 2026-10-04, pending)

- Queue panel dismiss: close on outside click and on `Esc` (X button already works).
- Sidebar Downloads section: opens a Downloads page listing all downloaded tracks.
- Offline mode: disconnect the app completely from the internet, open the Downloads
  page, and skip anything still downloading.
- Downloaded titles must never show the download progress bar (currently it flashes
  0→100% then hides on cache hits).

## Constraints / notes

- Data budget on the current connection is very limited: no audio streaming
  tests, no dependency downloads, and no YouTube fetches without asking first.
- `/audio/<id>` serves complete cached files from disk with seeking, streams the
  growing MP3 while ffmpeg is writing, answers `503` when the prefix wait
  expires with zero bytes, and can fall back to YouTube when no title/URL is
  known. Downloads are uncapped (pacing removed, see §1); queue prefetch
  follows `queue_depth`.
- Playback failure notes (2026-10-04): a wedged FG lane (fixed by the lane
  watchdog) and an empty-200 serve path (fixed by answering `503`) were the
  root causes of "no supported source" with "0 B downloaded". Failures now
  land in `spotify-clone.log` with lane depths attached.
