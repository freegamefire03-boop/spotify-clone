# Changelog

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
