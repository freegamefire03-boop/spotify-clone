# Changelog

## 2026-10-04 — Features Groups 1–6 (18 items, commits b3e1ba7 → 2f315ad)

### Group 1 — Quick UI wins
- Search focus on open + `Ctrl+L` shortcut (`b3e1ba7`).
- MP3 quality tri-switch 128/192/320, default 320 (`b3e1ba7`).
- Download-progress bar + text under now-playing, 1 s poll, auto-hide (`b3e1ba7`).
- Settings panel shell: memory mode, continue-current, queue depth (`b3e1ba7`).

### Group 2 — Settings behavior
- Memory mode backend: ffmpeg→stdout→RAM buffer, progressive + Range serving,
  disk cache still wins, DB purge on restart (`b3e1ba7`).

### Group 3 — Download policy
- 10s-ahead pacing via pipe-backpressure gate + hysteresis (`599bcec`).
- Pause freezes after securing the 10 s margin (`39bb534`).
- Queue prefetch: next-N via `Api.play(upcoming)`, gated on `continue_current` (`6949dcd`).
- Long-lived lanes: 1 FG + 2 BG workers, priority-routed `AudioJob`s (`d5ab998`).

### Group 4 — Library maintenance
- Startup orphan `.part` cleanup + sweep with stats (`97d4746`).
- Skip/interrupt: no resume (CBR can't); skip cancels + deletes (`b6ca617`).
- Folder change flow: validated picker, copy prompt + collision handling,
  in-flight guard (`5754d28`).

### Group 5 — Frontend views
- Conditional home: `:ytrecommended`/trending/library fallbacks, 10-min cache (`6bf4408`).
- Sidebar: push via hamburger, overlay via edge hover (`f7095d3`).
- Watch history: `play_count`/`last_played_at` + home sections (`c8a1c12`).

### Group 6 — Session/cookies
- Session restore: debounced queue+qIndex persist, paused restore (`4b3d5c0`).
- Cookie UI: paste-install + validation, status, remove, cache reset (`2f315ad`).

## 2026-10-04 — Playback-lane fix (post-d5ab998 regression)

### Root cause

- `d5ab998` (long-lived 1 FG + 2 BG lanes) let one wedged job pin the single
  foreground lane forever: `_resolve` (blocking yt-dlp, no deadline) and the
  pumped `stream.read()` (no timeout) never released the worker. Every later
  play queued behind it while *looking* active, timed out through the 5s +
  30s serve gates, and received a `200 OK` with zero bytes — Chromium's
  "no supported source" plus a stuck "0 B downloaded".
- Hover prefetch made it worse: every hovered row became an FG-lane job.

### Fixed

- Uncapped downloading (wifi-first): 10s-ahead pacing gate deleted
  (`_wants_pause`, `BUFFER_AHEAD_SEC`, `is_gated`); pump reads flat-out,
  pause = plain freeze-in-place. `continue_current` setting + UI removed;
  queue prefetch now depends only on `queue_depth` (default 0 → 3).
- Hover warms on the BG lane and no longer touches play history/playhead.
- Resolve bounded (`RESOLVE_TIMEOUT_SEC`, disposable thread) + per-job
  generation tokens: stale queued jobs drop at dispatch, late resolves never
  spawn ffmpeg, superseded workers can't poison newer state.
- Stall watchdog (`FIRST_BYTE_GRACE_SEC` + `STALL_TIMEOUT_SEC`): a hung
  ffmpeg is terminated and the job fails instead of wedging a lane. Paused
  time never trips it. ffmpeg `-rw_timeout` as network backstop.
- Honest errors: zero-byte serve paths answer `503` instead of empty `200`.
- Mid-session orphan `.part` reaper in `Api.play` (startup sweep was
  startup-only).
- Observability: timestamped `spotify-clone.log` (pythonw has no console) +
  FG/BG queue depths in `status`/`download_progress`.

### Verified

- `py_compile` clean, `node --check static/app.js` clean.
- 15 stubbed checks pass (watchdog, pause-exempt, hover→BG, 503, reaper,
  resolve-timeout bound, generation isolation, lanes, logging, prefetch).

## 2026-10-04 — Downloader regression fix (missing `import os`)

### Root cause

- The duplication-fix rework added `_validate_output()` in `downloader.py`,
  which references `os.name` — but `import os` was never added.
- Every completed download crashed with `NameError: name 'os' is not defined`,
  was marked `failed`, and had its `.part` file **deleted** by the failure
  handler. Symptom: full track plays fine, then no file is saved.
- Reproduced on a known-good MP3 before the fix; validator returns `True`
  on a genuine ffmpeg-produced MP3 after the fix.

### Fixed

- Added missing `import os` to `downloader.py`.
- Verified: `py_compile` clean, validator accepts real MP3, rejects
  missing/invalid files gracefully.

## 2026-10-04 — Instant-start playback fix (no manual buffer)

### Root cause

- First-ever play of a track failed with `MEDIA_ERR_SRC_NOT_SUPPORTED`, while
  the second track (or a retry of the first) worked. The 64KB probe floor plus
  the `startup_buffer = 0` ("instant") gate let the server stream bytes before
  ffmpeg had flushed a complete MP3 frame, so Chromium's demuxer rejected the
  stream on the header/frame race.

### Implemented

- Valid-frame gate in `_serve_partial` (`core.py`): waits for a real MP3 prefix
  (ID3v2-aware, first sync word `0xFF 0xFB/FA/F3/F2`) instead of an arbitrary
  byte count; first yielded chunk is always decodable.
- Adaptive throughput calibration (`_AdaptiveGate`): targets ~300ms of audio
  (~5KB at 128k, more at higher bitrates), capped by measured ffmpeg output —
  no user-facing knob.
- `prepare_track` bridge endpoint (`Api.prepare_track`): resolves when
  `/audio/<id>` can serve a valid prefix; the GUI awaits it before assigning
  `audio.src` instead of fire-and-forget `backend.play()`.
- Hover prefetch: 150ms-debounced `backend.play()` on result-row hover so the
  ffmpeg pipe is warm before the click.
- Client readiness (`app.js:startPlayback`): explicit `audio.load()`, waits for
  `canplay`/`loadeddata` (`readyState >= 2`), 3s timeout (was 15s), 80ms
  play-retry interval; `canplaythrough` deliberately not used (unreliable on
  growing streams).
- `state.errorRetried` is now reset on every `playTrack`, so the single media-
  error recovery works per track instead of leaking across tracks.
- ffmpeg MP3 output hardened (`downloader.py`): CBR enforcement
  (`-minrate/-maxrate/-bufsize`), `-write_id3v2 1 -id3v2_version 3`,
  `-flush_packets 1` for immediate frame delivery.
- Removed the manual `startup_buffer` setting: deleted from `settings.py`
  defaults, removed the `buf` spinner from `index.html`, removed
  `Api.set_startup_buffer` and all `startupBuffer` GUI wiring.

### Verified

- `python -m py_compile core.py downloader.py settings.py` clean.
- `node --check static/app.js` clean.
- No remaining code references to `startup_buffer`/`set_startup_buffer`
  (only stale `settings.json` key, ignored on load).

## 2026-10-03 — Phase 1 streaming-library snapshot

### Implemented

- Native single-process pywebview app with a direct Python/JavaScript bridge.
- Keyword search plus YouTube URL, bare video-id, playlist, Shorts, embed, live, and mobile/music/privacy-host URL handling.
- One-channel playback and storage: yt-dlp resolves the direct audio URL, and one ffmpeg process encodes directly to a growing MP3.
- Local SQLite library with title, URL, id, audio path, bitrate, duration, and status.
- Completed downloads are stored as real MP3 files and replayed offline from disk with seeking.
- Growing-first-play streaming with immediate response headers and a 64KB demuxer-probe floor.
- Configurable startup pre-buffer (`buf`, 0–30 seconds; `0` means instant playback).
- Windows file-lock safeguards, including short-lived read handles, a reader registry, and a retried `.part` → `.mp3` rename.
- Hidden ffmpeg console process.
- Playback-readiness waiting, play retry, and single media-error recovery.
- Friendly network/search failure messages.
- Startup and bridge-level database initialization.
- Active-download registration before slow URL resolution to avoid duplicate ffmpeg work.

### Fixed

- Playback could fail because response headers were withheld until the growing stream produced bytes.
- Playback could fail when Chromium received too little initial data to identify the stream.
- Partial m4a playback failure caused by MP4’s end-of-file `moov` index.
- A missing `tracks` table caused playback database calls to fail on fresh installations.
- Concurrent requests could previously register the same download twice.

### Known gaps and next work

- Explicitly verify one full offline second play.
- Add cookie upload/replacement/removal controls; cookie support is currently manual.
- Add optional authenticated landing content such as history, recommendations, and playlists.
- Add a conditional main page: cookies on shows YouTube recommendations; cookies off shows recent plus offline tracks; search replaces the main view.
- Add a YouTube-style sidebar with both persistent hamburger mode and hover-overlay behavior.
- Implement playback-aware downloading: 10-second lookahead, pause behavior, continued-current option, and queue prefetch.
- Add the settings panel: library-folder picker, memory mode, MP3-quality control, and download options.
- Implement library-folder changes and the copy/retain-existing-files prompt.
- Clean up orphaned unfinished `.part` files at startup.
- Add a long-lived background ffmpeg worker so audio processing does not cold-start for every track.
- Display download progress in the GUI.
- Add a supported way to seek beyond not-yet-downloaded bytes.
- Restore the last playing track and queue when the app opens.
- Focus search on startup and add a `Ctrl+L` search shortcut.
