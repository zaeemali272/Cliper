const $ = (s) => document.querySelector(s);
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
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

// ------------------------------------------------------------------ flow
async function analyze(url, force) {
  $("#analyze-btn").disabled = true;
  $("#prog-error").classList.add("hidden");
  $("#video-section").classList.add("hidden");
  $("#progress-card").classList.remove("hidden");
  $("#clips").innerHTML = ""; renderedVideoFor = null;
  try {
    job = await api("/api/analyze", { method: "POST", body: JSON.stringify({ url, force: !!force }) });
    history.replaceState(null, "", "#" + job.id);
    poll();
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

function showError(msg) {
  $("#progress-card").classList.remove("hidden");
  $("#prog-error").textContent = msg;
  $("#prog-error").classList.remove("hidden");
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
  if (job.clips && job.clips.length) renderClips();
  const ck = job.id + "|" + (job.clips || []).filter((c) => c.status === "done").map((c) => c.file).join(",");
  if (ck !== lastCheckKey) { lastCheckKey = ck; renderCheckList(); }
}
let lastCheckKey = "";

function renderVideo() {
  $("#thumb").src = job.thumbnail || "";
  $("#title").textContent = job.title || job.url;
  $("#uploader").textContent = job.uploader || job.platform;
  $("#duration").textContent = fmt(job.duration);
  $("#source").textContent = { youtube_heatmap: "YouTube most-replayed", chat_density: "chat activity", audio_energy: "audio energy" }[job.signal_source] || job.signal_source;
  $("#mode").textContent = "type: " + (job.mode || "?"); $("#mode").title = job.mode_reason || "";
  $("#opt-mode").querySelector('option[value=""]').textContent = `Auto (detected: ${job.mode || "?"})`;
  $("#highlights").innerHTML = (job.highlights || []).map((h, i) =>
    `<span class="chip" data-t="${h.time}"><b>#${i + 1}</b> ${fmt(h.time)}</span>`).join("");
  $("#highlights").querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => seek(+c.dataset.t)));
  $("#caption-hint").textContent = job.has_captions
    ? "YouTube auto-captions found — they'll be burned in word-by-word."
    : job.whisper_available ? "No platform captions; Whisper (local) will transcribe each clip." :
      "No captions for this video. Install Whisper (uv sync --extra whisper) for free local transcription, or leave captions off.";
  mountPlayer();
  $("#clips-card").classList.toggle("hidden", !(job.clips && job.clips.length));
}

function mountPlayer() {
  const wrap = $("#player");
  wrap.innerHTML = "";
  if (job.platform === "youtube" && job.video_id) {
    const f = document.createElement("iframe");
    f.allow = "autoplay; encrypted-media; picture-in-picture";
    f.setAttribute("width", "100%"); f.setAttribute("height", "100%");
    f.src = `https://www.youtube.com/embed/${job.video_id}?enablejsapi=1&rel=0&origin=${location.origin}`;
    wrap.appendChild(f);
    let yt = null;
    const ready = () => { yt = new YT.Player(f); };
    if (window.YT && YT.Player) ready();
    else { window.onYouTubeIframeAPIReady = ready; if (!$("#yt-api")) { const s = document.createElement("script"); s.id = "yt-api"; s.src = "https://www.youtube.com/iframe_api"; document.head.appendChild(s); } }
    player = { seek: (t) => { if (yt && yt.seekTo) { yt.seekTo(t, true); yt.playVideo(); } else f.src = `https://www.youtube.com/embed/${job.video_id}?enablejsapi=1&rel=0&autoplay=1&start=${Math.floor(t)}`; } };
  } else if (job.platform === "twitch" && job.video_id) {
    const vid = String(job.video_id).replace(/^v/, "");
    const src = (t) => `https://player.twitch.tv/?video=v${vid}&parent=${location.hostname}&autoplay=${t != null}${t != null ? "&time=" + Math.floor(t / 3600) + "h" + Math.floor(t % 3600 / 60) + "m" + Math.floor(t % 60) + "s" : ""}`;
    const f = document.createElement("iframe");
    f.allow = "autoplay; fullscreen"; f.src = src(null);
    wrap.appendChild(f);
    player = { seek: (t) => { f.src = src(t); } };
  } else {
    wrap.innerHTML = `<div class="ph">No embedded player</div>`;
    player = null;
  }
}

function seek(t) { if (player) player.seek(t); }

// ------------------------------------------------------------------ chart
const canvas = $("#chart");
function drawChart() {
  const sig = job.signal, n = sig.length, dur = job.duration || 1;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  const x = (t) => (t / dur) * W;

  // planned clip windows
  for (const c of job.clips || []) {
    ctx.fillStyle = c.status === "done" ? "rgba(77,209,143,.28)" : c.status === "error" ? "rgba(255,77,109,.25)" : "rgba(77,163,255,.28)";
    ctx.fillRect(x(c.start), 0, Math.max(2, x(c.end) - x(c.start)), H);
    ctx.fillStyle = "#cfd6e6"; ctx.font = "11px system-ui";
    ctx.fillText("#" + c.rank, x(c.start) + 3, 12);
  }
  // signal
  const g = ctx.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, "#ff4d6d"); g.addColorStop(1, "rgba(255,143,77,.15)");
  ctx.fillStyle = g;
  ctx.beginPath(); ctx.moveTo(0, H);
  for (let i = 0; i < n; i++) {
    const px = (i / (n - 1)) * W, py = H - Math.pow(sig[i], 0.8) * (H - 18);
    ctx.lineTo(px, py);
  }
  ctx.lineTo(W, H); ctx.closePath(); ctx.fill();
  // top moments
  ctx.fillStyle = "#ffd84d";
  for (const h of job.highlights || []) { ctx.beginPath(); ctx.arc(x(h.time), H - Math.pow(h.score, 0.8) * (H - 18), 3.5, 0, 7); ctx.fill(); }
  // hover
  if (hoverX != null) { ctx.fillStyle = "#fff8"; ctx.fillRect(hoverX, 0, 1, H); }
}
canvas.addEventListener("mousemove", (e) => {
  const r = canvas.getBoundingClientRect();
  hoverX = e.clientX - r.left;
  const t = (hoverX / r.width) * (job.duration || 0);
  const tip = $("#chart-tip");
  tip.style.left = hoverX + "px"; tip.textContent = fmt(t); tip.classList.remove("hidden");
  drawChart();
});
canvas.addEventListener("mouseleave", () => { hoverX = null; $("#chart-tip").classList.add("hidden"); drawChart(); });
canvas.addEventListener("click", (e) => {
  const r = canvas.getBoundingClientRect();
  seek(((e.clientX - r.left) / r.width) * (job.duration || 0));
});
window.addEventListener("resize", () => job && job.signal && drawChart());

// ------------------------------------------------------------------ clips
const ICON_DL = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 19h16"/></svg>`;
const ICON_C = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M15 9.5a4 4 0 1 0 0 5"/></svg>`;
function badgeHtml(r) {
  const parts = [];
  if (r.music && r.music.length) parts.push("🎵 " + r.music.map((m) => m.title).join(", "));
  if (r.visual != null) parts.push(`visual ${Math.round(r.visual * 100)}%`);
  if (r.audio != null) parts.push(`audio ${Math.round(r.audio * 100)}%`);
  return `<span class="badge ${r.level}" title="${(r.reasons || []).join("\n").replace(/"/g, "&quot;")}">${r.level.toUpperCase()} risk</span> <span class="hint">${parts.join(" · ")}</span>`;
}
function renderClips() {
  $("#clips-card").classList.remove("hidden");
  const done = job.clips.filter((c) => c.status === "done").length;
  $("#clips-count").textContent = `${done}/${job.clips.length}`;
  $("#zip-link").classList.toggle("hidden", done === 0);
  $("#zip-link").href = `/api/jobs/${job.id}/clips.zip`;
  $("#outpath").textContent = job.output_dir ? `Saved to ${job.output_dir}` : "";
  const landscape = job.options && job.options.layout === "original";
  const grid = $("#clips");
  for (const c of job.clips) {
    let el = grid.querySelector(`[data-rank="${c.rank}"]`);
    if (!el) {
      el = document.createElement("div"); el.className = "clip" + (landscape ? " landscape" : ""); el.dataset.rank = c.rank;
      grid.appendChild(el);
    }
    const key = c.status + "|" + (c.framing || "");
    if (el.dataset.status === key) continue;
    el.dataset.status = key;
    const src = `/api/jobs/${job.id}/clips/${c.file}`;
    const media = c.status === "done"
      ? `<video src="${src}#t=0.5" controls preload="metadata" playsinline></video>`
      : `<div class="ph">${c.status === "error" ? "failed" : c.status === "rendering" ? '<div class="spin"></div>' : "queued"}</div>`;
    el.innerHTML = `${media}<div class="info">
      <div class="row"><span class="rank">#${c.rank}</span><span>${fmt(c.start)} – ${fmt(c.end)} · ${Math.round(c.end - c.start)}s</span></div>
      ${c.text ? `<div class="hook" title="${(c.text || "").replace(/"/g, "&quot;")}">“${c.text}”</div>` : ""}
      <div class="row"><span>score ${(c.score * 100).toFixed(0)}</span><span class="chip" data-t="${c.start}">▶ source</span></div>
      ${c.status === "done" ? `<div class="actions"><a class="btn dl" href="${src}" download title="Download">${ICON_DL} MP4</a><button class="btn chk" title="Copyright check">${ICON_C} Check</button></div><div class="chkres">${c.check ? badgeHtml(c.check) : ""}</div>` : ""}
      ${c.error ? `<div class="err">${c.error}</div>` : ""}</div>`;
    el.querySelector(".chip").addEventListener("click", () => seek(c.start));
    const chk = el.querySelector(".chk");
    if (chk) chk.addEventListener("click", async () => {
      chk.disabled = true; chk.innerHTML = `${ICON_C} Checking…`;
      try {
        const r = await api(`/api/jobs/${job.id}/clips/${c.file}/check`, { method: "POST" });
        c.check = r; el.querySelector(".chkres").innerHTML = badgeHtml(r);
      } catch (e) { el.querySelector(".chkres").innerHTML = `<span class="err">${e.message}</span>`; }
      chk.disabled = false; chk.innerHTML = `${ICON_C} Check`;
    });
  }
  // remove stale cards from a previous generation
  for (const el of grid.children) if (!job.clips.find((c) => String(c.rank) === el.dataset.rank)) el.remove();
}

// ------------------------------------------------------------------ recent
async function loadRecent() {
  const rows = await api("/api/jobs").catch(() => []);
  $("#recent-card").classList.toggle("hidden", rows.length === 0);
  $("#recent").innerHTML = rows.map((r) => `<li>
    <a href="#${r.id}" data-id="${r.id}">${r.title || r.url}</a>
    <span class="st">${r.status}${r.clip_count ? " · " + r.clip_count + " clips" : ""}</span>
    <button class="x" data-del="${r.id}" title="Delete">✕</button></li>`).join("");
  $("#recent").querySelectorAll("a").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); open(a.dataset.id); }));
  $("#recent").querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/jobs/${b.dataset.del}`, { method: "DELETE" }); loadRecent();
  }));
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

// remember the generate settings across videos / reloads
const OPTS = ["opt-count", "opt-min", "opt-max", "opt-mode", "opt-layout", "opt-captions", "opt-speed", "opt-pitch",
  "opt-quality", "opt-punch", "opt-title", "opt-progress", "opt-grade", "opt-mirror"];
try {
  const saved = JSON.parse(localStorage.getItem("cliper.opts") || "{}");
  for (const id of OPTS) { const el = document.getElementById(id); if (el && id in saved) { if (el.type === "checkbox") el.checked = saved[id]; else el.value = saved[id]; } }
} catch {}
for (const id of OPTS) document.getElementById(id).addEventListener("change", () => {
  const out = {}; for (const k of OPTS) { const el = document.getElementById(k); out[k] = el.type === "checkbox" ? el.checked : el.value; }
  try { localStorage.setItem("cliper.opts", JSON.stringify(out)); } catch {}
});

loadRecent();
if (location.hash.length > 1) open(location.hash.slice(1));


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
