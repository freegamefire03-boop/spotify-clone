const audio = document.getElementById("audio");
const searchForm = document.getElementById("searchForm");
const searchInput = document.getElementById("searchInput");
const resultsList = document.getElementById("results");
const moreButton = document.getElementById("moreButton");
const emptyState = document.getElementById("emptyState");
const cookieStatus = document.getElementById("cookieStatus");
const npTitle = document.getElementById("npTitle");
const npChannel = document.getElementById("npChannel");
const playButton = document.getElementById("playButton");
const playIcon = document.getElementById("playIcon");
const pauseIcon = document.getElementById("pauseIcon");
const prevButton = document.getElementById("prevButton");
const nextButton = document.getElementById("nextButton");
const scrubber = document.getElementById("scrubber");
const currentTime = document.getElementById("currentTime");
const durationTime = document.getElementById("durationTime");
const volume = document.getElementById("volume");
const queueButton = document.getElementById("queueButton");
const queueBadge = document.getElementById("queueBadge");
const queuePanel = document.getElementById("queuePanel");
const queueList = document.getElementById("queueList");
const queueCount = document.getElementById("queueCount");
const clearQueue = document.getElementById("clearQueue");
const closeQueue = document.getElementById("closeQueue");
const addLinks = document.getElementById("addLinks");
const queueAdd = document.getElementById("queueAdd");
const queueLinks = document.getElementById("queueLinks");
const submitLinks = document.getElementById("submitLinks");
const cancelLinks = document.getElementById("cancelLinks");
const linksStatus = document.getElementById("linksStatus");
const homeTitle = document.getElementById("homeTitle");
const bitrateBtns = document.querySelectorAll(".bitrate-btn");
const settingsButton = document.getElementById("settingsButton");
const settingsBackdrop = document.getElementById("settingsBackdrop");
const settingsModal = document.getElementById("settingsModal");
const settingsX = document.getElementById("settingsX");
const mFooterClose = document.getElementById("mFooterClose");
const mMemoryCheck = document.getElementById("mMemoryCheck");
const mOfflineCheck = document.getElementById("mOfflineCheck");
const mQueueDepth = document.getElementById("mQueueDepth");
const mQMinus = document.getElementById("mQMinus");
const mQPlus = document.getElementById("mQPlus");
const mFolderPath = document.getElementById("mFolderPath");
const mChangeFolder = document.getElementById("mChangeFolder");
const mFolderError = document.getElementById("mFolderError");
const mCookiePill = document.getElementById("mCookiePill");
const mCookieState = document.getElementById("mCookieState");
const mCookieSize = document.getElementById("mCookieSize");
const mGuideBtn = document.getElementById("mGuideBtn");
const settingsGuideBackdrop = document.getElementById("settingsGuideBackdrop");
const mGuideClose = document.getElementById("mGuideClose");
const mCopySnippet = document.getElementById("mCopySnippet");
const mCookieSnippet = document.getElementById("mCookieSnippet");
const mCookieText = document.getElementById("mCookieText");
const mSaveCookies = document.getElementById("mSaveCookies");
const mClearCookies = document.getElementById("mClearCookies");
const mCookieError = document.getElementById("mCookieError");
const mToast = document.getElementById("mToast");
const mToastMsg = document.getElementById("mToastMsg");
const mMiniTitle = document.getElementById("mMiniTitle");
const mMiniArtist = document.getElementById("mMiniArtist");
const mMiniState = document.getElementById("mMiniState");
const mMiniPlay = document.getElementById("mMiniPlay");
const mMiniPrev = document.getElementById("mMiniPrev");
const mMiniNext = document.getElementById("mMiniNext");
const mMiniCur = document.getElementById("mMiniCur");
const mMiniDur = document.getElementById("mMiniDur");
const mMiniScrub = document.getElementById("mMiniScrub");
const mMiniFill = document.getElementById("mMiniFill");
const modalSettings = { memory_mode: false, offline_mode: false, queue_depth: 3 };
const menuButton = document.getElementById("menuButton");
const sidenav = document.getElementById("sidenav");
const edgeZone = document.getElementById("edgeZone");

const PAGE = 10;
const state = { query: "", results: [], total: 0, queue: [], qIndex: -1, current: null, loading: false, ready: false, mode: "home", home: [], homeMode: null };

// --- Multi-select state (main #results only) ---
const selectionSet = new Set();
let selCaret = null;
let selAnchor = null;

function rangeSlice(order, anchorId, targetId) {
  if (!order.length || targetId == null) return [];
  if (anchorId == null) return [targetId];
  const a = order.indexOf(anchorId);
  const b = order.indexOf(targetId);
  if (a === -1 || b === -1) return [targetId];
  const lo = Math.min(a, b), hi = Math.max(a, b);
  return order.slice(lo, hi + 1);
}

function toggleIdList(list, id) {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
}

function unionOrdered(a, b) {
  const seen = new Set(a);
  const out = [...a];
  for (const x of b) if (!seen.has(x)) { seen.add(x); out.push(x); }
  return out;
}

function moveCaret(order, caretId, delta) {
  if (!order.length) return null;
  let i = order.indexOf(caretId);
  if (i === -1) i = delta > 0 ? -1 : order.length;
  const j = Math.min(order.length - 1, Math.max(0, i + delta));
  return order[j];
}

function selectedIdsInDomOrder() {
  const order = [...resultsList.querySelectorAll(".row")].map((r) => r.dataset.id);
  const inSel = new Set(selectionSet);
  return order.filter((id) => inSel.has(id));
}

function domOrderIds() {
  return [...resultsList.querySelectorAll(".row")].map((r) => r.dataset.id);
}

function selectOnly(id) {
  selectionSet.clear();
  selectionSet.add(id);
  selCaret = id; selAnchor = id;
  syncSelectionUI();
}

function toggleOne(id) {
  if (selectionSet.has(id)) selectionSet.delete(id);
  else selectionSet.add(id);
  selCaret = id; selAnchor = id;
  syncSelectionUI();
}

function selectRangeTo(id, additive) {
  const ids = rangeSlice(domOrderIds(), selAnchor, id);
  if (additive) {
    for (const x of ids) selectionSet.add(x);
  } else {
    selectionSet.clear();
    for (const x of ids) selectionSet.add(x);
  }
  selCaret = id;
  if (selAnchor == null) selAnchor = id;
  syncSelectionUI();
}

function clearSelection(refocusContainer) {
  selectionSet.clear();
  selCaret = null; selAnchor = null;
  syncSelectionUI();
  if (refocusContainer) resultsList.focus({ preventScroll: true });
}

const navState = { push: false, overlay: false };
function renderNav() {
  document.body.classList.toggle("nav-push", navState.push);
  document.body.classList.toggle("nav-overlay", navState.overlay && !navState.push);
  const open = navState.push || navState.overlay;
  menuButton.setAttribute("aria-expanded", String(navState.push));
  menuButton.setAttribute("aria-label", navState.push ? "Close navigation" : "Open navigation");
  sidenav.setAttribute("aria-hidden", String(!open));
}
function setNav(push, overlay) {
  navState.push = push;
  navState.overlay = push ? false : overlay;
  renderNav();
}

const PLAY_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5 19 12 7 19.5z"/></svg>';
const PAUSE_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6.5" y="5" width="3.6" height="14"/><rect x="13.9" y="5" width="3.6" height="14"/></svg>';

// The GUI talks to Python directly through the pywebview bridge. There is no
// HTTP API in between; until the bridge is injected we show "not ready".
let backend = null;

function useBridge() {
  const api = window.pywebview && window.pywebview.api;
  if (!api) return false;
  backend = {
    search: (q, offset, limit) => api.search(q, offset, limit),
    status: () => api.status(),
    play: (id, title, url, upcoming, prevId, priority, prefetchOnly) => api.play(id, title, url, upcoming ?? null, prevId ?? null, priority ?? 0, prefetchOnly ?? false),
    prepareTrack: (id) => api.prepare_track(id),
    setBitrate: (val) => api.set_bitrate(val),
    downloadProgress: (id) => api.download_progress(id),
    getSettings: () => api.get_settings(),
    setMemoryMode: (val) => api.set_memory_mode(val),
    setQueueDepth: (val) => api.set_queue_depth(val),
    setOfflineMode: (val) => api.set_offline_mode(val),
    reportPlayhead: (id, s) => api.report_playhead(id, s),
    pauseDownload: (id) => api.pause_download(id),
    resumeDownload: (id) => api.resume_download(id),
    getLibraryDir: () => api.get_library_dir(),
    setLibraryDir: (p, copy) => api.set_library_dir(p, copy),
    getHome: () => api.get_home(),
    getDownloads: () => api.get_downloads(),
    getSession: () => api.get_session(),
    saveSession: (queue, qIndex) => api.save_session(queue, qIndex),
    getCookieStatus: () => api.get_cookie_status(),
    setCookiesFromText: (t, ow) => api.set_cookies_from_text(t, ow),
    removeCookies: () => api.remove_cookies(),
    addLinks: (text) => api.add_links(text),
  };
  state.ready = true;
  document.body.classList.add("native");
  refreshStatus();
  restoreSession().finally(() => {
    if (searchInput.value.trim()) runSearch(searchInput.value, false);
    else loadHome();
  });
  return true;
}

window.addEventListener("pywebviewready", useBridge);
if (!useBridge()) {
  cookieStatus.textContent = "starting...";
}

function focusSearch() {
  searchInput.focus();
  searchInput.select();
}

focusSearch();

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "l") {
    event.preventDefault();
    focusSearch();
  }
});

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0:00";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function setCookieStatus(cookies) {
  cookieStatus.textContent = cookies ? "cookies: on" : "cookies: off";
  cookieStatus.title = cookies
    ? "cookies.txt detected in the project root"
    : "No cookies.txt found - public results only";
}

function refreshStatus() {
  if (!backend) return;
  backend
    .status()
    .then((data) => {
      setCookieStatus(!!data.cookies);
      if (data.offline_mode) cookieStatus.textContent += " · offline";
      initBitrate(data);
    })
    .catch(() => setCookieStatus(false));
}

function initBitrate(status) {
  const bitrate = status.bitrate || 320;
  bitrateBtns.forEach(b => b.classList.toggle("active", Number(b.dataset.bitrate) === bitrate));
}

bitrateBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    const val = Number(btn.dataset.bitrate);
    bitrateBtns.forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    backend.setBitrate(val);
  });
});

let mToastTimer = null;
function showMToast(msg) {
  mToastMsg.textContent = msg;
  mToast.hidden = false;
  requestAnimationFrame(() => mToast.classList.add("show"));
  clearTimeout(mToastTimer);
  mToastTimer = setTimeout(() => {
    mToast.classList.remove("show");
    setTimeout(() => { mToast.hidden = true; }, 300);
  }, 2500);
}

function setCheck(btn, on) {
  btn.setAttribute("aria-checked", String(!!on));
  btn.textContent = on ? "✔" : "";
}

function refreshCookiePill() {
  if (!backend) return Promise.resolve();
  return backend.getCookieStatus().then((c) => {
    if (c && c.present) {
      const kb = (Number(c.size) / 1024).toFixed(1);
      const when = c.mtime ? new Date(c.mtime * 1000).toLocaleDateString() : "";
      mCookieState.textContent = "cookies on";
      mCookieSize.textContent = `${kb} KB${when ? " · " + when : ""}`;
      mCookiePill.classList.remove("off");
    } else {
      mCookieState.textContent = "cookies off";
      mCookieSize.textContent = "missing";
      mCookiePill.classList.add("off");
    }
  }).catch(() => {
    mCookieState.textContent = "cookies off";
    mCookieSize.textContent = "missing";
    mCookiePill.classList.add("off");
  });
}

function syncMiniPlayer() {
  if (!state.current) {
    mMiniTitle.textContent = "Nothing playing";
    mMiniArtist.textContent = "Pick a track";
  } else {
    mMiniTitle.textContent = state.current.title || "Unknown";
    mMiniArtist.textContent = state.current.channel || "";
  }
  const playing = !audio.paused && !audio.ended && !!state.current;
  mMiniPlay.textContent = playing ? "❚❚" : "▶";
  mMiniState.textContent = playing ? "Playing" : "Paused";
  const cur = Number(audio.currentTime) || 0;
  const total = Number.isFinite(audio.duration) && audio.duration > 0
    ? audio.duration : (Number(state.current && state.current.duration) || 0);
  mMiniCur.textContent = formatTime(cur);
  mMiniDur.textContent = formatTime(total);
  mMiniFill.style.width = total > 0 ? Math.min(100, (cur / total) * 100) + "%" : "0%";
}

function openSettings() {
  queuePanel.hidden = true;
  settingsBackdrop.hidden = false;
  if (!backend) return;
  backend.getSettings().then((s) => {
    modalSettings.memory_mode = !!s.memory_mode;
    modalSettings.offline_mode = !!s.offline_mode;
    modalSettings.queue_depth = Math.max(0, Math.min(10, Math.floor(Number(s.queue_depth ?? 3) || 0)));
    setCheck(mMemoryCheck, modalSettings.memory_mode);
    setCheck(mOfflineCheck, modalSettings.offline_mode);
    mQueueDepth.value = modalSettings.queue_depth;
  }).catch(() => {});
  backend.getLibraryDir().then((d) => { if (d && d.path) mFolderPath.value = d.path; }).catch(() => {});
  mFolderError.hidden = true;
  mCookieError.hidden = true;
  mCookieText.value = "";
  refreshCookiePill();
  syncMiniPlayer();
}

function closeSettingsPanel() {
  settingsBackdrop.hidden = true;
}

function closeSettingsGuide() {
  settingsGuideBackdrop.hidden = true;
}

function closeQueuePanel() {
  queuePanel.hidden = true;
}

function rowMarkup(track) {
  return `
    <li class="row" data-id="${track.id}" tabindex="-1" aria-selected="false">
      <img class="thumb" src="${track.thumbnail}" alt="" loading="lazy" />
      <div class="meta">
        <div class="title">${escapeHtml(track.title)}</div>
        <div class="channel">${escapeHtml(track.channel || "Unknown channel")} <span class="queued-flag" data-queued hidden></span></div>
      </div>
      <div class="duration">${track.duration ? formatTime(track.duration) : ""}</div>
      <div class="row-actions">
        <button class="play-button" data-action="play" title="Play now" aria-label="Play now">${PLAY_SVG}</button>
        <button class="icon-button" data-action="queue" title="Add to queue" aria-label="Add to queue">
          <svg viewBox="0 0 24 24" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        </button>
      </div>
    </li>`;
}

function updateRowStates() {
  const playing = !audio.paused && !audio.ended;
  document.querySelectorAll(".row").forEach((row) => {
    const isCurrent = Boolean(state.current && state.current.id === row.dataset.id);
    row.classList.toggle("is-current", isCurrent);
    const playBtn = row.querySelector('button[data-action="play"]');
    if (playBtn) {
      playBtn.innerHTML = isCurrent && playing ? PAUSE_SVG : PLAY_SVG;
      playBtn.title = isCurrent && playing ? "Pause" : "Play now";
    }
    const flag = row.querySelector("[data-queued]");
    const index = state.queue.findIndex((t) => t.id === row.dataset.id);
    if (index >= 0) {
      flag.hidden = false;
      flag.textContent = `• queued #${index + 1}`;
    } else {
      flag.hidden = true;
      flag.textContent = "";
    }
  });
  syncSelectionUI();
}

const selBar = document.getElementById("selBar");
const selAddBtn = document.getElementById("selAddBtn");
const selClearBtn = document.getElementById("selClearBtn");

function syncSelectionUI() {
  document.querySelectorAll("#results .row").forEach((row) => {
    const on = selectionSet.has(row.dataset.id);
    row.classList.toggle("is-selected", on);
    row.setAttribute("aria-selected", String(on));
    if (row.dataset.id === selCaret) row.dataset.caret = "1";
    else delete row.dataset.caret;
  });
  updateSelBar();
}

function updateSelBar() {
  const n = selectionSet.size;
  selBar.hidden = n === 0;
  selAddBtn.textContent = `Add ${n} to queue`;
}

function renderResults() {
  if (state.mode === "home") {
    const sections = state.homeSections && state.homeSections.length
      ? state.homeSections
      : [{ title: "", items: state.results }];
    resultsList.innerHTML = sections.map((s) =>
      (s.title ? `<h2 class="home-section-title">${escapeHtml(s.title)}</h2>` : "") +
      s.items.map(rowMarkup).join("")).join("");
    emptyState.hidden = state.results.length > 0;
    homeTitle.hidden = true;
    moreButton.hidden = true;
    updateRowStates();
    return;
  }
  if (state.mode === "downloads") {
    resultsList.innerHTML = (state.results.length
      ? `<h2 class="home-section-title">Downloads</h2>` : "")
      + state.results.map(rowMarkup).join("");
    emptyState.hidden = state.results.length > 0;
    if (!state.results.length)
      emptyState.querySelector("p").textContent = "No downloads yet — play something to save it.";
    homeTitle.hidden = true;
    moreButton.hidden = true;
    updateRowStates();
    return;
  }
  homeTitle.hidden = true;
  resultsList.innerHTML = state.results.map(rowMarkup).join("");
  emptyState.hidden = state.results.length > 0;
  moreButton.hidden = state.results.length === 0 || state.results.length >= state.total;
  moreButton.textContent = state.loading ? "Loading..." : `Show more (${state.results.length}/${state.total})`;
  moreButton.disabled = state.loading;
  updateRowStates();
}

async function loadHome() {
  if (!backend) return;
  state.mode = "home";
  state.query = "";
  searchInput.value = "";
  homeTitle.hidden = true;
  moreButton.hidden = true;
  if (state.home.length) {
    state.results = state.home;
    renderResults();
    return;
  }
  emptyState.hidden = false;
  emptyState.querySelector("p").textContent = "Loading home...";
  try {
    const data = await backend.getHome();
    state.homeMode = data.mode;
    state.home = data.items || [];
    state.homeSections = data.sections || [{ title: "", items: state.home }];
    state.results = state.home;
    if (!state.home.length) {
      emptyState.hidden = false;
      emptyState.querySelector("p").textContent = "Search for anything to start listening.";
    }
  } catch (_) {
    state.home = [];
    state.homeSections = [];
    state.results = [];
    emptyState.hidden = false;
    emptyState.querySelector("p").textContent = "Search for anything to start listening.";
  }
  renderResults();
}

async function loadDownloads() {
  if (!backend) return;
  state.mode = "downloads";
  state.query = "";
  searchInput.value = "";
  homeTitle.hidden = true;
  moreButton.hidden = true;
  state.results = [];
  state.total = 0;
  emptyState.hidden = false;
  emptyState.querySelector("p").textContent = "Loading downloads...";
  renderResults();
  try {
    const data = await backend.getDownloads();
    state.results = data.items || [];
    state.total = data.total ?? state.results.length;
  } catch (_) {
    state.results = [];
    state.total = 0;
  }
  renderResults();
}

async function runSearch(query, append) {
  const trimmed = query.trim();
  if (!trimmed || state.loading) return;
  if (!backend) {
    emptyState.hidden = false;
    emptyState.querySelector("p").textContent = "Backend not ready yet.";
    return;
  }
  state.mode = "search";
  homeTitle.hidden = true;
  state.loading = true;
  const offset = append ? state.results.length : 0;
  if (!append) {
    state.query = trimmed;
    state.results = [];
    emptyState.hidden = false;
    emptyState.querySelector("p").textContent = "Searching YouTube...";
  }
  renderResults();
  try {
    const data = await backend.search(trimmed, offset, PAGE);
    setCookieStatus(!!data.cookies);
    state.total = data.total || 0;
    state.results = append ? state.results.concat(data.items) : data.items;
    if (state.total === 0) {
      emptyState.hidden = false;
      emptyState.querySelector("p").textContent = `No results for "${trimmed}".`;
    }
  } catch (err) {
    if (!append) {
      state.results = [];
      state.total = 0;
      emptyState.hidden = false;
      emptyState.querySelector("p").textContent = `Search failed: ${err.message || err}`;
    }
  } finally {
    state.loading = false;
    renderResults();
  }
}

function trackById(id) {
  return state.results.find((t) => t.id === id) || state.queue.find((t) => t.id === id) || null;
}

function renderQueue() {
  queueBadge.textContent = state.queue.length;
  queueCount.textContent = state.queue.length;
  queueList.innerHTML = state.queue
    .map((track, index) => `
      <li class="queue-item${state.current && state.current.id === track.id ? " is-current" : ""}" data-index="${index}">
        <span class="q-title">${index + 1}. ${escapeHtml(track.title)}</span>
        <button class="q-remove" data-remove="${index}" title="Remove" aria-label="Remove">&times;</button>
      </li>`)
    .join("");
}

function updateTransportState() {
  const playing = !audio.paused && !audio.ended;
  playIcon.classList.toggle("icon-hidden", playing);
  pauseIcon.classList.toggle("icon-hidden", !playing);
  playButton.title = playing ? "Pause" : "Play";
  playButton.setAttribute("aria-label", playButton.title);
}

function updateNowPlaying() {
  if (!state.current) {
    npTitle.textContent = "Nothing playing";
    npChannel.textContent = "Pick a track with the play button";
    syncMiniPlayer();
    return;
  }
  npTitle.textContent = state.current.title;
  npChannel.textContent = state.current.channel || "";
  syncMiniPlayer();
}

async function getCompleteIds() {
  try {
    const data = await backend.getDownloads();
    return new Set((data.items || []).map((t) => t.id));
  } catch (_) {
    return new Set();
  }
}

function showNothingDownloaded() {
  state.current = null;
  audio.removeAttribute("src");
  audio.load();
  npTitle.textContent = "Nothing downloaded";
  npChannel.textContent = "Offline mode — play a downloaded track";
  updateTransportState();
  updateRowStates();
}

async function playNextComplete(seen = new Set()) {
  const complete = await getCompleteIds();
  while (state.qIndex + 1 < state.queue.length) {
    state.qIndex += 1;
    const next = state.queue[state.qIndex];
    if (seen.has(next.id)) continue;
    seen.add(next.id);
    if (!complete.has(next.id)) continue;
    return playTrack(next, seen);
  }
  showNothingDownloaded();
  return false;
}

async function playPrevComplete(seen = new Set()) {
  const complete = await getCompleteIds();
  while (state.qIndex - 1 >= 0) {
    state.qIndex -= 1;
    const prev = state.queue[state.qIndex];
    if (seen.has(prev.id)) continue;
    seen.add(prev.id);
    if (!complete.has(prev.id)) continue;
    return playTrack(prev, seen);
  }
  return false;
}

async function playTrack(track, _skipSeen = new Set()) {
  state.errorRetried = false;
  const prevId = state.current && state.current.id !== track.id ? state.current.id : null;
  state.current = track;
  lastPlayheadSent = -10;
  lastPlayheadAt = 0;
  const idx = state.queue.findIndex((t) => t.id === track.id);
  if (idx === -1) state.queue.splice(Math.max(state.qIndex + 1, 0), 0, track);
  state.qIndex = state.queue.findIndex((t) => t.id === track.id);
  scheduleSaveSession();

  updateNowPlaying();
  renderQueue();
  updateRowStates();

  if (backend && backend.downloadProgress) {
    startDlPolling(track.id);
  }

  scrubber.value = 0;
  currentTime.textContent = "0:00";
  const knownDuration = Number(track.duration) || 0;
  if (knownDuration > 0) scrubber.max = String(knownDuration);

  // Register the track and kick off the local-library download before playback,
  // so /audio/<id> can serve the file (or the partial file) from disk.
  // The upcoming window lets the backend prefetch the next queue items
  // (Option 2); it trims to queue_depth and ignores it when Option 1 is off.
  if (backend && backend.play) {
    const upcoming = state.queue
      .slice(state.qIndex + 1, state.qIndex + 1 + 5)
      .map((t) => ({ id: t.id, title: t.title, url: `https://www.youtube.com/watch?v=${t.id}` }));
    let playRes = null;
    try {
      playRes = await backend.play(track.id, track.title, `https://www.youtube.com/watch?v=${track.id}`, upcoming, prevId);
    } catch (_) { playRes = null; }
    if (playRes && playRes.skipped) {
      _skipSeen.add(track.id);
      return playNextComplete(_skipSeen);
    }
  }

  // Wait until the server can serve a valid MP3 prefix before assigning src.
  if (backend && backend.prepareTrack) {
    try {
      await backend.prepareTrack(track.id);
    } catch (_) {}
  }

  // The <audio> element cannot read from the Python bridge, so the bytes come
  // from the /audio route that pywebview serves from this same process.
  audio.preload = "auto";
  audio.src = `/audio/${encodeURIComponent(track.id)}`;
  await startPlayback();
  updateTransportState();
  updateRowStates();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

let dlPollTimer = null;
let dlPollGen = 0;

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function stopDlPolling() {
  dlPollGen++;
  if (dlPollTimer) { clearInterval(dlPollTimer); dlPollTimer = null; }
}

async function startDlPolling(videoId) {
  stopDlPolling();
  const gen = dlPollGen;
  const el = document.getElementById("npDownload");
  const fill = document.getElementById("npDlFill");
  const text = document.getElementById("npDlText");

  let data;
  try {
    data = await backend.downloadProgress(videoId);
  } catch (_) {
    return;
  }

  if ((data.status || "unknown") === "unknown") {
    const deadline = Date.now() + 3000;
    while ((data.status || "unknown") === "unknown" && Date.now() < deadline) {
      await sleep(500);
      if (gen !== dlPollGen) return;
      try {
        data = await backend.downloadProgress(videoId);
      } catch (_) {
        return;
      }
    }
  }
  if (gen !== dlPollGen) return;
  if ((data.status || "unknown") !== "downloading") return;

  const written = Number(data.written) || 0;
  const total = Number(data.total) || 0;
  const pct = total > 0 ? Math.min(100, Math.round((written / total) * 100)) : 0;
  el.hidden = false;
  fill.style.width = pct + "%";
  text.textContent = total > 0
    ? `${formatBytes(written)} / ${formatBytes(total)} (${pct}%)`
    : "Starting download...";

  dlPollTimer = setInterval(async () => {
    if (!backend) return;
    try {
      const d = await backend.downloadProgress(videoId);
      const st = d.status || "unknown";
      const w = Number(d.written) || 0;
      const t = Number(d.total) || 0;
      const p = t > 0 ? Math.min(100, Math.round((w / t) * 100)) : 0;

      if (st === "downloading") {
        fill.style.width = p + "%";
        text.textContent = t > 0
          ? `${formatBytes(w)} / ${formatBytes(t)} (${p}%)`
          : `${formatBytes(w)} downloaded`;
      } else if (st === "done") {
        fill.style.width = "100%";
        text.textContent = "Download complete";
        stopDlPolling();
        setTimeout(() => { el.hidden = true; }, 2000);
      } else if (st === "failed" || st === "cancelled") {
        text.textContent = st === "failed" ? "Download failed" : "Download cancelled";
        stopDlPolling();
        setTimeout(() => { el.hidden = true; }, 2000);
      }
    } catch (_) {
      stopDlPolling();
      el.hidden = true;
    }
  }, 1000);
}

async function startPlayback() {
  audio.load();

  await new Promise((resolve, reject) => {
    if (audio.readyState >= 2) return resolve();
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    const fail = () => {
      if (done) return;
      done = true;
      reject(new Error(audio.error ? audio.error.message : "media error"));
    };
    audio.addEventListener("canplay", finish, { once: true });
    audio.addEventListener("loadeddata", finish, { once: true });
    audio.addEventListener("error", fail, { once: true });
    setTimeout(finish, 3000);
  });

  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await audio.play();
      return;
    } catch (err) {
      if (err.name === "AbortError") return;
      if (attempt === 2) {
        npChannel.textContent = `Playback failed: ${err.message}`;
        return;
      }
      await sleep(80);
    }
  }
}

function addToQueue(track) {
  state.queue.push(track);
  renderQueue();
  updateRowStates();
  scheduleSaveSession();
}

function toggleAddLinks() {
  queueAdd.hidden = !queueAdd.hidden;
  if (!queueAdd.hidden) {
    linksStatus.textContent = "";
    queueLinks.value = "";
    queueLinks.focus();
  }
}

async function submitQueueLinks() {
  const text = queueLinks.value;
  if (!text.trim()) {
    linksStatus.textContent = "Paste some links first.";
    return;
  }
  linksStatus.textContent = "Resolving...";
  submitLinks.disabled = true;
  try {
    const res = await backend.addLinks(text);
    if (!res || !res.ok) {
      linksStatus.textContent = (res && res.error) || "Could not add links.";
      return;
    }
    const items = Array.isArray(res.items) ? res.items : [];
    const existing = new Set(state.queue.map((t) => t.id));
    let added = 0;
    for (const track of items) {
      if (!track || !track.id || existing.has(track.id)) continue;
      existing.add(track.id);
      addToQueue(track);
      added++;
    }
    const parts = [`Added ${added} track${added === 1 ? "" : "s"}`];
    if (res.skipped) parts.push(`${res.skipped} skipped`);
    if (res.errors && res.errors.length) parts.push(`${res.errors.length} failed`);
    linksStatus.textContent = parts.join(", ");
    queueLinks.value = "";
  } catch (_) {
    linksStatus.textContent = "Could not add links.";
  } finally {
    submitLinks.disabled = false;
  }
}

let sessionSaveTimer = null;
function sanitizeQueueForSave() {
  return state.queue.slice(0, 100).map((t) => ({
    id: t.id,
    title: t.title,
    channel: t.channel || "",
    duration: Number(t.duration) || 0,
    thumbnail: t.thumbnail || "",
  })).filter((t) => t.id);
}
function scheduleSaveSession() {
  if (!backend || !backend.saveSession) return;
  clearTimeout(sessionSaveTimer);
  sessionSaveTimer = setTimeout(() => {
    backend.saveSession(sanitizeQueueForSave(), state.qIndex).catch(() => {});
  }, 500);
}

async function restoreSession() {
  if (!backend || !backend.getSession) return;
  try {
    const s = await backend.getSession();
    if (!s || !Array.isArray(s.queue) || !s.queue.length) return;
    state.queue = s.queue;
    const qi = Number(s.qIndex);
    state.qIndex = Number.isInteger(qi) && qi >= 0 && qi < state.queue.length ? qi : -1;
    state.current = state.qIndex >= 0 ? state.queue[state.qIndex] : null;
    audio.removeAttribute("src");
    audio.load();
    updateNowPlaying();
    renderQueue();
    updateRowStates();
    updateTransportState();
  } catch (_) {}
}

function playNext(_skipSeen = new Set()) {
  if (state.qIndex + 1 < state.queue.length) {
    state.qIndex += 1;
    return playTrack(state.queue[state.qIndex], _skipSeen);
  }
  return false;
}

function playPrev() {
  if (audio.currentTime > 3) {
    audio.currentTime = 0;
    return true;
  }
  if (modalSettings.offline_mode) {
    return playPrevComplete();
  }
  if (state.qIndex > 0) {
    state.qIndex -= 1;
    playTrack(state.queue[state.qIndex]);
    return true;
  }
  return false;
}

function togglePlayback() {
  if (!state.current) return;
  if (!audio.getAttribute("src")) return playTrack(state.current);
  if (audio.paused) audio.play().catch(() => {});
  else audio.pause();
}

resultsList.addEventListener("pointerdown", (event) => {
  if (event.button === 2) return;
  const row = event.target.closest(".row");
  if (!row || !resultsList.contains(row)) return;
  const id = row.dataset.id;
  if (!id) return;
  const mod = event.ctrlKey || event.metaKey;
  if (mod && event.shiftKey) selectRangeTo(id, true);
  else if (mod) toggleOne(id);
  else if (event.shiftKey) selectRangeTo(id, false);
  else selectOnly(id);
  row.focus({ preventScroll: true });
});

resultsList.addEventListener("keydown", (event) => {
  const order = domOrderIds();
  if (!order.length) return;
  const inResults = resultsList.contains(document.activeElement) || document.activeElement === resultsList;
  if (!inResults) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "") && document.activeElement !== resultsList) return;

  const mod = event.ctrlKey || event.metaKey;
  const focusCaret = () => {
    const el = resultsList.querySelector(`.row[data-id="${CSS.escape(selCaret ?? "")}"]`);
    if (el) el.focus({ preventScroll: false });
  };

  if (mod && (event.key === "a" || event.key === "A")) {
    event.preventDefault();
    selectionSet.clear();
    for (const id of order) selectionSet.add(id);
    selCaret = order[order.length - 1];
    selAnchor = order[0];
    syncSelectionUI();
    return;
  }
  if (mod && event.key === " ") {
    event.preventDefault();
    if (selCaret != null) toggleOne(selCaret);
    return;
  }
  const delta = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1
    : event.key === "ArrowUp" || event.key === "ArrowLeft" ? -1 : 0;
  if (delta !== 0) {
    event.preventDefault();
    const next = moveCaret(order, selCaret, delta);
    if (next == null) return;
    if (event.shiftKey) {
      if (selAnchor == null) selAnchor = selCaret ?? next;
      const ids = new Set(rangeSlice(order, selAnchor, next));
      if (!mod) {
        selectionSet.clear();
        for (const x of ids) selectionSet.add(x);
      } else {
        for (const x of ids) selectionSet.add(x);
      }
      selCaret = next;
      syncSelectionUI();
      focusCaret();
    } else if (mod) {
      selCaret = next;
      syncSelectionUI();
      focusCaret();
    } else {
      selectOnly(next);
      focusCaret();
    }
  }
});

// NOTE: selection lives on pointerdown; click stays button-only so a future dblclick→play hook is trivial.
resultsList.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  const row = event.target.closest(".row");
  if (!row) return;
  const track = trackById(row.dataset.id);
  if (!track) return;
  if (button && button.dataset.action === "play") {
    if (state.current && state.current.id === track.id) togglePlayback();
    else playTrack(track);
  } else if (button && button.dataset.action === "queue") {
    addToQueue(track);
  }
});

let hoverTimer = null;
resultsList.addEventListener("mouseover", (event) => {
  const row = event.target.closest(".row");
  if (!row) return;
  const track = trackById(row.dataset.id);
  if (!track || !backend) return;
  if (hoverTimer) clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => {
    // Hover warmup: background lane (priority 1), prefetch-only (no play
    // history, no playhead/paused disturbance). Never queues ahead of clicks.
    backend.play(track.id, track.title, `https://www.youtube.com/watch?v=${track.id}`, null, null, 1, true).catch(() => {});
  }, 150);
});

queueList.addEventListener("click", (event) => {
  const remove = event.target.closest("[data-remove]");
  if (remove) {
    event.stopPropagation();
    const index = Number(remove.dataset.remove);
    state.queue.splice(index, 1);
    if (state.qIndex >= index) state.qIndex -= 1;
    renderQueue();
    updateRowStates();
    scheduleSaveSession();
    return;
  }
  const item = event.target.closest(".queue-item");
  if (item) {
    state.qIndex = Number(item.dataset.index);
    playTrack(state.queue[state.qIndex]);
  }
});

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch(searchInput.value, false);
});

searchInput.addEventListener("input", () => {
  if (!searchInput.value.trim() && state.mode === "search") loadHome();
});

searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (!settingsGuideBackdrop.hidden) { closeSettingsGuide(); event.preventDefault(); return; }
    if (!settingsBackdrop.hidden) { closeSettingsPanel(); event.preventDefault(); return; }
    if (!queuePanel.hidden) return;
    if (selectionSet.size) { clearSelection(false); event.preventDefault(); return; }
    searchInput.value = "";
    loadHome();
    if (navState.overlay) setNav(false, false);
  }
});

moreButton.addEventListener("click", () => runSearch(state.query, true));
playButton.addEventListener("click", () => togglePlayback());
prevButton.addEventListener("click", () => playPrev());
nextButton.addEventListener("click", () => playNext());
queueButton.addEventListener("click", () => {
  queuePanel.hidden = !queuePanel.hidden;
  if (!queuePanel.hidden) settingsBackdrop.hidden = true;
});
closeQueue.addEventListener("click", closeQueuePanel);

menuButton.addEventListener("click", () => setNav(!navState.push, false));
edgeZone.addEventListener("mouseenter", () => {
  if (!navState.push && !navState.overlay) setNav(false, true);
});
sidenav.addEventListener("mouseleave", () => {
  if (navState.overlay && !navState.push) setNav(false, false);
});
sidenav.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-nav]");
  if (!btn) return;
  if (btn.dataset.nav === "home") loadHome();
  if (btn.dataset.nav === "downloads") loadDownloads();
  if (btn.dataset.nav === "queue") { queuePanel.hidden = false; settingsBackdrop.hidden = true; }
  if (btn.dataset.nav === "settings") openSettings();
  if (navState.overlay) setNav(false, false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (!settingsGuideBackdrop.hidden) { closeSettingsGuide(); return; }
    if (!settingsBackdrop.hidden) { closeSettingsPanel(); return; }
    if (!queuePanel.hidden) { queuePanel.hidden = true; return; }
    if (selectionSet.size) { clearSelection(true); return; }
    if (navState.overlay && document.activeElement !== searchInput) setNav(false, false);
  }
});

document.addEventListener("click", (event) => {
  if (queuePanel.hidden) return;
  if (queuePanel.contains(event.target) || event.target === queueButton) return;
  queuePanel.hidden = true;
});

queuePanel.addEventListener("focusout", (event) => {
  if (queuePanel.contains(event.relatedTarget) || event.relatedTarget === queueButton) return;
  queuePanel.hidden = true;
});

clearQueue.addEventListener("click", () => {
  state.queue = [];
  state.qIndex = -1;
  renderQueue();
  updateRowStates();
  scheduleSaveSession();
});

selAddBtn.addEventListener("click", () => {
  if (!selectionSet.size) return;
  const queued = new Set(state.queue.map((t) => t.id));
  const domIds = selectedIdsInDomOrder();
  const domSet = new Set(domIds);
  const stale = [...selectionSet].filter((id) => !domSet.has(id));
  for (const id of [...domIds, ...stale]) {
    if (queued.has(id)) continue;
    const track = trackById(id);
    if (!track) continue;
    state.queue.push(track);
    queued.add(id);
  }
  renderQueue();
  updateRowStates();
  scheduleSaveSession();
});
selClearBtn.addEventListener("click", () => clearSelection(false));

addLinks.addEventListener("click", toggleAddLinks);
cancelLinks.addEventListener("click", toggleAddLinks);
submitLinks.addEventListener("click", submitQueueLinks);
queueLinks.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    submitQueueLinks();
  }
});

function refreshHomeAfterCookieChange() {
  refreshStatus();
  state.home = [];
  state.homeSections = [];
  loadHome();
}

window.addEventListener("pagehide", () => {
  if (sessionSaveTimer) {
    clearTimeout(sessionSaveTimer);
    sessionSaveTimer = null;
    if (backend && backend.saveSession)
      backend.saveSession(sanitizeQueueForSave(), state.qIndex).catch(() => {});
  }
});

settingsButton.addEventListener("click", openSettings);
settingsX.addEventListener("click", closeSettingsPanel);
mFooterClose.addEventListener("click", closeSettingsPanel);
settingsBackdrop.addEventListener("mousedown", (e) => { if (e.target === settingsBackdrop) closeSettingsPanel(); });

settingsModal.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    settingsModal.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const name = btn.dataset.tab;
    settingsModal.querySelectorAll(".panel-page").forEach((p) => { p.hidden = p.dataset.page !== name; });
  });
});

mGuideBtn.addEventListener("click", () => { settingsGuideBackdrop.hidden = false; });
mGuideClose.addEventListener("click", closeSettingsGuide);
settingsGuideBackdrop.addEventListener("mousedown", (e) => { if (e.target === settingsGuideBackdrop) closeSettingsGuide(); });
mCopySnippet.addEventListener("click", async () => {
  const text = mCookieSnippet.textContent;
  try {
    await navigator.clipboard.writeText(text);
    showMToast("Snippet copied — paste it in your console.");
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
    showMToast("Snippet copied — paste it in your console.");
  }
});

mMemoryCheck.addEventListener("click", () => {
  const on = mMemoryCheck.getAttribute("aria-checked") !== "true";
  setCheck(mMemoryCheck, on);
  modalSettings.memory_mode = on;
  if (backend) backend.setMemoryMode(on);
});

mOfflineCheck.addEventListener("click", async () => {
  const on = mOfflineCheck.getAttribute("aria-checked") !== "true";
  setCheck(mOfflineCheck, on);
  modalSettings.offline_mode = on;
  try { if (backend) await backend.setOfflineMode(on); } catch (_) {}
  refreshStatus();
  if (on) { closeSettingsPanel(); loadDownloads(); }
});

function clampMQueue() {
  let v = Math.floor(Number(mQueueDepth.value) || 0);
  v = Math.max(0, Math.min(10, v));
  mQueueDepth.value = v;
  return v;
}

function stepMQueue(d) {
  let v = Math.floor(Number(mQueueDepth.value) || 0);
  if (!Number.isFinite(v)) v = 0;
  v = Math.max(0, Math.min(10, v + d));
  mQueueDepth.value = v;
  if (backend) backend.setQueueDepth(v);
}

mQMinus.addEventListener("pointerdown", (e) => { e.preventDefault(); mQMinus.focus(); stepMQueue(-1); });
mQPlus.addEventListener("pointerdown", (e) => { e.preventDefault(); mQPlus.focus(); stepMQueue(1); });
mQueueDepth.addEventListener("input", () => { if (mQueueDepth.value !== "") clampMQueue(); });
mQueueDepth.addEventListener("blur", () => { const v = clampMQueue(); if (backend) backend.setQueueDepth(v); });
mQueueDepth.addEventListener("change", () => { const v = clampMQueue(); if (backend) backend.setQueueDepth(v); });

mChangeFolder.addEventListener("click", async () => {
  const newPath = mFolderPath.value.trim();
  if (!newPath || !backend) return;
  mFolderError.hidden = true;
  const copy = confirm("Do you want to copy the existing audios to the new path?");
  mChangeFolder.disabled = true;
  try {
    const res = await backend.setLibraryDir(newPath, copy);
    if (!res.ok) {
      mFolderError.textContent = res.error || "Could not change folder.";
      mFolderError.hidden = false;
      showMToast("Could not change folder.");
    } else if (res.changed && copy) {
      const skipped = res.skipped && res.skipped.length ? ` (${res.skipped.length} skipped)` : "";
      mFolderError.textContent = `Moved to ${res.path}: ${res.copied} copied${skipped}.`;
      mFolderError.hidden = false;
      mFolderPath.value = res.path;
      showMToast(`Folder changed · ${res.copied} copied${skipped}`);
    } else if (res.changed) {
      mFolderPath.value = res.path;
      showMToast("Download folder changed.");
    } else { showMToast("Folder unchanged."); }
  } catch (_) {
    mFolderError.textContent = "Could not change folder.";
    mFolderError.hidden = false;
    showMToast("Could not change folder.");
  } finally { mChangeFolder.disabled = false; }
});

mSaveCookies.addEventListener("click", async () => {
  const text = mCookieText.value;
  if (!text.trim()) {
    mCookieError.textContent = "Paste cookies.txt content first.";
    mCookieError.hidden = false;
    return;
  }
  mCookieError.hidden = true;
  try {
    let res = await backend.setCookiesFromText(text, false);
    if (!res.ok && res.need_confirm) {
      if (!confirm("cookies.txt exists. Overwrite?")) return;
      res = await backend.setCookiesFromText(text, true);
    }
    if (!res.ok) {
      mCookieError.textContent = res.error || "Could not save cookies.";
      mCookieError.hidden = false;
      showMToast("Could not save cookies.");
      return;
    }
    mCookieText.value = "";
    showMToast("Cookies saved.");
    refreshCookiePill();
    refreshHomeAfterCookieChange();
  } catch (_) {
    mCookieError.textContent = "Could not save cookies.";
    mCookieError.hidden = false;
    showMToast("Could not save cookies.");
  }
});

mClearCookies.addEventListener("click", async () => {
  if (!confirm("Remove cookies.txt? Recommendations turn off.")) return;
  mCookieError.hidden = true;
  try {
    await backend.removeCookies();
    mCookieText.value = "";
    showMToast("Cookies removed.");
    refreshCookiePill();
    refreshHomeAfterCookieChange();
  } catch (_) {
    mCookieError.textContent = "Could not remove cookies.";
    mCookieError.hidden = false;
  }
});

mMiniPlay.addEventListener("click", () => togglePlayback());
mMiniPrev.addEventListener("click", () => playPrev());
mMiniNext.addEventListener("click", () => playNext());
mMiniScrub.addEventListener("click", (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  const total = Number.isFinite(audio.duration) && audio.duration > 0
    ? audio.duration : (Number(state.current && state.current.duration) || 0);
  if (total > 0) audio.currentTime = frac * total;
});

let lastPlayheadSent = -10;
let lastPlayheadAt = 0;

function sendPlayhead(force) {
  if (!state.current || !backend || !backend.reportPlayhead) return;
  const now = Date.now();
  const pos = Number(audio.currentTime) || 0;
  if (!force && Math.abs(pos - lastPlayheadSent) < 1.0 && now - lastPlayheadAt < 2000) return;
  lastPlayheadSent = pos;
  lastPlayheadAt = now;
  backend.reportPlayhead(state.current.id, pos).catch(() => {});
}

audio.addEventListener("timeupdate", () => {
  if (scrubber.dataset.dragging === "1") return;
  scrubber.value = audio.currentTime;
  currentTime.textContent = formatTime(audio.currentTime);
  sendPlayhead(false);
  syncMiniPlayer();
});

audio.addEventListener("seeked", () => sendPlayhead(true));
audio.addEventListener("play", () => sendPlayhead(true));

audio.addEventListener("loadedmetadata", () => {
  const total = Number.isFinite(audio.duration) ? audio.duration : 0;
  const knownDuration = Number(state.current && state.current.duration) || 0;
  const displayDuration = knownDuration > 0 ? knownDuration : total;
  if (displayDuration > 0) scrubber.max = String(displayDuration);
  durationTime.textContent = formatTime(displayDuration);
  syncMiniPlayer();
});

audio.addEventListener("ended", () => {
  if (playNext()) return;
  updateTransportState();
});

audio.addEventListener("play", () => {
  updateTransportState();
  updateRowStates();
  if (state.current && backend && backend.resumeDownload)
    backend.resumeDownload(state.current.id).catch(() => {});
  sendPlayhead(true);
  syncMiniPlayer();
});
audio.addEventListener("pause", () => {
  updateTransportState();
  updateRowStates();
  if (state.current && backend && backend.pauseDownload)
    backend.pauseDownload(state.current.id).catch(() => {});
  syncMiniPlayer();
});
audio.addEventListener("error", () => {
  updateRowStates();
  if (!state.current) return;
  if (!state.errorRetried) {
    state.errorRetried = true;
    audio.load();
    startPlayback().catch(() => {});
    return;
  }
  state.errorRetried = false;
  npChannel.textContent = "Stream unavailable";
});

scrubber.addEventListener("pointerdown", () => { scrubber.dataset.dragging = "1"; });
window.addEventListener("pointerup", () => { scrubber.dataset.dragging = "0"; });
scrubber.addEventListener("input", () => { currentTime.textContent = formatTime(Number(scrubber.value)); });
scrubber.addEventListener("change", () => {
  if (Number.isFinite(audio.duration)) audio.currentTime = Number(scrubber.value);
});

volume.addEventListener("input", () => { audio.volume = Number(volume.value); });
audio.volume = Number(volume.value);

renderQueue();
renderResults();
refreshStatus();
if (!searchInput.value.trim()) loadHome();
