// Surface any uncaught JS error on the page so the user sees it instead of a blank page
window.addEventListener("error", function(ev) {
  try {
    var bar = document.getElementById("error");
    if (bar) {
      bar.style.display = "block";
      bar.textContent = "JS error: " + ev.message + " (" + (ev.filename || "?") + ":" + ev.lineno + ")";
    }
    // Also show app-section so user isn't stuck on a blank page
    var app = document.getElementById("app-section");
    if (app && app.style.display === "none") app.style.display = "block";
  } catch (_) {}
});
window.addEventListener("unhandledrejection", function(ev) {
  try {
    var bar = document.getElementById("error");
    if (bar) {
      bar.style.display = "block";
      bar.textContent = "JS promise rejection: " + (ev.reason && ev.reason.message ? ev.reason.message : ev.reason);
    }
  } catch (_) {}
});

const fmtTime = ms => {
  if (!ms || ms < 0) return "0:00";
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`;
};

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));
}

let thresholdEl = null;
let thresholdValEl = null;
let userInteractingThreshold = false;
function initThresholdControls() {
  thresholdEl = document.getElementById("threshold");
  thresholdValEl = document.getElementById("threshold-val");
  if (!thresholdEl || thresholdEl.dataset.bound === "1") return;
  thresholdEl.dataset.bound = "1";
  thresholdEl.addEventListener("input", () => {
    if (thresholdValEl) thresholdValEl.textContent = thresholdEl.value;
    userInteractingThreshold = true;
  });
  thresholdEl.addEventListener("change", async () => {
    await fetch("/api/threshold", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ threshold: parseInt(thresholdEl.value, 10) }),
    });
    setTimeout(() => userInteractingThreshold = false, 500);
  });
}

// --- Lyrics sync offset ---
const lyrOffsetEl = document.getElementById("lyr-offset");
const lyrOffsetValEl = document.getElementById("lyr-offset-val");
let userInteractingLyrOffset = false;
let pendingOffsetSave = null;

function fmtOffset(secs) {
  const sign = secs > 0 ? "+" : (secs < 0 ? "−" : "+");
  return `${sign}${Math.abs(secs).toFixed(1)}s`;
}

function commitOffset(secs) {
  clearTimeout(pendingOffsetSave);
  pendingOffsetSave = setTimeout(async () => {
    await fetch("/api/lyrics-offset", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ offset_ms: Math.round(secs * 1000) }),
    });
  }, 200);
}

if (lyrOffsetEl) {
  lyrOffsetEl.addEventListener("input", () => {
    const secs = parseFloat(lyrOffsetEl.value);
    lyrOffsetValEl.textContent = fmtOffset(secs);
    userInteractingLyrOffset = true;
  });
  lyrOffsetEl.addEventListener("change", () => {
    const secs = parseFloat(lyrOffsetEl.value);
    commitOffset(secs);
    setTimeout(() => userInteractingLyrOffset = false, 400);
  });

  const dn = document.getElementById("lyr-offset-down");
  const up = document.getElementById("lyr-offset-up");
  const rs = document.getElementById("lyr-offset-reset");
  if (dn) dn.addEventListener("click", () => {
    let secs = parseFloat(lyrOffsetEl.value) - 0.1;
    secs = Math.max(-10, Math.min(10, secs));
    secs = Math.round(secs * 10) / 10;
    lyrOffsetEl.value = secs;
    lyrOffsetValEl.textContent = fmtOffset(secs);
    commitOffset(secs);
  });
  if (up) up.addEventListener("click", () => {
    let secs = parseFloat(lyrOffsetEl.value) + 0.1;
    secs = Math.max(-10, Math.min(10, secs));
    secs = Math.round(secs * 10) / 10;
    lyrOffsetEl.value = secs;
    lyrOffsetValEl.textContent = fmtOffset(secs);
    commitOffset(secs);
  });
  if (rs) rs.addEventListener("click", () => {
    lyrOffsetEl.value = 0;
    lyrOffsetValEl.textContent = fmtOffset(0);
    commitOffset(0);
  });
}

function bindInlineLyricsControls() {
  const inline = document.getElementById("lyr-offset-inline");
  const inlineVal = document.getElementById("lyr-offset-val-inline");
  const inlineDn = document.getElementById("lyr-offset-down-inline");
  const inlineUp = document.getElementById("lyr-offset-up-inline");
  const inlineRs = document.getElementById("lyr-offset-reset-inline");
  if (!inline || inline.dataset.bound === "1") return;
  inline.dataset.bound = "1";
  const sync = secs => {
    inline.value = secs;
    if (lyrOffsetEl) lyrOffsetEl.value = secs;
    if (inlineVal) inlineVal.textContent = fmtOffset(secs);
    if (lyrOffsetValEl) lyrOffsetValEl.textContent = fmtOffset(secs);
  };
  inline.addEventListener("input", () => {
    const secs = parseFloat(inline.value);
    sync(secs);
    userInteractingLyrOffset = true;
  });
  inline.addEventListener("change", () => {
    const secs = parseFloat(inline.value);
    commitOffset(secs);
    setTimeout(() => userInteractingLyrOffset = false, 400);
  });
  if (inlineDn) inlineDn.addEventListener("click", () => {
    let secs = parseFloat(inline.value) - 0.1;
    secs = Math.max(-10, Math.min(10, Math.round(secs * 10) / 10));
    sync(secs);
    commitOffset(secs);
  });
  if (inlineUp) inlineUp.addEventListener("click", () => {
    let secs = parseFloat(inline.value) + 0.1;
    secs = Math.max(-10, Math.min(10, Math.round(secs * 10) / 10));
    sync(secs);
    commitOffset(secs);
  });
  if (inlineRs) inlineRs.addEventListener("click", () => {
    sync(0);
    commitOffset(0);
  });
}

let toastTimer = null;
function showToast(msg, isError=false) {
  const el = isError ? document.getElementById("error") : document.getElementById("toast");
  const other = isError ? document.getElementById("toast") : document.getElementById("error");
  el.textContent = msg; el.style.display = "block"; other.style.display = "none";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.display = "none"; }, 4500);
}

async function postAction(path, body={}) {
  try {
    const r = await fetch(path, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.error) showToast(d.error, true);
    else if (d.message) showToast(d.message);
    refresh();
    loadAnalytics(true);
    return d;
  } catch (e) { showToast(e.message || "Request failed", true); }
}

let analyticsInFlight = false;
let analyticsLoadedAt = 0;
const ANALYTICS_REFRESH_MS = 60000;

function bindClick(id, handler) {
  const el = document.getElementById(id);
  if (el) el.addEventListener("click", handler);
}

function initSettingsSaveButton() {
  const btn = document.getElementById("btn-save-settings");
  if (!btn || btn.dataset.bound === "1") return;
  btn.dataset.bound = "1";
  btn.addEventListener("click", async () => {
    const form = document.getElementById("settings-form");
    const status = document.getElementById("settings-status");
    if (!form || !status) return;
    const updates = {};
    form.querySelectorAll(".setting-input").forEach(el => {
      if (el.dataset.readonly === "1") return;
      const key = el.dataset.key;
      let value;
      if (el.type === "checkbox") {
        value = el.checked ? "1" : "0";
        const orig = (settingsOriginal[key] || "").toLowerCase();
        const origBool = ["1","true","yes","on"].includes(orig);
        if (el.checked !== origBool) updates[key] = value;
      } else {
        value = el.value;
        if (value !== (settingsOriginal[key] || "")) updates[key] = value;
      }
    });
    if (Object.keys(updates).length === 0) {
      status.classList.remove("error");
      status.textContent = "No changes to save.";
      showToast("No settings changed.");
      return;
    }
    status.classList.remove("error");
    status.textContent = "Saving...";
    btn.disabled = true;
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ updates }),
      });
      const d = await r.json();
      if (d.error) {
        status.classList.add("error");
        status.textContent = d.error;
        showToast(d.error, true);
      } else {
        const sync = d.favorite_sync || {};
        const syncNote = sync.eligible_total != null
          ? ` Eligible artists at this threshold: ${fmtNum(sync.eligible_total)}.`
          : "";
        status.classList.remove("error");
        status.textContent = (d.message || "Settings saved.") + syncNote;
        showToast(d.message || "Settings saved.");
        settingsLoaded = false;
        await loadSettings();
        await refresh();
        loadAnalytics(true);
        loadFavoritesOverview(true);
      }
    } catch (e) {
      status.classList.add("error");
      status.textContent = "Save failed: " + (e.message || e);
      showToast("Save failed.", true);
    } finally {
      btn.disabled = false;
    }
  });
}

function initCatchUpButton() {
  const btn = document.getElementById("btn-catch-up");
  if (!btn || btn.dataset.bound === "1") return;
  btn.dataset.bound = "1";
  btn.addEventListener("click", async () => {
    btn.disabled = true; btn.textContent = "Checking last 50 plays...";
    await postAction("/api/catch-up");
    btn.disabled = false; btn.textContent = "Catch up on recently played";
  });
}

let _clientStateSig = "";
function publishClientState() {
  const payload = {
    visible: !document.hidden,
    focused: typeof document.hasFocus === "function" ? !!document.hasFocus() : true,
  };
  const sig = JSON.stringify(payload);
  if (sig === _clientStateSig) return;
  _clientStateSig = sig;
  fetch("/api/client-state", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: sig,
    keepalive: true,
  }).catch(() => {});
}

window.addEventListener("DOMContentLoaded", () => {
  initThresholdControls();
  initCatchUpButton();
  initSettingsSaveButton();
  publishClientState();
  bindInlineLyricsControls();
  syncRightDrawerWidth();
  syncLayoutGeometry();
  const bar = document.getElementById('player-bar');
  if (bar) {
    bar.addEventListener('transitionend', (e) => {
      if (e.propertyName === 'height') syncLayoutGeometry();
    });
  }
});
document.addEventListener("visibilitychange", publishClientState);
window.addEventListener("focus", publishClientState);
window.addEventListener("blur", publishClientState);
window.addEventListener('resize', () => {
  syncRightDrawerWidth();
  syncLayoutGeometry();
});

bindClick("btn-like", () => postAction("/api/like"));
bindClick("btn-skip", async () => { await postAction("/api/skip-remove"); refreshQueueAfterRemove(); });

// --- Add to playlist ---
let addPlLoaded = false;
let _playlistListInFlight = false;
async function loadAddPlOptions() {
  const sel = document.getElementById("add-pl-select");
  if (addPlLoaded) return;
  if (_playlistRetryUntilMs > Date.now()) {
    const left = Math.max(1, Math.ceil((_playlistRetryUntilMs - Date.now()) / 1000));
    sel.innerHTML = `<option value="">Spotify cooling down - retry in ${left}s</option>`;
    return;
  }
  if (_playlistListInFlight) return;
  sel.innerHTML = `<option value="">Loading your playlists...</option>`;
  try {
    _playlistListInFlight = true;
    const r = await fetch("/api/playlists");
    const d = await r.json();
    if (!r.ok || d.error) {
      if (r.status === 503 || r.status === 429 || d.error === "guard" || d.error === "rate_limited") {
        const retryIn = (d.retry_after || 15) * 1000;
        _playlistRetryUntilMs = Date.now() + retryIn;
        if (_playlistRetryTimer) clearTimeout(_playlistRetryTimer);
        _playlistRetryTimer = setTimeout(() => {
          _playlistRetryTimer = null;
          _playlistRetryUntilMs = 0;
          playlistsLoaded = false;
          addPlLoaded = false;
        }, retryIn);
        sel.innerHTML = `<option value="">Spotify cooling down - retry later</option>`;
        return;
      }
      sel.innerHTML = `<option value="">Error: ${escapeHtml(d.error)}</option>`;
      return;
    }
    if (d.warning) showToast(d.warning);
    // Exclude the active discover playlist (can't add a track to where it already is)
    const opts = d.playlists
      .filter(p => p.id !== d.active_id)
      .map(p => `<option value="${p.id}">${escapeHtml(p.name)} - ${p.tracks} tracks</option>`);
    addPlLoaded = true;
    const addOpts = d.playlists
      .filter(p => p.id !== d.active_id)
      .map(p => ({ id: p.id, name: p.name, suffix: "- " + p.tracks + " tracks", active: false }));
    initPlCombo("pl-combo-add", "add-pl-search", "pl-combo-add-list", "add-pl-select-val", addOpts);
  } catch (e) {
    const inp = document.getElementById("add-pl-search");
    if (inp) inp.placeholder = "Failed to load playlists";
  } finally {
    _playlistListInFlight = false;
  }
}

bindClick("btn-add-pl-toggle", () => {
  const panel = document.getElementById("add-pl-panel");
  const btn = document.getElementById("btn-add-pl-toggle");
  if (!panel || !btn) return;
  panel.classList.toggle("open");
  btn.classList.toggle("is-open");
  if (panel.classList.contains("open")) {
    loadAddPlOptions();
    // Position panel near the + button
    const rect = btn.getBoundingClientRect();
    const pw = 272; // panel width + padding
    let left = rect.left + rect.width / 2 - pw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    const top = rect.top - 8; // panel sits above button, bottom anchored
    panel.style.left = left + "px";
    panel.style.bottom = (window.innerHeight - top) + "px";
    panel.style.right = "auto";
    panel.style.top = "auto";
  }
});

bindClick("btn-confirm-add", async () => {
  const _addHidden = document.getElementById("add-pl-select-val");
  const _addInp = document.getElementById("add-pl-search");
  const remove = document.getElementById("add-pl-remove").checked;
  if (_addHidden) _addHidden.value = (_addInp && _addInp.dataset.selectedId) || _addHidden.value;
  const sel = _addHidden;
  if (!sel || !sel.value) { showToast("Pick a playlist first.", true); return; }
  const btn = document.getElementById("btn-confirm-add");
  btn.disabled = true; btn.textContent = "Adding...";
  const d = await postAction("/api/add-to-playlist", {
    playlist_id: sel.value,
    also_remove: remove,
  });
  btn.disabled = false; btn.textContent = "Add to playlist";
  if (d && !d.error) {
    document.getElementById("add-pl-panel").classList.remove("open");
    document.getElementById("btn-add-pl-toggle").classList.remove("is-open");
  }
});

// --- Playlist queue (overlay drawer) ---
let queueLoaded = false;
let queueInitialized = false;
let queueTracks = [];
let queueTotal = 0;
let queueLastFailedAt = 0;
const QUEUE_FAIL_BACKOFF_MS = 30000;

function fmtCountdown(s) {
  if (s >= 3600) { const h = Math.floor(s/3600), m = Math.floor((s%3600)/60); return m > 0 ? `${h}h ${m}m` : `${h}h`; }
  if (s >= 60)   { const m = Math.floor(s/60), sec = s%60; return sec > 0 ? `${m}m ${sec}s` : `${m}m`; }
  return `${s}s`;
}

function startQueueRateLimitCountdown(listEl, seconds) {
  const autoRetry = seconds <= 600;
  let remaining = seconds;
  const retryBtn = `<button style="margin-top:10px;font-size:11px;padding:4px 10px;background:none;border:1px solid var(--border);border-radius:6px;color:var(--muted);cursor:pointer" onclick="loadQueue(true)">Retry now</button>`;
  function tick() {
    if (!document.getElementById("queue-list")) return;
    if (remaining <= 0) {
      if (autoRetry) { loadQueue(true); } else {
        listEl.innerHTML = `<div class="queue-empty">Rate limit window passed.<br>${retryBtn}</div>`;
      }
      return;
    }
    const msg = autoRetry
      ? `Retrying in ${fmtCountdown(remaining)}...`
      : `Retry-After: ${fmtCountdown(remaining)} (won't auto-retry)`;
    listEl.innerHTML = `<div class="queue-empty">Rate limited by Spotify.<br><span style="font-size:11px;color:var(--muted)">${msg}</span><br>${retryBtn}</div>`;
    remaining--;
    setTimeout(tick, 1000);
  }
  tick();
}

let _queuePartialPollTimer = null;

function _stopQueuePartialPoll() {
  if (_queuePartialPollTimer) { clearTimeout(_queuePartialPollTimer); _queuePartialPollTimer = null; }
}

async function loadQueue(force=false) {
  if (queueLoaded && !force) return;
  if (!force && queueLastFailedAt && (Date.now() - queueLastFailedAt) < QUEUE_FAIL_BACKOFF_MS) return;
  _stopQueuePartialPoll();
  const listEl = document.getElementById("queue-list");
  const countEl = document.getElementById("queue-count");
  if (!listEl) return;
  if (!force) listEl.innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading queue</div>`;
  try {
    const r = await fetch(`/api/playlist-tracks${force ? "?force=1" : ""}`);
    const d = await r.json();
    if (!r.ok || d.error) {
      queueLastFailedAt = Date.now();
      const retryAfter = d.retry_after || (r.status === 503 ? 10 : null);
      if (retryAfter) {
        // Guard/rate-limit: show silent countdown and auto-retry
        const isGuard = d.error === "guard" || r.status === 503;
        if (!isGuard) startQueueRateLimitCountdown(listEl, retryAfter);
        else {
          listEl.innerHTML = `<div class="queue-empty" style="color:var(--muted);font-size:11px">Spotify cooling down - retrying in <span id="ql-cd">${retryAfter}</span>s</div>`;
          let cd = retryAfter;
          const iv = setInterval(() => {
            const el = document.getElementById("ql-cd");
            if (el) el.textContent = --cd;
            if (cd <= 0) { clearInterval(iv); loadQueue(true); }
          }, 1000);
        }
      } else {
        listEl.innerHTML = `<div class="queue-empty">${escapeHtml(d.error || "Error loading queue")}<br><button style="margin-top:10px;font-size:11px;padding:4px 10px;background:none;border:1px solid var(--border);border-radius:6px;color:var(--muted);cursor:pointer" onclick="loadQueue(true)">Retry</button></div>`;
      }
      return;
    }
    queueLastFailedAt = 0;
    queueTracks = d.tracks || [];
    queueTotal = d.total || 0;
    // Mark loaded even if partial — we have usable tracks to display
    queueLoaded = true;
    window._queueTotal = queueTracks.length;
    window._trackGenreCache = window._trackGenreCache || {};
    for (const t of queueTracks) {
      if (t && t.id && Array.isArray(t.genres) && t.genres.length) {
        window._trackGenreCache[t.id] = t.genres;
      }
    }
    if (countEl) { countEl.textContent = queueTotal || queueTracks.length; countEl.classList.toggle("visible", queueTracks.length > 0); }
    renderQueue();
    fetchQueueGenres(queueTracks);

    // If the server is still fetching remaining pages, poll for updates
    if (d.partial) {
      _queuePartialPollTimer = setTimeout(async function _pollPartial() {
        try {
          const pr = await fetch("/api/playlist-tracks");
          const pd = await pr.json();
          if (pr.ok && !pd.error) {
            const newTracks = pd.tracks || [];
            const prevLen = queueTracks.length;
            if (newTracks.length > prevLen) {
              queueTracks = newTracks;
              queueTotal = pd.total || newTracks.length;
              window._queueTotal = newTracks.length;
              window._trackGenreCache = window._trackGenreCache || {};
              for (const t of newTracks) {
                if (t && t.id && Array.isArray(t.genres) && t.genres.length) {
                  window._trackGenreCache[t.id] = t.genres;
                }
              }
              if (countEl) { countEl.textContent = queueTotal; countEl.classList.toggle("visible", newTracks.length > 0); }
              renderQueue();
              fetchQueueGenres(newTracks.slice(prevLen));
            }
            if (pd.partial) {
              _queuePartialPollTimer = setTimeout(_pollPartial, 4000);
            }
          }
        } catch(e) {}
      }, 4000);
    }
  } catch(e) {
    queueLastFailedAt = Date.now();
    listEl.innerHTML = `<div class="queue-empty">Failed to load.<br><button style="margin-top:10px;font-size:11px;padding:4px 10px;background:none;border:1px solid var(--border);border-radius:6px;color:var(--muted);cursor:pointer" onclick="loadQueue(true)">Retry</button></div>`;
  }
}

function renderQueue() {
  const listEl = document.getElementById("queue-list");
  if (!listEl) return;
  if (!queueTracks.length) {
    const hint = (queueTotal > 0) ? ` (Spotify reports ${queueTotal} total)` : "";
    listEl.innerHTML = `<div class="queue-empty">No tracks${hint}.<br><button style="margin-top:10px;font-size:11px;padding:4px 10px;background:none;border:1px solid var(--border);border-radius:6px;color:var(--muted);cursor:pointer" onclick="loadQueue(true)">Refresh</button></div>`;
    return;
  }
  const currentId = window._currentTrackId || null;
  listEl.innerHTML = queueTracks.map(t => {
    const isCurrent = t.id === currentId;
    const art = t.image
      ? `<img class="queue-art" src="${escapeHtml(t.image)}" alt="" loading="lazy">`
      : `<div class="queue-art-placeholder"></div>`;
    const dur = t.duration_ms ? Math.floor(t.duration_ms/60000) + ":" + String(Math.floor((t.duration_ms%60000)/1000)).padStart(2,"0") : "";
    const _gc = window._trackGenreCache || {};
    const cachedGenres = _gc[t.id] || [];
    const genreTags = cachedGenres.length
      ? `<div class="queue-genres">${cachedGenres.slice(0,4).map(g=>`<span class="queue-genre">${escapeHtml(g)}</span>`).join("")}</div>`
      : "";
    return `<div class="queue-item${isCurrent ? " current-track" : ""}" id="qi-${escapeHtml(t.id)}" role="listitem">
      ${art}
      <div class="queue-meta">
        <div class="queue-name">${escapeHtml(t.name)}</div>
        <div class="queue-artist">${escapeHtml(t.artists)}${dur ? ` <span class="queue-dur">- ${dur}</span>` : ""}</div>
        ${genreTags}
      </div>
      <button class="queue-play${isCurrent ? ' playing' : ''}" data-uri="${escapeHtml(t.uri||'')}"
              onclick="playTrack(this.dataset.uri)" title="Play" aria-label="Play ${escapeHtml(t.name)}">&#9654;</button>
    </div>`;
  }).join("");
  wrapArtistLinks(listEl);
  if (currentId) {
    const el = document.getElementById(`qi-${currentId}`);
    if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

async function playTrack(uri) {
  try {
    const d = await postAction("/api/play-track", { uri });
    if (d && !d.error) showToast("Playing…");
    else if (d && d.error) showToast(d.error, true);
  } catch(e) { showToast("Playback failed.", true); }
}

async function playTrackByMeta(name, artist, fallbackUrl) {
  const qName = (name || "").trim();
  const qArtist = (artist || "").trim();
  if (!qName) return;
  try {
    const q = qArtist ? `track:"${qName}" artist:"${qArtist}"` : qName;
    const r = await fetch("/api/search?" + new URLSearchParams({ q, type: "track" }));
    const d = await r.json();
    const match = (d.tracks || [])[0];
    if (match && match.uri) {
      return playTrack(match.uri);
    }
  } catch (_) {}
  if (fallbackUrl) {
    window.open(fallbackUrl, "_blank");
    return;
  }
  showToast(`Couldn't resolve ${qName} on Spotify yet.`, true);
}

function syncRightDrawerWidth() {
  const workspaceOpen = document.getElementById('workspace-shell')?.classList.contains('open');
  const researchOpen = document.getElementById('research-modal-backdrop')?.classList.contains('open');
  let width = '0px';
  if (workspaceOpen) width = '760px';
  else if (researchOpen) width = '380px';
  document.documentElement.style.setProperty('--right-drawer-w', width);
}

function syncLayoutGeometry() {
  const root = document.documentElement;
  const bar = document.getElementById('player-bar');
  if (!root || !bar) return;
  const rect = bar.getBoundingClientRect();
  const playerHeight = Math.max(0, Math.round(rect.height));
  const contentBottom = Math.max(0, Math.round(window.innerHeight - rect.top));
  root.style.setProperty('--player-total-h', playerHeight + 'px');
  root.style.setProperty('--content-bottom-offset', contentBottom + 'px');
}

async function playerAction(action) {
  const epMap = { prev: "/api/playback/prev", toggle: "/api/playback/toggle", next: "/api/playback/next" };
  const ep = epMap[action];
  if (!ep) return;
  try {
    const d = await postAction(ep);
    if (action === "toggle") {
      // Optimistic UI: flip play/pause icon
      const btn = document.getElementById("btn-play-pause");
      if (btn) {
        const isPlaying = btn.innerHTML.trim() === "⏸";
        btn.innerHTML = isPlaying ? "▶" : "⏸";
      }
    }
    if (d && d.error) showToast(d.error, true);
  } catch(e) { showToast("Playback failed.", true); }
}

// Queue loads on first auth ready
function initQueue() {
  if (queueInitialized) return;
  queueInitialized = true;
  loadQueue();
}
function refreshQueueAfterRemove() { queueLoaded = false; loadQueue(); }

// --- Persistent sidebar ---
const SIDEBAR_W_KEY = "discover-cleaner.sidebar-w";
const SIDEBAR_COLLAPSED_KEY = "discover-cleaner.sidebar-collapsed";

function _applySidebarWidth(w) {
  document.documentElement.style.setProperty("--sidebar-w", w + "px");
  const main = document.getElementById("app-main");
  if (main) main.style.left = w + "px";
  const sidebar = document.getElementById("app-sidebar");
  if (sidebar) sidebar.style.width = w + "px";
}

const SIDEBAR_TAB_KEY = "sidebar_tab";

function collapseSidebar() {
  const sidebar = document.getElementById("app-sidebar");
  if (!sidebar) return;
  sidebar.classList.add("collapsed");
  document.documentElement.style.setProperty("--sidebar-w", "0px");
  const main = document.getElementById("app-main");
  if (main) main.style.left = "0px";
  const foldIcon = document.getElementById("fold-icon");
  if (foldIcon) foldIcon.innerHTML = "&#8250;";
  try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, "1"); } catch(_) {}
}

function expandSidebar() {
  const sidebar = document.getElementById("app-sidebar");
  if (!sidebar) return;
  sidebar.classList.remove("collapsed");
  const w = parseInt(localStorage.getItem(SIDEBAR_W_KEY) || "280", 10);
  _applySidebarWidth(w);
  const foldIcon = document.getElementById("fold-icon");
  if (foldIcon) foldIcon.innerHTML = "&#8249;";
  try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, "0"); } catch(_) {}
  if (!queueLoaded && !queueLastFailedAt) loadQueue();
}

function toggleSidebarFold() {
  const sidebar = document.getElementById("app-sidebar");
  if (!sidebar) return;
  if (sidebar.classList.contains("collapsed")) {
    expandSidebar();
  } else {
    collapseSidebar();
  }
}

// ── Profile menu ──
function toggleProfileMenu() {
  const menu = document.getElementById("profile-menu");
  const backdrop = document.getElementById("profile-menu-backdrop");
  const btn = document.getElementById("profile-menu-btn");
  if (!menu) return;
  const isOpen = menu.classList.contains("open");
  if (isOpen) {
    menu.classList.remove("open");
    if (backdrop) backdrop.classList.remove("open");
    if (btn) btn.setAttribute("aria-expanded", "false");
  } else {
    menu.classList.add("open");
    if (backdrop) backdrop.classList.add("open");
    if (btn) btn.setAttribute("aria-expanded", "true");
  }
}
function closeProfileMenu() {
  const menu = document.getElementById("profile-menu");
  const backdrop = document.getElementById("profile-menu-backdrop");
  const btn = document.getElementById("profile-menu-btn");
  if (menu) menu.classList.remove("open");
  if (backdrop) backdrop.classList.remove("open");
  if (btn) btn.setAttribute("aria-expanded", "false");
}
document.addEventListener("keydown", e => { if (e.key === "Escape") closeProfileMenu(); });

// Sidebar resize drag
(function() {
  const handle = document.getElementById("sidebar-resize");
  if (!handle) return;
  let dragging = false, startX = 0, startW = 0;
  handle.addEventListener("mousedown", e => {
    dragging = true; startX = e.clientX;
    startW = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--sidebar-w") || "280", 10);
    handle.classList.add("dragging");
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
  document.addEventListener("mousemove", e => {
    if (!dragging) return;
    const w = Math.max(160, Math.min(520, startW + e.clientX - startX));
    _applySidebarWidth(w);
  });
  document.addEventListener("mouseup", e => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    document.body.style.userSelect = "";
    const w = Math.max(160, Math.min(520, startW + e.clientX - startX));
    try { localStorage.setItem(SIDEBAR_W_KEY, String(w)); } catch(_) {}
  });
})();

// Restore sidebar state
(function() {
  const _savedCollapse = localStorage.getItem(SIDEBAR_COLLAPSED_KEY);
  const collapsed = _savedCollapse !== "0"; // default closed; only open if explicitly saved as "0"
  const w = parseInt(localStorage.getItem(SIDEBAR_W_KEY) || "280", 10);
  if (collapsed) {
    collapseSidebar();
  } else {
    _applySidebarWidth(w);
    const foldIcon = document.getElementById("fold-icon");
    if (foldIcon) foldIcon.innerHTML = "&#8249;";
  }
})();

// Sidebar playlist picker toggle
function toggleSidebarPicker() {
  const picker = document.getElementById("sidebar-pl-picker");
  if (!picker) return;
  const opening = !picker.classList.contains("open");
  picker.classList.toggle("open");
  if (opening) {
    // Always force-reload so initPlCombo re-registers handlers and list renders fresh
    playlistsLoaded = false;
    loadPlaylists();
    const inp = document.getElementById("pl-search");
    if (inp) setTimeout(() => inp.focus(), 60);
  }
}

// --- Seekable progress bar ---
(function() {
  const track = document.getElementById("bar-progress-track");
  if (!track) return;
  track.addEventListener("click", async function(e) {
    const dur = window._currentDurationMs;
    if (!dur) return;
    const rect = track.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const posMs = Math.floor(pct * dur);
    // Suppress status-poll progress updates for 1.5s to avoid jitter
    window._seekSuppressUntil = Date.now() + 1500;
    // Optimistic UI update
    document.getElementById("track-progress").style.width = (pct * 100) + "%";
    document.getElementById("track-pos").textContent = fmtTime(posMs);
    try {
      await fetch("/api/seek", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({position_ms: posMs})
      });
    } catch(e) { console.error("Seek failed:", e); }
  });
})();

function syncBarMarquee(el) {
  if (!el) return;
  const inner = el.querySelector(".marquee-inner");
  if (!inner) return;
  const slack = Math.max(0, inner.scrollWidth - el.clientWidth);
  if (slack > 6) {
    el.classList.add("is-overflowing");
    el.style.setProperty("--marquee-shift", `${slack}px`);
    el.style.setProperty("--marquee-duration", `${Math.max(2.5, Math.min(7, slack / 28))}s`);
  } else {
    el.classList.remove("is-overflowing");
    el.style.setProperty("--marquee-shift", "0px");
    el.style.setProperty("--marquee-duration", "0s");
  }
}

function syncPlayerBarMarquees() {
  document.querySelectorAll(".player-bar .bar-marquee").forEach(syncBarMarquee);
}

window.addEventListener("resize", () => {
  window.clearTimeout(window._barMarqueeResizeTimer);
  window._barMarqueeResizeTimer = window.setTimeout(syncPlayerBarMarquees, 80);
});

// --- Sidebar queue filter ---
(function() {
  const input = document.getElementById("sidebar-search");
  if (!input) return;
  input.addEventListener("input", () => {
    const q = input.value.trim();
    if (!q) { renderQueue(); return; }
    const listEl = document.getElementById("queue-list");
    if (!listEl || !queueTracks.length) return;
    const ql = q.toLowerCase();
    const filtered = queueTracks.filter(t =>
      t.name.toLowerCase().includes(ql) || t.artists.toLowerCase().includes(ql)
    );
    if (!filtered.length) {
      listEl.innerHTML = `<div class="queue-empty">No matches in queue.</div>`;
    } else {
      const currentId = window._currentTrackId || null;
      listEl.innerHTML = filtered.map(t => {
        const isCurrent = t.id === currentId;
        const art = t.image
          ? `<img class="queue-art" src="${escapeHtml(t.image)}" alt="" loading="lazy">`
          : `<div class="queue-art-placeholder"></div>`;
        return `<div class="queue-item${isCurrent ? " current-track" : ""}">
          ${art}
          <div class="queue-meta">
            <div class="queue-name">${escapeHtml(t.name)}</div>
            <div class="queue-artist">${escapeHtml(t.artists)}</div>
          </div>
          <button class="queue-play${isCurrent ? " playing" : ""}" data-uri="${escapeHtml(t.uri||"")}"
                  onclick="playTrack(this.dataset.uri)">▶</button>
        </div>`;
      }).join("");
    }
  });
})();

// ── Topbar search ──────────────────────────────────────────────────────────
(function() {
  const input = document.getElementById("topbar-search-input");
  const resultsEl = document.getElementById("topbar-search-results");
  if (!input || !resultsEl) return;
  let timer = null;

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { resultsEl.style.display = "none"; return; }
    if (q.length < 2) return;
    timer = setTimeout(() => doTopbarSearch(q), 450);
  });
  input.addEventListener("keydown", e => {
    if (e.key === "Escape") { input.value = ""; resultsEl.style.display = "none"; input.blur(); }
  });
  // Click outside closes results
  document.addEventListener("click", e => {
    if (!document.getElementById("topbar-search").contains(e.target)) {
      resultsEl.style.display = "none";
    }
  });

  async function doTopbarSearch(q) {
    try {
      const r = await fetch("/api/search?" + new URLSearchParams({q, type: "track,artist"}));
      const d = await r.json();
      if (input.value.trim() !== q) return;
      const parts = [];
      if (d.tracks && d.tracks.length) {
        parts.push(`<div class="topbar-search-section">Songs</div>`);
        parts.push(d.tracks.slice(0, 6).map(t => {
          const thumb = t.image
            ? `<img class="topbar-search-thumb" src="${escapeHtml(t.image)}" alt="">`
            : `<div class="topbar-search-thumb"></div>`;
          return `<div class="topbar-search-row" onclick="playTrack('${escapeHtml(t.uri||"")}');document.getElementById('topbar-search-results').style.display='none';">
            ${thumb}
            <div class="topbar-search-meta">
              <div class="topbar-search-name">${escapeHtml(t.name)}</div>
              <div class="topbar-search-sub">${escapeHtml(t.artists)}</div>
            </div>
            <button class="topbar-search-play" onclick="event.stopPropagation();playTrack('${escapeHtml(t.uri||"")}');document.getElementById('topbar-search-results').style.display='none';" title="Play">▶</button>
          </div>`;
        }).join(""));
      }
      if (d.artists && d.artists.length) {
        parts.push(`<div class="topbar-search-section">Artists</div>`);
        parts.push(d.artists.slice(0, 4).map(a => {
          const thumb = a.image
            ? `<img class="topbar-search-thumb round" src="${escapeHtml(a.image)}" alt="">`
            : `<div class="topbar-search-thumb round"></div>`;
          const genres = (a.genres||[]).slice(0,2).join(", ");
          return `<div class="topbar-search-row" onclick="event.stopPropagation();openArtistPanel('${escapeHtml(a.id||"")}','${escapeHtml(a.name)}','${escapeHtml(a.image||"")}');document.getElementById('topbar-search-results').style.display='none';">
            ${thumb}
            <div class="topbar-search-meta">
              <div class="topbar-search-name">${escapeHtml(a.name)}</div>
              ${genres ? `<div class="topbar-search-sub">${escapeHtml(genres)}</div>` : ""}
            </div>
          </div>`;
        }).join(""));
      }
      if (!parts.length) parts.push(`<div class="topbar-search-empty">No results</div>`);
      resultsEl.innerHTML = parts.join("");
      resultsEl.style.display = "";
    } catch(e) { console.error("Search failed", e); }
  }
})();

// ── Artist profile panel ────────────────────────────────────────────────────
// ── Research Queue ───────────────────────────────────────────────────────

let _researchCurrentStatus = 'active';
let _researchModal = { entityType: 'track', entityId: null, entityName: '' };
let _workspaceTab = 'research';
let _workspaceArtistContext = {
  title: 'Explore',
  subtitle: 'Context without the pile-up.',
  kicker: 'Artist relationship',
  image: '',
};
let _currentArtistPage = { artistId: '', artistName: '', artistImage: '', info: null };

function setWorkspaceHeader(title, subtitle, kicker, image='') {
  const kickerEl = document.getElementById('workspace-kicker');
  const titleEl = document.getElementById('artist-panel-name');
  const subtitleEl = document.getElementById('workspace-subtitle');
  const avatarEl = document.getElementById('artist-panel-avatar');
  if (kickerEl) kickerEl.textContent = kicker || '';
  if (titleEl) titleEl.textContent = title || '';
  if (subtitleEl) subtitleEl.textContent = subtitle || '';
  if (avatarEl) {
    avatarEl.src = image || '';
    avatarEl.style.display = image ? '' : 'none';
  }
}

function switchWorkspaceTab(tab) {
  _workspaceTab = (tab === 'analytics' ? 'research' : (tab || 'research'));
  const shell = document.getElementById('workspace-shell');
  const backdrop = document.getElementById('workspace-backdrop');
  const tabCaret = document.querySelector('.workspace-drawer-tab span');
  if (shell) shell.classList.add('open');
  if (backdrop) backdrop.classList.add('open');
  if (tabCaret) tabCaret.innerHTML = '&#8250;';
  syncRightDrawerWidth();
  ['research','favorites'].forEach(name => {
    document.getElementById(`workspace-tab-${name}`)?.classList.toggle('active', name === _workspaceTab);
    document.getElementById(`workspace-pane-${name}`)?.classList.toggle('active', name === _workspaceTab);
  });
  if (_workspaceTab === 'research') {
    setWorkspaceHeader('Research', 'Threads to pull, promote, or revisit later.', 'Research queue');
    loadResearch(_researchCurrentStatus);
  } else if (_workspaceTab === 'favorites') {
    setWorkspaceHeader('Favorites', 'Trusted artists, release radar, and opt-outs.', 'Favorite artists');
    loadFavoritesOverview();
  }
}

function openWorkspace(tab) {
  closeResearchModal();
  switchWorkspaceTab(tab || _workspaceTab || 'research');
  requestAnimationFrame(syncLayoutGeometry);
}

function toggleWorkspaceDrawer() {
  const shell = document.getElementById('workspace-shell');
  if (!shell) return;
  if (shell.classList.contains('open')) {
    closeWorkspace();
  } else {
    openWorkspace(_workspaceTab || 'research');
  }
}

function closeWorkspace() {
  document.getElementById('workspace-shell')?.classList.remove('open');
  document.getElementById('workspace-backdrop')?.classList.remove('open');
  const tabCaret = document.querySelector('.workspace-drawer-tab span');
  if (tabCaret) tabCaret.innerHTML = '&#8249;';
  syncRightDrawerWidth();
  requestAnimationFrame(syncLayoutGeometry);
}

function goToDashboard() {
  document.getElementById('artist-page-view')?.classList.remove('active');
  const dash = document.querySelector('.dashboard-stack');
  if (dash) dash.style.display = '';
  const qs = document.getElementById('qs-grid');
  if (qs) qs.style.display = '';
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function showArtistPageShell() {
  const dash = document.querySelector('.dashboard-stack');
  if (dash) dash.style.display = 'none';
  document.getElementById('artist-page-view')?.classList.add('active');
  const qs = document.getElementById('qs-grid');
  if (qs) qs.style.display = 'none';
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function openCurrentArtistPage() {
  const primaryArtist = ((window._lastTrack || {}).artists || '').split(',')[0].trim();
  if (_currentArtistPage.info && (_currentArtistPage.artistId || _currentArtistPage.artistName)) {
    openArtistPanel(_currentArtistPage.artistId, _currentArtistPage.artistName, _currentArtistPage.artistImage);
  } else if (primaryArtist) {
    openArtistPanel('', primaryArtist, '');
  }
}

function openResearchDrawer() {
  openWorkspace('research');
}

function closeResearchDrawer() {
  closeWorkspace();
}

function switchResearchTab(btn, status) {
  document.querySelectorAll('.research-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  _researchCurrentStatus = status;
  loadResearch(status);
}

async function loadResearch(status) {
  const list = document.getElementById('research-list');
  list.innerHTML = '<div class="spinner-wrap"><span class="spinner"></span>Loading research queue</div>';
  try {
    const r = await fetch('/api/research?status=' + encodeURIComponent(status || 'active'));
    const d = await r.json();
    // Update count badges
    const counts = d.counts || {};
    document.getElementById('rc-active').textContent   = counts.active   || 0;
    document.getElementById('rc-promoted').textContent = counts.promoted || 0;
    document.getElementById('rc-archived').textContent = counts.archived || 0;
    // Update topbar badge
    const rqBadge = document.getElementById('rq-active-badge');
    if (rqBadge) rqBadge.textContent = counts.active || 0;
    renderResearchList(d.items || [], list);
  } catch(e) {
    list.innerHTML = '<div class="research-empty">Failed to load.</div>';
  }
}

function renderResearchList(items, containerEl) {
  if (!items.length) {
    containerEl.innerHTML = `<div class="research-empty">
      <div class="research-empty-icon">🔬</div>
      Nothing here yet — add tracks, artists, or albums you want to investigate.
    </div>`;
    return;
  }
  containerEl.innerHTML = items.map(item => {
    const tags = (item.reasons || []).map(t =>
      `<span class="research-tag">${escapeHtml(t)}</span>`).join('');
    const note = item.note
      ? `<div class="research-item-note">"${escapeHtml(item.note)}"</div>` : '';
    const isArchived  = item.status === 'archived';
    const isPromoted  = item.status === 'promoted';
    const promoteBtn = !isPromoted && !isArchived
      ? `<button class="research-action-btn promote" onclick="researchAction(${item.id},'promoted')" title="Mark as promoted">↑ Promote</button>` : '';
    const archiveBtn = !isArchived
      ? `<button class="research-action-btn archive" onclick="researchAction(${item.id},'archived')" title="Archive">✕</button>` : '';
    const restoreBtn = isArchived
      ? `<button class="research-action-btn" onclick="researchAction(${item.id},'active')">Restore</button>` : '';
    return `<div class="research-item research-status-${escapeHtml(item.status)}">
      <div class="research-item-header">
        <span class="research-item-type">${escapeHtml(item.entity_type)}</span>
        <span class="research-item-name" title="${escapeHtml(item.entity_name)}">${escapeHtml(item.entity_name)}</span>
        <div class="research-item-actions">${promoteBtn}${archiveBtn}${restoreBtn}</div>
      </div>
      ${tags ? `<div class="research-tags">${tags}</div>` : ''}
      ${note}
    </div>`;
  }).join('');
}

async function researchAction(id, status) {
  try {
    await fetch(`/api/research/${id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status})
    });
    loadResearch(_researchCurrentStatus);
  } catch(e) {}
}

function openResearchModal(entityType, entityId, entityName) {
  if (!entityName) return;
  closeWorkspace();
  _researchModal = { entityType: entityType || 'track', entityId: entityId || null, entityName };
  const backdrop = document.getElementById('research-modal-backdrop');
  document.getElementById('research-modal-entity').textContent =
    (entityType || 'track').toUpperCase() + ': ' + entityName;
  document.getElementById('research-modal-note').value = '';
  // Build tag buttons
  const seedTags = [
    'interesting vocals','guitar tone','atmosphere','production','lyrics',
    'emotional pull','revisit later','artist curiosity','album curiosity',
    'unusual structure','sonic texture','era / period','live version','cover / rendition'
  ];
  document.getElementById('research-modal-tags').innerHTML = seedTags.map(tag =>
    `<button class="research-modal-tag" onclick="this.classList.toggle('selected')">${escapeHtml(tag)}</button>`
  ).join('');
  backdrop.classList.add('open');
  syncRightDrawerWidth();
  requestAnimationFrame(syncLayoutGeometry);
  setTimeout(() => document.getElementById('research-modal-note').focus(), 50);
}

function closeResearchModal() {
  document.getElementById('research-modal-backdrop').classList.remove('open');
  syncRightDrawerWidth();
  requestAnimationFrame(syncLayoutGeometry);
}

async function submitResearch() {
  const reasons = [...document.querySelectorAll('.research-modal-tag.selected')]
    .map(b => b.textContent);
  const note = document.getElementById('research-modal-note').value.trim();
  try {
    const r = await fetch('/api/research', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        entity_type: _researchModal.entityType,
        entity_id:   _researchModal.entityId,
        entity_name: _researchModal.entityName,
        reasons, note
      })
    });
    if (r.ok) {
      closeResearchModal();
      // Update badge count
      loadResearch(_researchCurrentStatus);
      showToast('Added to Research Queue.');
    } else {
      const d = await r.json();
      showToast((d.error || 'Failed to add.'), true);
    }
  } catch(e) {
    showToast('Failed to add.', true);
  }
}

// ── Discography ──────────────────────────────────────────────────────────

let _discoArtistId   = null;
let _discoData       = null;
let _discoCache      = {};
let _discoTrackCache = {};
let _discoOpenAlbums = {};
let _discoFilter     = 'all';    // all | album | ep | single | live
let _discoShowVariants = false;

async function loadDiscography(artistId) {
  _discoArtistId = artistId;
  const section = document.getElementById('disco-section');
  if (!section) return;
  section.innerHTML = '<div class="spinner-wrap"><span class="spinner"></span>Loading discography</div>';
  try {
    const r = await fetch('/api/artist/discography?artist_id=' + encodeURIComponent(artistId));
    const d = await r.json();
    if (d.error) {
      const retryMatch = String(d.error || '').match(/Retry in (\d+)s/i);
      if (retryMatch) {
        const retryS = retryMatch[1];
        const cached = _discoCache[artistId];
        if (cached) {
          renderDiscography(cached, section);
          section.insertAdjacentHTML('beforeend', `<div class="artist-panel-meta-note">Spotify metadata is cooling off. Showing cached discography for now; try again in ${escapeHtml(retryS)}s.</div>`);
        } else {
          section.innerHTML = `<div class="empty">Spotify metadata is cooling off. Discography will be ready again in about ${escapeHtml(retryS)}s.</div>`;
        }
      } else {
        section.innerHTML = `<div class="empty">${escapeHtml(d.error)}</div>`;
      }
      return;
    }
    _discoData = d;
    _discoCache[artistId] = d;
    renderDiscography(d, section);
    renderArtistCoverageSummary(d);
    if (d._stale_notice) {
      section.insertAdjacentHTML('beforeend', `<div class="artist-panel-meta-note">Spotify metadata is cooling off. Showing cached discography for now.</div>`);
    }
  } catch(e) {
    section.innerHTML = '<div class="empty">Could not load discography.</div>';
  }
}

function renderArtistCoverageSummary(summary) {
  const coverageEl = document.getElementById('artist-page-coverage');
  if (!coverageEl) return;
  const heard = Number(summary?.catalog_tracks_heard || 0);
  const total = Number(summary?.catalog_tracks_total || 0);
  if (!(total > 0)) {
    coverageEl.innerHTML = `<span class="workspace-pane-sub">Catalog coverage will appear once discography loads.</span>`;
    return;
  }
  const coveragePct = Number(summary?.catalog_coverage_pct || 0);
  const unheard = Math.max(0, total - heard);
  const unheardPct = total > 0 ? Math.max(0, 100 - coveragePct) : 0;
  coverageEl.innerHTML = `
    <div class="artist-page-coverage-top">
      <div class="artist-page-coverage-copy">
        <strong>${fmtNum(unheardPct)}% unheard</strong>
        <span>${fmtNum(unheard)} songs still waiting for you across ${fmtNum(total)} mapped tracks.</span>
      </div>
      <div class="artist-page-coverage-pills">
        <span class="artist-page-coverage-pill">${fmtNum(coveragePct)}% heard</span>
        <span class="artist-page-coverage-pill">${fmtNum(heard)} heard</span>
        <span class="artist-page-coverage-pill">${fmtNum(unheard)} unheard</span>
      </div>
    </div>
    <div class="artist-page-coverage-bar"><div class="artist-page-coverage-fill" style="width:${Math.max(0, Math.min(100, coveragePct))}%"></div></div>`;
}

function renderDiscography(d, section) {
  const albums  = d.albums || [];
  const eras    = d.eras   || [];
  const total   = d.total_albums   || 0;
  const explored= d.explored_albums || 0;

  const heardTracks = Number(d.catalog_tracks_heard || 0);
  const totalTracks = Number(d.catalog_tracks_total || 0);
  const unheardTracks = Math.max(0, totalTracks - heardTracks);
  const topEra = d.top_era ? ` Preferred era: <strong>${escapeHtml(d.top_era)}</strong>.` : '';
  let html = `<div class="disco-summary">${explored} of ${total} albums and EPs explored.${topEra}</div>`;
  html += `<div class="disco-callout-row">
    <div class="disco-callout"><div class="val">${fmtNum(d.catalog_coverage_pct || 0)}%</div><div class="lbl">Catalog heard</div></div>
    <div class="disco-callout"><div class="val">${fmtNum(unheardTracks)}</div><div class="lbl">Tracks left to hear</div></div>
  </div>`;
  if ((d.catalog_tracks_total || 0) > 0) {
    html += `<div class="artist-panel-meta-note" style="margin-bottom:10px">${fmtNum(d.catalog_tracks_heard || 0)} of ${fmtNum(d.catalog_tracks_total || 0)} mapped songs heard at least once.</div>`;
  }

  // Era pills
  if (eras.length) {
    html += `<div class="disco-era-row">${eras.map((e,i) =>
      `<span class="disco-era-pill${i===0?' top':''}">${escapeHtml(e.era)} · ${e.heard_tracks} tracks</span>`
    ).join('')}</div>`;
  }

  // Filter buttons
  const types = ['all','unheard','album','ep','single','live','compilation'];
  html += `<div class="disco-filter-row">
    ${types.map(t => `<button class="disco-filter-btn${_discoFilter===t?' active':''}"
      onclick="setDiscoFilter('${t}')">${t}</button>`).join('')}
    <button class="disco-show-variants" onclick="toggleDiscoVariants()">
      ${_discoShowVariants ? 'hide variants' : 'show variants'}
    </button>
  </div>`;

  // Album list
  const filtered = albums.filter(a => {
    if (!_discoShowVariants && a.is_variant) return false;
    if (_discoFilter === 'unheard') return (a.exploration_pct || 0) <= 0;
    if (_discoFilter !== 'all' && a.album_class !== _discoFilter) return false;
    return true;
  });

  if (!filtered.length) {
    html += `<div class="empty" style="font-size:11px">Nothing in this filter.</div>`;
  } else {
    html += filtered.map(a => {
      const img = (a.images && a.images[0]) ? a.images[0].url : '';
      const artEl = img
        ? `<img class="disco-album-art" src="${escapeHtml(img)}" alt="" loading="lazy">`
        : `<div class="disco-album-art"></div>`;
      const pct   = a.exploration_pct || 0;
      const state = a.exploration_state || 'unheard';
      const heardTracks = Number(a.heard_tracks || 0);
      const totalTracks = Number(a.total_tracks || 0);
      const unheardTracks = Math.max(0, totalTracks - heardTracks);
      const isOpen = !!_discoOpenAlbums[a.id];
      const expBar = `<div class="disco-exp-wrap">
        <div class="disco-exp-bar"><div class="disco-exp-fill ${state}" style="width:${pct}%"></div></div>
        <div class="disco-exp-label">${pct > 0 ? pct+'%' : '-'}</div>
      </div>`;
      const variantLabel = a.is_variant ? ' <span style="font-size:9px;opacity:.5">(variant)</span>' : '';
      const typeClass = ['album','live','compilation'].includes(a.album_class) ? a.album_class : '';
      const detailId = `disco-tracks-${escapeHtml(a.id || '')}`;
      const footer = unheardTracks > 0
        ? `${fmtNum(unheardTracks)} unheard`
        : `Fully explored`;
      const tracksHtml = isOpen
        ? `<div class="disco-tracklist" id="${detailId}">${renderDiscographyTracklist(a.id)}</div>`
        : '';
      return `<div class="disco-album${a.is_variant?' is-variant':''}">
        <button class="disco-album-head" type="button" onclick="toggleDiscographyRelease('${escapeHtml(a.id || '')}')">
          <span class="disco-toggle">${isOpen ? '&#8722;' : '&#43;'}</span>
          ${artEl}
          <div class="disco-album-info">
            <div class="disco-album-name">${escapeHtml(a.canonical_name || a.name || '')}${variantLabel}</div>
            <div class="disco-album-meta">
              <span class="disco-type-badge ${typeClass}">${escapeHtml(a.album_class)}</span>
              <span>${a.release_year || '-'}</span>
              ${a.total_tracks ? `<span>${a.total_tracks} tracks</span>` : ''}
              <span>${fmtNum(heardTracks)} heard</span>
              <span>${escapeHtml(footer)}</span>
            </div>
          </div>
          ${expBar}
        </button>
        ${tracksHtml}
      </div>`;
    }).join('');
  }

  section.innerHTML = html;
}

function renderDiscographyTracklist(albumId) {
  const data = _discoTrackCache[albumId];
  if (!data) {
    queueMicrotask(() => loadDiscographyRelease(albumId));
    return '<div class="spinner-wrap"><span class="spinner"></span>Loading songs</div>';
  }
  const tracks = data.tracks || [];
  if (!tracks.length) {
    return '<div class="empty" style="font-size:11px">No songs found for this release.</div>';
  }
  return tracks.map(t => {
    const heard = !!t.heard;
    const play = t.uri
      ? `playTrack(${JSON.stringify(t.uri)})`
      : `playTrackByMeta(${JSON.stringify(t.name || '')},${JSON.stringify((_currentArtistPage.artistName || ''))},'')`;
    return `<div class="disco-track-row${heard ? ' heard' : ''}">
      <div class="disco-track-no">${fmtNum(t.track_number || 0)}</div>
      <div class="disco-track-name-wrap">
        <div class="disco-track-name">${escapeHtml(t.name || '')}</div>
        <div class="disco-track-sub">${t.duration_ms ? fmtTime(t.duration_ms) : ''}</div>
      </div>
      <span class="disco-track-badge${heard ? ' heard' : ''}">${heard ? 'Heard' : 'Unheard'}</span>
      <button class="disco-track-play" type="button" onclick='${play}'>Play</button>
    </div>`;
  }).join('');
}

async function loadDiscographyRelease(albumId) {
  if (_discoTrackCache[albumId] && !_discoTrackCache[albumId]._loading) return;
  _discoTrackCache[albumId] = { _loading: true, tracks: [] };
  const target = document.getElementById(`disco-tracks-${albumId}`);
  if (target) target.innerHTML = '<div class="spinner-wrap"><span class="spinner"></span>Loading songs</div>';
  try {
    const r = await fetch('/api/artist/discography/release?album_id=' + encodeURIComponent(albumId));
    const d = await r.json();
    if (d.error) {
      _discoTrackCache[albumId] = { tracks: [], error: d.error };
    } else {
      _discoTrackCache[albumId] = d;
    }
  } catch (e) {
    _discoTrackCache[albumId] = { tracks: [], error: 'Could not load songs.' };
  }
  const el = document.getElementById(`disco-tracks-${albumId}`);
  if (el) {
    if (_discoTrackCache[albumId].error) {
      el.innerHTML = `<div class="empty" style="font-size:11px">${escapeHtml(_discoTrackCache[albumId].error)}</div>`;
    } else {
      el.innerHTML = renderDiscographyTracklist(albumId);
      if (_discoTrackCache[albumId]._stale_notice) {
        el.insertAdjacentHTML('beforeend', `<div class="artist-panel-meta-note">Spotify metadata is cooling off. Showing cached tracklist for now.</div>`);
      }
    }
  }
}

function toggleDiscographyRelease(albumId) {
  _discoOpenAlbums[albumId] = !_discoOpenAlbums[albumId];
  const section = document.getElementById('disco-section');
  if (_discoData && section) renderDiscography(_discoData, section);
}

function setDiscoFilter(f) {
  _discoFilter = f;
  const section = document.getElementById('disco-section');
  if (_discoData && section) renderDiscography(_discoData, section);
}

function toggleDiscoVariants() {
  _discoShowVariants = !_discoShowVariants;
  const section = document.getElementById('disco-section');
  if (_discoData && section) renderDiscography(_discoData, section);
}


// ── Dev error overlay ────────────────────────────────────────────────────
let _devErrors = [];
let _spotifySearchBackoffUntilMs = 0;
let _spotifySearchRetryAfterS = null;
let _spotifyGuardProtectUntilMs = 0;
let _spotifyGuardProtectReason = null;
let _spotifyGuardGlobalUntilMs = 0;
let _spotifyGuardRestoredUntilMs = 0;
let _spotifyGuardRestoredReason = null;
let _spotifyGuardRestoredRetryAfterS = null;
let _spotifyGuardRestoredPending = false;
let _spotifyGuardRevalidating = false;

function renderSpotifySearchCooldown() {
  const banner = document.getElementById("spotify-search-cooldown");
  const badge = document.getElementById("spotify-search-cooldown-badge");
  const copy = document.getElementById("spotify-search-cooldown-copy");
  const clearBtn = document.getElementById("spotify-guard-clear-btn");
  if (!banner || !badge || !copy) return;
  const globalRemaining = Math.max(0, Math.ceil((_spotifyGuardGlobalUntilMs - Date.now()) / 1000));
  const protectRemaining = Math.max(0, Math.ceil((_spotifyGuardProtectUntilMs - Date.now()) / 1000));
  const restoredRemaining = Math.max(0, Math.ceil((_spotifyGuardRestoredUntilMs - Date.now()) / 1000));
  const remaining = Math.max(0, Math.ceil((_spotifySearchBackoffUntilMs - Date.now()) / 1000));
  if (clearBtn) clearBtn.style.display = "none";
  if (globalRemaining > 60) {
    banner.style.display = "flex";
    const h = Math.floor(globalRemaining / 3600);
    const m = Math.floor((globalRemaining % 3600) / 60);
    const s = globalRemaining % 60;
    let countdown = h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`;
    const availAt = new Date(_spotifyGuardGlobalUntilMs).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    badge.textContent = h > 0 ? `${h}h ${m}m` : `${m}m ${s}s`;
    copy.innerHTML = `<strong>Spotify rate-limited</strong><br>All Spotify enrichment is blocked for <code>${countdown}</code> — available again at <strong>${availAt}</strong>. Playback and queue still work.`;
  } else if (_spotifyGuardRestoredPending && restoredRemaining > 0) {
    banner.style.display = "flex";
    badge.textContent = restoredRemaining + "s";
    if (clearBtn) clearBtn.style.display = "inline-flex";
    const staleWord = _spotifyGuardRevalidating ? "revalidating now" : "restored from a previous run";
    const extra = _spotifyGuardRestoredReason ? ` after <code>${escapeHtml(_spotifyGuardRestoredReason)}</code>` : "";
    const retry = _spotifyGuardRestoredRetryAfterS != null ? ` Last seen Retry-After was <code>${_spotifyGuardRestoredRetryAfterS}s</code>.` : "";
    copy.innerHTML = `<strong>Spotify cooldown restored</strong><br>This is local saved state, ${staleWord}${extra}.${retry}`;
  } else if (protectRemaining > 0) {
    banner.style.display = "flex";
    badge.textContent = protectRemaining + "s";
    copy.innerHTML = `<strong>Spotify protect mode</strong><br>Spam-prone enrichment is auto-blocked for <code>${protectRemaining}s</code>${_spotifyGuardProtectReason ? ` after <code>${escapeHtml(_spotifyGuardProtectReason)}</code>` : ""}.`;
  } else if (remaining > 0) {
    banner.style.display = "flex";
    badge.textContent = remaining + "s";
    if (_spotifySearchRetryAfterS != null) {
      copy.innerHTML = `<strong>Spotify search cooldown</strong><br>Spotify returned <code>Retry-After: ${_spotifySearchRetryAfterS}s</code> for search requests.`;
    } else {
      copy.innerHTML = `<strong>Spotify search cooldown</strong><br>Artist lookups and similar-track search are temporarily cooling off.`;
    }
  } else {
    banner.style.display = "none";
    badge.textContent = "0s";
  }
}
setInterval(renderSpotifySearchCooldown, 1000);

async function clearLocalGuardState() {
  try {
    const r = await fetch("/api/dev/clear-guard", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || "Failed to clear guard state.");
    _spotifyGuardGlobalUntilMs = 0;
    _spotifyGuardProtectUntilMs = 0;
    _spotifyGuardRestoredUntilMs = 0;
    _spotifyGuardRestoredPending = false;
    _spotifySearchBackoffUntilMs = 0;
    showToast(d.message || "Cleared local Spotify guard state.");
    renderSpotifySearchCooldown();
  } catch (e) {
    showToast(e.message || "Couldn't clear local guard state.", true);
  }
}

async function pollDevErrors() {
  try {
    const r = await fetch("/api/dev/errors");
    const d = await r.json();
    _devErrors = d.errors || [];
    const badge = document.getElementById("dev-error-badge");
    if (badge) {
      if (_devErrors.length > 0) {
        badge.style.display = "inline-block";
        badge.textContent = "Errors: " + _devErrors.length;
      } else {
        badge.style.display = "none";
      }
    }
  } catch(e) {}
  setTimeout(pollDevErrors, 12000);
}
function openDevErrors() {
  const modal    = document.getElementById("dev-error-modal");
  const backdrop = document.getElementById("dev-error-modal-backdrop");
  const list     = document.getElementById("dev-error-list");
  if (!modal) return;
  if (!_devErrors.length) { list.innerHTML = '<div class="empty" style="padding:16px">No errors recorded.</div>'; }
  else {
    list.innerHTML = _devErrors.map(e => {
      const d = new Date(e.ts);
      const time = d.toLocaleTimeString();
      return `<div class="dev-err-entry">
        <div class="dev-err-meta">${escapeHtml(time)} - ${escapeHtml(e.context || "unknown")}</div>
        <div class="dev-err-msg">${escapeHtml(e.type)}: ${escapeHtml(e.message)}</div>
        <div class="dev-err-tb">${escapeHtml(e.traceback || "")}</div>
      </div>`;
    }).join("");
  }
  backdrop.classList.add("open");
  modal.classList.add("open");
}
function closeDevErrors() {
  document.getElementById("dev-error-modal")?.classList.remove("open");
  document.getElementById("dev-error-modal-backdrop")?.classList.remove("open");
}
function copyDevErrors() {
  const text = JSON.stringify(_devErrors, null, 2);
  navigator.clipboard.writeText(text).then(
    () => showToast("Errors copied to clipboard"),
    () => showToast("Clipboard write failed", true)
  );
}
pollDevErrors();

// Last.fm import
let _lfmPollTimer = null;

async function startLfmImport(reset=false) {
  const btn     = document.getElementById("btn-lfm-import");
  const resetBtn= document.getElementById("btn-lfm-reset");
  const stat    = document.getElementById("lfm-import-status");
  if (btn) btn.disabled = true;
  if (resetBtn) resetBtn.disabled = true;
  stat.textContent = reset ? "Resetting and re-importing..." : "Starting...";
  try {
    const r = await fetch("/api/lastfm/import", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({reset})});
    const d = await r.json();
    if (d.error) { stat.textContent = "Error: " + d.error; if (btn) btn.disabled = false; return; }
    pollLfmImport();
  } catch(e) {
    stat.textContent = "Request failed.";
    if (btn) btn.disabled = false;
  }
}

async function pollLfmImport() {
  if (_lfmPollTimer) clearTimeout(_lfmPollTimer);
  try {
    const r = await fetch("/api/lastfm/import");
    const d = await r.json();
    const stat   = document.getElementById("lfm-import-status");
    const bar    = document.getElementById("lfm-import-bar");
    const wrap   = document.getElementById("lfm-import-bar-wrap");
    const detail = document.getElementById("lfm-import-detail");
    const btn    = document.getElementById("btn-lfm-import");

    if (d.status === "running") {
      if (wrap) wrap.style.display = "block";
      const pct = d.total_pages > 0 ? Math.round((d.page / d.total_pages) * 100) : 0;
      if (bar)    bar.style.width = pct + "%";
      if (stat)   stat.textContent = `Page ${d.page} of ${d.total_pages} - ${d.imported.toLocaleString()} events imported`;
      if (detail) detail.textContent = d.total > 0 ? `${d.total.toLocaleString()} total scrobbles` : "";
      _lfmPollTimer = setTimeout(pollLfmImport, 1500);
    } else if (d.status === "done") {
      if (bar)    bar.style.width = "100%";
      if (stat)   stat.textContent = `Done - ${d.imported.toLocaleString()} events imported`;
      if (detail) detail.textContent = "";
      if (btn)    btn.disabled = false;
      if (document.getElementById("btn-lfm-reset")) document.getElementById("btn-lfm-reset").disabled = false;
    } else if (d.status === "error") {
      if (stat)   stat.textContent = "Error: " + (d.error || "unknown");
      if (btn)    btn.disabled = false;
      if (document.getElementById("btn-lfm-reset")) document.getElementById("btn-lfm-reset").disabled = false;
    }
  } catch(e) {
    _lfmPollTimer = setTimeout(pollLfmImport, 3000);
  }
}

// On settings open, check if an import is already running and resume polling
function refreshLfmImportStatus() {
  fetch("/api/lastfm/import").then(r=>r.json()).then(d=>{
    if (d.status === "running") pollLfmImport();
    else if (d.status !== "idle") {
      const stat = document.getElementById("lfm-import-status");
      if (stat) stat.textContent = d.status === "done"
        ? `Done - ${(d.imported||0).toLocaleString()} events imported`
        : (d.error ? "Error: " + d.error : "");
    }
  }).catch(()=>{});
}

function openArtistPanel(artistId, artistName, artistImage) {
  const nameEl   = document.getElementById("artist-page-name");
  const avatarEl = document.getElementById("artist-page-avatar");
  const bodyEl   = document.getElementById("artist-page-body");
  const subtitleEl = document.getElementById("artist-page-subtitle");
  const heroBioEl = document.getElementById("artist-page-hero-bio");
  const heroTagsEl = document.getElementById("artist-page-hero-tags");
  const heroMetaEl = document.getElementById("artist-page-hero-meta");
  const heroStatsEl = document.getElementById("artist-page-hero-stats");
  if (!bodyEl) return;
  nameEl.textContent  = artistName || "Artist";
  avatarEl.src = artistImage || "";
  avatarEl.style.display = artistImage ? "" : "none";
  if (subtitleEl) subtitleEl.textContent = "Relationship, discography, and context";
  if (heroBioEl) { heroBioEl.textContent = ""; heroBioEl.style.display = "none"; }
  if (heroTagsEl) { heroTagsEl.innerHTML = ""; heroTagsEl.style.display = "none"; }
  if (heroMetaEl) { heroMetaEl.textContent = ""; heroMetaEl.style.display = "none"; }
  if (heroStatsEl) heroStatsEl.innerHTML = "";
  _workspaceArtistContext = {
    title: artistName || 'Artist',
    subtitle: 'Relationship, discography, and context',
    kicker: 'Artist relationship',
    image: artistImage || '',
  };
  bodyEl.innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading artist</div>`;
  showArtistPageShell();
  // Fetch artist info
  const params = new URLSearchParams();
  if (artistId)   params.set("artist_id",   artistId);
  if (artistName) params.set("artist_name",  artistName);
  params.set("source", "page");  // only page views are logged as ARTIST_VIEWED
  fetch("/api/artist-info?" + params.toString())
    .then(r => r.json())
    .then(info => renderArtistPanel(info, artistName, artistImage))
    .catch(e  => { bodyEl.innerHTML = `<div class="empty">Failed to load.</div>`; });
}

function closeArtistPanel() {
  goToDashboard();
}

function renderArtistPanel(info, artistName, artistImage) {
  const bodyEl  = document.getElementById("artist-page-body");
  const nameEl  = document.getElementById("artist-page-name");
  const avatarEl = document.getElementById("artist-page-avatar");
  const kickerEl = document.getElementById("artist-page-kicker");
  const subtitleEl = document.getElementById("artist-page-subtitle");
  const coverageEl = document.getElementById("artist-page-coverage");
  const heroBioEl = document.getElementById("artist-page-hero-bio");
  const heroTagsEl = document.getElementById("artist-page-hero-tags");
  const heroMetaEl = document.getElementById("artist-page-hero-meta");
  const heroStatsEl = document.getElementById("artist-page-hero-stats");
  if (!bodyEl) return;
  const resolvedArtistImage = artistImage
    || info.artist_image
    || ((info.top_tracks && info.top_tracks[0] && info.top_tracks[0].image) || "")
    || ((info.related && info.related[0] && info.related[0].image) || "");
  if (avatarEl) {
    avatarEl.src = resolvedArtistImage || "";
    avatarEl.style.display = resolvedArtistImage ? "" : "none";
  }
  if (kickerEl) kickerEl.textContent = "Artist relationship";
  // Liked badge in panel header
  const existingBadge = document.getElementById("artist-liked-badge");
  if (existingBadge) existingBadge.remove();
  if (info.artist_liked) {
    const badge = document.createElement("span");
    badge.id = "artist-liked-badge";
    badge.className = "artist-liked-badge";
    badge.innerHTML = "&#9733; liked artist";
    nameEl.parentNode.insertBefore(badge, nameEl.nextSibling);
  }

  const sections = [];
  const ls = info.listen_stats || {};
  if (subtitleEl) {
    const parts = [];
    if (ls.listen_count) parts.push(`${fmtNum(ls.listen_count)} listens`);
    if (ls.distinct_tracks) parts.push(`${fmtNum(ls.distinct_tracks)} unique tracks`);
    if (ls.distinct_days) parts.push(`${fmtNum(ls.distinct_days)} active days`);
    subtitleEl.textContent = parts.length ? parts.join(" - ") : "Relationship, catalog progress, and discovery context";
  }
  if (heroBioEl) {
    const bio = (info.bio || "").trim();
    if (bio) {
      heroBioEl.textContent = bio.length > 360 ? `${bio.slice(0, 360).trim()}...` : bio;
      heroBioEl.style.display = "";
    } else {
      heroBioEl.textContent = "";
      heroBioEl.style.display = "none";
    }
  }
  if (heroTagsEl) {
    const tags = (info.lastfm_tags || []).slice(0, 8);
    if (tags.length) {
      heroTagsEl.innerHTML = tags.map(t => `<span class="artist-page-hero-tag">${escapeHtml(t)}</span>`).join("");
      heroTagsEl.style.display = "";
    } else {
      heroTagsEl.innerHTML = "";
      heroTagsEl.style.display = "none";
    }
  }
  if (heroMetaEl) {
    const metaParts = [];
    if (info.stats && info.stats.listeners) metaParts.push(`${fmtNum(info.stats.listeners)} listeners on Last.fm`);
    if (info.lastfm_url) metaParts.push(`<a class="artist-link-btn" href="${escapeHtml(info.lastfm_url)}" target="_blank">view on Last.fm &#8599;</a>`);
    if (metaParts.length) {
      heroMetaEl.innerHTML = metaParts.join(" - ");
      heroMetaEl.style.display = "";
    } else {
      heroMetaEl.innerHTML = "";
      heroMetaEl.style.display = "none";
    }
  }
  _workspaceArtistContext = {
    title: artistName || info.name || 'Artist',
    subtitle: (subtitleEl && subtitleEl.textContent) || 'Relationship, discography, and context',
    kicker: 'Artist relationship',
    image: resolvedArtistImage || '',
  };
  _currentArtistPage = {
    artistId: info.artist_id || '',
    artistName: artistName || info.name || '',
    artistImage: resolvedArtistImage || '',
    info,
  };
  const cachedDisco = (info.artist_id && _discoCache[info.artist_id]) || ((_discoArtistId === info.artist_id) ? _discoData : null);
  if (cachedDisco && cachedDisco.catalog_tracks_total) {
    renderArtistCoverageSummary(cachedDisco);
  }
  const researchBtn = document.getElementById("artist-page-research-btn");
  if (researchBtn) {
    researchBtn.onclick = () => openResearchModal('artist', info.artist_id || (artistName || info.name || ''), artistName || info.name || '');
  }
  if (heroStatsEl) {
    heroStatsEl.innerHTML = [
      { v: fmtNum(ls.listen_count || 0), k: "Total listens" },
      { v: fmtNum(ls.distinct_tracks || 0), k: "Unique tracks" },
      { v: fmtNum(ls.distinct_albums || 0), k: "Releases touched" },
      { v: fmtNum(ls.distinct_days || 0), k: "Active days" },
      { v: fmtNum(ls.revisit_days || 0), k: "Return days" },
      { v: fmtNum(ls.spotify_liked_count || ls.liked_count_total || ls.liked_count || 0), k: "Saved in Spotify" },
    ].map(stat => `
      <div class="artist-page-hero-stat">
        <div class="v">${stat.v}</div>
        <div class="k">${stat.k}</div>
      </div>`).join("");
  }

  function fmtArtistDate(ts) {
    if (!ts) return "-";
    try {
      return new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    } catch(_) {
      return "-";
    }
  }

  // Taste similarity
  if (ls.similarity_pct !== null && ls.similarity_pct !== undefined) {
    const pct = ls.similarity_pct;
    const label = pct >= 70 ? "Strong match" : pct >= 40 ? "Moderate match" : pct > 0 ? "Slight overlap" : "New territory";
    sections.push(`<div class="artist-panel-section">
      <h4>Taste match</h4>
      <div class="artist-similarity-bar"><div class="artist-similarity-fill" style="width:${pct}%"></div></div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted)">
        <span>${escapeHtml(label)}</span><span>${pct}%</span>
      </div>
      ${ls.matching_genres && ls.matching_genres.length ? `<div style="margin-top:6px">${ls.matching_genres.map(g=>`<span class="artist-panel-tag">${escapeHtml(g)}</span>`).join("")}</div>` : ""}
    </div>`);
  }

  // Your history with this artist
  if (ls.listen_count > 0 || ls.liked_count > 0 || ls.imported_listens > 0) {
    const detailCards = [
      `<div class="artist-page-detail-card"><div class="k">First heard</div><div class="v">${escapeHtml(fmtArtistDate(ls.first_heard_at))}</div><div class="sub">the first time this artist showed up in your recorded history</div></div>`,
      `<div class="artist-page-detail-card"><div class="k">Last heard</div><div class="v">${escapeHtml(fmtArtistDate(ls.last_heard_at))}</div><div class="sub">most recent documented listen across app + imports</div></div>`,
      `<div class="artist-page-detail-card"><div class="k">Imported scrobbles</div><div class="v">${fmtNum(ls.imported_listens || 0)}</div><div class="sub">${fmtNum(ls.app_listens || 0)} live app listens tracked locally</div></div>`,
      `<div class="artist-page-detail-card"><div class="k">Saved in Spotify</div><div class="v">${fmtNum(ls.spotify_liked_count || ls.liked_count_total || ls.liked_count || 0)}</div><div class="sub">tracks from this artist already sitting in your library</div></div>`
    ].join("");
    sections.push(`<div class="artist-panel-section">
      <div class="artist-page-section-head">
        <div>
          <h4>Your relationship</h4>
          <div class="artist-page-section-copy">A cleaner read on how long this artist has been with you, how often you return, and how much of that relationship lives in Spotify vs imports.</div>
        </div>
      </div>
      <div class="artist-page-detail-grid">${detailCards}</div>
    </div>`);
  }

  const buildArtistDiscoveryRow = (t, fallbackSub, emptyLabel) => {
    const img = t.image
      ? `<img class="artist-page-discovery-art" src="${escapeHtml(t.image)}" alt="">`
      : `<div class="artist-page-discovery-art"></div>`;
    const playAction = t.uri
      ? `playTrack(${JSON.stringify(t.uri)})`
      : `playTrackByMeta(${JSON.stringify(t.name || '')},${JSON.stringify(t.artists || info.name || '')},${JSON.stringify(t.url || '')})`;
    return `<div class="artist-page-discovery-row">
      ${img}
      <div>
        <div class="artist-page-discovery-name">${escapeHtml(t.name || '')}</div>
        <div class="artist-page-discovery-sub">${escapeHtml(t.album || t.artists || fallbackSub || emptyLabel)}</div>
      </div>
      <div class="artist-page-discovery-dur">${t.duration_ms ? fmtTime(t.duration_ms) : ''}</div>
      <button class="artist-page-discovery-play" type="button" onclick='${playAction}' title="Play">&#9654;</button>
    </div>`;
  };
  const topTrackRows = (info.top_tracks || []).slice(0, 6).map(t => buildArtistDiscoveryRow(t, "Top track", "Top track")).join("");
  const unheardRows = (info.unheard_top_tracks || []).map(t => buildArtistDiscoveryRow(t, "Unheard track", "Unheard track")).join("");
  sections.push(`<div class="artist-page-section">
    <div class="artist-page-section-head">
      <div>
        <h4>Where to start</h4>
        <div class="artist-page-section-copy">One side shows the songs that define this artist at a glance. The other keeps your untouched entry points within easy reach so you can move straight into discovery.</div>
      </div>
    </div>
    <div class="artist-page-discovery-grid">
      <div class="artist-page-discovery-col">
        <h5>Top tracks</h5>
        ${topTrackRows
          ? `<div class="artist-page-discovery-shell">${topTrackRows}</div>`
          : `<div class="artist-page-discovery-empty">No top tracks surfaced for this artist yet.</div>`}
      </div>
      <div class="artist-page-discovery-col">
        <h5>Haven't heard yet</h5>
        ${unheardRows
          ? `<div class="artist-page-discovery-shell">${unheardRows}</div>`
          : `<div class="artist-page-discovery-empty">No unheard top tracks surfaced yet. The discography below is still the best way to hunt for untouched material.</div>`}
      </div>
    </div>
  </div>`);

  // Discography section placeholder (loaded async if artistId known)
  const discoHint = (ls.imported_listens || 0) > 0
    ? `Imported Last.fm scrobbles count in the stats above, but album-by-album progress only fills in for listens the app could map to Spotify albums.`
    : `Discography progress fills in as the app maps listens to Spotify albums over time.`;
  const discoSection = `<div class="artist-panel-section" id="disco-section-wrap">
    <h4>Discography</h4>
    <div id="disco-section"><div class="spinner-wrap"><span class="spinner"></span>Loading discography</div></div>
    <div class="artist-panel-meta-note">${escapeHtml(discoHint)}</div>
  </div>`;

  // Append "Add to Research" CTA at the bottom of the panel
  const researchCta = `<div class="artist-panel-section" style="margin-top:8px;border-top:1px solid rgba(255,255,255,0.07);padding-top:12px">
    <button class="research-open-btn" style="width:100%;justify-content:center"
      onclick="openResearchModal('artist','${escapeHtml(artistName||'')}','${escapeHtml(artistName||'')}')">
      &#128300; Add Artist to Research
    </button>
  </div>`;
  bodyEl.innerHTML = (sections.length ? sections.join("") : `<div class="empty">No info available.</div>`) + discoSection + researchCta;
  if (coverageEl && !cachedDisco) renderArtistCoverageSummary(info);
  // Trigger discography load if we have an artist id
  if (info && info.artist_id) {
    loadDiscography(info.artist_id);
  } else if (_discoArtistId) {
    loadDiscography(_discoArtistId);
  }
}

// Make artist names clickable — call this after rendering queue/history rows
function wrapArtistLinks(containerEl) {
  if (!containerEl) return;
  containerEl.querySelectorAll(".queue-artist, .history-sub, .bar-artist").forEach(el => {
    if (el.dataset.artistLinked) return;
    el.dataset.artistLinked = "1";
    const name = el.textContent.split(",")[0].trim().replace(/·.*$/, "").trim();
    if (!name) return;
    el.title = `View ${name} profile`;
    el.classList.add("artist-link");
    el.addEventListener("click", e => {
      e.stopPropagation();
      openArtistPanel("", name, "");
    });
  });
}

// Legacy stubs (referenced elsewhere) — sidebar is always visible now
function openQueue() { expandSidebar(); }
function closeQueue() {}
function toggleQueue() { expandSidebar(); }
// Restore drawer state from last session
(function() {
  let saved = "0";
  try { saved = localStorage.getItem(QUEUE_DRAWER_KEY) || "0"; } catch(_) {}
  if (saved === "1") openQueue();
})();

// --- Playlist switcher ---
let playlistsLoaded = false;
let _playlistRetryTimer = null;
let _playlistRetryUntilMs = 0;
async function loadPlaylists(force=false) {
  if (playlistsLoaded && !force) return;
  if (!force && _playlistRetryUntilMs > Date.now()) return;
  if (_playlistListInFlight) return;
  const inp = document.getElementById("pl-search");
  if (inp) inp.placeholder = "Loading playlists...";
  try {
    _playlistListInFlight = true;
    const r = await fetch("/api/playlists");
    const d = await r.json();
    if (!r.ok || d.error) {
      if (r.status === 503 || r.status === 429 || d.error === "guard" || d.error === "rate_limited") {
        const retryIn = (d.retry_after || 15) * 1000;
        _playlistRetryUntilMs = Date.now() + retryIn;
        if (_playlistRetryTimer) clearTimeout(_playlistRetryTimer);
        if (inp) inp.placeholder = `Cooling down - retrying in ${Math.round(retryIn/1000)}s...`;
        _playlistRetryTimer = setTimeout(() => {
          playlistsLoaded = false;
          _playlistRetryTimer = null;
          _playlistRetryUntilMs = 0;
          loadPlaylists();
        }, retryIn);
        return;
      }
      if (inp) inp.placeholder = "Error loading playlists";
      return;
    }
    _playlistRetryUntilMs = 0;
    if (_playlistRetryTimer) { clearTimeout(_playlistRetryTimer); _playlistRetryTimer = null; }
    if (d.warning) showToast(d.warning);
    playlistsLoaded = true;
    window._plComboOptions = d.playlists.map(p => ({
      id: p.id,
      name: p.name,
      suffix: (p.collaborative && !p.owned ? "[collab] " : "") + (p.id === d.active_id ? "(active) " : "") + "- " + p.tracks + " tracks",
      active: p.id === d.active_id,
    }));
    if (inp) inp.placeholder = "Search playlists or paste URL...";
    initPlCombo("pl-combo-sidebar", "pl-search", "pl-combo-sidebar-list", "pl-select-val", window._plComboOptions,
      (id) => doSetPlaylist(id));
    // Open the dropdown immediately so user sees options
    const list = document.getElementById("pl-combo-sidebar-list");
    if (list) {
      list.innerHTML = window._plComboOptions.map((o, i) =>
        `<div class="pl-combo-opt${o.active ? " selected" : ""}" data-id="${escapeHtml(o.id)}" data-name="${escapeHtml(o.name)}" data-idx="${i}">${escapeHtml(o.name)}<span style="color:var(--muted);font-size:10px;margin-left:4px">${o.suffix||""}</span></div>`
      ).join("");
      list.classList.remove("hidden");
    }
  } catch (e) {
    if (inp) inp.placeholder = "Failed to load playlists";
  } finally {
    _playlistListInFlight = false;
  }
}

async function doSetPlaylist(value) {
  if (!value) { showToast("Pick a playlist or paste a Spotify URL.", true); return; }
  const picker = document.getElementById("sidebar-pl-picker");
  const inp = document.getElementById("pl-search");
  if (inp) inp.disabled = true;
  const d = await postAction("/api/set-playlist", { playlist: value.trim() });
  if (inp) inp.disabled = false;
  if (d && !d.error) {
    if (picker) picker.classList.remove("open");
    if (inp) { inp.value = ""; inp.dataset.selectedId = ""; }
    playlistsLoaded = false;
    queueLoaded = false;
    loadQueue();
    showToast("Playlist set!");
  } else if (d && d.error) {
    showToast(d.error, true);
  }
}

bindClick("pl-toggle", () => {
  const panel = document.getElementById("pl-switcher");
  if (!panel) return;
  panel.classList.toggle("open");
  if (panel.classList.contains("open")) loadPlaylists();
});

bindClick("btn-set-playlist", async () => {
  const sel = document.getElementById("pl-select-val");
  const inp = document.getElementById("pl-search");
  const paste = document.getElementById("pl-paste");
  const value = paste.value.trim() || (inp && inp.dataset.selectedId) || (sel ? sel.value : "");
  if (!value) { showToast("Pick a playlist or paste a URL first.", true); return; }
  const btn = document.getElementById("btn-set-playlist");
  btn.disabled = true; btn.textContent = "Setting...";
  const d = await postAction("/api/set-playlist", { playlist: value });
  btn.disabled = false; btn.textContent = "Set as discovery playlist";
  if (d && !d.error) {
    paste.value = "";
    document.getElementById("pl-switcher").classList.remove("open");
    playlistsLoaded = false;
    queueLoaded = false; loadQueue();  // reload queue on playlist change
  }
});

// ── Spotify import ───────────────────────────────────────────────────────────
let _spotifyImportPoll = null;

async function startSpotifyImport() {
  const btn = document.getElementById("btn-spotify-import");
  const statusEl = document.getElementById("spotify-import-status");
  const detailEl = document.getElementById("spotify-import-detail");
  if (btn) btn.disabled = true;
  const d = await postAction("/api/spotify/import", {});
  if (d && d.error) {
    if (statusEl) statusEl.textContent = "Error: " + d.error;
    if (btn) btn.disabled = false;
    return;
  }
  if (statusEl) statusEl.textContent = "Running…";
  if (detailEl) detailEl.style.display = "block";
  clearInterval(_spotifyImportPoll);
  _spotifyImportPoll = setInterval(async () => {
    try {
      const r = await fetch("/api/spotify/import");
      const s = await r.json();
      const parts = [];
      if (s.imported_plays)   parts.push(`${s.imported_plays} plays`);
      if (s.imported_artists) parts.push(`${s.imported_artists} artists`);
      if (s.imported_liked)   parts.push(`${s.imported_liked} liked`);
      const summary = parts.length ? parts.join(" · ") : "…";
      if (detailEl) detailEl.textContent = s.step ? `${s.step}  ${summary}` : summary;
      if (s.status === "done" || s.status === "error") {
        clearInterval(_spotifyImportPoll);
        if (btn) btn.disabled = false;
        if (statusEl) statusEl.textContent = s.status === "done"
          ? `✓ Done — ${summary}`
          : `✗ Error: ${s.error || "unknown"}`;
        if (s.status === "done") {
          playlistsLoaded = false;
          loadQueue();
        }
      }
    } catch(e) { /* ignore transient */ }
  }, 1500);
}

// Auto-poll if import was already running when page loaded
(async () => {
  try {
    const r = await fetch("/api/spotify/import");
    const s = await r.json();
    if (s.status === "running") startSpotifyImport();
  } catch(e) {}
})();

function renderItem(it, options={}) {
  const removed = options.removed;
  const favs = options.favs || [];
  let tagHTML = "";
  if (removed) {
    const reasonText = it.reason || "";
    tagHTML = `<span class="removed-tag">REMOVED</span>${reasonText ? `<span class="reason-tag">(${reasonText})</span>` : ""}`;
  } else if (it.was_in_playlist) {
    tagHTML = `<span class="kept-tag">in playlist</span>`;
  }
  const isFav = anyArtistFav(it.track.artists, favs);
  const favStar = isFav ? `<span class="fav-marker"></span>` : "";
  const img = it.track.image ? `<img class="item-art" src="${it.track.image}">` : `<div class="item-art"></div>`;
  return `
    <div class="item${isFav ? ' fav-artist' : ''}">
      ${img}
      <div class="item-meta">
        <div class="t">${escapeHtml(it.track.name)}</div>
        <div class="a">${escapeHtml(it.track.artists)}${favStar}</div>
        <div class="ts">${it.ts} - ${it.listened  -  0}s listened ${tagHTML}</div>
      </div>
    </div>`;
}

function renderTrackTile(t) {
  const img = t.image ? `<img src="${t.image}">` : `<div style="width:36px;height:36px;background:var(--panel);border-radius:4px"></div>`;
  return `<a class="tile" href="${t.url || '#'}" target="_blank">
    ${img}
    <div class="tile-text">
      <div class="tile-row"><div class="tname">${escapeHtml(t.name)}</div></div>
      ${t.artists ? `<div class="tsub">${escapeHtml(t.artists)}</div>` : ""}
    </div>
  </a>`;
}
function renderArtistTile(a) {
  const img = a.image ? `<img src="${a.image}">` : `<div style="width:36px;height:36px;background:var(--panel);border-radius:4px"></div>`;
  const sub = (a.genres && a.genres.length) ? a.genres.join(", ") : "";
  return `<a class="tile" href="${a.url || '#'}" target="_blank">
    ${img}
    <div class="tile-text">
      <div class="tile-row"><div class="tname">${escapeHtml(a.name)}</div></div>
      ${sub ? `<div class="tsub">${escapeHtml(sub)}</div>` : ""}
    </div>
  </a>`;
}

function toggleLyricsFloat(show) {
  const el = document.getElementById('lyrics-float');
  if (!el) return;
  const shouldShow = typeof show === 'boolean' ? show : el.classList.contains('hidden');
  el.classList.toggle('hidden', !shouldShow);
}

function toggleNowDetailDrawer(force) {
  const bar = document.getElementById('player-bar');
  const expandBtn = document.getElementById('bar-expand-btn');
  const expandCaret = document.getElementById('player-bar-handle-caret');
  const main = document.getElementById('app-main');
  const sidebar = document.getElementById('app-sidebar');
  if (!bar) return;
  const shouldOpen = typeof force === 'boolean' ? force : !bar.classList.contains('expanded');
  bar.classList.toggle('expanded', shouldOpen);
  if (expandBtn) expandBtn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
  if (expandCaret) expandCaret.textContent = shouldOpen ? '▼' : '▲';
  if (main) main.classList.toggle('now-detail-open', shouldOpen);
  if (sidebar) sidebar.classList.toggle('detail-open', shouldOpen);
  // Set target offset immediately so sidebars animate in sync with the bar
  const _root = document.documentElement;
  const _targetH = shouldOpen ? Math.min(window.innerHeight * 0.52, 500) : 92;
  _root.style.setProperty('--content-bottom-offset', _targetH + 'px');
  bar.addEventListener('transitionend', syncLayoutGeometry, { once: true });
}

(function initLyricsFloatDrag() {
  window.addEventListener('DOMContentLoaded', () => {
    const el = document.getElementById('lyrics-float');
    const handle = document.getElementById('lyrics-float-header');
    if (!el || !handle) return;
    let dragging = false;
    let startX = 0;
    let startY = 0;
    let originLeft = 0;
    let originTop = 0;
    handle.addEventListener('mousedown', (e) => {
      if (e.target.closest('button')) return;
      const rect = el.getBoundingClientRect();
      dragging = true;
      startX = e.clientX;
      startY = e.clientY;
      originLeft = rect.left;
      originTop = rect.top;
      el.style.left = rect.left + 'px';
      el.style.top = rect.top + 'px';
      el.style.right = 'auto';
      el.style.bottom = 'auto';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    document.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const nextLeft = Math.max(12, Math.min(window.innerWidth - el.offsetWidth - 12, originLeft + (e.clientX - startX)));
      const nextTop = Math.max(72, Math.min(window.innerHeight - el.offsetHeight - 12, originTop + (e.clientY - startY)));
      el.style.left = nextLeft + 'px';
      el.style.top = nextTop + 'px';
    });
    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      document.body.style.userSelect = '';
    });
  });
})();

let lastLyricsTrackId = null;
function renderLyrics(lyrics, progress_ms, offsetMs=0) {
  const card = document.getElementById("lyrics-card");
  // Apply user offset: positive = lyrics later (audio ahead of tool's view)
  progress_ms = Math.max(0, (progress_ms || 0) + (offsetMs || 0));
  if (!lyrics) {
    card.innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading lyrics</div>`;
    lastLyricsTrackId = null;
    return;
  }
  if (!lyrics.found) {
    card.innerHTML = `<div class="empty">No lyrics found on lrclib for this track.</div>`;
    lastLyricsTrackId = null;
    return;
  }
  if (lyrics.synced && lyrics.lines.length) {
    // Find active line
    let activeIdx = -1;
    for (let i = 0; i < lyrics.lines.length; i++) {
      if (lyrics.lines[i].ts_ms <= progress_ms) activeIdx = i;
      else break;
    }
    const html = lyrics.lines.map((l, i) => {
      let cls = "lyr-line";
      if (i === activeIdx) cls += " active";
      else if (i < activeIdx) cls += " past";
      return `<div class="${cls}" data-idx="${i}">${escapeHtml(l.text || "♪")}</div>`;
    }).join("");
    // Only re-render fully if track changed; otherwise just toggle classes
    if (card.dataset.lyrtrack !== lyrics.signature) {
      card.innerHTML = html;
      card.dataset.lyrtrack = lyrics.signature;
    } else {
      const lines = card.querySelectorAll(".lyr-line");
      lines.forEach((el, i) => {
        el.className = "lyr-line" + (i === activeIdx ? " active" : i < activeIdx ? " past" : "");
      });
    }
    // Auto-scroll to active line
    if (activeIdx >= 0) {
      const activeEl = card.querySelector(`.lyr-line[data-idx="${activeIdx}"]`);
      if (activeEl) {
        const cardRect = card.getBoundingClientRect();
        const elRect = activeEl.getBoundingClientRect();
        const offset = elRect.top - cardRect.top - cardRect.height/2 + elRect.height/2;
        if (Math.abs(offset) > 60) card.scrollTop += offset;
      }
    }
  } else if (lyrics.plain) {
    card.innerHTML = `<div class="lyr-plain">${escapeHtml(lyrics.plain)}</div><div class="note">No time-synced lyrics for this one - showing plain.</div>`;
  } else {
    card.innerHTML = `<div class="empty">No lyrics found.</div>`;
  }
}

function fmtNum(n) {
  if (!n && n !== 0) return "-";
  if (n >= 1e9) return (n/1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n/1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n/1e3).toFixed(1) + "K";
  return String(n);
}

function renderTrackTileWithMatch(t, favList) {
  const img = t.image ? `<img src="${t.image}">` : `<div style="width:36px;height:36px;background:var(--panel);border-radius:4px"></div>`;
  const matchPill = (typeof t.match === "number" && t.match > 0)
    ? `<span class="match-pill">${Math.round(t.match * 100)}%</span>` : "";
  const fav = anyArtistFav(t.artists, favList) ? " fav-artist" : "";
  return `<a class="tile${fav}" href="${t.url || '#'}" target="_blank">
    ${img}
    <div class="tile-text">
      <div class="tile-row">
        <div class="tname">${escapeHtml(t.name)}</div>
        ${matchPill}
      </div>
      ${t.artists ? `<div class="tsub">${escapeHtml(t.artists)}</div>` : ""}
    </div>
  </a>`;
}

function renderArtistTileWithMatch(a, favList) {
  const img = a.image ? `<img src="${a.image}">` : `<div style="width:36px;height:36px;background:var(--panel);border-radius:4px"></div>`;
  const sub = (a.genres && a.genres.length) ? a.genres.join(", ") : "";
  const matchPill = (typeof a.match === "number" && a.match > 0)
    ? `<span class="match-pill">${Math.round(a.match * 100)}%</span>` : "";
  const fav = isArtistFav(a.name, favList) ? " fav-artist" : "";
  return `<a class="tile${fav}" href="${a.url || '#'}" target="_blank">
    ${img}
    <div class="tile-text">
      <div class="tile-row">
        <div class="tname">${escapeHtml(a.name)}</div>
        ${matchPill}
      </div>
      ${sub ? `<div class="tsub">${escapeHtml(sub)}</div>` : ""}
    </div>
  </a>`;
}

function renderArtistAbout(info, favList) {
  const card = document.getElementById("artist-about-card");
  const topCard = document.getElementById("artist-now-top-card");
  if (!info) {
    card.innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading artist context</div>`;
    if (topCard) topCard.innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading top tracks</div>`;
    return;
  }
  const sections = [];
  const heroImage = info.artist_image
    || ((info.top_tracks && info.top_tracks[0] && info.top_tracks[0].image) || "")
    || ((info.related && info.related[0] && info.related[0].image) || "");

  // ── Compact hero: small image + name + short bio ──
  const artistName = escapeHtml(info.name || "");
  if (heroImage) {
    sections.push(`<div class="nd-about-hero">
      <img class="nd-about-img" src="${escapeHtml(heroImage)}" alt="">
      <div class="nd-about-name">${artistName}</div>
    </div>`);
  } else {
    sections.push(`<div class="nd-about-name-only">${artistName}</div>`);
  }

  if (info.bio) {
    const bioShort = info.bio.slice(0, 280) + (info.bio.length > 280 ? "..." : "");
    sections.push(`<div class="nd-about-bio">${escapeHtml(bioShort)}</div>`);
  }

  if (info.lastfm_tags && info.lastfm_tags.length) {
    const pills = info.lastfm_tags.slice(0,8).map(t => `<span class="lfm-tag">${escapeHtml(t)}</span>`).join("");
    sections.push(`<div class="lfm-tag-row" style="margin-top:8px">${pills}</div>`);
  }
  if (info.stats) {
    const lis = fmtNum(info.stats.listeners);
    const pl = fmtNum(info.stats.playcount);
    sections.push(`<div class="nd-about-stats">${lis} listeners - ${pl} plays${info.lastfm_url ? ` - <a class="spotify-link" href="${info.lastfm_url}" target="_blank">lfm &#8599;</a>` : ""}</div>`);
  }
  if (info.everynoise_url) {
    sections.push(`<a class="nd-about-everynoise" href="${info.everynoise_url}" target="_blank">&#8599; Every Noise at Once</a>`);
  }

  // ── Compact top tracks list ──
  if (info.top_tracks && info.top_tracks.length) {
    const rows = info.top_tracks.slice(0,3).map(t => {
      const img = t.image ? `<img class="nd-track-img" src="${escapeHtml(t.image)}" alt="">` : `<div class="nd-track-img"></div>`;
      const subBits = [];
      if (t.album) subBits.push(escapeHtml(t.album));
      else if (t.artists) subBits.push(escapeHtml(t.artists));
      return `<div class="nd-track-row">
        ${img}
        <div class="nd-track-meta">
          <span class="nd-track-name">${escapeHtml(t.name)}</span>
          <span class="nd-track-sub">${subBits.join(' - ') || 'Top track'}</span>
        </div>
        <div class="nd-track-side">
          ${t.is_liked ? `<span class="nd-track-like">&#9829;</span>` : ``}
          ${t.duration_ms ? `<span class="nd-track-dur">${fmtTime(t.duration_ms)}</span>` : ``}
          ${t.uri ? `<button class="nd-track-play" type="button" onclick="event.stopPropagation();playTrack('${escapeHtml(t.uri)}')">Play</button>` : ``}
        </div>
      </div>`;
    }).join("");
    sections.push(`<div class="now-detail-section-label" style="margin-top:10px">Top tracks</div><div class="nd-track-list">${rows}</div>`);
  }

  if (info.spotify_url || info.artist_id) {
    const spUrl = info.spotify_url || `https://open.spotify.com/artist/${info.artist_id}`;
    sections.push(`<div style="margin-top:10px"><button class="dashboard-link-btn" onclick="openCurrentArtistPage()">Open full artist view</button></div>`);
  }

  card.innerHTML = sections.length ? sections.join("") : `<div class="empty">No artist info available.</div>`;
}

function renderArtistRecs(info, favList) {
  const card = document.getElementById("artist-recs-card");
  if (!info) { card.innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading artist analytics</div>`; return; }
  const sections = [];
  const ls = info.listen_stats || {};

  // ── Core stats grid ──
  const totalListens = (ls.listen_count || 0) + (ls.imported_listens || 0);
  const likedCount = (Number(ls.liked_count_total) || Number(ls.liked_count) || 0);
  const likeRate = totalListens > 0 ? Math.round(likedCount / totalListens * 100) : null;
  const coveragePct = info.catalog_coverage_pct || 0;
  sections.push(`<div class="nd-stat-grid">
    <div class="nd-stat"><span class="nd-stat-val">${fmtNum(likedCount)}</span><span class="nd-stat-lbl">Liked songs</span></div>
    <div class="nd-stat"><span class="nd-stat-val">${coveragePct}%</span><span class="nd-stat-lbl">Works played</span></div>
    <div class="nd-stat"><span class="nd-stat-val">${fmtNum(ls.revisit_days || 0)}</span><span class="nd-stat-lbl">Return days</span></div>
  </div>`);

  // ── Like rate bar ──
  if (likeRate !== null && totalListens > 5) {
    sections.push(`<div class="nd-like-bar-wrap">
      <div class="nd-like-bar-track"><div class="nd-like-bar-fill" style="width:${Math.min(100,likeRate)}%"></div></div>
      <span class="nd-like-bar-label">${likeRate}% liked - ${likedCount} of ${fmtNum(totalListens)} plays</span>
    </div>`);
  }

  // ── Top albums ──
  if (info.unheard_top_tracks && info.unheard_top_tracks.length) {
    const rows = info.unheard_top_tracks.slice(0,3).map(t => `
      <div class="nd-unheard-row">
        <div class="nd-unheard-meta">
          <div class="nd-unheard-name">${escapeHtml(t.name || '')}</div>
          <div class="nd-unheard-sub">${escapeHtml(t.album || t.artists || 'Unheard suggestion')}${t.duration_ms ? ` - ${fmtTime(t.duration_ms)}` : ''}</div>
        </div>
        ${t.uri ? `<button class="nd-track-play" type="button" onclick="playTrack('${escapeHtml(t.uri)}')">Play</button>` : ``}
      </div>`).join("");
    sections.push(`<div class="now-detail-section-label" style="margin-top:10px">Haven't heard yet</div><div class="nd-unheard-list">${rows}</div>`);
  }

  // ── Related artists ──
  if (info.related && info.related.length) {
    const chips = info.related.slice(0,3).map(a => {
      const img = a.image ? `<img class="nd-related-img" src="${escapeHtml(a.image)}" alt="">` : `<div class="nd-related-img"></div>`;
      const url = a.url ? `href="${escapeHtml(a.url)}" target="_blank"` : '';
      const matchPct = a.match ? ` <span class="nd-related-match">${Math.round(a.match * 100)}%</span>` : '';
      return `<a class="nd-related-chip" ${url}>${img}<span class="nd-related-name">${escapeHtml(a.name)}${matchPct}</span></a>`;
    }).join("");
    sections.push(`<div class="now-detail-section-label" style="margin-top:10px">Similar artists</div><div class="nd-related-list">${chips}</div>`);
  }

  // ── Taste matches (similar tracks the user has liked) ──
  const tasteTracks = (info.matches_taste || []).filter(t => t.name);
  if (tasteTracks.length) {
    const rows = tasteTracks.slice(0,3).map(t => {
      const img = t.image ? `<img class="nd-track-img" src="${escapeHtml(t.image)}" alt="">` : `<div class="nd-track-img"></div>`;
      const url = t.url ? `href="${escapeHtml(t.url)}" target="_blank"` : '';
      const art = t.artists ? `<span class="nd-track-sub">${escapeHtml(t.artists)}</span>` : '';
      return `<a class="nd-track-row" ${url}>${img}<div><span class="nd-track-name">${escapeHtml(t.name)}</span>${art}</div></a>`;
    }).join("");
    sections.push(`<div class="now-detail-section-label" style="margin-top:10px">Tracks you might know</div><div class="nd-track-list">${rows}</div>`);
  }

  card.innerHTML = sections.length ? sections.join("") : `<div class="empty">Artist analytics are warming up.</div>`;
}

function renderArtistAboutV2(info, favList) {
  const card = document.getElementById("artist-about-card");
  if (!info) {
    card.innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading artist context</div>`;
    return;
  }
  const ls       = info.listen_stats || {};
  const artistName  = escapeHtml(info.name || "");
  const heroImage   = info.artist_image || ((info.top_tracks || []).find(t => t.image)?.image || "") || ((window._lastTrack || {}).image || "");

  function fmtDate(ts) {
    if (!ts) return "-";
    const d = new Date(ts);
    return d.toLocaleDateString(undefined, { month: "short", year: "numeric" });
  }

  // Header row: avatar + name
  const avatar = heroImage
    ? `<img class="nd-artist-avatar" src="${escapeHtml(heroImage)}" alt="">`
    : `<div class="nd-artist-avatar" style="background:var(--panel-2)"></div>`;
  const subParts = [];
  if (info.stats) subParts.push(`${fmtNum(info.stats.listeners)} lfm listeners`);
  if (info.everynoise_url) subParts.push(`<a class="nd-about-everynoise" href="${escapeHtml(info.everynoise_url)}" target="_blank" style="display:inline">Every Noise &#8599;</a>`);
  const header = `<div class="nd-artist-header">
    ${avatar}
    <div class="nd-artist-title">
      <div class="nd-artist-name">${artistName}</div>
      ${subParts.length ? `<div class="nd-artist-sub">${subParts.join(" - ")}</div>` : ""}
    </div>
    ${(info.spotify_url || info.artist_id) ? `<button class="dashboard-link-btn" style="flex-shrink:0;font-size:10px;padding:4px 8px" onclick="openCurrentArtistPage()">Full view</button>` : ""}
  </div>`;

  const firstHeard = fmtDate(ls.first_heard_at);
  const lastHeard  = fmtDate(ls.last_heard_at);
  const statsHtml = `<div class="nd-date-row">
    First heard <strong>${firstHeard}</strong> - Last heard <strong>${lastHeard}</strong>
  </div>`;

  // Bio
  let bioHtml = "";
  if (info.bio) {
    const bioShort = info.bio.slice(0, 220) + (info.bio.length > 220 ? "..." : "");
    bioHtml = `<div class="nd-about-bio">${escapeHtml(bioShort)}</div>`;
  }

  // Genre tags
  let tagsHtml = "";
  if (info.lastfm_tags && info.lastfm_tags.length) {
    tagsHtml = `<div class="lfm-tag-row" style="margin-top:6px;flex-shrink:0">${info.lastfm_tags.slice(0,6).map(t => `<span class="lfm-tag">${escapeHtml(t)}</span>`).join("")}</div>`;
  }

  const tracks = (info.top_tracks || []).slice(0, 6);
  const trackRows = tracks.length ? tracks.map(t => {
    const imgSrc = t.image || "";
    const img = imgSrc
      ? `<img class="nd-track-img-sm" src="${escapeHtml(imgSrc)}" alt="">`
      : `<div class="nd-track-img-sm"></div>`;
    const playAction = t.uri
      ? `playTrack(${JSON.stringify(t.uri)})`
      : `playTrackByMeta(${JSON.stringify(t.name||'')},${JSON.stringify(t.artists||info.name||'')},${JSON.stringify(t.url||'')})`;
    const subBits = [];
    if (t.album) subBits.push(escapeHtml(t.album));
    if (t.duration_ms) subBits.push(fmtTime(t.duration_ms));
    if (t.is_liked) subBits.push('Liked');
    return `<div class="nd-track-row-sm" role="button" tabindex="0" onclick='${playAction}' onkeydown='if(event.key==="Enter"||event.key===" "){event.preventDefault();${playAction}}'>
      <button class="nd-track-play-sm" type="button" onclick='event.stopPropagation();${playAction}' title="Play">&#9654;</button>
      ${img}
      <span class="nd-track-meta-sm">
        <span class="nd-track-name-sm">${escapeHtml(t.name||'')}</span>
        <span class="nd-track-sub-sm">${subBits.join(' - ') || escapeHtml(t.artists || info.name || 'Top track')}</span>
      </span>
    </div>`;
  }).join("") : `<div class="empty" style="font-size:11px;padding:8px 0">No tracks yet</div>`;

  const related = (info.related || []).slice(0, 6);
  const simRows = related.length ? related.map(a => {
    const imgSrc = a.image || "";
    const img = imgSrc
      ? `<img class="nd-sim-img" src="${escapeHtml(imgSrc)}" alt="">`
      : `<div class="nd-sim-img"></div>`;
    const matchPct = a.match ? `<span class="nd-sim-match">${Math.round(a.match*100)}%</span>` : '';
    const clickAct = `openArtistPanel(${JSON.stringify(a.id||'')},${JSON.stringify(a.name||'')})`;
    return `<button class="nd-sim-row" type="button" onclick='${clickAct}'>
      ${img}
      <span class="nd-sim-name">${escapeHtml(a.name||'')}</span>
      ${matchPct}
    </button>`;
  }).join("") : `<div class="empty" style="font-size:11px;padding:8px 0">No similar artists</div>`;

  card.innerHTML = `<div class="nd-about-stack">
    ${header}
    ${statsHtml}
    ${bioHtml}
    ${tagsHtml}
    <div class="nd-about-sections">
      <div class="nd-two-col">
        <div class="nd-col">
          <div class="nd-col-label">Top Tracks</div>
          <div class="nd-col-scroll">${trackRows}</div>
        </div>
        <div class="nd-col">
          <div class="nd-col-label">Similar Artists</div>
          <div class="nd-col-scroll">${simRows}</div>
        </div>
      </div>
    </div>
  </div>`;
}

function renderArtistRecsV2(info, favList) {
  const card = document.getElementById("artist-recs-card");
  if (!info) { card.innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading</div>`; return; }

  const num = (v, fallback=0) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  };
  const ls = info.listen_stats || {};
  const totalPlays = num(ls.listen_count) + num(ls.imported_listens);
  const likedCount = num(ls.liked_count_total, num(ls.liked_count));
  const catalogTracksTotal = num(info.catalog_tracks_total, 0);
  const catalogTracksHeard = num(info.catalog_tracks_heard, 0);
  const coveragePctRaw = Number(info.catalog_coverage_pct);
  const coveragePct = Number.isFinite(coveragePctRaw)
    ? coveragePctRaw
    : (catalogTracksTotal > 0 ? Math.round((catalogTracksHeard / catalogTracksTotal) * 100) : 0);
  const unheardCount = catalogTracksTotal > 0
    ? Math.max(0, catalogTracksTotal - catalogTracksHeard)
    : null;
  const coverageText = catalogTracksTotal > 0
    ? `${fmtNum(catalogTracksHeard)} of ${fmtNum(catalogTracksTotal)} mapped tracks heard at least once`
    : `Catalog coverage appears after discography loads.`;
  const statsHtml = `<div class="nd-stat-grid">
    <div class="nd-stat"><span class="nd-stat-val">${fmtNum(totalPlays)}</span><span class="nd-stat-lbl">Plays</span></div>
    <div class="nd-stat"><span class="nd-stat-val">${unheardCount !== null ? fmtNum(unheardCount) : "-"}</span><span class="nd-stat-lbl">Unheard tracks</span></div>
    <div class="nd-stat"><span class="nd-stat-val">${fmtNum(likedCount)}</span><span class="nd-stat-lbl">Liked songs</span></div>
  </div>
  <div class="nd-like-bar-wrap">
    <div class="nd-like-bar-track"><div class="nd-like-bar-fill" style="width:${Math.max(0, Math.min(100, coveragePct))}%"></div></div>
    <span class="nd-like-bar-label">${coveragePct}% catalog completion - ${escapeHtml(coverageText)}</span>
  </div>`;

  card.innerHTML = `${statsHtml}`;
}
async function favoriteAction(name, action='remove') {
  try {
    const r = await fetch('/api/favorites', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({artist: name, action})
    });
    const d = await r.json();
    if (d.error) {
      showToast(d.error, true);
      return;
    }
    showToast(d.message || 'Updated favorites.');
    refresh();
    loadFavoritesOverview(true);
  } catch(e) {
    showToast('Favorite update failed.', true);
  }
}

async function loadFavoritesOverview(force=false) {
  if (window._favoritesOverviewInFlight) return;
  if (!force && window._favoritesOverviewRetryUntilMs && window._favoritesOverviewRetryUntilMs > Date.now()) return;
  const summaryCard = document.getElementById('favorites-summary-card');
  const releasePaneCard = document.getElementById('favorites-release-pane-card');
  const dashboardCard = document.getElementById('favorites-release-card');
  const spinner = '<div class="spinner-wrap"><span class="spinner"></span>Loading favorites</div>';
  if (force) {
    if (summaryCard) summaryCard.innerHTML = spinner;
    if (releasePaneCard) releasePaneCard.innerHTML = spinner;
    if (dashboardCard) dashboardCard.innerHTML = spinner;
  }
  try {
    window._favoritesOverviewInFlight = true;
    const r = await fetch('/api/favorites/overview' + (force ? '?force=1' : ''));
    const d = await r.json();
    if (!r.ok || d.error) {
      // Guard/503: silently retry after cooldown, don't show error
      if (r.status === 503 || d.error === "guard") {
        const retryAfter = (d.retry_after || 15) * 1000;
        window._favoritesOverviewRetryUntilMs = Date.now() + retryAfter;
        if (window._favoritesOverviewRetryTimer) clearTimeout(window._favoritesOverviewRetryTimer);
        window._favoritesOverviewRetryTimer = setTimeout(() => {
          window._favoritesOverviewRetryTimer = null;
          window._favoritesOverviewRetryUntilMs = 0;
          loadFavoritesOverview(force);
        }, retryAfter);
        return;
      }
      const msg = escapeHtml(d.error || "Could not load favorites");
      if (summaryCard) summaryCard.innerHTML = `<div class="empty">${msg}</div>`;
      if (releasePaneCard) releasePaneCard.innerHTML = `<div class="empty">${msg}</div>`;
      if (dashboardCard) dashboardCard.innerHTML = `<div class="empty">${msg}</div>`;
      return;
    }
    window._favoritesOverviewRetryUntilMs = 0;
    if (window._favoritesOverviewRetryTimer) {
      clearTimeout(window._favoritesOverviewRetryTimer);
      window._favoritesOverviewRetryTimer = null;
    }
    renderFavoritesOverview(d);
  } catch(e) {
    if (summaryCard) summaryCard.innerHTML = '<div class="empty">Could not load favorites.</div>';
    if (releasePaneCard) releasePaneCard.innerHTML = '<div class="empty">Could not load release feed.</div>';
    if (dashboardCard) dashboardCard.innerHTML = '<div class="empty">Could not load favorites.</div>';
  } finally {
    window._favoritesOverviewInFlight = false;
  }
}

function renderFavoritesOverview(data) {
  const summaryCard = document.getElementById('favorites-summary-card');
  const releasePaneCard = document.getElementById('favorites-release-pane-card');
  const dashboardCard = document.getElementById('favorites-release-card');
  const favorites = data.favorites || [];
  const optOuts = data.opt_outs || [];
  const releases = data.release_feed || [];

  if (summaryCard) {
    summaryCard.innerHTML = `
      <div class="workspace-pane-title">Favorite artists <span style="color:var(--muted);font-size:12px;font-weight:500">(${favorites.length})</span></div>
      <div class="workspace-pane-sub" style="margin-bottom:12px">Artists can be auto-favorited from your liked songs threshold; removing one here also opts it out of future auto-picks.</div>
      ${favorites.length ? `<div class="favorite-chip-row">
        ${favorites.map(item => `<span class="favorite-chip">
          <span>${escapeHtml(item.display_name || item.name)}${item.liked_count ? ` - ${fmtNum(item.liked_count)} liked` : ''}</span>
          <button onclick='favoriteAction(${JSON.stringify(item.display_name || item.name)},"remove")' title="Unfavorite and opt out">remove</button>
        </span>`).join('')}
      </div>` : `<div class="empty">No favorite artists yet.</div>`}
      ${optOuts.length ? `<div style="margin-top:14px">
        <div class="dashboard-eyebrow" style="margin-bottom:8px">Opted out of auto-favorite</div>
        <div class="favorite-chip-row">
          ${optOuts.map(item => `<span class="favorite-chip">
            <span>${escapeHtml(item.display_name || item.name)}</span>
            <button onclick='favoriteAction(${JSON.stringify(item.display_name || item.name)},"add")' title="Allow favorites again">restore</button>
          </span>`).join('')}
        </div>
      </div>` : ''}
    `;
  }

  const releaseHtml = releases.length
    ? `<div class="favorite-release-list">
        ${releases.map(item => `
          <div class="favorite-release-row">
            ${item.image ? `<img class="favorite-release-art" src="${escapeHtml(item.image)}" alt="">` : `<div class="favorite-release-art"></div>`}
            <div>
              <div class="favorite-release-title">${escapeHtml(item.album_name)}</div>
              <div class="favorite-release-sub">${escapeHtml(item.artist_name)} - ${escapeHtml(item.album_type || 'release')}</div>
            </div>
            <div class="favorite-release-date">${escapeHtml(item.release_date || 'date unknown')}</div>
          </div>`).join('')}
      </div>`
    : `<div class="empty">${escapeHtml(data.release_error || 'No favorite-artist releases ready yet.')}</div>`;
  const releasePreviewHtml = releases.length
    ? `<div class="favorite-release-list">
        ${releases.slice(0, 10).map(item => `
          <div class="favorite-release-row">
            ${item.image ? `<img class="favorite-release-art" src="${escapeHtml(item.image)}" alt="">` : `<div class="favorite-release-art"></div>`}
            <div>
              <div class="favorite-release-title">${escapeHtml(item.album_name)}</div>
              <div class="favorite-release-sub">${escapeHtml(item.artist_name)} - ${escapeHtml(item.album_type || 'release')}</div>
            </div>
            <div class="favorite-release-date">${escapeHtml(item.release_date || 'date unknown')}</div>
          </div>`).join('')}
      </div>`
    : releaseHtml;

  if (releasePaneCard) {
    releasePaneCard.innerHTML = `
      <div class="workspace-pane-title">Favorite release radar</div>
      <div class="workspace-pane-sub" style="margin-bottom:12px">Recent releases from artists you already trust, shown newest first.</div>
      ${releaseHtml}
    `;
  }
  if (dashboardCard) {
    dashboardCard.innerHTML = `
      <div class="workspace-pane-sub" style="margin-bottom:10px">Recent releases from your favorites.</div>
      ${releasePreviewHtml}
    `;
  }
}

let refreshInFlight = false;
let _favoritesOverviewPrimed = false;
async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const r = await fetch("/api/status");
    const d = await r.json();

    if (!d.auth_ready) {
      document.getElementById("login-section").style.display = "block";
      document.getElementById("app-section").style.display = "none";
      analyticsLoadedAt = 0;
      return;
    }
    document.getElementById("login-section").style.display = "none";
    document.getElementById("app-section").style.display = "block";
    // Update profile bar username
    if (d.user_display_name) {
      const nameEl = document.getElementById("profile-name");
      if (nameEl && nameEl.textContent !== d.user_display_name) nameEl.textContent = d.user_display_name;
    }
    // Auto-load queue on first auth success
    initQueue();

    const errEl = document.getElementById("error");
    if (d.error) { errEl.textContent = d.error; errEl.style.display = "block"; }
    else { errEl.style.display = "none"; }
    _spotifySearchBackoffUntilMs = d.spotify_search_backoff_until_ms || 0;
    _spotifySearchRetryAfterS = d.spotify_search_retry_after_s ?? null;
    _spotifyGuardProtectUntilMs = d.spotify_guard_protect_until_ms || 0;
    _spotifyGuardProtectReason = d.spotify_guard_protect_reason || null;
    _spotifyGuardGlobalUntilMs = d.spotify_guard_backoff_until_ms || 0;
    _spotifyGuardRestoredUntilMs = d.spotify_guard_restored_until_ms || 0;
    _spotifyGuardRestoredReason = d.spotify_guard_restored_reason || null;
    _spotifyGuardRestoredRetryAfterS = d.spotify_guard_restored_retry_after_s ?? null;
    _spotifyGuardRestoredPending = !!d.spotify_guard_restored_pending;
    _spotifyGuardRevalidating = !!d.spotify_guard_revalidating;
    renderSpotifySearchCooldown();

    document.getElementById("pl-name").textContent = d.playlist_name || "-";
    document.getElementById("pl-count").textContent = Number.isFinite(Number(d.playlist_total)) ? fmtNum(Number(d.playlist_total)) : "-";
    // Sync sidebar
    const sbarName = document.getElementById("sidebar-pl-name");
    const sbarCount = document.getElementById("sidebar-pl-count");
    if (sbarName) sbarName.textContent = d.playlist_name || "-";
    if (sbarCount) sbarCount.textContent = d.playlist_total != null ? `${d.playlist_total} tracks` : "";
    const sbarDur = document.getElementById("sidebar-pl-duration");
    if (sbarDur && d.playlist_duration_ms) {
      const totalMin = Math.round(d.playlist_duration_ms / 60000);
      const hrs = Math.floor(totalMin / 60);
      const mins = totalMin % 60;
      sbarDur.textContent = hrs > 0 ? `- ${hrs} hr ${mins} min` : `- ${mins} min`;
    }

    if (!thresholdEl || !thresholdValEl) initThresholdControls();
    if (thresholdEl && thresholdValEl && !userInteractingThreshold) {
      thresholdEl.value = d.threshold_seconds;
      thresholdValEl.textContent = d.threshold_seconds;
    }
    document.getElementById("threshold-info").textContent = `/ ${d.threshold_seconds}s`;

    if (!userInteractingLyrOffset) {
      const offSecs = (d.lyrics_offset_ms || 0) / 1000;
      lyrOffsetEl.value = offSecs;
      lyrOffsetValEl.textContent = fmtOffset(offSecs);
      const inlineEl = document.getElementById("lyr-offset-inline");
      const inlineVal = document.getElementById("lyr-offset-val-inline");
      if (inlineEl) inlineEl.value = offSecs;
      if (inlineVal) inlineVal.textContent = fmtOffset(offSecs);
    }

    const nowTitle = document.getElementById("now-title");
    const nowArtist = document.getElementById("now-artist");
    const nowArtistText = document.getElementById("now-artist-text");
    const nowArt = document.getElementById("now-art");
    const badges = document.getElementById("now-badges");
    const genresEl = document.getElementById("now-genres");
    const btnLike = document.getElementById("btn-like");
    const btnSkip = document.getElementById("btn-skip");

    if (d.current_track) {
      const prevTrackId = window._currentTrackId;
      window._currentTrackId = d.current_track.id || null;
      const trackChanged = window._currentTrackId !== prevTrackId;
      if (queueLoaded && trackChanged) renderQueue();
      nowTitle.textContent = d.current_track.name;
      nowArtistText.textContent = d.current_track.artists;
      nowArtist.classList.add("artist-link");
      nowArtist.onclick = () => {
        const name = (d.current_track.artists || "").split(",")[0].trim();
        const primaryArtist = (d.current_track.artist_list && d.current_track.artist_list[0]) || {};
        openArtistPanel(primaryArtist.id || "", name, "");
      };
      nowArt.src = d.current_track.image || "";
      nowArt.style.visibility = d.current_track.image ? "visible" : "hidden";

      // Keep play/pause button icon in sync
      const ppBtn = document.getElementById("btn-play-pause");
      if (ppBtn) ppBtn.innerHTML = d.is_playing ? '<span class="player-btn-glyph">&#10074;&#10074;</span>' : '<span class="player-btn-glyph">&#9654;</span>';
      if (ppBtn) ppBtn.textContent = d.is_playing ? "||" : ">";

      if (ppBtn) ppBtn.innerHTML = d.is_playing ? '<span class="player-btn-glyph">&#10074;&#10074;</span>' : '<span class="player-btn-glyph">&#9654;</span>';
      const b = [];
      if (d.is_playing) b.push(`<span class="badge">&#9654; playing</span>`);
      else b.push(`<span class="badge paused">&#9208; paused</span>`);
      if (d.current_in_playlist) b.push(`<span class="badge in-playlist">&#10003; in target playlist</span>`);
      else b.push(`<span class="badge not-in-playlist">not in target playlist</span>`);
      if (d.current_is_liked) b.push(`<span class="badge liked">&#9829; liked</span>`);
      if (d.current_track.url) b.push(`<a class="spotify-link" href="${d.current_track.url}" target="_blank">open &#8599;</a>`);
      badges.innerHTML = b.join(" ");

      const genres = d.current_track.genres || [];
      if (genres.length && d.current_track.id) {
        window._trackGenreCache = window._trackGenreCache || {};
        const _hadGenres = (window._trackGenreCache[d.current_track.id] || []).length > 0;
        window._trackGenreCache[d.current_track.id] = genres;
        if (!_hadGenres && queueLoaded) setTimeout(renderQueue, 60);
      }
      if (genres.length) {
        genresEl.innerHTML = genres.slice(0, 8).map(g => `<span class="genre">${escapeHtml(g)}</span>`).join("");
      } else if (window._currentTrackId && d.progress_ms < 4000) {
        genresEl.innerHTML = `<span class="loading-pulse">loading...</span>`;
      } else {
        genresEl.innerHTML = "";
      }

      syncPlayerBarMarquees();
      btnLike.disabled = false;
      if (btnSkip) btnSkip.disabled = false;
      document.getElementById("btn-add-pl-toggle").disabled = false;
      btnLike.classList.toggle("is-liked", !!d.current_is_liked);
      btnLike.setAttribute("aria-pressed", d.current_is_liked ? "true" : "false");
      btnLike.title = d.current_is_liked ? "Unlike this track" : "Like this track";

      // Favorite-star button on the now-playing artist
      const resBtn = document.getElementById("btn-research");
      if (resBtn) resBtn.disabled = false;
      const favBtn = document.getElementById("btn-fav");
      const primaryArtist = (d.current_track.artists || "").split(",")[0].trim();
      const isFav = isArtistFav(primaryArtist, d.favorite_artists);
      favBtn.disabled = false;
      favBtn.classList.toggle("is-fav", isFav);
      favBtn.innerHTML = isFav ? "&#9733;" : "&#9734;";
      favBtn.setAttribute("aria-pressed", isFav ? "true" : "false");
      favBtn.dataset.artist = primaryArtist;
      favBtn.title = isFav ? "Unfavorite this artist" : "Mark as favorite artist";

      // Lyrics + artist info (split into two cards)
      const lyr = d.current_lyrics;
      if (lyr) lyr.signature = d.current_track.id;
      renderLyrics(lyr, d.progress_ms, d.lyrics_offset_ms || 0);
      window._lastFavArtists = d.favorite_artists;
      window._lastTrack = d.current_track;
      const artistList = d.current_track.artist_list || [];
      if (trackChanged) {
        clearPickedArtistSelection();
      }
      const preservePickedArtist = shouldPreservePickedArtist(window._currentTrackId, artistList);
      renderArtistPicker(artistList);
      if (!preservePickedArtist) {
        renderArtistAboutV2(d.current_artist_info, d.favorite_artists);
        renderArtistRecsV2(d.current_artist_info, d.favorite_artists);
      }
    } else {
      clearPickedArtistSelection();
      nowTitle.textContent = "Nothing playing";
      nowArtistText.textContent = "Start a song on Spotify";
      nowArt.style.visibility = "hidden";
      badges.innerHTML = ""; genresEl.innerHTML = "";
      syncPlayerBarMarquees();
      btnLike.disabled = true;
      if (btnSkip) btnSkip.disabled = true;
      document.getElementById("btn-add-pl-toggle").disabled = true;
      document.getElementById("btn-fav").disabled = true;
      document.getElementById("btn-fav").innerHTML = "&#9734;";
      document.getElementById("btn-fav").setAttribute("aria-pressed", "false");
      btnLike.setAttribute("aria-pressed", "false");
      document.getElementById("lyrics-card").innerHTML = `<div class="empty">—</div>`;
      document.getElementById("artist-about-card").innerHTML = `<div class="empty">—</div>`;
      document.getElementById("artist-recs-card").innerHTML = `<div class="empty">—</div>`;
      document.getElementById("artist-now-top-card").innerHTML = `<div class="empty">—</div>`;
    }

    // Sidebar cards
    renderStats(d);
    renderWeekly(d.weekly_top, d.favorite_artists);
    loadAnalytics();

    const pct = d.duration_ms ? Math.min(100, (d.progress_ms / d.duration_ms) * 100) : 0;
    if (!window._seekSuppressUntil || Date.now() > window._seekSuppressUntil) {
      document.getElementById("track-progress").style.width = pct + "%";
      document.getElementById("track-pos").textContent = fmtTime(d.progress_ms);
    }
    document.getElementById("track-dur").textContent = fmtTime(d.duration_ms);
    // Store for seek handler
    window._currentDurationMs = d.duration_ms || 0;
    // Threshold marker on progress bar
    const thrMarker = document.getElementById("bar-threshold-marker");
    if (thrMarker && d.duration_ms && d.threshold_seconds) {
      const thrPct = Math.min(99, (d.threshold_seconds * 1000 / d.duration_ms) * 100);
      thrMarker.style.left = thrPct + "%";
      thrMarker.style.display = "";
    } else if (thrMarker) { thrMarker.style.display = "none"; }
    // Listened indicator (threshold-progress hidden via CSS, listened-sec still updated)
    document.getElementById("listened-sec").textContent = `${d.current_listened.toFixed(1)}s`;

    // keep the history tab on its selected feed; don't let the heartbeat
    // overwrite Spotify recently-played with the app's own timeline render.
    if (typeof renderSidebarHistory === "function" && (_sidebarTab !== "history" || _historyFeed !== "spotify")) {
      renderSidebarHistory(d.timeline, d.recently_removed);
    }
    // compat: removed-list (hidden)
    const removedEl = document.getElementById("removed-list");
    if (removedEl) removedEl.innerHTML = "";

    document.getElementById("last-poll").textContent = d.last_poll || "—";
    window._latestStatus = d;
    renderDashboardSurface();
  } catch (e) { console.error(e); }
  finally { refreshInFlight = false; }
}

// --- Favorites helpers + UI handlers ---
function isArtistFav(name, favList) {
  if (!name || !favList) return false;
  const k = name.trim().toLowerCase();
  return favList.includes(k);
}

function anyArtistFav(artistsCsv, favList) {
  if (!artistsCsv || !favList || !favList.length) return false;
  return artistsCsv.split(",").some(a => isArtistFav(a, favList));
}

bindClick("btn-fav", async () => {
  const btn = document.getElementById("btn-fav");
  if (!btn) return;
  const artist = btn.dataset.artist;
  if (!artist) return;
  btn.disabled = true;
  try {
    const r = await fetch("/api/favorites", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ action: "toggle", artist }),
    });
    const d = await r.json();
    if (d.message) showToast(d.message);
  } catch (e) { showToast("Favorite toggle failed", true); }
  btn.disabled = false;
  refresh();
});

// --- Settings panel ---
let settingsLoaded = false;
let settingsOriginal = {};   // key -> original value, used to detect dirty fields

const SECTION_TITLES = {
  spotify: "Spotify",
  lastfm: "Last.fm",
  polling: "Polling & threshold",
  playlist: "Playlist",
  behavior: "Behavior",
};

async function loadSettings() {
  const form = document.getElementById("settings-form");
  if (!form) return;
  form.innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading settings</div>`;
  try {
    const r = await fetch("/api/settings");
    const d = await r.json();
    if (d.error) { form.innerHTML = `<div class="empty">Error: ${escapeHtml(d.error)}</div>`; return; }
    settingsOriginal = {};
    const groups = {};
    for (const s of d.settings) {
      settingsOriginal[s.key] = s.value || "";
      (groups[s.section] = groups[s.section] || []).push(s);
    }
    const SECTION_NOTES = {
      spotify: "Saving changes here clears your cached login and signs you out — you'll need to log in again. The Redirect URL is read-only because it must match your Spotify Dashboard exactly.",
    };
    let html = "";
    for (const section of ["spotify", "lastfm", "polling", "playlist", "behavior"]) {
      const items = groups[section];
      if (!items) continue;
      html += `<div class="settings-section">`;
      html += `<div class="settings-section-title">${escapeHtml(SECTION_TITLES[section] || section)}</div>`;
      if (SECTION_NOTES[section]) {
        html += `<div class="settings-section-note">${escapeHtml(SECTION_NOTES[section])}</div>`;
      }
      for (const it of items) {
        html += renderSettingField(it);
      }
      html += `</div>`;
    }
    form.innerHTML = html;
    // Wire up dirty tracking and show/hide buttons
    form.querySelectorAll(".setting-input").forEach(el => {
      if (el.dataset.readonly === "1") return;
      el.addEventListener("input", () => {
        const key = el.dataset.key;
        const orig = settingsOriginal[key] || "";
        let cur;
        if (el.type === "checkbox") {
          cur = el.checked ? "1" : "0";
          // Compare to original interpreted as bool
          const origBool = ["1","true","yes","on"].includes((orig || "").toLowerCase());
          el.closest(".setting-row").classList.toggle("dirty", el.checked !== origBool);
        } else {
          cur = el.value;
          el.closest(".setting-row").classList.toggle("dirty", cur !== orig);
        }
      });
    });
    form.querySelectorAll(".setting-show").forEach(btn => {
      btn.addEventListener("click", () => {
        const input = btn.previousElementSibling;
        if (!input) return;
        if (input.type === "password") { input.type = "text"; btn.textContent = "hide"; }
        else { input.type = "password"; btn.textContent = "show"; }
      });
    });
    settingsLoaded = true;
  } catch (e) {
    form.innerHTML = `<div class="empty">Failed to load: ${escapeHtml(e.message)}</div>`;
  }
}

function renderSettingField(it) {
  const safeKey = escapeHtml(it.key);
  const safeLabel = escapeHtml(it.label);
  const hintText = it.readonly ? `${it.hint ? it.hint + " " : ""}Read-only here.` : it.hint;
  const safeHint = hintText ? `<div class="setting-hint">${escapeHtml(hintText)}</div>` : "";
  if (it.type === "bool") {
    const checked = ["1","true","yes","on"].includes((it.value || "").toLowerCase());
    return `<div class="setting-row bool">
      <input type="checkbox" class="setting-input" id="set-${safeKey}" data-key="${safeKey}" data-readonly="${it.readonly ? "1" : "0"}" ${checked ? "checked" : ""} ${it.readonly ? "disabled" : ""}>
      <label for="set-${safeKey}">${safeLabel}${safeHint}</label>
    </div>`;
  }
  const inputType = it.type === "number" ? "number" : (it.secret ? "password" : "text");
  const valAttr = `value="${escapeHtml(it.value || "")}"`;
  const inputHtml = `<input id="set-${safeKey}" type="${inputType}" class="setting-input" data-key="${safeKey}" data-readonly="${it.readonly ? "1" : "0"}" ${valAttr} autocomplete="off" spellcheck="false" ${it.readonly ? "readonly" : ""}>`;
  const wrapper = it.secret
    ? `<div class="setting-secret-row">${inputHtml}<button type="button" class="setting-show">show</button></div>`
    : inputHtml;
  return `<div class="setting-row">
    <label for="set-${safeKey}">${safeLabel}</label>
    ${wrapper}
    ${safeHint}
  </div>`;
}

bindClick("settings-toggle", () => {
  openSettings();
  const card = document.getElementById("settings-card");
  const toggle = document.getElementById("settings-toggle");
  if (!card || !toggle) return;
  const isHidden = card.style.display === "none";
  card.style.display = isHidden ? "block" : "none";
  toggle.textContent = isHidden ? "(hide)" : "(show)";
  if (isHidden && !settingsLoaded) loadSettings();
});

// --- Stats / Weekly render functions ---
function fmtDuration(ms) {
  if (!ms || ms < 0) return "0m";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function renderStats(d) {
  updateStatsStrip(d);
  const stats = d.stats || {};
  const card = document.getElementById("stats-card");
  const sessionMs = Date.now() - (d.session_started_ms || Date.now());
  const totalActionsToday = (Number.isFinite(Number(stats.removed_today)) ? fmtNum(Number(stats.removed_today)) : "-") + (Number.isFinite(Number(stats.liked_today)) ? fmtNum(Number(stats.liked_today)) : "-") + (stats.added_today  -  0);
  const removedWeek = d.removed_last_7  -  0;
  const liked30d = d.liked_30d  -  0;
  const tiles = [
    `<div class="stat-tile accent">
       <div class="stat-label">Removed today</div>
       <div class="stat-value">${Number.isFinite(Number(stats.removed_today)) ? fmtNum(Number(stats.removed_today)) : "-"}</div>
       <div class="stat-sub">${removedWeek} this week</div>
     </div>`,
    `<div class="stat-tile like-color">
       <div class="stat-label">Liked this month</div>
       <div class="stat-value">${liked30d}</div>
       <div class="stat-sub">${stats.liked_total  -  0} all-time</div>
     </div>`,
    `<div class="stat-tile warn">
       <div class="stat-label">Scrobbles today</div>
       <div class="stat-value">${d.scrobbles_today != null ? d.scrobbles_today : "—"}</div>
       <div class="stat-sub">via Last.fm</div>
     </div>`,
    `<div class="stat-tile">
       <div class="stat-label">Session time</div>
       <div class="stat-value">${fmtDuration(sessionMs)}</div>
       <div class="stat-sub">${totalActionsToday} action${totalActionsToday === 1 ? "" : "s"} today</div>
     </div>`,
    `<div class="stat-tile fav-tile">
       <div class="stat-label">Favorite artists</div>
       <div class="stat-value">${(d.favorite_artists || []).length}</div>
       <div class="stat-sub">★ tap on now-playing to add</div>
     </div>`,
  ];
  card.innerHTML = `<div class="stat-grid">${tiles.join("")}</div>`;
}

function renderTrackTilePlain(t, favList) {
  const plays = Number(t.playcount || 0);
  const img = t.image
    ? `<img class="weekly-art" src="${t.image}" alt="">`
    : `<div class="weekly-art-placeholder"></div>`;
  const fav = anyArtistFav(t.artists, favList) ? " fav-artist" : "";
  const sourcePill = t.url && t.lastfm_url && t.url !== t.lastfm_url
    ? `<span class="weekly-source">Spotify</span>`
    : `<span class="weekly-source">Last.fm</span>`;
  const safeHref = t.url || t.lastfm_url || "#";
  return { plays, img, fav, sourcePill, safeHref };
}

function renderWeekly(weekly, favList) {
  const ids = ["weekly-card", "dashboard-weekly-card"];
  const cards = ids.map(id => document.getElementById(id)).filter(Boolean);
  if (!cards.length) return;
  if (!weekly || !weekly.length) {
    const html = `<div class="empty">No data yet - needs both LASTFM_API_KEY and LASTFM_USERNAME in .env.</div>`;
    cards.forEach(card => { card.innerHTML = html; });
    return;
  }
  const maxPlays = weekly.reduce((m, t) => Math.max(m, Number(t.playcount || 0)), 0) || 1;
  const totalPlays = weekly.reduce((sum, t) => sum + Number(t.playcount || 0), 0);
  const artistCount = new Set(
    weekly
      .map(t => (t.artists || "").trim())
      .filter(Boolean)
      .map(a => a.toLowerCase())
  ).size;
  const favoriteCount = weekly.filter(t => anyArtistFav(t.artists, favList)).length;
  const rows = weekly.slice(0, 5).map((t, idx) => {
    const parts = renderTrackTilePlain(t, favList);
    const widthPct = Math.max(8, Math.round((parts.plays / maxPlays) * 100));
    return `<a class="weekly-row${parts.fav}" href="${parts.safeHref}" target="_blank">
      <div class="weekly-rank">${idx + 1}</div>
      ${parts.img}
      <div class="weekly-meta">
        <div class="weekly-name-row">
          <div class="weekly-name">${escapeHtml(t.name || "")}</div>
          ${parts.sourcePill}
        </div>
        <div class="weekly-artist">${escapeHtml(t.artists || "")}</div>
        <div class="weekly-bar"><div class="weekly-bar-fill" style="width:${widthPct}%"></div></div>
      </div>
      <div class="weekly-count">
        <span class="weekly-count-value">${fmtNum(parts.plays)}</span>
        <span class="weekly-count-label">plays</span>
      </div>
    </a>`;
  }).join("");
  const html = `
    <div class="dashboard-card-topline">
      <div>
        <div class="dashboard-eyebrow">Last.fm week</div>
        <h3>What kept resurfacing</h3>
      </div>
      <div class="dashboard-card-note">A compact view of the songs that dominated your week on Last.fm, especially the ones overlapping with favorite artists.</div>
    </div>
    <div class="weekly-summary">
      <div class="weekly-metric">
        <div class="weekly-metric-label">Top 8 plays</div>
        <div class="weekly-metric-value">${fmtNum(totalPlays)}</div>
      </div>
      <div class="weekly-metric">
        <div class="weekly-metric-label">Artists</div>
        <div class="weekly-metric-value">${fmtNum(artistCount)}</div>
      </div>
      <div class="weekly-metric">
        <div class="weekly-metric-label">Favorites</div>
        <div class="weekly-metric-value">${fmtNum(favoriteCount)}</div>
      </div>
    </div>
    <div class="weekly-note">Seven-day Last.fm chart.</div>
    <div class="weekly-list">${rows}</div>
  `;
  cards.forEach(card => { card.innerHTML = html; });
}

function fmtSigned(n) {
  const v = Number(n || 0);
  if (v > 0) return `+${v}`;
  if (v < 0) return `${v}`;
  return "0";
}

function fmtClock(tsMs) {
  if (!tsMs) return "—";
  return new Date(tsMs).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function renderSparkline(points) {
  const nums = (points || []).map(v => Number(v || 0));
  if (!nums.length) return "";
  const width = 360;
  const height = 92;
  const pad = 8;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = Math.max(1, max - min);
  const xFor = idx => pad + ((width - pad * 2) * idx / Math.max(1, nums.length - 1));
  const yFor = val => height - pad - (((val - min) / span) * (height - pad * 2));
  const pointsAttr = nums.map((val, idx) => `${xFor(idx)},${yFor(val)}`).join(" ");
  const fillPath = [
    `M ${xFor(0)} ${height - pad}`,
    ...nums.map((val, idx) => `L ${xFor(idx)} ${yFor(val)}`),
    `L ${xFor(nums.length - 1)} ${height - pad}`,
    "Z",
  ].join(" ");
  return `<svg class="spark-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${fillPath}" fill="rgba(29,185,84,0.10)"></path>
    <polyline points="${pointsAttr}" fill="none" stroke="rgba(29,185,84,0.95)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></polyline>
  </svg>`;
}

function renderListeningHeatmap(heatmap) {
  const data = heatmap || {};
  const days = Array.isArray(data.days) ? data.days : [];
  if (!days.length) return `<div class="dashboard-empty">No month view yet.</div>`;
  const firstWeekday = Number(days[0].weekday || 0);
  const maxCount = Math.max(1, Number(data.max_count || 0));
  const blanks = Array.from({ length: firstWeekday }, () => ({ blank: true }));
  const cells = blanks.concat(days);
  const yLabels = ["M", "", "W", "", "F", "", "S"]
    .map(label => `<div>${label}</div>`)
    .join("");
  const cellHtml = cells.map(cell => {
    if (cell.blank) return `<div class="analytics-heatmap-cell blank"></div>`;
    const count = Number(cell.listened || 0);
    const ratio = count / maxCount;
    const level = count <= 0 ? 0 : ratio >= 0.85 ? 4 : ratio >= 0.55 ? 3 : ratio >= 0.25 ? 2 : 1;
    const title = `${cell.short_label}: ${fmtNum(count)} listens${cell.minutes ? `, ${cell.minutes}m` : ""}`;
    return `<div class="analytics-heatmap-cell l${level}" title="${escapeHtml(title)}"></div>`;
  }).join("");
  const hottest = data.hottest_day_label
    ? `${escapeHtml(data.hottest_day_label)} · ${fmtNum(data.hottest_day_count || 0)} listens`
    : "No standout day yet";
  return `
    <div class="analytics-heatmap-shell">
      <div class="analytics-heatmap-head">
        <div class="analytics-heatmap-title">Month at a glance</div>
        <div class="analytics-heatmap-note">Last 35 days</div>
      </div>
      <div class="analytics-heatmap-grid-wrap">
        <div class="analytics-heatmap-y">${yLabels}</div>
        <div class="analytics-heatmap-grid">${cellHtml}</div>
      </div>
      <div class="analytics-heatmap-meta">
        <span class="analytics-heatmap-pill">${fmtNum(data.active_days || 0)} active days</span>
        <span class="analytics-heatmap-pill">Peak day: ${hottest}</span>
      </div>
    </div>
  `;
}

function renderActivityChart(series) {
  // Bars: listened per day (main) + removed as thin accent strip
  // Overlays: cumulative likes line + avg-removed reference line
  const n = series.length;
  if (!n) return "";
  const cappedSeries = series.map(row => ({
    ...row,
    removedCapped: Math.min(Number(row.removed || 0), Number(row.listened || 0)),
  }));
  const maxListened = Math.max(1, ...cappedSeries.map(r => r.listened || 0));
  const avgRemoved = cappedSeries.reduce((sum, r) => sum + (r.removedCapped || 0), 0) / n;

  // Build bar columns
  const bars = cappedSeries.map((row, idx) => {
    const showLabel = idx === 0 || idx === n - 1 || idx % 3 === 0;
    const listenH = row.listened > 0 ? Math.max(4, Math.round((row.listened / maxListened) * 100)) : 0;
    const removedH = row.removedCapped > 0 ? Math.max(2, Math.round((row.removedCapped / maxListened) * 100)) : 0;
    return `<div class="trend-day" title="${escapeHtml(row.short_label)} - heard ${row.listened || 0} (removed ${row.removedCapped || 0} of those), liked ${row.liked || 0}">
      <div class="trend-bars">
        <div class="trend-bar listen" style="height:${listenH}%"></div>
        <div class="trend-bar removed trend-bar-removed-accent" style="height:${removedH}%"></div>
      </div>
      <div class="trend-day-label">${showLabel ? escapeHtml(row.short_label) : "&middot;"}</div>
    </div>`;
  }).join("");

  // Build SVG overlay for cumulative likes + avg-removed line
  // SVG coordinate space matches the chart area (percentage based)
  const W = 1000, H = 100;  // viewBox units; bars occupy full height
  const colW = W / n;

  // Cumulative likes line (secondary axis — scaled to chart height)
  let cumLiked = 0;
  const totalLiked = series.reduce((s, r) => s + (r.liked || 0), 0);
  const likeScale = Math.max(1, totalLiked);  // scale: full height = all 14d likes
  const likePoints = cappedSeries.map((row, idx) => {
    cumLiked += (row.liked || 0);
    const x = colW * idx + colW / 2;
    const y = H - Math.round((cumLiked / likeScale) * (H - 4)) - 2;
    return `${x},${y}`;
  }).join(" ");

  // Avg-removed reference line
  const avgH = H - Math.round((avgRemoved / maxListened) * (H - 4));
  const avgLineY = Math.max(2, Math.min(H - 2, avgH));

  const overlaySvg = totalLiked > 0 || avgRemoved > 0 ? `
    <svg class="trend-overlay-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
      ${avgRemoved > 0 ? `<line x1="0" y1="${avgLineY}" x2="${W}" y2="${avgLineY}"
        stroke="var(--accent-2)" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.55"/>` : ""}
      ${totalLiked > 0 ? `<polyline points="${likePoints}" fill="none"
        stroke="var(--like)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>` : ""}
    </svg>` : "";

  const likedLabel = totalLiked > 0
    ? `<span><i class="legend-dot" style="background:var(--like)"></i> liked (running total: ${totalLiked})</span>`
    : "";
  const avgLabel = avgRemoved > 0
    ? `<span><i class="legend-line-dash" style="background:var(--accent-2)"></i> avg removed/day (${avgRemoved.toFixed(1)})</span>`
    : "";

  return `
    <div class="analytics-section-title">14-day activity</div>
    <div class="analytics-legend">
      <span><i class="legend-dot" style="background:#60a5fa"></i> heard</span>
      <span><i class="legend-dot" style="background:var(--accent-2); opacity:0.6"></i> removed <span style="opacity:0.6;font-size:10px">(subset of heard)</span></span>
      ${likedLabel}
      ${avgLabel}
    </div>
    <div class="trend-chart-wrap">
      <div class="trend-chart">${bars}</div>
      ${overlaySvg}
    </div>
  `;
}

function renderOverviewAnalytics(data) {
  const cards = ["analytics-overview-card", "dashboard-overview-card"]
    .map(id => document.getElementById(id))
    .filter(Boolean);
  if (!cards.length) return;
  if (!data || data.error) {
    const html = `<div class="empty">${escapeHtml(data && data.error ? data.error : "Analytics unavailable.")}</div>`;
    cards.forEach(card => { card.innerHTML = html; });
    return;
  }
  const s = data.summary || {};
  const series = data.series || [];
  if (!series.length) {
    const html = `<div class="empty">No analytics yet - listen to a few tracks and this will fill in.</div>`;
    cards.forEach(card => { card.innerHTML = html; });
    return;
  }
  const backlogPoints = series.map(row => Number(row.backlog || 0));
  const paceText = s.runway_days != null
    ? `${s.runway_days} days to clear at this pace`
    : `Need more than ${s.break_even_per_day}/day removed to outrun weekly inflow`;
  const netTone = s.net_weekly >= 0
    ? `Outpaced inflow by ${fmtNum(s.net_weekly)} track${Math.abs(s.net_weekly) === 1 ? "" : "s"} this week`
    : `Backlog grew by ${fmtNum(Math.abs(s.net_weekly))} track${Math.abs(s.net_weekly) === 1 ? "" : "s"} this week`;
  const html = `
    <div class="dashboard-card-topline">
      <div>
        <div class="dashboard-eyebrow">Backlog pressure</div>
        <h3>What the queue is doing</h3>
      </div>
      <div class="dashboard-card-note">This is the mechanical side of discovery: what is entering, what you are clearing, and how fast the playlist should shrink if you keep this pace.</div>
    </div>
    <div class="analytics-overview-grid">
      <div>
        <div class="analytics-metrics">
          <div class="analytics-metric">
            <div class="analytics-label">Tracks left</div>
            <div class="analytics-value">${fmtNum(s.playlist_total || 0)}</div>
            <div class="analytics-sub">${fmtNum(s.discover_weekly_inflow || 0)}/week inflow (configured)</div>
          </div>
          <div class="analytics-metric">
            <div class="analytics-label">Removed this week</div>
            <div class="analytics-value">${fmtNum(s.removed_last_7 || 0)}</div>
            <div class="analytics-sub">${s.removed_per_day != null ? `${s.removed_per_day}/day avg` : "—"}</div>
          </div>
          <div class="analytics-metric">
            <div class="analytics-label">Break-even</div>
            <div class="analytics-value">${s.break_even_per_day != null ? s.break_even_per_day : "—"}<span style="font-size:0.7em;opacity:0.7">/day</span></div>
            <div class="analytics-sub">to outrun inflow</div>
          </div>
          <div class="analytics-metric">
            <div class="analytics-label">Listened this week</div>
            <div class="analytics-value">${fmtNum(s.listened_last_7 || 0)}</div>
            <div class="analytics-sub">${s.avg_listen_s != null ? `${s.avg_listen_s}s avg` : "—"}</div>
          </div>
          <div class="analytics-metric">
            <div class="analytics-label">Liked (14 days)</div>
            <div class="analytics-value" style="color:var(--like)">${fmtNum(s.liked_last_14 || 0)}</div>
            <div class="analytics-sub">${s.liked_last_7 || 0} this week</div>
          </div>
          <div class="analytics-metric">
            <div class="analytics-label">Active days</div>
            <div class="analytics-value">${fmtNum((data.profile || {}).active_days_14 || 0)}</div>
            <div class="analytics-sub">out of the last 14</div>
          </div>
        </div>
        <div class="analytics-trend-card">
          ${renderActivityChart(series)}
        </div>
      </div>
      <div class="analytics-pace-card">
        <div class="analytics-section-title">Backlog pace</div>
        <div class="spark-wrap">
          <div class="spark-head">
            <div>
              <div class="analytics-label">Net weekly change</div>
              <div class="spark-value">${fmtSigned(s.net_weekly || 0)}</div>
            </div>
            <div class="spark-note">
              ${escapeHtml(netTone)}<br>
              ${escapeHtml(paceText)}
            </div>
          </div>
          ${renderSparkline(backlogPoints)}
          <div class="spark-axis">
            <span>${escapeHtml(series[0].short_label)}</span>
            <span>${escapeHtml(series[series.length - 1].short_label)}</span>
          </div>
          <div class="pace-mini-grid">
            <div class="pace-mini">
              <div class="pace-mini-label" title="% of tracks you heard and then liked (last 7 days)">Liked %</div>
              <div class="pace-mini-value">${s.keep_rate != null ? `${s.keep_rate}%` : "—"}</div>
            </div>
            <div class="pace-mini">
              <div class="pace-mini-label" title="% of tracks you heard and then removed from the playlist (last 7 days)">Removed %</div>
              <div class="pace-mini-value">${s.clear_rate != null ? `${s.clear_rate}%` : "—"}</div>
            </div>
            <div class="pace-mini">
              <div class="pace-mini-label">Avg listen</div>
              <div class="pace-mini-value">${s.avg_listen_s != null ? `${s.avg_listen_s}s` : "—"}</div>
            </div>
          </div>
          <div class="library-note">
            Backlog curve uses playlist snapshots, backfilled from your ${fmtNum(s.discover_weekly_inflow || 0)}/week inflow setting.
          </div>
          ${renderListeningHeatmap((data.profile || {}).listening_heatmap_35)}
        </div>
      </div>
    </div>
  `;
  cards.forEach(card => { card.innerHTML = html; });
}

function renderGenreBars(items, opts={}) {
  const rows = items || [];
  if (!rows.length) return `<div class="empty">${escapeHtml(opts.empty || "No genre data yet.")}</div>`;
  const maxCount = Math.max(1, ...rows.map(row => Number(row.count || 0)));
  return `<div class="genre-list">${rows.map(row => {
    const width = Math.max(10, Math.round((Number(row.count || 0) / maxCount) * 100));
    const sub = opts.showMinutes && row.minutes
      ? `${fmtNum(row.count || 0)} listens - ${row.minutes.toFixed(1)}m`
      : `${fmtNum(row.count || 0)} track${Number(row.count || 0) === 1 ? "" : "s"}`;
    return `<div class="genre-row">
      <div class="genre-meta">
        <div class="genre-name">${escapeHtml(row.name || "")}</div>
        <div class="genre-count">${fmtNum(row.count || 0)}</div>
      </div>
      <div class="genre-sub">${escapeHtml(sub)}</div>
      <div class="genre-bar"><div class="genre-fill" style="width:${width}%"></div></div>
    </div>`;
  }).join("")}</div>`;
}

function renderRecentListenTracks(items) {
  const rows = (items || []).slice(0, 5);
  if (!rows.length) return `<div class="empty">No recent listen history yet.</div>`;
  return `<div class="recent-track-list">${rows.map(item => {
    const art = item.image ? `<img src="${item.image}" alt="">` : `<div class="recent-track-placeholder"></div>`;
    const genre = item.genre
      ? `<span class="recent-track-genre">${escapeHtml(item.genre)}</span>`
      : `<span class="recent-track-genre" style="opacity:0.55">no genre</span>`;
    return `<a class="recent-track-row" href="${item.url || "#"}" target="_blank">
      ${art}
      <div>
        <div class="recent-track-name">${escapeHtml(item.track_name || "")}</div>
        <div class="recent-track-artist">${escapeHtml(item.artists || "")}</div>
      </div>
      <div class="recent-track-side">
        ${genre}
        <div class="recent-track-time">${item.listened_s != null ? `${item.listened_s.toFixed(1)}s` : "played"}</div>
      </div>
    </a>`;
  }).join("")}</div>`;
}

function renderAffinityArtistCard(item) {
  if (!item) return `<div class="empty">No favorite artist signal yet.</div>`;
  const art = item.image ? `<img class="affinity-art" src="${escapeHtml(item.image)}" alt="">` : `<div class="affinity-art-ph"></div>`;
  const rows = (item.top_tracks || []).slice(0, 5).map((track, idx) => `
    <div class="affinity-row">
      <div class="affinity-rank">${idx + 1}</div>
      <div>
        <div class="affinity-row-name">${escapeHtml(track.name || "")}</div>
        <div class="affinity-row-sub">${escapeHtml(track.artists || item.name || "")}</div>
      </div>
      <div class="affinity-row-count">${fmtNum(track.listens || 0)}</div>
    </div>
  `).join("");
  return `
    <div class="affinity-card">
      <div class="affinity-kicker">Favorite artist right now</div>
      <div class="affinity-head" style="margin-top:8px">
        ${art}
        <div>
          <div class="affinity-title">${escapeHtml(item.name || "")}</div>
          <div class="affinity-sub">${fmtNum(item.listens || 0)} serious listens in the last 30 days</div>
        </div>
      </div>
      <div class="affinity-list">${rows || `<div class="empty">No top songs yet.</div>`}</div>
    </div>
  `;
}

function renderAffinitySongCard(item) {
  if (!item) return `<div class="empty">No favorite song signal yet.</div>`;
  const art = item.image ? `<img class="affinity-art" src="${escapeHtml(item.image)}" alt="">` : `<div class="affinity-art-ph"></div>`;
  const rows = (item.runners_up || []).slice(0, 4).map((track, idx) => `
    <div class="affinity-row">
      <div class="affinity-rank">${idx + 2}</div>
      <div>
        <div class="affinity-row-name">${escapeHtml(track.name || "")}</div>
        <div class="affinity-row-sub">${escapeHtml(track.artists || "")}</div>
      </div>
      <div class="affinity-row-count">${fmtNum(track.listens || 0)}</div>
    </div>
  `).join("");
  return `
    <div class="affinity-card">
      <div class="affinity-kicker">Favorite song right now</div>
      <div class="affinity-head" style="margin-top:8px">
        ${art}
        <div>
          <div class="affinity-title">${escapeHtml(item.name || "")}</div>
          <div class="affinity-sub">${escapeHtml(item.artists || "")} • ${fmtNum(item.listens || 0)} listens</div>
        </div>
      </div>
      <div class="affinity-list">${rows || `<div class="empty">No runners-up yet.</div>`}</div>
    </div>
  `;
}

function renderGenreAnalyticsLegacy(data) {
  const card = document.getElementById("analytics-genre-card");
  if (!card) return;
  if (!data || data.error) {
    card.innerHTML = `<div class="empty">${escapeHtml(data && data.error ? data.error : "Taste profile unavailable.")}</div>`;
    return;
  }
  const listened = (data.genres && data.genres.listened) || [];
  const recentLiked = (data.genres && data.genres.liked) || [];
  const recentTracks = (data.genres && data.genres.recent_tracks) || [];
  const library = (data.genres && data.genres.library) || {};
  const libraryGenres = library.genres || [];
  const libraryLoading = !!library.loading;
  const dominantRecentGenre = listened.length ? listened[0].name : null;
  const dominantRecentCount = listened.length ? listened[0].count : 0;
  const likedLean = recentLiked.length
    ? `Recent likes are leaning ${recentLiked.slice(0, 3).map(row => row.name).join(", ")}.`
    : `Like a few tracks from the dashboard and this section will start showing your keepers.`;
  const libraryScopeText = libraryLoading
    ? `Profiling your liked songs in the background${library.processed_tracks ? ` (${fmtNum(library.processed_tracks)} / ${fmtNum(library.sample_size || 0)} scanned)` : "."}`
    : library.is_full_scan
      ? `Based on your full liked-song library: ${fmtNum(library.sample_size || 0)} tracks${library.tracks_with_genres ? `, with genre matches on ${fmtNum(library.tracks_with_genres)} of them.` : "."}`
      : `Based on the latest ${fmtNum(library.sample_size || 0)} liked tracks${library.tracks_with_genres ? `, with genre matches on ${fmtNum(library.tracks_with_genres)} of them.` : "."}`;
  card.innerHTML = `
    <div class="taste-grid">
      <div>
        <div class="taste-section-title">What you’ve listened to lately</div>
        ${renderGenreBars(listened, { empty: "No recent listens logged yet.", showMinutes: true })}
        <div class="library-note">${escapeHtml(likedLean)}</div>
      </div>
      <div>
        <div class="taste-section-title">Liked Songs composition</div>
        ${renderGenreBars(libraryGenres, { empty: libraryLoading ? "Profiling your liked library…" : "No liked-library genre profile yet." })}
        <div class="library-note">
          ${libraryLoading
            ? "Pulling a cached genre picture for your recent liked songs in the background."
            : `Based on the latest ${fmtNum(library.sample_size || 0)} liked tracks${library.tracks_with_genres ? `, with genre matches on ${fmtNum(library.tracks_with_genres)} of them.` : "."}`}
        </div>
      </div>
    </div>
  `;
}

function renderGenreAnalytics(data) {
  const cards = ["analytics-genre-card", "dashboard-taste-card"]
    .map(id => document.getElementById(id))
    .filter(Boolean);
  if (!cards.length) return;
  if (!data || data.error) {
    const html = `<div class="empty">${escapeHtml(data && data.error ? data.error : "Taste profile unavailable.")}</div>`;
    cards.forEach(card => { card.innerHTML = html; });
    return;
  }
  const recentLiked = (data.genres && data.genres.liked) || [];
  const library = (data.genres && data.genres.library) || {};
  const libraryGenres = library.genres || [];
  const libraryLoading = !!library.loading;
  const profile = data.profile || {};
  const likedLean = recentLiked.length
    ? `Recent likes are leaning ${recentLiked.slice(0, 3).map(row => row.name).join(", ")}.`
    : `Like a few tracks from the dashboard and this section will start showing your keepers.`;
  const libraryScopeText = libraryLoading
    ? "Pulling a cached genre picture for your liked songs in the background."
    : library.is_full_scan
      ? `Based on your full liked-song library: ${fmtNum(library.sample_size || 0)} tracks${library.tracks_with_genres ? `, with genre matches on ${fmtNum(library.tracks_with_genres)} of them.` : "."}`
      : `Based on the latest ${fmtNum(library.sample_size || 0)} liked tracks${library.tracks_with_genres ? `, with genre matches on ${fmtNum(library.tracks_with_genres)} of them.` : "."}`;
  const html = `
    <div class="dashboard-card-topline">
      <div>
        <div class="dashboard-eyebrow">Affinity</div>
        <h3>What is pulling you in</h3>
      </div>
      <div class="dashboard-card-note">Your strongest current artist and song signals, plus the genre shape of the library you already trust enough to keep.</div>
    </div>
    <div class="taste-grid">
      <div>
        <div class="taste-section-title">Right now</div>
        <div class="affinity-grid">
          ${renderAffinityArtistCard(profile.favorite_artist)}
          ${renderAffinitySongCard(profile.favorite_song)}
        </div>
      </div>
      <div>
        <div class="taste-section-title">Liked Songs composition</div>
        ${renderGenreBars(libraryGenres.slice(0, 5), { empty: libraryLoading ? "Profiling your liked library…" : "No liked-library genre profile yet." })}
        <div class="library-note">${libraryScopeText}</div>
        <div class="library-note">${escapeHtml(likedLean)}</div>
      </div>
    </div>
  `;
  cards.forEach(card => { card.innerHTML = html; });
}

function timelineBadgeLabel(item) {
  if (!item) return "event";
  if (item.type === "now") return "now";
  if (item.type === "listen") return "listen";
  if (item.type === "liked") return "liked";
  if (item.type === "added") return "routed";
  if (item.type === "removed" && item.reason === "threshold") return "auto-removed";
  if (item.type === "removed") return "removed";
  return item.type || "event";
}

function timelineTitle(item) {
  if (!item) return "";
  if (item.type === "now") return `Listening now · ${item.listened_s != null ? `${item.listened_s.toFixed(1)}s in` : "in progress"}`;
  if (item.type === "listen") return `Played · ${item.listened_s != null ? `${item.listened_s.toFixed(1)}s` : "session logged"}`;
  if (item.type === "liked") return "Liked this song";
  if (item.type === "added") return item.target ? `Sent to ${item.target}` : "Sent to another playlist";
  if (item.type === "removed") {
    if (item.reason === "threshold") return "Auto-removed at threshold";
    if (item.reason === "like") return "Liked and removed";
    if (item.reason === "skip") return "Skipped and removed";
    if (item.reason === "catch-up") return "Catch-up removal";
    if (item.reason) return `Removed · ${item.reason}`;
    return "Removed from discover";
  }
  return "Activity";
}

function timelineNote(item) {
  if (!item) return "";
  if (item.type === "now") return "Current playback is shown here so the timeline feels live, even before the next track rolls over.";
  if (item.type === "listen") return item.reason === "current" ? "" : "Logged from the playback watcher once the track moved on.";
  if (item.type === "liked") return item.listened_s != null ? `Liked after ${item.listened_s.toFixed(1)} seconds.` : "Saved to your Spotify library.";
  if (item.type === "added") return item.target ? `Routed out of the backlog into ${item.target}.` : "Sent to another playlist.";
  if (item.type === "removed") {
    if (item.reason === "threshold") return "Hit the listen threshold and was automatically cleared from the target playlist.";
    if (item.reason === "skip") return "You chose to skip it and clear it immediately.";
    if (item.reason === "catch-up") return "Removed because it showed up in your recent plays and was still sitting in the backlog.";
    if (item.reason === "like") return "Promoted into Liked Songs and cleared from the backlog.";
    if (item.reason) return `Removal reason: ${item.reason}.`;
  }
  return "";
}

function renderTimelineAnalytics(data) {
  const card = document.getElementById("analytics-timeline-card");
  if (!card) return;
  if (!data || data.error) {
    card.innerHTML = `<div class="empty">${escapeHtml(data && data.error ? data.error : "Timeline unavailable.")}</div>`;
    return;
  }
  const items = data.timeline || [];
  if (!items.length) {
    card.innerHTML = `<div class="empty">No activity logged yet today.</div>`;
    return;
  }
  card.innerHTML = `<div class="timeline-list">${items.map(item => {
    const art = item.image ? `<img src="${item.image}" alt="">` : `<div class="timeline-placeholder"></div>`;
    const href = item.url || "#";
    const note = timelineNote(item);
    return `<div class="timeline-item">
      <div class="timeline-time">${escapeHtml(fmtClock(item.ts_ms))}</div>
      <div class="timeline-body">
        <div class="timeline-head">
          <div class="timeline-title">${escapeHtml(timelineTitle(item))}</div>
          <div class="timeline-pill ${escapeHtml(item.type || "event")}">${escapeHtml(timelineBadgeLabel(item))}</div>
        </div>
        <a class="timeline-track" href="${href}" target="_blank">
          ${art}
          <div>
            <div class="timeline-track-name">${escapeHtml(item.track_name || "")}</div>
            <div class="timeline-track-artist">${escapeHtml(item.artists || "")}</div>
          </div>
        </a>
        ${note ? `<div class="timeline-note">${escapeHtml(note)}</div>` : ""}
      </div>
    </div>`;
  }).join("")}</div>`;
}

function renderAnalytics(data) {
  renderOverviewAnalytics(data);
  renderGenreAnalytics(data);
  renderTimelineAnalytics(data);
  window._latestAnalytics = data || null;
  renderDashboardSurface();
}

function renderDashboardSurface() {
  const analytics = window._latestAnalytics || {};
  const status = window._latestStatus || {};
  renderDashboardMomentumCard(analytics, status);
  renderDashboardCurrentLaneCard(analytics, status);
  renderDashboardBreadthCard(analytics, status);
  renderDashboardClockCard(analytics, status);
}

function renderDashboardMomentumCard(data, status) {
  const card = document.getElementById("dashboard-momentum-card");
  if (!card) return;
  if (!data || data.error) {
    card.innerHTML = `<div class="dashboard-empty">${escapeHtml((data && data.error) || "Discovery pulse is still warming up.")}</div>`;
    return;
  }
  const s = data.summary || {};
  const series = data.series || [];
  const listenedPoints = series.map(row => Number(row.listened || 0));
  const scrobblesToday = status.scrobbles_today != null ? fmtNum(Number(status.scrobbles_today) || 0) : "-";
  const headline = fmtNum(s.listened_last_7 || 0);
  const tone = (s.net_weekly || 0) >= 0
    ? `You cleared ${fmtNum(s.removed_last_7 || 0)} tracks against ${fmtNum(s.discover_weekly_inflow || 0)} new arrivals this week.`
    : `Backlog grew by ${fmtNum(Math.abs(s.net_weekly || 0))} this week - your inflow is outrunning your clears.`;
  card.innerHTML = `
    <div class="dashboard-card-topline">
      <div>
        <div class="dashboard-eyebrow">This week</div>
        <h3>The shape of your listening week</h3>
      </div>
      <div class="dashboard-card-note">A quick read on how much you explored, how much you cleared, and whether the backlog feels lighter than it did a few days ago.</div>
    </div>
    <div class="dashboard-intel-hero">
      <div class="dashboard-callout">
        <div class="dashboard-callout-value">${headline}</div>
        <div class="dashboard-summary-line">${escapeHtml(tone)}</div>
      </div>
      <div>
        ${listenedPoints.length ? renderSparkline(listenedPoints) : `<div class="dashboard-empty">No listening trace yet.</div>`}
      </div>
    </div>
    <div class="dashboard-kpi-row">
      <div class="dashboard-kpi-pill"><span class="k">Removed</span><span class="v">${fmtNum(s.removed_last_7 || 0)}</span></div>
      <div class="dashboard-kpi-pill"><span class="k">Liked</span><span class="v">${fmtNum(s.liked_last_7 || 0)}</span></div>
      <div class="dashboard-kpi-pill"><span class="k">Keep rate</span><span class="v">${s.keep_rate != null ? `${s.keep_rate}%` : "-"}</span></div>
      <div class="dashboard-kpi-pill"><span class="k">Scrobbles today</span><span class="v">${scrobblesToday}</span></div>
    </div>
  `;
}

function renderDashboardCurrentLaneCard(data, status) {
  const card = document.getElementById("dashboard-current-lane-card");
  if (!card) return;
  if (!data || data.error) {
    card.innerHTML = `<div class="dashboard-empty">${escapeHtml((data && data.error) || "Current lane unavailable.")}</div>`;
    return;
  }
  const listened = ((data.genres || {}).listened) || [];
  const dominant = listened[0];
  const artistText = (status.current_track && status.current_track.artists) ? status.current_track.artists : "No track in focus";
  card.innerHTML = `
    <div class="dashboard-eyebrow">Current lane</div>
    <h3>${escapeHtml((dominant && dominant.name) || "Still learning your lane")}</h3>
    <div class="dashboard-inline-pills">
      <span class="dashboard-inline-pill">Right now: ${escapeHtml(artistText)}</span>
      ${dominant ? `<span class="dashboard-inline-pill">${fmtNum(dominant.count || 0)} listens - ${(dominant.minutes || 0).toFixed(1)}m</span>` : ``}
    </div>
    <div style="margin-top:14px">${renderGenreBars(listened.slice(0, 5), { empty: "No recent genre mix yet.", showMinutes: true })}</div>
  `;
}

function renderDashboardBreadthCard(data, status) {
  const card = document.getElementById("dashboard-breadth-card");
  if (!card) return;
  if (!data || data.error) {
    card.innerHTML = `<div class="dashboard-empty">${escapeHtml((data && data.error) || "Listening breadth unavailable.")}</div>`;
    return;
  }
  const p = data.profile || {};
  const s = data.summary || {};
  const backlogRunway = s.runway_days != null ? `${s.runway_days}d` : "needs pace";
  const nowTrack = (status.current_track || {}).name || "No active playback";
  const breadthNote = p.unique_tracks_7 > p.unique_artists_7
    ? `You sampled broadly: ${fmtNum(p.unique_tracks_7 || 0)} distinct tracks spread across ${fmtNum(p.unique_artists_7 || 0)} artists.`
    : `This was a tighter week: more return visits than wide roaming.`;
  card.innerHTML = `
    <div class="dashboard-eyebrow">Scope</div>
    <h3>How broad the week felt</h3>
    <div class="dashboard-mini-grid">
      <div class="dashboard-mini-stat">
        <div class="lbl">Artists / 7d</div>
        <div class="val">${fmtNum(p.unique_artists_7 || 0)}</div>
        <div class="sub">distinct voices you returned to</div>
      </div>
      <div class="dashboard-mini-stat">
        <div class="lbl">Albums / 7d</div>
        <div class="val">${fmtNum(p.unique_albums_7 || 0)}</div>
        <div class="sub">catalog branches touched</div>
      </div>
      <div class="dashboard-mini-stat">
        <div class="lbl">Tracks / 7d</div>
        <div class="val">${fmtNum(p.unique_tracks_7 || 0)}</div>
        <div class="sub">distinct songs sampled</div>
      </div>
      <div class="dashboard-mini-stat">
        <div class="lbl">Replay ratio</div>
        <div class="val">${p.replay_ratio_7 != null ? `${p.replay_ratio_7}%` : "-"}</div>
        <div class="sub">repeat listens vs new track touches</div>
      </div>
      <div class="dashboard-mini-stat">
        <div class="lbl">Active days / 14</div>
        <div class="val">${fmtNum(p.active_days_14 || 0)}</div>
        <div class="sub">days with at least one serious listen</div>
      </div>
      <div class="dashboard-mini-stat">
        <div class="lbl">Current streak</div>
        <div class="val">${fmtNum(p.current_streak_days || 0)}</div>
        <div class="sub">consecutive listening days</div>
      </div>
    </div>
    <div class="dashboard-kpi-row" style="margin-top:16px">
      <div class="dashboard-kpi-pill"><span class="k">Listen time</span><span class="v">${p.listen_minutes_7 != null ? `${p.listen_minutes_7}m` : "-"}</span></div>
      <div class="dashboard-kpi-pill"><span class="k">Playlist clear %</span><span class="v">${s.clear_rate != null ? `${s.clear_rate}%` : "-"}</span></div>
      <div class="dashboard-kpi-pill"><span class="k">Runway</span><span class="v">${backlogRunway}</span></div>
      <div class="dashboard-kpi-pill"><span class="k">Now playing</span><span class="v" style="font-size:13px">${escapeHtml(nowTrack)}</span></div>
    </div>
    <div class="dashboard-spark-note">${escapeHtml(breadthNote)}</div>
  `;
}

function renderDashboardNowCard(data, status) {
  const card = document.getElementById("dashboard-now-card");
  if (!card) return;
  const track = (status && status.current_track) || null;
  const info = (status && status.current_artist_info) || {};
  if (!track) {
    card.innerHTML = `<div class="dashboard-empty">No active playback right now.</div>`;
    return;
  }
  const trackArt = track.image ? `<img class="now-focus-art" src="${escapeHtml(track.image)}" alt="">` : `<div class="now-focus-art-ph"></div>`;
  const artistArtUrl = info.artist_image || ((info.top_tracks || []).find(t => t.image)?.image) || "";
  const artistArt = artistArtUrl ? `<img class="now-focus-art" src="${escapeHtml(artistArtUrl)}" alt="">` : `<div class="now-focus-art-ph"></div>`;
  const thresholdPct = status.threshold_seconds ? Math.max(0, Math.min(100, Math.round(((status.current_listened || 0) / status.threshold_seconds) * 100))) : 0;
  const topTracks = (info.top_tracks || []).slice(0, 5).map((t, idx) => `
    <div class="affinity-row">
      <div class="affinity-rank">${idx + 1}</div>
      <div>
        <div class="affinity-row-name">${escapeHtml(t.name || "")}</div>
        <div class="affinity-row-sub">${escapeHtml((t.album || "") || (track.album || ""))}</div>
      </div>
      <div class="affinity-row-count">${t.duration_ms ? fmtTime(t.duration_ms) : ""}</div>
    </div>
  `).join("");
  card.innerHTML = `
    <div class="dashboard-eyebrow">Now spotlight</div>
    <h3>Who you're listening to now</h3>
    <div class="now-spotlight-grid">
      <div class="now-focus-card">
        <div class="now-focus-head">
          ${trackArt}
          <div>
            <div class="now-focus-title">${escapeHtml(track.name || "")}</div>
            <div class="now-focus-sub">${escapeHtml(track.artists || "")}</div>
            <div class="now-focus-sub">${escapeHtml(track.album || "")}</div>
          </div>
        </div>
        <div class="now-focus-progress"><div class="now-focus-progress-fill" style="width:${thresholdPct}%"></div></div>
        <div class="now-focus-grid">
          <div class="now-focus-pill"><div class="k">Listened</div><div class="v">${(status.current_listened || 0).toFixed(1)}s</div></div>
          <div class="now-focus-pill"><div class="k">Threshold</div><div class="v">${fmtNum(status.threshold_seconds || 0)}s</div></div>
          <div class="now-focus-pill"><div class="k">Liked</div><div class="v">${status.current_is_liked ? "Yes" : "No"}</div></div>
          <div class="now-focus-pill"><div class="k">In playlist</div><div class="v">${status.current_in_playlist ? "Yes" : "No"}</div></div>
        </div>
      </div>
      <div class="now-focus-card">
        <div class="now-focus-head">
          ${artistArt}
          <div>
            <div class="now-focus-title">${escapeHtml((((track.artists || "").split(",")[0]) || "Artist").trim())}</div>
            <div class="now-focus-sub">${escapeHtml((info.lastfm_listeners || info.lastfm_playcount) ? `${fmtNum(info.lastfm_listeners || 0)} listeners` : "Artist context live")}</div>
          </div>
        </div>
        <div class="affinity-list" style="margin-top:12px">${topTracks || `<div class="empty">No top tracks loaded for this artist yet.</div>`}</div>
      </div>
    </div>
  `;
}

function renderDashboardClockCard(data) {
  const card = document.getElementById("dashboard-clock-card");
  if (!card) return;
  if (!data || data.error) {
    card.innerHTML = `<div class="dashboard-empty">${escapeHtml((data && data.error) || "Listening rhythm unavailable.")}</div>`;
    return;
  }
  const profile = data.profile || {};
  const bins = Array.isArray(profile.listening_clock) ? profile.listening_clock : [];
  const maxBin = Math.max(1, ...bins, 1);
  const bars = bins.length ? bins.map((n, idx) => {
    const h = n > 0 ? Math.max(6, Math.round((Number(n || 0) / maxBin) * 100)) : 6;
    return `<div class="dashboard-clock-bar" title="${idx}:00 - ${fmtNum(n || 0)} listens" style="height:${h}%"></div>`;
  }).join("") : "";
  const busiestHour = Number.isFinite(Number(profile.busiest_hour))
    ? `${String(Number(profile.busiest_hour)).padStart(2, "0")}:00`
    : "—";
  const rhythmNote = Number.isFinite(Number(profile.busiest_hour))
    ? `Your listening tends to crest around ${busiestHour}, with ${fmtNum(profile.busiest_hour_count || 0)} serious plays in that peak window.`
    : `We need a little more listening history before your rhythm settles into shape.`;
  card.innerHTML = `
    <div class="dashboard-eyebrow">When it happened</div>
    <h3>Your listening rhythm</h3>
    <div class="dashboard-kpi-row" style="margin-top:14px">
      <div class="dashboard-kpi-pill"><span class="k">Busiest hour</span><span class="v">${busiestHour}</span></div>
      <div class="dashboard-kpi-pill"><span class="k">Plays then</span><span class="v">${fmtNum(profile.busiest_hour_count || 0)}</span></div>
      <div class="dashboard-kpi-pill"><span class="k">Research</span><span class="v"><button class="dashboard-link-btn" type="button" onclick="openWorkspace('research')">Open queue</button></span></div>
      <div class="dashboard-kpi-pill"><span class="k">Favorites</span><span class="v"><button class="dashboard-link-btn" type="button" onclick="openWorkspace('favorites')">Open releases</button></span></div>
    </div>
    <div class="dashboard-clock">${bars || `<div class="dashboard-empty">No listening clock yet.</div>`}</div>
    <div class="dashboard-clock-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div>
    <div class="dashboard-spark-note">${escapeHtml(rhythmNote)}</div>
  `;
}

async function loadAnalytics(force=false) {
  if (analyticsInFlight) return;
  if (!force && (Date.now() - analyticsLoadedAt) < ANALYTICS_REFRESH_MS) return;
  analyticsInFlight = true;
  try {
    const r = await fetch("/api/analytics");
    const d = await r.json();
    if (!d.error) {
      const libraryLoading = !!(((d.genres || {}).library || {}).loading);
      analyticsLoadedAt = Date.now() - (libraryLoading ? (ANALYTICS_REFRESH_MS - 15000) : 0);
    }
    renderAnalytics(d);
  } catch (e) {
    renderAnalytics({ error: e.message || "Analytics request failed." });
  } finally {
    analyticsInFlight = false;
  }
}

function closeAnalytics() {
  closeWorkspace();
}

// ── Settings modal ──
function openSettings() {
  document.getElementById("settings-modal").classList.add("open");
  document.getElementById("settings-modal-backdrop").classList.add("visible");
  initSettingsSaveButton();
  loadSettings();
  refreshLfmImportStatus();
}
function closeSettings() {
  document.getElementById("settings-modal").classList.remove("open");
  document.getElementById("settings-modal-backdrop").classList.remove("visible");
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeWorkspace(); closeSettings(); closeProfileMenu(); }
});

// Sidebar tab switching
let _sidebarTab = "history";
let _historyLoaded = false;
let _historyFeed = "timeline";
let _likedSongsLoaded = false;
let _likedSongsTracks = [];
function switchSidebarTab(tab) {
  _sidebarTab = tab;
  try { localStorage.setItem(SIDEBAR_TAB_KEY, tab); } catch(_) {}
  const tq = document.getElementById("tab-queue");
  const th = document.getElementById("tab-history");
  const tl = document.getElementById("tab-liked");
  if (tq) tq.classList.toggle("active", tab === "queue");
  if (th) th.classList.toggle("active", tab === "history");
  if (tl) tl.classList.toggle("active", tab === "liked");
  const qp = document.getElementById("sidebar-queue-panel");
  const hp = document.getElementById("sidebar-history-panel");
  const lp = document.getElementById("sidebar-liked-panel");
  if (qp) qp.style.display = tab === "queue" ? "" : "none";
  if (hp) hp.style.display = tab === "history" ? "" : "none";
  if (lp) lp.style.display = tab === "liked" ? "" : "none";
  if (tab === "history") loadSidebarHistory();
  if (tab === "liked") loadSidebarLikedSongs();
}

(function restoreSidebarTab() {
  let saved = "history";
  try { saved = localStorage.getItem(SIDEBAR_TAB_KEY) || "history"; } catch(_) {}
  if (!["queue", "history", "liked"].includes(saved)) saved = "history";
  switchSidebarTab(saved);
})();

// Compact stats strip + quick stat grid update
function updateStatsStrip(d) {
  const stats = d.stats || {};
  const set = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
  const numOrDash = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? fmtNum(n) : "-";
  };
  const sessionMs = Date.now() - (d.session_started_ms || Date.now());
  set("qs-heard",     numOrDash(stats.listened_today));
  set("qs-liked",     numOrDash(stats.liked_today));
  set("qs-removed",   numOrDash(stats.removed_today));
  set("qs-playlist",  numOrDash(d.playlist_total));
  set("qs-spqueue",   numOrDash(d.spotify_queue_count));
  set("qs-session",   fmtDuration(sessionMs));
  set("qs-scrobbles", d.scrobbles_today != null ? fmtNum(Number(d.scrobbles_today) || 0) : "-");
}

async function loadSidebarHistory() {
  if (_historyLoaded) return;
  const el = document.getElementById("sidebar-history-list");
  if (!el) return;
  _historyFeed = "spotify";
  el.innerHTML = `<div class="queue-empty"><span class="spinner"></span> Loading history...</div>`;
  try {
    const r = await fetch("/api/history");
    const d = await r.json();
    if (!r.ok || d.error) {
      if (r.status === 503 || d.error === "guard") {
        const retryIn = d.retry_after || 15;
        el.innerHTML = `<div class="queue-empty" style="color:var(--muted);font-size:11px">Cooling down - retrying in ${retryIn}s</div>`;
        setTimeout(() => { _historyLoaded = false; loadSidebarHistory(); }, retryIn * 1000);
        return;
      }
      _historyFeed = "timeline";
      el.innerHTML = `<div class="queue-empty">${escapeHtml(d.error || "Failed to load history")}</div>`;
      return;
    }
    _historyLoaded = true;
    renderSidebarHistory(d.tracks || [], []);
  } catch(e) {
    _historyFeed = "timeline";
    el.innerHTML = `<div class="queue-empty">Failed to load history.</div>`;
  }
}

function renderSidebarHistory(tracks, _unused) {
  const el = document.getElementById("sidebar-history-list");
  if (!el) return;
  if (!tracks.length) {
    el.innerHTML = `<div class="queue-empty">No history yet.</div>`;
    return;
  }
  el.innerHTML = tracks.map(item => {
    const art = item.image
      ? `<img class="history-art" src="${escapeHtml(item.image)}" alt="" loading="lazy">`
      : `<div class="history-art" style="background:var(--panel-2)"></div>`;
    const pillType = item.type === "removed" ? "removed" : item.type === "liked" ? "liked" : "";
    const pillLabel = pillType || "";
    const name = item.track_name || item.name || "";
    const artists = item.artists || "";
    const genres = (item.genres || []).slice(0, 3);
    const genreHtml = genres.length
      ? `<div class="history-genres">${genres.map(g => `<span class="history-genre">${escapeHtml(g)}</span>`).join("")}</div>`
      : "";
    const playBtn = item.uri
      ? `<button class="queue-play" type="button" onclick="event.stopPropagation();playTrack('${escapeHtml(item.uri)}')" title="Play">&#9654;</button>`
      : "";
    return `<div class="history-item">
      ${art}
      <div class="history-meta">
        <div class="history-name">${escapeHtml(name)}</div>
        <div class="history-sub">${escapeHtml(artists)}</div>
        ${genreHtml}
      </div>
      ${pillLabel ? `<span class="history-pill ${pillType}">${escapeHtml(pillLabel)}</span>` : ""}
      ${playBtn}
    </div>`;
  }).join("");
  wrapArtistLinks(el);
}

async function loadSidebarLikedSongs(force=false) {
  if (_likedSongsLoaded && !force) return;
  const el = document.getElementById("sidebar-liked-list");
  if (!el) return;
  el.innerHTML = `<div class="queue-empty"><span class="spinner"></span> Loading liked songs...</div>`;
  try {
    const r = await fetch(`/api/liked-songs${force ? "?force=1" : ""}`);
    const d = await r.json();
    if (!r.ok || d.error) {
      if (r.status === 503 || d.error === "guard") {
        const retryIn = d.retry_after || 15;
        el.innerHTML = `<div class="queue-empty" style="color:var(--muted);font-size:11px">Cooling down - retrying in ${retryIn}s</div>`;
        setTimeout(() => { _likedSongsLoaded = false; loadSidebarLikedSongs(force); }, retryIn * 1000);
        return;
      }
      el.innerHTML = `<div class="queue-empty">${escapeHtml(d.error || "Failed to load liked songs")}</div>`;
      return;
    }
    _likedSongsLoaded = true;
    _likedSongsTracks = d.tracks || [];
    window._trackGenreCache = window._trackGenreCache || {};
    _likedSongsTracks.forEach(t => {
      if (t && t.id && Array.isArray(t.genres) && t.genres.length) {
        window._trackGenreCache[t.id] = t.genres;
      }
    });
    renderSidebarLikedSongs(_likedSongsTracks);
    fetchQueueGenres(_likedSongsTracks.map(t => ({ id: t.id, artist: t.artist, genres: t.genres })));
  } catch (e) {
    el.innerHTML = `<div class="queue-empty">Failed to load liked songs.</div>`;
  }
}

function renderSidebarLikedSongs(tracks) {
  const el = document.getElementById("sidebar-liked-list");
  if (!el) return;
  if (!tracks.length) {
    el.innerHTML = `<div class="queue-empty">No liked songs loaded yet.</div>`;
    return;
  }
  const genreCache = window._trackGenreCache || {};
  const currentId = window._currentTrackId || null;
  el.innerHTML = tracks.map(item => {
    const isCurrent = item.id && item.id === currentId;
    const art = item.image
      ? `<img class="queue-art" src="${escapeHtml(item.image)}" alt="" loading="lazy">`
      : `<div class="queue-art-placeholder"></div>`;
    const genres = (genreCache[item.id] || item.genres || []).slice(0, 4);
    const genreHtml = genres.length
      ? `<div class="queue-genres">${genres.map(g => `<span class="queue-genre">${escapeHtml(g)}</span>`).join("")}</div>`
      : "";
    const duration = item.duration_ms ? fmtTime(item.duration_ms) : "";
    return `<div class="queue-item${isCurrent ? " current-track" : ""}">
      ${art}
      <div class="queue-meta">
        <div class="queue-name">${escapeHtml(item.name || "")}</div>
        <div class="queue-artist">${escapeHtml(item.artists || item.artist || "")}${duration ? ` · ${escapeHtml(duration)}` : ""}</div>
        ${genreHtml}
      </div>
      ${item.uri ? `<button class="queue-play${isCurrent ? " playing" : ""}" type="button" data-uri="${escapeHtml(item.uri)}" onclick="playTrack(this.dataset.uri)" title="Play">▶</button>` : ""}
    </div>`;
  }).join("");
  wrapArtistLinks(el);
}

// ── Playlist combobox ──────────────────────────────────────────────────────
function _isSpotifyPlaylistInput(v) {
  return v && (v.includes("spotify.com/playlist/") || v.includes("spotify:playlist:") ||
               /^[A-Za-z0-9]{22,}$/.test(v.trim()));
}

function initPlCombo(wrapperId, inputId, listId, hiddenId, options, onSelectCallback) {
  const input = document.getElementById(inputId);
  const list  = document.getElementById(listId);
  const hidden = document.getElementById(hiddenId);
  if (!input || !list || !hidden) return;
  // Remove any stale listeners by cloning
  const newInput = input.cloneNode(true);
  input.parentNode.replaceChild(newInput, input);
  const inp = newInput;
  let focusedIdx = -1;
  let _opts = options; // live reference

  function getFiltered(q) {
    const query = (q || "").trim().toLowerCase();
    return query ? _opts.filter(o => o.name.toLowerCase().includes(query)) : _opts;
  }

  function renderList(q) {
    const filtered = getFiltered(q);
    if (!filtered.length) {
      list.innerHTML = `<div class="pl-combo-empty">No matches</div>`;
    } else {
      list.innerHTML = filtered.map((o, i) =>
        `<div class="pl-combo-opt${o.active ? " selected" : ""}" data-id="${escapeHtml(o.id)}" data-name="${escapeHtml(o.name)}" data-idx="${i}">${escapeHtml(o.name)}<span style="color:var(--muted);font-size:10px;margin-left:4px">${o.suffix||""}</span></div>`
      ).join("");
    }
    focusedIdx = -1;
    list.classList.remove("hidden");
  }

  function selectOpt(id, name) {
    hidden.value = id;
    inp.value = name;
    inp.dataset.selectedId = id;
    list.classList.add("hidden");
    if (onSelectCallback) onSelectCallback(id, name);
  }

  function closeList() { list.classList.add("hidden"); }

  inp.addEventListener("focus", () => {
    if (!_isSpotifyPlaylistInput(inp.value)) { inp.select(); renderList(inp.value); }
  });
  inp.addEventListener("input", () => {
    if (_isSpotifyPlaylistInput(inp.value)) { closeList(); }
    else renderList(inp.value);
  });
  inp.addEventListener("keydown", e => {
    const val = inp.value.trim();
    if (e.key === "Enter") {
      e.preventDefault();
      if (_isSpotifyPlaylistInput(val)) {
        closeList();
        if (onSelectCallback) onSelectCallback(val, val);
        return;
      }
      const fo = list.querySelector(".pl-combo-opt.focused") || list.querySelector(".pl-combo-opt");
      if (fo) selectOpt(fo.dataset.id, fo.dataset.name);
      return;
    }
    const opts = list.querySelectorAll(".pl-combo-opt");
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focusedIdx = Math.min(focusedIdx + 1, opts.length - 1);
      opts.forEach((o, i) => o.classList.toggle("focused", i === focusedIdx));
      if (opts[focusedIdx]) opts[focusedIdx].scrollIntoView({block:"nearest"});
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focusedIdx = Math.max(focusedIdx - 1, 0);
      opts.forEach((o, i) => o.classList.toggle("focused", i === focusedIdx));
      if (opts[focusedIdx]) opts[focusedIdx].scrollIntoView({block:"nearest"});
    } else if (e.key === "Escape") {
      closeList(); inp.blur();
    }
  });
  inp.addEventListener("blur", () => setTimeout(closeList, 150));
  list.addEventListener("mousedown", e => {
    const opt = e.target.closest(".pl-combo-opt");
    if (opt) { e.preventDefault(); selectOpt(opt.dataset.id, opt.dataset.name); }
  });
}

// ── Artist picker ────────────────────────────────────────────────────────────
let _pickedArtistIdx = 0;
let _pickedArtistTrackId = null;
let _pickedArtistId = "";
let _pickedArtistName = "";

function clearPickedArtistSelection() {
  _pickedArtistIdx = 0;
  _pickedArtistTrackId = null;
  _pickedArtistId = "";
  _pickedArtistName = "";
}

function shouldPreservePickedArtist(trackId, artistList) {
  if (!_pickedArtistTrackId || !trackId || _pickedArtistTrackId !== trackId) return false;
  if (!_pickedArtistId && !_pickedArtistName) return false;
  const matchIdx = (artistList || []).findIndex(a =>
    (_pickedArtistId && (a.id || "") === _pickedArtistId) ||
    (_pickedArtistName && (a.name || "") === _pickedArtistName)
  );
  if (matchIdx < 0) return false;
  _pickedArtistIdx = matchIdx;
  return true;
}

function renderArtistPicker(artistList) {
  const row = document.getElementById("artist-picker-row");
  if (!row) return;
  if (!artistList || artistList.length <= 1) { row.style.display = "none"; return; }
  row.style.display = "flex";
  row.innerHTML = artistList.map((a, i) =>
    `<button class="artist-pick-btn${i === _pickedArtistIdx ? " active" : ""}"
      data-idx="${i}" data-id="${a.id}" data-name="${escapeHtml(a.name)}"
      onclick="pickArtist(${i})">${escapeHtml(a.name)}</button>`
  ).join("");
}

async function pickArtist(idx) {
  _pickedArtistIdx = idx;
  const row = document.getElementById("artist-picker-row");
  if (!row) return;
  row.querySelectorAll(".artist-pick-btn").forEach((b, i) => b.classList.toggle("active", i === idx));
  const btn = row.querySelector(`[data-idx="${idx}"]`);
  if (!btn) return;
  const artistId = btn.dataset.id;
  const artistName = btn.dataset.name;
  _pickedArtistTrackId = window._currentTrackId || null;
  _pickedArtistId = artistId || "";
  _pickedArtistName = artistName || "";
  // Show loading state
  document.getElementById("artist-about-card").innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading artist context</div>`;
  document.getElementById("artist-recs-card").innerHTML  = `<div class="spinner-wrap"><span class="spinner"></span>Loading artist analytics</div>`;
  const topCard = document.getElementById("artist-now-top-card");
  if (topCard) topCard.innerHTML = `<div class="spinner-wrap"><span class="spinner"></span>Loading top tracks</div>`;
  try {
    const track = window._lastTrack || {};
    const url = `/api/artist-info?artist_id=${encodeURIComponent(artistId)}&artist_name=${encodeURIComponent(artistName)}&track_id=${encodeURIComponent(track.id||"")}&track_name=${encodeURIComponent(track.name||"")}`;
    const r = await fetch(url);
    const info = await r.json();
    if (info.error) { showToast(info.error, true); return; }
    // Update genre bar for picked artist
    if (info.lastfm_tags && info.lastfm_tags.length) {
      const genresEl = document.getElementById("now-genres");
      if (genresEl) {
        genresEl.innerHTML = info.lastfm_tags.slice(0, 5).map(g =>
          `<span class="genre">${escapeHtml(g)}</span>`).join("");
        syncPlayerBarMarquees();
      }
    }
    renderArtistAboutV2(info, window._lastFavArtists || []);
    renderArtistRecsV2(info, window._lastFavArtists || []);
  } catch(e) { showToast("Failed to load artist info.", true); }
}

// ── Queue genre fetch (Last.fm, cached, no Spotify search) ──────────────────
let _queueGenreFetched = false;
async function fetchQueueGenres(tracks) {
  if (!tracks || !tracks.length) return;
  window._trackGenreCache = window._trackGenreCache || {};
  for (const t of tracks) {
    if (t && t.id && Array.isArray(t.genres) && t.genres.length) {
      window._trackGenreCache[t.id] = t.genres;
    }
  }
  // Collect missing track ids with their primary artist
  const _gc = window._trackGenreCache || {};
  const needed = [];
  for (const t of tracks) {
    if (_gc[t.id] && _gc[t.id].length) continue; // already cached
    const artist = ((t.artists || t.artist || "") + "").split(",")[0].trim();
    if (artist && t.id) needed.push({ id: t.id, artist });
  }
  if (!needed.length) return;
  try {
    const r = await fetch("/api/queue-genres", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracks: needed }),
    });
    const d = await r.json();
    const byTrack = d.by_track || {};
    for (const [trackId, genres] of Object.entries(byTrack)) {
      if (genres && genres.length) window._trackGenreCache[trackId] = genres;
    }
    // Re-render visible lists that consume the shared track genre cache.
    renderQueue();
    if (_likedSongsTracks && _likedSongsTracks.length) renderSidebarLikedSongs(_likedSongsTracks);
  } catch(e) { console.error("Queue genres fetch failed", e); }
}

refresh();
const STATUS_REFRESH_MS = Math.max(3000, (window.APP_CONFIG && window.APP_CONFIG.pollIntervalMs) || 3000);
let _pollInterval = setInterval(refresh, STATUS_REFRESH_MS);
let _pollPaused = false;

function togglePolling() {
  _pollPaused = !_pollPaused;
  const badge = document.getElementById("poll-pause-badge");
  const btn   = document.getElementById("poll-toggle-btn");
  if (_pollPaused) {
    clearInterval(_pollInterval);
    _pollInterval = null;
    if (badge) badge.classList.add("visible");
    if (btn)   { btn.textContent = "Resume"; btn.classList.add("paused"); }
  } else {
    _pollInterval = setInterval(refresh, STATUS_REFRESH_MS);
    refresh(); // immediate catch-up poll
    if (badge) badge.classList.remove("visible");
    if (btn)   { btn.textContent = "Pause"; btn.classList.remove("paused"); }
  }
}

// ---------- Mini widget ----------
function openWidget() {
  const popup = window.open("/widget", "discover-widget",
    "width=320,height=160,menubar=no,toolbar=no,location=no,status=no,resizable=yes");
  if (!popup) showToast("Allow popups for this site to use the widget.");
}
