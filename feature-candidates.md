# Feature candidates (selfimprove loop, 2026-10-08)

Brainstorm of every plausible feature for this app. Each item: what + why + rough size (S <1h, M few hours, L multi-session). Excludes already-implemented work (see to-do.md); "extends" notes where it builds on something existing.

## Playback

- Gapless playback — live albums/DJ sets pause between tracks today; gapless keeps them continuous. (M)
- Crossfade (configurable 0–12s) — smoother transitions, Spotify parity. (M)
- Volume normalization — replay-gain tags written at encode time, applied at play time so tracks match in loudness. (L)
- Sleep timer — stop/quit after N minutes; top user-requested player feature. (S)
- Playback speed (0.5–2x) — podcasts/long talks; extends realDuration/seek work. (S)
- 10-band equalizer — presets + custom, applied at play time via WebAudio (no re-encode). (L)
- Skip silence — auto-skip long silent intros/outros in talks. (M)
- A–B repeat — loop a section; practice/transcription use. (S)
- Chapter support — MP3 chapters for long files, chapter list + skip. (M)
- Lyrics view — synced LRC from local sidecar files first, providers later. (M)
- Always-on-top mini player — compact window surviving minimize. (M)
- System media-keys / OS media integration — play/pause from keyboard/OSD. (M)

## Queue

- Save queue as playlist — currently queues die with the session except one restore slot. (S)
- Queue history (already-played stack) — back-button through what actually played incl. shuffles. (M)
- Move to top / move to bottom per row — faster than grip-drag for long queues. (S)
- Remove duplicates button — dedupe by id in one click. (S)
- Clear played (keep upcoming) — one-click queue hygiene. (S)
- Export queue to M3U — portability of the current queue specifically. (S)

## Library & playlists

- Playlists CRUD (create/rename/delete/add/remove/reorder) — explicitly deferred earlier; the biggest missing library primitive. (L)
- Likes / favorites (heart) — one-tap save + Liked Songs auto-playlist. (M)
- Smart playlists — most-played, recently-added, unplayed, longest; extends play_count/last_played_at. (M)
- Album-art grid view — visual browsing alternative to rows. (M)
- Listening stats — top artists/tracks, hours played, streaks; extends watch history. (M)
- Full-library search (title+artist+playlist) — offline search covers downloads only today. (M)
- Duplicate finder — same audio under different ids/titles; merge or purge. (M)
- Tags / labels per track — user organization beyond playlists. (M)
- Ratings (1–5 stars) — sort/filter + smart playlists input. (S)

## Downloads

- Failed-downloads retry center — list failed/cancelled rows with one-click retry. (M)
- Pause-all / resume-all — extends per-track pause. (S)
- Per-track quality override — re-encode one track without changing global bitrate. (M)
- Scheduled / night downloads — bulk-fetch during off-hours; extends channel stagger. (M)
- Auto-download subscriptions — watch channels, staggered fetch of new uploads. (L)
- Storage meter + cleanup — per-track sizes exist; add totals + purge unplayed/old. (M)
- Corrupt-file detector + re-download — ffprobe pass over library, quarantine bad files. (M)

## Search & discovery

- Autoplay / Up-next radio — when queue ends, continue with related tracks. (L)
- Artist radio (from channel) — endless related from one seed; extends channel resolve. (M)
- Search filters — duration/upload-date/type; yt-dlp supports them. (M)
- Search history + suggestions — recent queries dropdown. (S)
- Trending page — a real view over the YouTube recommendations/trending feed used for home today. (S)
- New-uploads feed — from watched channels/playlists. (M)

## Robustness & data

- Backup / restore (library.db + settings + files index) — one-click safety. (M)
- In-app log viewer — spotify-clone.log is file-only today; surface tail in settings. (S)
- Crash recovery — reopen with the same queue + current track after a kill (whole-app state; per-track positions are the podcast-mode item below). (M)
- Health check on startup — ffmpeg/ffprobe/yt-dlp versions, disk space, codecs. (S)

## Settings & UX

- Themes (dark variants / accent color) — the UI is single-theme today. (M)
- Keyboard-shortcut cheatsheet + remapping — shortcuts exist but are undiscoverable. (M)
- System-tray minimize + close-to-tray — background listening. (M)
- Portable mode — keep everything (db/settings/files) next to the exe. (S)
- Multi-language UI — string table + 2–3 languages to start. (L)
- Onboarding tour — first-run guide through search/queue/downloads/settings. (S)

## Integrations

- Last.fm scrobbling — standard listening-history integration. (M)
- Import Spotify / YouTube playlists — via pasted URLs; extends add-links. (L)
- Export M3U / CSV of library — portability out. (S)
- Share track (copy YouTube link) — per-row share action. (S)
- Local-file import — drop existing MP3s into the library with metadata scan. (M)

## Long-form / podcasts

- Per-track resume position + bookmarks — remember where you left every long file with resume prompts, plus optional named bookmarks (podcast mode). (M)
