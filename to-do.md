# TO-DO — mixed implemented and pending features

> Status snapshot: 2026-10-04.
>
> - **Implemented:** persistent-library core, direct-to-MP3 downloading, offline replay from disk, instant-start playback (valid-frame gate + adaptive burst + `prepare_track` + hover prefetch; manual pre-buffer removed), hidden ffmpeg process, playback readiness/retry behavior, friendly network errors, and YouTube URL search.
> - **Still pending:** playback-aware download pacing, pause behavior, queue prefetch, memory mode, MP3-quality switch, settings panel, library-folder change flow, orphan cleanup, download-progress display, seeking into not-yet-downloaded bytes, startup session restore, search focus/shortcut, conditional home view, YouTube-style sidebar, and a long-lived background ffmpeg worker.

## 1. Playback-aware downloading

**Status: not implemented.**

**Default behaviour**

- Playback must be *live*: download the current track in the background as the
  connection allows, always keeping a **10 second buffer ahead** of the playhead.
- **Pause stops the download** — but only after the 10s-ahead margin is secured.
  In other words: play = stream + download ahead; pause = finish securing 10s,
  then stop fetching.

**Settings**

| # | Option | Default | Behaviour |
|---|--------|---------|-----------|
| 1 | Continue downloading the current track | OFF | When ON: download the current audio with **no 10s cap**, and keep downloading even while the audio is paused. |
| 2 | Download the queue as well | OFF | Only available when option 1 is ON (no point securing the queue if the current track isn't secured). Accepts an **integer >= 1**: how many upcoming queue items to keep secured. Re-checked whenever a new track starts: make sure the next N queue items are downloaded, **current playback has priority**. With a stable connection and N=2, the app secures the next 2, and after each new track starts it tops the margin back up to 2. |
| 3 | Persistent local library (always on) | ON | Every audio is downloaded and kept in persistent storage. **Implemented.** |

## 2. Local library, DB and storage

**Status: core implemented; folder-change behavior not implemented.**

- **Filename** = the YouTube title. **Implemented**, with Windows-unsafe characters removed and filename collisions disambiguated.
- **Local DB** records, per audio: `title`, `url`, `id` (when the video has one),
  and the `audio path`. **Implemented.**
- Next time an audio is requested it is **fetched from storage** instead of the
  network, and it is **accessible offline**. **Implemented for complete files.**
- **Settings: download folder picker.**
  - Default folder: the user's **Music** folder.
  - If the user changes the path, prompt:
    *"Do you want to copy the existing audios to the new path?"*
    - **Yes** → copy all existing audios to the new path and update the DB paths.
    - **No** → leave them where they are. The DB keeps the **old** paths for those
      files (so they are still fetched from there) while **new** downloads go to
      the new folder.

## 3. Real files, not DRM

**Status: implemented for successfully completed downloads.**

- The saved audios are **real, usable MP3 files** — not encrypted, not tied to
  the app, unlike Spotify.

## 4. Memory mode (GUI toggle, in the player)

**Status: not implemented.** A `memory_mode` setting key exists, but there is no
GUI toggle and the downloader does not yet use it.

- Default **OFF**.
- When ON: new audios are **not saved to disk**. They are loaded into memory,
  used for playback, and discarded when finished.
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

**Status: not implemented.** There is currently no landing view or hamburger sidebar.

- If cookies are on, the main page should show YouTube-style recommendations, preferably the exact recommendations YouTube would show if yt-dlp supports them.
- If cookies are off, the main page should show recently played tracks plus offline tracks.
- Any search should hide the main-page content and let search results take over.
- Add a left sidebar that can be opened by clicking a three-line menu icon or by hovering the left edge.
- Sidebar opened with the icon must remain open until the icon is clicked again.
- Icon-opened sidebar must not cover app content; search results and the layout must resize/rescale around it.
- Edge-hover sidebar must float above the app without resizing/rescaling it, and must close when the pointer leaves the sidebar frame.

## 7. Other pending work captured during implementation

- MP3 quality selector: tri-position 128/192/320 control, default 320, in the player scrubber.
- Settings panel: expose memory mode, continue-current, and queue depth (startup pre-buffer removed 2026-10-04 — auto-calibrated).
- Download-progress display wired to the existing backend progress data.
- Startup cleanup for orphaned unfinished download files.
- Skipped/interrupted-download handling: decide whether partial downloads resume, are retained, or are deleted.
- Download-folder change flow and prompt described in section 2.
- Cookie management UI: `cookies.txt` must currently be placed manually in the project root; the GUI only displays cookie status.
- Personalized landing content: search and playback work with or without cookies, but there is no YouTube watch-history, recommendations, preferences, or playlist landing page.
- Long-lived background ffmpeg worker: start the audio-processing worker when the app starts and keep it alive. Current behavior launches a short-lived ffmpeg process for each download and exits when that download completes or fails. The pending design must define a worker/service interface that can receive successive audio jobs without a cold start for every track.
- Session restore: when the app first opens, remember and restore the last playing track plus the queue.
- Search focus: focus the search box on first open, and add `Ctrl+L` as a shortcut that returns focus to it for typing.

## Constraints / notes

- Data budget on the current connection is very limited: no audio streaming
  tests, no dependency downloads, and no YouTube fetches without asking first.
- `/audio/<id>` serves complete cached files from disk with seeking, streams the
  growing MP3 while ffmpeg is writing, and can fall back to YouTube when no
  title/URL is known. Playback-aware pacing, pause behavior, and queue prefetch
  are still pending.
