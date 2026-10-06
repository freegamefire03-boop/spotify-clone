# TO-DO — mixed implemented and pending features

> Status snapshot: 2026-10-06 (mass-implementation loop: §5 + all §8 items done).
>
> - **Implemented:** persistent-library core, direct-to-MP3 downloading, offline replay from disk, instant-start playback (valid-frame gate + adaptive burst + `prepare_track` + hover prefetch; manual pre-buffer removed), hidden ffmpeg process, playback readiness/retry behavior, friendly network errors, YouTube URL search, search focus + `Ctrl+L`, MP3-quality switch (128/192/320), download-progress display, settings panel (memory mode, prefetch depth, download folder, cookies, session), memory-mode backend, queue prefetch, long-lived encode lanes (1 FG + 2 BG), orphan `.part` cleanup, skip/interrupt handling (cancel + delete, no resume), download-folder change flow + copy prompt, conditional home view, YouTube-style sidebar, watch history + home sections, session restore, cookie management UI.
> - **Implemented 2026-10-06:** seek-beyond-edge hold-open (§5), panel exclusivity + dismiss, Home-button fix, progress-bar suppression on cache hits, queue add-links (**untested**, see §8), Downloads page, offline mode, settings-modal swap (preview polished: retired Continue toggle replaced by Offline mode, stepper fixed to 0–10, all settings wired, live mini-player).
> - **Implemented then superseded:** 10s-ahead pacing and pause-securing-10s were built, then removed by the playback-lane fix — downloads are now uncapped with a stall watchdog (see §1). `continue_current` setting retired.
> - **Still pending:** re-test of queue add-links dedupe in a clean interpreter (see §8); live/GUI confirmation of the 2026-10-06 items in the running app.

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

## 5. Seeking beyond the downloaded portion

**Status: implemented 2026-10-06 (approach 1: hold the Range request open).**

**Current behaviour:** the growing stream answers `Accept-Ranges: bytes`
(no `Content-Length`), so the scrubber can drag into unwritten bytes. A seek
past the written edge (`Range: bytes=N-`, N > 0) is held open while the
download continues in the same single channel: the request waits (fresh 30 s
budget, polled every 50 ms) for the encoder to write past N, then serves
`206` with `Content-Range: bytes N-/*` and streams from N to the growth edge.
Timeout, paused (frozen edge), or dead download answers `416` (`bytes */*`).
Initial loads (no Range, `bytes=0-`, suffix/invalid ranges) take the byte-
identical 200 flow. Prefix-wait 503s, adaptive burst, and the stall watchdog
are unchanged; complete files still use exact-total 206s (`_serve_file`).

**Not done:** re-positioning ffmpeg (`-ss`) — rejected in favor of hold-open
(single channel, no re-encode). Memory-mode growing seeks stay non-seekable
(default-off edge case, separate buffer machinery).

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

## 8. Round-2 UI work (noted 2026-10-04, implemented 2026-10-06)

- Panel exclusivity + dismiss (do first): the queue panel and settings panel
  must never overlap/coexist — opening one closes the other. The queue panel
  must close instantly on focus loss, on outside click, and on its close
  button (X already works; `Esc` should also close it).
- Home button broken: the sidebar Home item currently does nothing. It must
  return to the home page, whose content today is recent titles. Recommendations
  as home content later if possible (note: recommendations already exist when
  cookies are on — see §6; cookies-off home stays recent titles).
- Queue "add links" button: a control in the queue panel accepting a pasted
  batch of links or a single playlist link, adding every resolved track to the
  queue (backend already resolves playlist links via the search path — reuse it).
  **Implemented (commit 7bd1867) but UNTESTED.** An isolated logic test of
  `core.add_links` (stubbed `_fetch_url_items`, no network) failed with a
  mysterious result: the per-URL dedupe (`seen_urls`, core.py:502-504) did not
  fire — a duplicated `watch?v=` link in a batch was fetched twice (the recorded
  call list contained the same URL twice), while the per-id dedupe worked and
  the returned items were correct. The source on disk contains the dedupe, so
  the cause is unknown (suspect a stale `__pycache__` or environment quirk).
  The test was abandoned per user instruction; the feature needs a re-test in
  a clean interpreter. (One real bug was found and fixed during bring-up: the
  `submitLinks` element/function name collision in app.js.)
- Double-click a title plays it (single click only selects; `dblclick` plays
  the row while keeping the selection from the first click).
- Responsive Windows-style row selection (expected result: selection feels
  instant and easy). State model: `selectionSet` + `caret` + `anchor`.
  Select on `pointerdown` (never on release); `user-select: none` on rows.
  - Plain click: selection = {clicked}; caret = anchor = clicked.
  - Ctrl+click: toggle exactly the clicked row, others unchanged;
    caret = anchor = clicked.
  - Shift+click: selection = contiguous range anchor→clicked (replaces outside
    selection); caret = clicked; anchor does NOT move (repeat Shift+clicks
    re-evaluate from the same anchor, other direction included).
  - Ctrl+Shift+click: add range anchor→clicked to the existing selection.
  - Keyboard: arrows move caret (plain = select only it + anchor follows);
    Shift+arrows extend/shrink anchor→caret; Ctrl+arrows move caret only;
    Ctrl+Space toggles caret; Ctrl+A selects all.
- Click-and-drag rubber-band select: past a ~4px movement threshold a drag
  becomes a selection rectangle (intersected rows selected); plain drag
  replaces, Ctrl+drag adds/toggles vs the pre-drag snapshot, Shift+drag extends
  anchor→pointer live. Anchor stays at the drag origin afterwards. Auto-scroll
  near container edges with speed scaling; scrollbar drags never start a
  rubber band; a drag starting on an already-selected row is reserved for
  future reorder/DnD, not selection.
- Sidebar Downloads section: opens a Downloads page listing all downloaded tracks.
- Offline mode: disconnect the app completely from the internet, open the Downloads
  page, and skip anything still downloading.
- Downloaded titles must never show the download progress bar (currently it flashes
  0→100% then hides on cache hits).
- Settings panel swap: `preview.html` holds a settings-modal redesign proposal.
  First improve it to completion (fully polished, all settings wired), *then*
  swap the app's settings panel with it — not before.

## Constraints / notes

- Network posture is wifi-first (no data-budget cap on downloads): still, no live
  audio streaming tests, dependency downloads, or YouTube fetches without
  asking first.
- `/audio/<id>` serves complete cached files from disk with seeking, streams the
  growing MP3 while ffmpeg is writing, answers `503` when the prefix wait
  expires with zero bytes, and can fall back to YouTube when no title/URL is
  known. Downloads are uncapped (pacing removed, see §1); queue prefetch
  follows `queue_depth`.
- Playback failure notes (2026-10-04): a wedged FG lane (fixed by the lane
  watchdog) and an empty-200 serve path (fixed by answering `503`) were the
  root causes of "no supported source" with "0 B downloaded". Failures now
  land in `spotify-clone.log` with lane depths attached.
