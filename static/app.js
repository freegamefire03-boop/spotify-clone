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

const PAGE = 10;
const state = { query: "", results: [], total: 0, queue: [], qIndex: -1, current: null, loading: false, ready: false };

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
    play: (id, title, url) => api.play(id, title, url),
    prepareTrack: (id) => api.prepare_track(id),
  };
  state.ready = true;
  document.body.classList.add("native");
  refreshStatus();
  if (searchInput.value.trim()) runSearch(searchInput.value, false);
  return true;
}

window.addEventListener("pywebviewready", useBridge);
if (!useBridge()) {
  cookieStatus.textContent = "starting...";
}

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
    })
    .catch(() => setCookieStatus(false));
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
  resultsList.innerHTML = state.results.map(rowMarkup).join("");
  emptyState.hidden = state.results.length > 0;
  moreButton.hidden = state.results.length === 0 || state.results.length >= state.total;
  moreButton.textContent = state.loading ? "Loading..." : `Show more (${state.results.length}/${state.total})`;
  moreButton.disabled = state.loading;
  updateRowStates();
}

async function runSearch(query, append) {
  const trimmed = query.trim();
  if (!trimmed || state.loading) return;
  if (!backend) {
    emptyState.hidden = false;
    emptyState.querySelector("p").textContent = "Backend not ready yet.";
    return;
  }
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
  state.current = track;
  const idx = state.queue.findIndex((t) => t.id === track.id);
  if (idx === -1) state.queue.splice(Math.max(state.qIndex + 1, 0), 0, track);
  state.qIndex = state.queue.findIndex((t) => t.id === track.id);

  updateNowPlaying();
  renderQueue();
  updateRowStates();

  scrubber.value = 0;
  currentTime.textContent = "0:00";
  const knownDuration = Number(track.duration) || 0;
  if (knownDuration > 0) scrubber.max = String(knownDuration);

  // Register the track and kick off the local-library download before playback,
  // so /audio/<id> can serve the file (or the partial file) from disk.
  if (backend && backend.play) {
    backend.play(track.id, track.title, `https://www.youtube.com/watch?v=${track.id}`).catch(() => {});
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
    backend.play(track.id, track.title, `https://www.youtube.com/watch?v=${track.id}`).catch(() => {});
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

moreButton.addEventListener("click", () => runSearch(state.query, true));
playButton.addEventListener("click", () => togglePlayback());
prevButton.addEventListener("click", () => playPrev());
nextButton.addEventListener("click", () => playNext());
queueButton.addEventListener("click", () => { queuePanel.hidden = !queuePanel.hidden; });
closeQueue.addEventListener("click", () => { queuePanel.hidden = true; });

clearQueue.addEventListener("click", () => {
  state.queue = [];
  state.qIndex = -1;
  renderQueue();
  updateRowStates();
});

audio.addEventListener("timeupdate", () => {
  if (scrubber.dataset.dragging === "1") return;
  scrubber.value = audio.currentTime;
  currentTime.textContent = formatTime(audio.currentTime);
});

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

audio.addEventListener("play", () => { updateTransportState(); updateRowStates(); });
audio.addEventListener("pause", () => { updateTransportState(); updateRowStates(); });
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
