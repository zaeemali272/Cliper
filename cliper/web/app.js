const $ = (s) => document.querySelector(s);

let currentUser = null;
let currentQuotas = null;

const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const errorMsg = data.detail || r.statusText;
    if (r.status === 401 && !path.includes("/api/auth/me")) {
      openAuthModal("login", errorMsg);
    } else if (r.status === 429) {
      openUpgradeModal(errorMsg);
    }
    throw new Error(errorMsg);
  }
  return data;
};

let job = null;         // current job state
let pollTimer = null;
let player = null;      // { seek(t) }
let hoverX = null;

const fmt = (s) => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
};

// ------------------------------------------------------------------ AUTH & SAAS FLOW

async function checkAuth() {
  try {
    const res = await api("/api/auth/me");
    currentUser = res.user;
    currentQuotas = res.quotas;
    renderUserArea();
  } catch (err) {
    currentUser = null;
    currentQuotas = null;
    renderUserArea();
  }
}

function renderUserArea() {
  const el = $("#user-area");
  if (currentUser) {
    const initial = (currentUser.email || "U").charAt(0).toUpperCase();
    el.innerHTML = `
      <div class="user-menu-wrapper">
        <button class="avatar-btn" onclick="toggleUserDropdown(event)" title="${escapeHtml(currentUser.email)}">
          <div class="avatar-circle">${initial}</div>
        </button>
        <div id="user-dropdown" class="user-dropdown hidden">
          <div class="user-dropdown-header">
            <div class="user-email-text">${escapeHtml(currentUser.email)}</div>
            <div class="user-status-pill">${escapeHtml(currentUser.status_label)}</div>
          </div>
          <div class="user-dropdown-divider"></div>
          <button class="user-dropdown-item" onclick="openCookieModal()">
            <span>🍪 YouTube Cookies</span>
          </button>
          ${!currentUser.is_pro ? `
          <button class="user-dropdown-item" onclick="openUpgradeModal()">
            <span>⚡ Upgrade to Pro</span>
          </button>` : ''}
          <div class="user-dropdown-divider"></div>
          <button class="user-dropdown-item danger" onclick="handleLogout()">
            <span>🚪 Logout</span>
          </button>
        </div>
      </div>
    `;
  } else {
    el.innerHTML = `
      <button class="tab" onclick="openAuthModal('login')">Log In</button>
      <button class="primary" style="padding:6px 14px;font-size:13px;" onclick="openAuthModal('signup')">Sign Up Free</button>
    `;
  }
}

window.toggleUserDropdown = (e) => {
  e.stopPropagation();
  const dropdown = $("#user-dropdown");
  if (dropdown) dropdown.classList.toggle("hidden");
};

document.addEventListener("click", (e) => {
  if (!e.target.closest(".user-menu-wrapper")) {
    const dropdown = $("#user-dropdown");
    if (dropdown) dropdown.classList.add("hidden");
  }
});

let activeAuthMode = "login";

function openAuthModal(mode = "login", error = "") {
  activeAuthMode = mode;
  switchAuthTab(mode);
  $("#auth-error").classList.toggle("hidden", !error);
  if (error) $("#auth-error").textContent = error;
  $("#auth-modal").classList.remove("hidden");
}

function closeAuthModal() {
  $("#auth-modal").classList.add("hidden");
}

function switchAuthTab(mode) {
  activeAuthMode = mode;
  $("#tab-login-btn").classList.toggle("active", mode === "login");
  $("#tab-signup-btn").classList.toggle("active", mode === "signup");
  $("#auth-submit-btn").textContent = mode === "login" ? "Log In" : "Sign Up (Start Free 7-Day Trial)";
  $("#auth-error").classList.add("hidden");
}

async function handleAuthSubmit(e) {
  e.preventDefault();
  const email = $("#auth-email").value.trim();
  const password = $("#auth-password").value;
  const btn = $("#auth-submit-btn");
  
  btn.disabled = true;
  $("#auth-error").classList.add("hidden");
  
  try {
    const endpoint = activeAuthMode === "signup" ? "/api/auth/signup" : "/api/auth/login";
    const res = await api(endpoint, {
      method: "POST",
      body: JSON.stringify({ email, password })
    });
    currentUser = res.user;
    closeAuthModal();
    await checkAuth();
    loadRecent();
  } catch (err) {
    $("#auth-error").textContent = err.message;
    $("#auth-error").classList.remove("hidden");
  } finally {
    btn.disabled = false;
  }
}

async function handleLogout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {}
  currentUser = null;
  renderUserArea();
  $("#recent").innerHTML = "";
  $("#video-section").classList.add("hidden");
}

function openUpgradeModal(reason = "") {
  $("#upgrade-modal").classList.remove("hidden");
}

function closeUpgradeModal() {
  $("#upgrade-modal").classList.add("hidden");
}

async function triggerStripeCheckout() {
  if (!currentUser) {
    closeUpgradeModal();
    openAuthModal("signup", "Create a free account to upgrade.");
    return;
  }
  try {
    const res = await api("/api/billing/checkout", {
      method: "POST",
      body: JSON.stringify({
        success_url: window.location.origin + "/?stripe=success",
        cancel_url: window.location.origin + "/?stripe=cancel"
      })
    });
    if (res.url) {
      window.location.href = res.url;
    }
  } catch (err) {
    alert("Stripe Checkout: " + err.message);
  }
}

// ------------------------------------------------------------------ flow
async function analyze(url, force) {
  if (!currentUser) {
    openAuthModal("signup", "Please sign up or log in to analyze videos.");
    return;
  }
  $("#analyze-btn").disabled = true;
  $("#prog-error").classList.add("hidden");
  $("#video-section").classList.add("hidden");
  $("#progress-card").classList.remove("hidden");
  $("#clips").innerHTML = ""; renderedVideoFor = null;
  try {
    job = await api("/api/analyze", { method: "POST", body: JSON.stringify({ url, force: !!force }) });
    history.replaceState(null, "", "#" + job.id);
    poll();
    checkAuth(); // update remaining quota count
  } catch (err) {
    showError(err.message);
  } finally {
    $("#analyze-btn").disabled = false;
  }
}

$("#reanalyze").addEventListener("click", () => job && analyze(job.url, true));

$("#url-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  analyze($("#url").value, false);
});

$("#generate-btn").addEventListener("click", async () => {
  if (!currentUser) {
    openAuthModal("signup", "Please sign up or log in to generate clips.");
    return;
  }
  const num = (id) => ($(id).value === "" ? null : +$(id).value);
  const body = {
    count: +$("#opt-count").value, min_len: +$("#opt-min").value, max_len: +$("#opt-max").value,
    layout: $("#opt-layout").value, captions: $("#opt-captions").value, mode: $("#opt-mode").value || null,
    speed: num("#opt-speed"), pitch: num("#opt-pitch"), max_height: num("#opt-quality"),
    punch: $("#opt-punch").checked, title: $("#opt-title").checked, progress: $("#opt-progress").checked,
    grade: $("#opt-grade").checked, mirror: $("#opt-mirror").checked,
  };
  $("#generate-btn").disabled = true;
  try {
    job = await api(`/api/jobs/${job.id}/generate`, { method: "POST", body: JSON.stringify(body) });
    $("#clips-card").classList.remove("hidden");
    poll();
    checkAuth(); // update remaining quota count
  } catch (err) {
    alert(err.message);
    $("#generate-btn").disabled = false;
  }
});

function poll() {
  clearTimeout(pollTimer);
  const tick = async () => {
    try {
      job = await api(`/api/jobs/${job.id}`);
    } catch (err) { showError(err.message); return; }
    render();
    if (job.status === "analyzing" || job.status === "generating") pollTimer = setTimeout(tick, 1500);
    else loadRecent();
  };
  tick();
}

function openCookieModal() {
  $("#cookie-modal").classList.remove("hidden");
  fetchCookieStatus();
}

function closeCookieModal() {
  $("#cookie-modal").classList.add("hidden");
}

async function fetchCookieStatus() {
  try {
    const res = await api("/api/cookies");
    const msg = $("#cookie-msg");
    if (res.has_cookies) {
      msg.textContent = "✓ Active YouTube cookies are saved on the server!";
      msg.style.color = "var(--ok)";
    } else {
      msg.textContent = "No cookies saved yet.";
      msg.style.color = "var(--muted)";
    }
  } catch {}
}

async function handleCookieSubmit(e) {
  e.preventDefault();
  const text = $("#cookie-text").value.trim();
  if (!text) return;
  try {
    await api("/api/cookies", {
      method: "POST",
      body: JSON.stringify({ cookies: text })
    });
    $("#cookie-msg").textContent = "✓ YouTube cookies saved successfully!";
    $("#cookie-msg").style.color = "var(--ok)";
    setTimeout(closeCookieModal, 1500);
  } catch (err) {
    alert("Cookie save failed: " + err.message);
  }
}

async function clearServerCookies() {
  if (!confirm("Clear saved server cookies?")) return;
  try {
    await api("/api/cookies", { method: "DELETE" });
    $("#cookie-text").value = "";
    $("#cookie-msg").textContent = "Cookies cleared.";
    $("#cookie-msg").style.color = "var(--muted)";
  } catch (err) {
    alert("Clear cookies failed: " + err.message);
  }
}

function showError(msg) {
  $("#progress-card").classList.remove("hidden");
  $("#prog-error").textContent = msg;
  $("#prog-error").classList.remove("hidden");
  if (msg.toLowerCase().includes("sign in to confirm") || msg.toLowerCase().includes("not a bot") || msg.toLowerCase().includes("cookies")) {
    openCookieModal();
  }
}

// ------------------------------------------------------------------ render
let renderedVideoFor = null;
function render() {
  const busy = job.status === "analyzing" || job.status === "generating";
  $("#progress-card").classList.toggle("hidden", !busy && job.status !== "error");
  $("#prog-msg").textContent = job.message || job.status;
  $("#prog-pct").textContent = Math.round((job.progress || 0) * 100) + "%";
  $("#prog-fill").style.width = Math.round((job.progress || 0) * 100) + "%";
  if (job.status === "error") showError(job.error || "Something went wrong");
  const logEl = $("#prog-log");
  logEl.innerHTML = (job.log || []).map((l) => `<li><time>${new Date(l.t * 1000).toLocaleTimeString()}</time><span>${l.msg}</span></li>`).join("");
  logEl.scrollTop = logEl.scrollHeight;

  if (job.signal) {
    $("#video-section").classList.remove("hidden");
    if (renderedVideoFor !== job.id) { renderedVideoFor = job.id; renderVideo(); }
    drawChart();
  }
  $("#generate-btn").disabled = busy;

  $("#thumb").src = job.thumbnail || "";
  $("#title").textContent = job.title || job.url;
  $("#uploader").textContent = job.uploader || job.platform;
  $("#duration").textContent = fmt(job.duration || 0);
  $("#source").textContent = job.platform || "video";
  $("#mode").textContent = job.mode || "auto";
  $("#mode").title = job.mode_reason || "";

  renderClips();
}

function renderVideo() {
  const wrap = $("#player");
  wrap.innerHTML = "";
  if (job.video_id && job.platform === "youtube") {
    const iframe = document.createElement("iframe");
    iframe.src = `https://www.youtube-nocookie.com/embed/${job.video_id}?enablejsapi=1&rel=0`;
    iframe.allow = "autoplay; encrypted-media";
    wrap.appendChild(iframe);
    player = {
      seek(t) {
        iframe.contentWindow?.postMessage(JSON.stringify({ event: "command", func: "seekTo", args: [t, true] }), "*");
        iframe.contentWindow?.postMessage(JSON.stringify({ event: "command", func: "playVideo", args: [] }), "*");
      },
    };
  } else if (job.video_id && job.platform === "twitch") {
    const iframe = document.createElement("iframe");
    const parent = location.hostname || "localhost";
    iframe.src = `https://player.twitch.tv/?video=${job.video_id}&parent=${parent}&autoplay=false`;
    iframe.allow = "autoplay; fullscreen";
    wrap.appendChild(iframe);
    player = { seek(t) { iframe.contentWindow?.postMessage({ jsonrpc: "2.0", method: "seek", params: [t] }, "*"); } };
  } else {
    wrap.innerHTML = `<div class="ph">Preview player not available for this source — click clips below to watch</div>`;
    player = null;
  }
}

function drawChart() {
  const canvas = $("#chart");
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * window.devicePixelRatio;
  canvas.height = rect.height * window.devicePixelRatio;
  ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
  const W = rect.width, H = rect.height;

  const sig = job.signal || [];
  const wins = job.windows || [];
  const peaks = job.peaks || [];
  if (!sig.length) return;

  ctx.clearRect(0, 0, W, H);

  // clip window overlays
  for (const w of wins) {
    const x1 = (w.start / job.duration) * W;
    const x2 = (w.end / job.duration) * W;
    ctx.fillStyle = "rgba(77,163,255,0.18)";
    ctx.fillRect(x1, 0, x2 - x1, H);
    ctx.strokeStyle = "rgba(77,163,255,0.6)";
    ctx.lineWidth = 1;
    ctx.strokeRect(x1 + 0.5, 0.5, x2 - x1, H - 1);
  }

  // heatmap signal line / area
  ctx.beginPath();
  ctx.moveTo(0, H);
  for (let i = 0; i < sig.length; i++) {
    const x = (i / (sig.length - 1)) * W;
    const y = H - sig[i] * (H - 10) - 5;
    ctx.lineTo(x, y);
  }
  ctx.lineTo(W, H);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, "rgba(255,77,109,0.5)");
  grad.addColorStop(1, "rgba(255,77,109,0.02)");
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  for (let i = 0; i < sig.length; i++) {
    const x = (i / (sig.length - 1)) * W;
    const y = H - sig[i] * (H - 10) - 5;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = "#ff4d6d";
  ctx.lineWidth = 2;
  ctx.stroke();

  // top moment peak dots
  for (const p of peaks) {
    const x = (p.time / job.duration) * W;
    const idx = Math.min(sig.length - 1, Math.floor((p.time / job.duration) * sig.length));
    const y = H - (sig[idx] || 0) * (H - 10) - 5;
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fillStyle = "#ffd84d";
    ctx.fill();
    ctx.strokeStyle = "#000";
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  // hover cursor
  if (hoverX != null) {
    ctx.strokeStyle = "rgba(255,255,255,0.4)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(hoverX, 0); ctx.lineTo(hoverX, H); ctx.stroke();
  }
}

const canvas = $("#chart");
canvas.addEventListener("mousemove", (e) => {
  const rect = canvas.getBoundingClientRect();
  hoverX = e.clientX - rect.left;
  const t = (hoverX / rect.width) * (job?.duration || 0);
  const tip = $("#chart-tip");
  tip.style.left = hoverX + "px";
  tip.textContent = fmt(t);
  tip.classList.remove("hidden");
  drawChart();
});
canvas.addEventListener("mouseleave", () => {
  hoverX = null;
  $("#chart-tip").classList.add("hidden");
  drawChart();
});
canvas.addEventListener("click", (e) => {
  if (!job || !job.duration) return;
  const rect = canvas.getBoundingClientRect();
  const t = ((e.clientX - rect.left) / rect.width) * job.duration;
  player?.seek(t);
});

function renderClips() {
  const clips = job.clips || [];
  if (!clips.length) { $("#clips-card").classList.add("hidden"); return; }
  $("#clips-card").classList.remove("hidden");
  $("#clips-count").textContent = `(${clips.filter((c) => c.status === "done").length}/${clips.length})`;
  const hasDone = clips.some((c) => c.status === "done");
  const zip = $("#zip-link");
  zip.classList.toggle("hidden", !hasDone);
  zip.href = `/api/jobs/${job.id}/clips.zip`;

  const container = $("#clips");
  container.innerHTML = clips.map((c) => {
    try {
      const isLandscape = c.layout === "original";
      let seoBtnHtml = '';
      if (c.seo) {
        const seoTitle = escapeHtml((c.seo && c.seo.title) || '');
        const seoDesc = escapeHtml((c.seo && c.seo.description) || '');
        const seoTags = escapeHtml((c.seo && Array.isArray(c.seo.hashtags)) ? c.seo.hashtags.join(' ') : (c.seo && c.seo.hashtags) || '');
        seoBtnHtml = `
        <div class="seo-container">
          <button class="seo-btn" onclick="event.stopPropagation(); toggleSeoPopup(this)">⚡ SEO Tags</button>
          <div class="seo-popup">
            <div class="seo-pop-header">
              <span>SEO Copy Pack</span>
              <button class="seo-copy-all" onclick="copyAllSeo(this)">Copy All</button>
            </div>
            <div class="seo-field">
              <div class="seo-label-row"><span>TITLE</span><button class="seo-item-copy" onclick="copyText(this)">Copy</button></div>
              <div class="seo-val">${seoTitle}</div>
            </div>
            <div class="seo-field">
              <div class="seo-label-row"><span>DESCRIPTION</span><button class="seo-item-copy" onclick="copyText(this)">Copy</button></div>
              <div class="seo-val">${seoDesc}</div>
            </div>
            <div class="seo-field">
              <div class="seo-label-row"><span>TAGS</span><button class="seo-item-copy" onclick="copyText(this)">Copy</button></div>
              <div class="seo-val">${seoTags}</div>
            </div>
          </div>
        </div>`;
      }

      let media = `<div class="ph"><div class="spin"></div> &nbsp; ${escapeHtml(c.status || 'rendering')}</div>`;
      if (c.status === "done") {
        media = `<video src="/api/jobs/${job.id}/clips/${c.file}#t=0.1" controls preload="metadata"></video>`;
      } else if (c.status === "error") {
        media = `<div class="ph err">${escapeHtml(c.error || "Failed")}</div>`;
      }

      const rankStr = c.rank != null ? `#${c.rank}` : '';
      const startStr = c.start != null ? fmt(c.start) : '0:00';
      const endStr = c.end != null ? fmt(c.end) : '0:00';
      const durationStr = (c.start != null && c.end != null) ? ` (${Math.round(c.end - c.start)}s)` : '';

      return `<div class="clip ${isLandscape ? 'landscape' : ''}">
        ${media}
        ${seoBtnHtml}
        <div class="info">
          <div class="row"><span class="rank">${rankStr}</span> <span>${startStr} – ${endStr}${durationStr}</span></div>
          ${c.hook ? `<div class="hook" title="${escapeHtml(c.hook)}">“${escapeHtml(c.hook)}”</div>` : ""}
          ${c.status === "done" ? `<div class="actions"><a class="btn" href="/api/jobs/${job.id}/clips/${c.file}" download>Download MP4</a></div>` : ""}
        </div>
      </div>`;
    } catch (err) {
      console.error("Error rendering clip:", err, c);
      return `<div class="clip"><div class="ph err">Error displaying clip</div></div>`;
    }
  }).join("");
}

function escapeHtml(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

window.toggleSeoPopup = (btn) => {
  const pop = btn.nextElementSibling;
  const isShow = pop.classList.contains('force-show');
  document.querySelectorAll('.seo-popup').forEach(p => p.classList.remove('force-show'));
  if (!isShow) pop.classList.add('force-show');
};

document.addEventListener('click', (e) => {
  if (!e.target.closest('.seo-container')) {
    document.querySelectorAll('.seo-popup').forEach(p => p.classList.remove('force-show'));
  }
});

window.copyText = (btn) => {
  const val = btn.closest('.seo-field').querySelector('.seo-val').textContent;
  navigator.clipboard.writeText(val);
  const orig = btn.textContent;
  btn.textContent = 'Copied!';
  setTimeout(() => btn.textContent = orig, 1500);
};

window.copyAllSeo = (btn) => {
  const pop = btn.closest('.seo-popup');
  const vals = Array.from(pop.querySelectorAll('.seo-val')).map(v => v.textContent);
  const text = `TITLE:\n${vals[0]}\n\nDESCRIPTION:\n${vals[1]}\n\nTAGS:\n${vals[2]}`;
  navigator.clipboard.writeText(text);
  const orig = btn.textContent;
  btn.textContent = 'Copied All!';
  setTimeout(() => btn.textContent = orig, 1500);
};

async function loadRecent() {
  if (!currentUser) {
    $("#recent").innerHTML = '<li class="hint">Log in to view your recent jobs.</li>';
    return;
  }
  try {
    const list = await api("/api/jobs");
    $("#recent").innerHTML = list.map((r) => `
      <li>
        <a href="#${r.id}" onclick="open('${r.id}')">${r.title || r.url}</a>
        <span class="st">${r.status}</span>
        <button class="x" onclick="delJob('${r.id}')">✕</button>
      </li>
    `).join("") || '<li class="hint">No recent jobs yet. Paste a URL above to start!</li>';
  } catch (err) {
    $("#recent").innerHTML = `<li class="error">${err.message}</li>`;
  }
}

async function delJob(id) {
  try {
    await api(`/api/jobs/${id}`, { method: "DELETE" });
    if (job?.id === id) { job = null; $("#video-section").classList.add("hidden"); }
    loadRecent();
  } catch (err) { alert(err.message); }
}

async function open(id) {
  try {
    job = await api(`/api/jobs/${id}`);
    history.replaceState(null, "", "#" + id);
    $("#url").value = job.url;
    $("#clips").innerHTML = "";
    renderedVideoFor = null;
    poll();
  } catch (err) { showError(err.message); }
}

// remember generate settings
const OPTS = ["opt-count", "opt-min", "opt-max", "opt-mode", "opt-layout", "opt-captions", "opt-speed", "opt-pitch",
  "opt-quality", "opt-punch", "opt-title", "opt-progress", "opt-grade", "opt-mirror"];
try {
  const saved = JSON.parse(localStorage.getItem("cliper.opts") || "{}");
  for (const id of OPTS) { const el = document.getElementById(id); if (el && id in saved) { if (el.type === "checkbox") el.checked = saved[id]; else el.value = saved[id]; } }
} catch {}
for (const id of OPTS) document.getElementById(id)?.addEventListener("change", () => {
  const out = {}; for (const k of OPTS) { const el = document.getElementById(k); if (el) out[k] = el.type === "checkbox" ? el.checked : el.value; }
  try { localStorage.setItem("cliper.opts", JSON.stringify(out)); } catch {}
});

// INITIALIZE APP
checkAuth().then(() => {
  loadRecent();
  if (location.hash.length > 1) open(location.hash.slice(1));
});

// ------------------------------------------------------------------ tabs
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  $("#tab-clips").classList.toggle("hidden", b.dataset.tab !== "clips");
  $("#tab-check").classList.toggle("hidden", b.dataset.tab !== "check");
  if (b.dataset.tab === "check") renderCheckList();
}));

// ------------------------------------------------------------------ copyright check
function reportHtml(r) {
  const music = r.music && r.music.length
    ? `<div class="music">🎵 ${r.music.map((m) => m.url ? `<a href="${m.url}" target="_blank">${m.title} – ${m.artist}</a>` : `${m.title} – ${m.artist}`).join(", ")}</div>` : "";
  return `<div><span class="badge ${r.level}">${r.level.toUpperCase()} risk · ${Math.round(r.score * 100)}</span>
    ${r.visual != null ? ` &nbsp; visual ${Math.round(r.visual * 100)}%` : ""}${r.audio != null ? ` · audio ${Math.round(r.audio * 100)}%` : ""}</div>
    ${music}<ul>${(r.reasons || []).map((x) => `<li>${x}</li>`).join("")}</ul><p class="hint">${r.note}</p>`;
}

function renderCheckList() {
  const list = $("#check-clips");
  const clips = (job && job.clips || []).filter((c) => c.status === "done");
  $("#check-clips-card").classList.toggle("hidden", clips.length === 0);
  $("#check-job-title").textContent = job ? job.title || "" : "";
  list.innerHTML = clips.map((c) => `<div class="check-row" data-file="${c.file}">
      <video src="/api/jobs/${job.id}/clips/${c.file}#t=0.5" preload="metadata" muted></video>
      <div><b>#${c.rank}</b> ${fmt(c.start)} – ${fmt(c.end)}${c.hook ? ` · “${c.hook}”` : ""}
        <div class="res">${c.check ? reportHtml(c.check) : '<span class="hint">not checked yet</span>'}</div></div>
      <button class="chk" data-file="${c.file}">Check</button></div>`).join("");
  list.querySelectorAll(".chk").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "Checking…";
    const row = b.closest(".check-row");
    try {
      const r = await api(`/api/jobs/${job.id}/clips/${b.dataset.file}/check`, { method: "POST" });
      row.querySelector(".res").innerHTML = reportHtml(r);
      const c = job.clips.find((x) => x.file === b.dataset.file); if (c) c.check = r;
    } catch (e) { row.querySelector(".res").innerHTML = `<span class="error">${e.message}</span>`; }
    b.disabled = false; b.textContent = "Re-check";
  }));
}

$("#chk-btn").addEventListener("click", async () => {
  const f = $("#chk-file").files[0];
  if (!f) return alert("Choose a video file first");
  const fd = new FormData();
  fd.append("file", f); fd.append("source_url", $("#chk-url").value); fd.append("source_start", $("#chk-start").value || 0);
  fd.append("speed", $("#chk-speed").value || 1);
  $("#chk-btn").disabled = true; $("#chk-btn").textContent = "Checking… (up to a minute)";
  $("#chk-result").classList.add("hidden");
  try {
    const r = await fetch("/api/check", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    $("#chk-result").innerHTML = reportHtml(await r.json());
  } catch (e) { $("#chk-result").innerHTML = `<span class="error">${e.message}</span>`; }
  $("#chk-result").classList.remove("hidden");
  $("#chk-btn").disabled = false; $("#chk-btn").textContent = "Check";
});
if (new URLSearchParams(location.search).get("tab") === "check") document.querySelector('.tab[data-tab="check"]').click();
