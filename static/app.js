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
const homeTitle = document.getElementById("homeTitle");
const bitrateBtns = document.querySelectorAll(".bitrate-btn");
const settingsButton = document.getElementById("settingsButton");
const settingsPanel = document.getElementById("settingsPanel");
const closeSettings = document.getElementById("closeSettings");
const memoryMode = document.getElementById("memoryMode");
const queueDepth = document.getElementById("queueDepth");
const libraryDir = document.getElementById("libraryDir");
const changeFolder = document.getElementById("changeFolder");
const folderError = document.getElementById("folderError");
const cookieDetail = document.getElementById("cookieDetail");
const cookieText = document.getElementById("cookieText");
const saveCookies = document.getElementById("saveCookies");
const removeCookies = document.getElementById("removeCookies");
const cookieError = document.getElementById("cookieError");
const menuButton = document.getElementById("menuButton");
const sidenav = document.getElementById("sidenav");
const edgeZone = document.getElementById("edgeZone");

const PAGE = 10;
const state = { query: "", results: [], total: 0, queue: [], qIndex: -1, current: null, loading: false, ready: false, mode: "home", home: [], homeMode: null };

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
    reportPlayhead: (id, s) => api.report_playhead(id, s),
    pauseDownload: (id) => api.pause_download(id),
    resumeDownload: (id) => api.resume_download(id),
    getLibraryDir: () => api.get_library_dir(),
    setLibraryDir: (p, copy) => api.set_library_dir(p, copy),
    getHome: () => api.get_home(),
    getSession: () => api.get_session(),
    saveSession: (queue, qIndex) => api.save_session(queue, qIndex),
    getCookieStatus: () => api.get_cookie_status(),
    setCookiesFromText: (t, ow) => api.set_cookies_from_text(t, ow),
    removeCookies: () => api.remove_cookies(),
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

function openSettings() {
  queuePanel.hidden = true;
  settingsPanel.hidden = false;
  backend.getSettings().then((s) => {
    memoryMode.checked = !!s.memory_mode;
    queueDepth.value = Number(s.queue_depth ?? 3) || 0;
  }).catch(() => {});
  backend.getLibraryDir().then((d) => {
    if (d && d.path) libraryDir.value = d.path;
  }).catch(() => {});
  cookieError.hidden = true;
  cookieText.value = "";
  backend.getCookieStatus().then((c) => {
    if (c && c.present) {
      const kb = (Number(c.size) / 1024).toFixed(1);
      const when = c.mtime ? new Date(c.mtime * 1000).toLocaleDateString() : "";
      cookieDetail.textContent = `present · ${kb} KB${when ? " · " + when : ""}`;
    } else {
      cookieDetail.textContent = "missing";
    }
  }).catch(() => { cookieDetail.textContent = "missing"; });
}

function closeSettingsPanel() {
  settingsPanel.hidden = true;
}

function closeQueuePanel() {
  queuePanel.hidden = true;
}

function rowMarkup(track) {
  return `
    <li class="row" data-id="${track.id}">
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
    return;
  }
  npTitle.textContent = state.current.title;
  npChannel.textContent = state.current.channel || "";
}

async function playTrack(track) {
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
    backend.play(track.id, track.title, `https://www.youtube.com/watch?v=${track.id}`, upcoming, prevId).catch(() => {});
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

function playNext() {
  if (state.qIndex + 1 < state.queue.length) {
    state.qIndex += 1;
    return playTrack(state.queue[state.qIndex]);
  }
  return false;
}

function playPrev() {
  if (audio.currentTime > 3) {
    audio.currentTime = 0;
    return true;
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
    if (!queuePanel.hidden) return;
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
  if (!queuePanel.hidden) settingsPanel.hidden = true;
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
  if (btn.dataset.nav === "queue") { queuePanel.hidden = false; settingsPanel.hidden = true; }
  if (btn.dataset.nav === "settings") openSettings();
  if (navState.overlay) setNav(false, false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !queuePanel.hidden) {
    queuePanel.hidden = true;
    return;
  }
  if (event.key === "Escape" && navState.overlay && document.activeElement !== searchInput) {
    setNav(false, false);
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

function refreshHomeAfterCookieChange() {
  refreshStatus();
  state.home = [];
  state.homeSections = [];
  loadHome();
}

saveCookies.addEventListener("click", async () => {
  const text = cookieText.value;
  if (!text.trim()) {
    cookieError.textContent = "Paste cookies.txt content first.";
    cookieError.hidden = false;
    return;
  }
  cookieError.hidden = true;
  try {
    let res = await backend.setCookiesFromText(text, false);
    if (!res.ok && res.need_confirm) {
      if (!confirm("cookies.txt exists. Overwrite?")) return;
      res = await backend.setCookiesFromText(text, true);
    }
    if (!res.ok) {
      cookieError.textContent = res.error || "Could not save cookies.";
      cookieError.hidden = false;
      return;
    }
    cookieText.value = "";
    openSettings();
    refreshHomeAfterCookieChange();
  } catch (_) {
    cookieError.textContent = "Could not save cookies.";
    cookieError.hidden = false;
  }
});

removeCookies.addEventListener("click", async () => {
  if (!confirm("Remove cookies.txt? Recommendations turn off.")) return;
  cookieError.hidden = true;
  try {
    await backend.removeCookies();
    cookieText.value = "";
    openSettings();
    refreshHomeAfterCookieChange();
  } catch (_) {
    cookieError.textContent = "Could not remove cookies.";
    cookieError.hidden = false;
  }
});

window.addEventListener("pagehide", () => {
  if (sessionSaveTimer) {
    clearTimeout(sessionSaveTimer);
    sessionSaveTimer = null;
    if (backend && backend.saveSession)
      backend.saveSession(sanitizeQueueForSave(), state.qIndex).catch(() => {});
  }
});

settingsButton.addEventListener("click", openSettings);
closeSettings.addEventListener("click", closeSettingsPanel);

memoryMode.addEventListener("change", () => {
  backend.setMemoryMode(memoryMode.checked);
});

queueDepth.addEventListener("change", () => {
  const val = Math.max(0, Math.min(10, Math.floor(Number(queueDepth.value) || 0)));
  queueDepth.value = val;
  backend.setQueueDepth(val);
});

changeFolder.addEventListener("click", async () => {
  const newPath = libraryDir.value.trim();
  if (!newPath) return;
  folderError.hidden = true;
  const copy = confirm("Do you want to copy the existing audios to the new path?");
  changeFolder.disabled = true;
  try {
    const res = await backend.setLibraryDir(newPath, copy);
    if (!res.ok) {
      folderError.textContent = res.error || "Could not change folder.";
      folderError.hidden = false;
    } else if (res.changed && copy) {
      const skipped = res.skipped && res.skipped.length ? ` (${res.skipped.length} skipped)` : "";
      folderError.textContent = `Moved to ${res.path}: ${res.copied} copied${skipped}.`;
      folderError.hidden = false;
      libraryDir.value = res.path;
    } else if (res.changed) {
      libraryDir.value = res.path;
    }
  } catch (_) {
    folderError.textContent = "Could not change folder.";
    folderError.hidden = false;
  } finally {
    changeFolder.disabled = false;
  }
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
});

audio.addEventListener("seeked", () => sendPlayhead(true));
audio.addEventListener("play", () => sendPlayhead(true));

audio.addEventListener("loadedmetadata", () => {
  const total = Number.isFinite(audio.duration) ? audio.duration : 0;
  const knownDuration = Number(state.current && state.current.duration) || 0;
  const displayDuration = knownDuration > 0 ? knownDuration : total;
  if (displayDuration > 0) scrubber.max = String(displayDuration);
  durationTime.textContent = formatTime(displayDuration);
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
});
audio.addEventListener("pause", () => {
  updateTransportState();
  updateRowStates();
  if (state.current && backend && backend.pauseDownload)
    backend.pauseDownload(state.current.id).catch(() => {});
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
