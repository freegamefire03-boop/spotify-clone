# TO-DO — future features (not started)

## 1. Playback-aware downloading

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
| 3 | Persistent local library (always on) | ON | Every audio is downloaded and kept in persistent storage. |

## 2. Local library, DB and storage

- **Filename** = the YouTube title.
- **Local DB** records, per audio: `title`, `url`, `id` (when the video has one),
  and the `audio path`.
- Next time an audio is requested it is **fetched from storage** instead of the
  network, and it is **accessible offline**.
- **Settings: download folder picker.**
  - Default folder: the user's **Music** folder.
  - If the user changes the path, prompt:
    *"Do you want to copy the existing audios to the new path?"*
    - **Yes** → copy all existing audios to the new path and update the DB paths.
    - **No** → leave them where they are. The DB keeps the **old** paths for those
      files (so they are still fetched from there) while **new** downloads go to
      the new folder.

## 3. Real files, not DRM

- The saved audios are **real, usable MP3 files** — not encrypted, not tied to
  the app, unlike Spotify.

## 4. Memory mode (GUI toggle, in the player)

- Default **OFF**.
- When ON: new audios are **not saved to disk**. They are loaded into memory,
  used for playback, and discarded when finished.
- Audios already in local storage are still fetched from storage — this option
  only stops *saving new ones*.

---

## Constraints / notes

- Data budget on the current connection is very limited: no audio streaming
  tests, no dependency downloads, and no YouTube fetches without asking first.
- Playback currently streams through the in-process `/audio/<id>` route; the
  download features above will need that route to keep reading a file from disk
  once the track is cached, instead of proxying YouTube every time.
