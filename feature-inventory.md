# Feature / element inventory (2026-10-08)

Every user-facing option and element that exists in the app today. Used to find
missing BASIC essentials (the small things like shuffle / delete that every
music player needs). Big epics (playlists CRUD, equalizer, etc.) live in
`feature-candidates.md` and are out of scope for this comparison.

## Transport (player footer)

- Play / pause toggle (main button + per-row play buttons + settings mini-player)
- Previous: restarts track if >3s in, else previous queue item
- Next: next queue item (random item when shuffle ON); auto-advance on track end
- Shuffle toggle (random next/prev within queue; auto-enabled by context shuffle)
- Scrubber: seek bar with white played-fill, current/total time labels
- Volume slider (no mute button, no percentage readout)
- No repeat mode at all (no repeat-off / repeat-all / repeat-one)
- No playback speed, no sleep timer, no crossfade/gapless

## Now playing area

- Title + channel, download-progress bar (auto-hides; never on cache hits)
- Friendly error text in channel slot ("Playback failed…", "Stream unavailable")

## Queue panel

- Open via Queue button/badge, sidebar, Esc/outside-click/focus-loss dismiss
- Click row = play; per-row × remove; Clear (all); Add-links batch paste
- Grip-drag reorder (qIndex follows, persists to session)
- Shuffle queue in place (keeps current index); no save-as-playlist, no dedupe

## Main list (search / home / downloads)

- Rows: thumb, title, channel, duration, Play button, +Queue button
- Per-row Delete-from-disk button (Downloads view only, with confirm)
- Single click = select; double-click = play; hover = background prefetch
- Windows-style multi-select (Ctrl/Shift/keyboard) + rubber-band drag select
- Floating bar: "Add N to queue", "Delete", × (auto-closes after action)
- Search: auto-focus, Ctrl+L refocus, Esc clears; paged "Show more"
- Search finds tracks AND channel cards (channel → +Queue whole channel / staggered Download)
- Channel bulk download: 10s initial delay, one start per 30s, BG lanes
- Home: Recently-played + Recommended (cookies) or library sections
- Downloads: 5 sorts (recent/title/artist/duration ↑↓), artist group headers
- Context Shuffle buttons (home/search results, downloads toolbar)

## Sidebar / navigation

- Hamburger push mode, edge-hover overlay, Home / Downloads / Queue / Settings
- No right-click context menus anywhere; no keyboard-shortcut cheatsheet UI

## Settings modal (3 tabs)

- Playback: memory-mode toggle, offline-mode toggle, queue-depth stepper 0–10
- Storage: download-folder change + copy-prompt, MP3 quality 128/192/320 (player extras)
- Cookies: paste/validate/save/clear cookies.txt + extraction guide
- Live mini-player in modal header; toast feedback; Esc/backdrop close
- No themes, no language, no equalizer, no sleep timer, no key remapping

## Sessions / modes

- Session restore (queue + index, paused, cold-start on first gesture)
- Offline mode (local files only, incomplete skipped, search = local)
- Memory mode (no disk writes; rows purged on restart)
- Startup reconcile of stale downloads; skip = cancel + delete .part (no resume)

## Confirmations / safety

- confirm() on: cookies overwrite/clear, folder copy choice, single + bulk delete
- No undo for delete; no failed-download retry center; no storage meter

## Crew verdict — missing BASIC essentials (2026-10-08)

4 advisors (chatgpt, gemini, deepseek, qwen) ranked independently; perplexity
returned junk, excluded. Votes in brackets.

Unanimous [4/4]: mute button (S); repeat off/all/one (S); volume % readout (S);
failed-download retry (S); keyboard shortcuts + cheatsheet (S/M);
storage-usage meter (S/M); undo for destructive actions (M).
Strong [3/4]: right-click context menus (M); playback speed 0.5–2x (S);
sleep timer (S); system media keys (M).
Solid [2/4]: duration + channel in queue rows (S); track-change OS
notification (S); resume playback position (M); search within queue (S);
scrub seek tooltip (S); queue duplicate warning (S).
Single votes: stop button (S); tray minimize (M); output-device selector (M);
pause-all downloads (M); drag-drop file import (M).
Rejected (already exist): recently-played list, playback error text.
