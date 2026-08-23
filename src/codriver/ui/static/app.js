/* codriver UI, vanilla JS, one WebSocket, a handful of fetches. */

const $ = (sel) => document.querySelector(sel);
// Anything that came from a file or from the network is text, never markup:
// stage names, note words, author names. Every innerHTML template goes
// through this.
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const api = async (path, method = "GET", body) => {
  const res = await fetch(path, {
    method,
    // X-Codriver marks a request as coming from this page; the server refuses
    // state changes without it, which keeps other websites out of the API.
    headers: { "X-Codriver": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
};
const fmtKm = (m) => (m / 1000).toFixed(3);
const log = (el, line) => { el.textContent += line + "\n"; el.scrollTop = el.scrollHeight; };

let STATE = null;
let currentStage = null;

// ---------------------------------------------------------------- tabs
document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("#tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "config") loadConfig();
    if (b.dataset.tab === "stages") refreshState();
    try { localStorage.setItem("codriver.tab", b.dataset.tab); } catch (_) {}
  })
);
try {
  const t = localStorage.getItem("codriver.tab");
  if (t) document.querySelector(`nav button[data-tab="${t}"]`)?.click();
} catch (_) {}

// ---------------------------------------------------------------- state
async function refreshState() {
  STATE = await api("/api/state");
  applyColours(STATE.colours);
  $("#lan-url").textContent = STATE.lan_url;
  $("#port-hint").textContent = STATE.telemetry_port;
  setJobPill(STATE.job);

  const recSel = $("#build-recording");
  recSel.innerHTML = STATE.recordings.map((r) => `<option>${esc(r.file)}</option>`).join("") || "<option disabled>no recordings yet</option>";

  const stageOpts = STATE.stages.map((s) => `<option value="${esc(s.name)}">${esc(s.name)} · ${fmtKm(s.length_m)} km · ${s.notes} notes</option>`).join("");
  $("#drive-stage").innerHTML = stageOpts || "<option disabled>build a stage first</option>";
  $("#btn-overlay").textContent = STATE.overlay ? "Overlay: on" : "Overlay: off";
  $("#btn-overlay").classList.toggle("primary", !!STATE.overlay);

  $("#stage-list").innerHTML = STATE.stages.map((s) =>
    `<li data-name="${esc(s.name)}" class="${currentStage === s.name ? "sel" : ""}">
       <b>${esc(s.name)}</b><span class="muted"> ${fmtKm(s.length_m)} km · ${s.notes} notes · ${s.learned_runs} runs learned</span></li>`).join("")
    || "<li class='muted'>none yet, record and build on the Setup tab</li>";
  document.querySelectorAll("#stage-list li[data-name]").forEach((li) =>
    li.addEventListener("click", () => showStage(li.dataset.name)));

  const gaps = STATE.voice_gaps || {};
  const gapBadge = (v) => gaps[v]
    ? ` <span class="badge" title="words added since this pack was generated; they play as beeps until you generate the pack again">missing: ${esc(gaps[v].join(", "))}</span>`
    : "";
  const voices = STATE.voices.map((v) => `<li><b>${esc(v)}</b>${v === STATE.voice_pack ? ' <span class="tag">active</span>' : ""}${gapBadge(v)}</li>`).join("");
  $("#voice-list").innerHTML = voices || "<li class='muted'>no voice pack yet, generate one</li>";
  $("#say-pack").innerHTML = STATE.voices.map((v) => `<option ${v === STATE.voice_pack ? "selected" : ""}>${esc(v)}</option>`).join("");

  // Voice state, shown where people actually look: Setup and Drive.
  const voiceOk = STATE.voices.includes(STATE.voice_pack);
  const activeGaps = voiceOk ? gaps[STATE.voice_pack] : null;
  $("#voice-setup-text").innerHTML = voiceOk
    ? (activeGaps
        ? `Active voice: <b>${esc(STATE.voice_pack)}</b>, but it is missing words added since it was generated (<b>${esc(activeGaps.join(", "))}</b>), which play as beeps. Generate it again on the Voice tab, same language, same name, about 20 seconds.`
        : `Active voice: <b>${esc(STATE.voice_pack)}</b>. Done. More voices and a test button are on the Voice tab.`)
    : (STATE.voices.length
        ? `You have a voice pack (${esc(STATE.voices.join(", "))}) but <b>${esc(STATE.voice_pack)}</b> is selected and does not exist. Pick one under Config, Voice.`
        : `<b>No voice yet.</b> Right now the co-driver would only beep. Generate one:`);
  $("#voice-setup-actions").hidden = voiceOk;
  $("#drive-voice-warn").hidden = voiceOk;

  loadCommunity();
}

let COMMUNITY = null;
async function loadCommunity() {
  try {
    COMMUNITY = await api("/api/community");
  } catch (x) {
    COMMUNITY = { available: false, reason: x.message };
  }
  const text = $("#community-text"), list = $("#community-list");
  if (!COMMUNITY.available) {
    text.textContent = COMMUNITY.reason || "not reachable";
    list.innerHTML = "";
    return;
  }
  text.innerHTML = `Stages shared by other players, from <a href="${COMMUNITY.url}" target="_blank" rel="noopener">${COMMUNITY.repo}</a>. Click one to preview it, Install to drive it. Share your own with the Share button on your stage.`;
  list.innerHTML = COMMUNITY.stages.map((s) =>
    `<li data-file="${esc(s.file)}" class="${currentCommunity === s.file ? "sel" : ""}">
       <div class="li-main"><b>${esc(s.name)}</b><span class="muted"> ${fmtKm(s.length_m || 0)} km · ${s.notes || 0} notes${s.author ? " · by " + esc(s.author) : ""}</span></div>
       <button class="small-btn" data-install="${esc(s.file)}" ${s.installed ? "disabled" : ""}>${s.installed ? "installed" : "Install"}</button></li>`).join("")
    || "<li class='muted'>nothing shared yet. Be the first: build a stage and press Share.</li>";
  list.querySelectorAll("li[data-file]").forEach((li) =>
    li.addEventListener("click", (e) => { if (e.target.closest("button")) return; showCommunityStage(li.dataset.file); }));
  list.querySelectorAll("button[data-install]").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); installCommunity(b.dataset.install, b); }));
}

async function installCommunity(file, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "installing…"; }
  try {
    const r = await api("/api/community/install", "POST", { file });
    if (btn) btn.textContent = "installed";
    currentCommunity = null;
    await refreshState();
    showStage(r.name);
  } catch (x) {
    if (btn) { btn.disabled = false; btn.textContent = "Install"; }
    alert(x.message);
  }
}

// A community stage, looked at before installing: same map, same notes, no
// buttons that would only make sense for a stage on this PC.
let currentCommunity = null;
async function showCommunityStage(file) {
  currentCommunity = file; currentStage = null;
  document.querySelectorAll("#stage-list li").forEach((li) => li.classList.remove("sel"));
  document.querySelectorAll("#community-list li").forEach((li) => li.classList.toggle("sel", li.dataset.file === file));
  $("#stage-actions").hidden = true; $("#community-actions").hidden = true;
  $("#stage-out").textContent = ""; $("#stage-notes").innerHTML = "";
  $("#stage-title").textContent = "loading preview…";
  let st;
  try { st = await api(`/api/community/preview/${file}`); }
  catch (x) { $("#stage-title").textContent = "preview failed"; $("#stage-out").textContent = x.message; return; }
  const by = st.community && st.community.author ? ` · by ${st.community.author}` : "";
  $("#stage-title").textContent = `${st.name}, ${fmtKm(st.length_m)} km, ${st.notes.length} notes${by} · community preview`;
  const btn = $("#btn-install-preview");
  btn.disabled = st.installed; btn.textContent = st.installed ? "already installed" : "Install this stage";
  btn.onclick = () => installCommunity(file, btn);
  $("#community-actions").hidden = false;
  drawMap(st);
  renderNotes(st);
}

function renderNotes(st) {
  $("#stage-notes").innerHTML = st.notes.map((n) =>
    `<tr><td>${fmtKm(n.at_m)}</td><td><b>${esc(n.text)}</b></td><td class="muted">${n.radius_m ? "r=" + n.radius_m.toFixed(0) + " m" : esc(n.kind)}${n.observed_kmh ? " · ~" + n.observed_kmh + " km/h" : ""}</td></tr>`).join("");
}

function setJobPill(job) {
  const pill = $("#jobpill");
  pill.className = "pill " + (job.busy ? "busy" : "idle");
  pill.textContent = job.busy ? job.label || job.kind : "idle";
  $("#btn-capture").disabled = job.busy;
  $("#btn-capture-stop").disabled = !(job.busy && job.kind === "capture");
  $("#btn-session").disabled = job.busy;
  $("#btn-session-stop").disabled = !(job.busy && job.kind === "session");
  $("#btn-run").disabled = job.busy;
  $("#btn-auto").disabled = job.busy;
  $("#btn-run-stop").disabled = !(job.busy && (job.kind === "run" || job.kind === "auto"));
  $("#btn-scan").disabled = job.busy;
  $("#btn-voice-gen").disabled = job.busy;
}

// ---------------------------------------------------------------- websocket
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (m) => handleEvent(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connectWS, 1500);
  setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 15000);
}

function handleEvent(e) {
  const job = e.job;
  if (e.kind === "started_job" || e.kind === "finished" || e.kind === "error") {
    refreshState();
    if (e.kind === "error") alert(`${job}: ${e.message}`);
  }
  if (job === "scan") onScan(e);
  if (job === "capture") onCapture(e);
  if (job === "session") onSession(e);
  if (job === "run") onRun(e);
  if (job === "auto") { onRun(e); onAuto(e); }
  if (job === "voice") onVoice(e);
}

// ---------------------------------------------------------------- setup: scan
$("#btn-scan").onclick = () => { $("#scan-out").textContent = ""; api("/api/scan", "POST", { duration: 20 }).catch((x) => alert(x.message)); };
function onScan(e) {
  const out = $("#scan-out");
  if (e.kind === "scan_started") log(out, `listening on ${e.ports} ports for ${e.duration}s. Drive now…`);
  if (e.kind === "scan_hit") log(out, `✔ packets on port ${e.port}${e.looks_like_fh6 ? ", that's Forza" : ""}`);
  if (e.kind === "scan_done") {
    if (!e.found.length) {
      const lo = e.reserved ? e.reserved[0] : 5200, hi = e.reserved ? e.reserved[1] : 5300;
      log(out, `nothing received on the ports this scan may use. It deliberately stays out of ${lo}-${hi}: listening there can break Data Out itself.`);
      log(out, `So if Data Out is on, IP is 127.0.0.1 and you were driving, the game is almost certainly set to a port in that range (the guides all say 5300). In Forza set Data Out IP Port to ${e.configured}, then drive and press Check again.`);
    }
    else {
      const best = e.found[0];
      log(out, best.port === e.configured
        ? `port ${best.port} matches your config. You are set.`
        : `the game sends to ${best.port}, config says ${e.configured}. Set Data Out IP Port to ${e.configured} in game, or change telemetry.port in Config.`);
    }
    if (e.refused.length) log(out, `in use by another program: ${e.refused.join(", ")}`);
  }
}

// ---------------------------------------------------------------- setup: capture
$("#btn-capture").onclick = () => {
  $("#capture-status").textContent = "";
  api("/api/capture", "POST", { name: $("#capture-name").value }).catch((x) => alert(x.message));
};
$("#btn-capture-stop").onclick = () => api("/api/stop", "POST");
$("#btn-session").onclick = () => { $("#capture-status").textContent = ""; api("/api/session", "POST", {}).catch((x) => alert(x.message)); };
$("#btn-session-stop").onclick = () => api("/api/stop", "POST");
function onSession(e) {
  const s = $("#capture-status");
  if (e.kind === "session_started") s.textContent = `auto-record on port ${e.port}: waiting for a race (free roam is ignored)…`;
  if (e.kind === "race_started") s.textContent = `recording race #${e.race}…`;
  if (e.kind === "status") s.textContent = e.racing
    ? `recording race · ${e.packets} packets · ${(e.speed_kmh || 0).toFixed(0)} km/h`
    : `waiting for a race · ${e.races} saved so far${e.idle ? " · stream idle" : ""}`;
  if (e.kind === "race_saved") { s.textContent = `saved ${e.path} (${e.seconds} s). ${e.races} race(s) this session. Build it below.`; refreshState(); }
  if (e.kind === "race_discarded") s.textContent = `a ${e.seconds} s blip was not a race, dropped`;
  if (e.kind === "done") { s.textContent = `auto-record stopped: ${e.races.length} race(s) saved${e.discarded ? `, ${e.discarded} blip(s) dropped` : ""}`; refreshState(); }
}
function onCapture(e) {
  const s = $("#capture-status");
  if (e.kind === "waiting") s.textContent = `waiting for telemetry on port ${e.port}… drive!`;
  if (e.kind === "started") s.textContent = `recording → ${e.path}`;
  if (e.kind === "status") s.textContent = e.idle
    ? `${e.packets} packets · stream idle (menu/pause?)`
    : `${e.packets} packets · ${e.elapsed_s.toFixed(0)} s · ${e.race_on ? "RACE" : "menu"} · ${e.speed_kmh.toFixed(0)} km/h · ${e.distance_m.toFixed(0)} m`;
  if (e.kind === "done") {
    s.textContent = e.packets ? `saved ${e.packets} packets (${e.seconds.toFixed(0)} s) → ${e.path}. Now build it below.` : "no packets received. Nothing saved.";
    refreshState();
  }
}

// ---------------------------------------------------------------- setup: build
$("#btn-build").onclick = async () => {
  const out = $("#build-out"); out.textContent = "building…";
  try {
    const r = await api("/api/build", "POST", { capture: $("#build-recording").value, name: $("#build-name").value });
    out.textContent = r.report + "\n\n" + r.notes.join("\n");
    refreshState();
  } catch (x) { out.textContent = "error: " + x.message; }
};

// ---------------------------------------------------------------- drive
$("#btn-run").onclick = () => {
  $("#calls").innerHTML = "";
  api("/api/run", "POST", { stage: $("#drive-stage").value, record: $("#drive-record").checked }).catch((x) => alert(x.message));
};
$("#btn-run-stop").onclick = () => api("/api/stop", "POST");
$("#btn-auto").onclick = () => { $("#calls").innerHTML = ""; api("/api/auto", "POST", {}).catch((x) => alert(x.message)); };
function onAuto(e) {
  if (e.kind === "auto_started") {
    $("#hud-state").textContent = "auto";
    $("#hud-next").textContent = "waiting for a race";
    $("#hud-sub").textContent = `${e.stages.length} stage(s) armed. Drive; races are recognised on their own.`;
  }
  if (e.kind === "race_started") $("#hud-sub").textContent = `race #${e.race}: looking for its stage…`;
  if (e.kind === "auto_matched") $("#hud-sub").textContent = `stage recognised: ${e.stage} (${e.distance_m} m from its start)`;
  if (e.kind === "auto_unmatched") { $("#hud-next").textContent = "unknown race, recording it"; $("#hud-sub").textContent = "no stage starts here. Build it from the recording afterwards."; }
  if (e.kind === "race_saved") { $("#hud-next").textContent = "waiting for the next race"; $("#hud-sub").textContent = e.stage ? `saved a run of ${e.stage} (${e.seconds} s), Learn material` : `saved ${e.path} (${e.seconds} s): build it on the Setup tab`; refreshState(); }
  if (e.kind === "race_discarded") $("#hud-sub").textContent = `a ${e.seconds} s blip was not a race, dropped`;
  if (e.kind === "auto_status" && !e.racing) $("#hud-state").textContent = "auto";
  if (e.kind === "auto_done") { $("#hud-state").textContent = "done"; $("#hud-next").textContent = `auto stopped: ${e.races} race(s), ${e.matched} with a stage`; $("#hud-sub").textContent = ""; refreshState(); }
}
$("#btn-overlay").onclick = async () => {
  try { await api("/api/overlay", "POST", { on: !(STATE && STATE.overlay) }); }
  catch (x) { alert(x.message); }
  refreshState();
};
function onRun(e) {
  if (e.kind === "waiting") {
    $("#hud-state").textContent = "waiting";
    $("#hud-next").textContent = `${e.stage} · waiting for telemetry on ${e.port}`;
    $("#hud-sub").textContent = e.voice
      ? `voice: ${e.voice}`
      : "no voice pack loaded, you will hear beeps. Generate one on the Voice tab and pick it under Config, then press Stop and Start on the Drive tab.";
  }
  if (e.kind === "localised") { $("#hud-sub").textContent = `localised at ${fmtKm(e.along_m)} km (${e.off_m.toFixed(1)} m off line)`; }
  if (e.kind === "suspended") { $("#hud-state").textContent = "suspended"; $("#hud-sub").textContent = "stream stopped (pause / rewind / finish?)"; }
  if (e.kind === "jump") { $("#hud-sub").textContent = "position jump, rewound? re-localising"; }
  if (e.kind === "status") {
    $("#hud-state").textContent = e.state;
    $("#hud-km").textContent = fmtKm(e.along_m);
    $("#hud-speed").textContent = e.speed_kmh.toFixed(0);
    $("#hud-off").textContent = e.off_m.toFixed(1) + " m";
    $("#hud-counts").textContent = `${e.spoken} / ${e.dropped}`;
    if (e.next) { $("#hud-next").textContent = e.next; $("#hud-sub").textContent = `in ${Math.max(0, e.next_at_m - e.along_m).toFixed(0)} m`; }
    else { $("#hud-next").textContent = "no notes remaining"; }
  }
  if (e.kind === "note") {
    const li = document.createElement("li");
    li.innerHTML = `<b>${esc(e.text)}</b> <span class="muted">${fmtKm(e.at_m)} km · lead ${e.lead_m.toFixed(0)} m · ${e.duration_s.toFixed(2)} s</span>`;
    $("#calls").prepend(li);
    $("#hud-next").classList.add("flash"); setTimeout(() => $("#hud-next").classList.remove("flash"), 400);
  }
  if (e.kind === "done") { $("#hud-state").textContent = "done"; $("#hud-sub").textContent = e.summary + (e.recorded_to ? ` · recorded ${e.recorded_packets} packets` : ""); }
}

// ---------------------------------------------------------------- stages
async function showStage(name) {
  currentStage = name; currentCommunity = null;
  document.querySelectorAll("#stage-list li").forEach((li) => li.classList.toggle("sel", li.dataset.name === name));
  document.querySelectorAll("#community-list li").forEach((li) => li.classList.remove("sel"));
  $("#community-actions").hidden = true;
  const st = await api(`/api/stages/${name}`);
  $("#stage-title").textContent = `${st.name}, ${fmtKm(st.length_m)} km, ${st.notes.length} notes`;
  $("#stage-actions").hidden = false;
  $("#learn-count").textContent = st.runs.length;
  $("#btn-learn").disabled = st.runs.length === 0;
  $("#stage-out").textContent = "";
  drawMap(st);
  renderNotes(st);
}
$("#btn-rebuild").onclick = async () => { $("#stage-out").textContent = "rebuilding…"; try { const r = await api(`/api/stages/${currentStage}/rebuild`, "POST"); $("#stage-out").textContent = r.report; showStage(currentStage); refreshState(); } catch (x) { $("#stage-out").textContent = "error: " + x.message; } };
$("#btn-learn").onclick = async () => { $("#stage-out").textContent = "learning…"; try { const r = await api(`/api/stages/${currentStage}/learn`, "POST"); $("#stage-out").textContent = r.report; showStage(currentStage); refreshState(); } catch (x) { $("#stage-out").textContent = "error: " + x.message; } };
$("#btn-share").onclick = async () => {
  const author = prompt("Your name for the credits (optional):", localStorage.getItem("codriver.author") || "") ;
  if (author === null) return;
  try { localStorage.setItem("codriver.author", author); } catch (_) {}
  $("#stage-out").textContent = "preparing…";
  try {
    const r = await api(`/api/stages/${currentStage}/share`, "POST", { author });
    if (r.via === "relay") {
      $("#stage-out").innerHTML =
        `Shared. Pull request: <a href="${esc(r.pr_url)}" target="_blank" rel="noopener">${esc(r.pr_url)}</a>\n\n` +
        `It appears in the Community list of every install once the maintainer merges it` +
        (r.updated ? ` (this updates a stage that was shared before)` : ``) + `. Thank you!`;
    } else {
      $("#stage-out").textContent =
        `Share file written: ${r.path}\n\n` +
        (r.relay_error ? `The one-click relay did not answer (${r.relay_error}), so this is the manual way:\n` : ``) +
        `A browser tab with the community upload page should have opened (${r.upload_url}).\n` +
        `Drag the file from the folder that opened onto the page, press "Propose changes", and GitHub turns it into a pull request.\n` +
        `It appears in the Community list once it is merged. Thank you!`;
    }
  } catch (x) { $("#stage-out").textContent = "error: " + x.message; }
};
$("#btn-delete").onclick = async () => { if (!confirm(`delete stage ${currentStage}?`)) return; await api(`/api/stages/${currentStage}`, "DELETE"); currentStage = null; $("#stage-actions").hidden = true; $("#stage-title").textContent = "no stage selected"; $("#stage-notes").innerHTML = ""; MAP_STAGE = null; drawMap(); refreshState(); };

// The severity palette comes from the server (display.colours in the
// config), the same one the overlay arrow uses. Nothing is hard-coded here.
let COLOURS = {};
function applyColours(colours) {
  COLOURS = colours || {};
  const root = document.documentElement;
  for (const k of ["1", "2", "3", "4", "5", "6"]) root.style.setProperty(`--c${k}`, COLOURS[k] || "#888");
  root.style.setProperty("--cs", COLOURS.S || "#a0a0a8");
}
// Stage map. Wheel (or pinch) zooms around the cursor, drag pans,
// double-click or the Fit button shows the whole stage again.
let MAP_STAGE = null;
const MAP_VIEW = { zoom: 1, tx: 0, ty: 0 };
function resetMapView() { MAP_VIEW.zoom = 1; MAP_VIEW.tx = 0; MAP_VIEW.ty = 0; }
function zoomMapAt(cx, cy, k) {
  const z = Math.min(80, Math.max(1, MAP_VIEW.zoom * k));
  k = z / MAP_VIEW.zoom;
  MAP_VIEW.tx = cx - (cx - MAP_VIEW.tx) * k;
  MAP_VIEW.ty = cy - (cy - MAP_VIEW.ty) * k;
  MAP_VIEW.zoom = z;
  if (z === 1) resetMapView();
}

function drawMap(st) {
  if (st) { if (st !== MAP_STAGE) resetMapView(); MAP_STAGE = st; }
  st = MAP_STAGE;
  const c = $("#map"), ctx = c.getContext("2d"), dpr = devicePixelRatio;
  const W = c.width = c.clientWidth * dpr, H = c.height = Math.round(c.clientWidth * 0.6) * dpr;
  ctx.clearRect(0, 0, W, H);
  $("#map-zoom").textContent = "";
  if (!st || !st.line.length) return;
  const xs = st.line.map((p) => p[0]), zs = st.line.map((p) => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minZ = Math.min(...zs), maxZ = Math.max(...zs);
  const pad = 24 * dpr;
  const fit = Math.min((W - 2 * pad) / Math.max(1, maxX - minX), (H - 2 * pad) / Math.max(1, maxZ - minZ));
  const { zoom, tx, ty } = MAP_VIEW;
  const X = (x) => (pad + (x - minX) * fit) * zoom + tx, Y = (z) => (H - pad - (z - minZ) * fit) * zoom + ty;  // z up = north
  ctx.lineWidth = 4 * dpr; ctx.lineCap = "round"; ctx.lineJoin = "round";
  // One path per run of equal colour, not one per segment: this redraws on
  // every drag move and a stage is a few thousand segments.
  let colour = null;
  for (let i = 1; i < st.line.length; i++) {
    const label = st.markings[i] || "S";
    const next = COLOURS[label === "S" ? "S" : label.slice(1)] || "#888";
    if (next !== colour) {
      if (colour) ctx.stroke();
      colour = next; ctx.strokeStyle = colour;
      ctx.beginPath(); ctx.moveTo(X(st.line[i - 1][0]), Y(st.line[i - 1][1]));
    }
    ctx.lineTo(X(st.line[i][0]), Y(st.line[i][1]));
  }
  if (colour) ctx.stroke();
  // start marker
  ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(X(xs[0]), Y(zs[0]), 6 * dpr, 0, 7); ctx.fill();
  // notes
  ctx.font = `${11 * dpr}px system-ui, sans-serif`;
  st.notes.forEach((n) => {
    const p = st.line[Math.min(n.index, st.line.length - 1)];
    const x = X(p[0]), y = Y(p[1]);
    if (x < -200 || y < -50 || x > W + 50 || y > H + 50) return;  // off screen while zoomed
    ctx.fillStyle = n.kind === "corner" ? "#ffffff" : n.kind === "water" ? (COLOURS.water || "#2fb3ff") : (COLOURS.hazard || "#ffd60a");
    ctx.beginPath(); ctx.arc(x, y, 3.5 * dpr, 0, 7); ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,.85)";
    ctx.fillText(n.text, x + 6 * dpr, y - 4 * dpr);
  });
  if (zoom > 1.01) $("#map-zoom").textContent = `${zoom.toFixed(1)}×`;
}

(function wireMap() {
  const c = $("#map");
  const ptrs = new Map();
  let drag = null, pinch = null, raf = 0;
  const redraw = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; drawMap(); }); };
  const canvasPoint = (clientX, clientY) => {
    const r = c.getBoundingClientRect();
    return [(clientX - r.left) * devicePixelRatio, (clientY - r.top) * devicePixelRatio];
  };
  c.addEventListener("wheel", (e) => {
    if (!MAP_STAGE) return;
    e.preventDefault();
    const [cx, cy] = canvasPoint(e.clientX, e.clientY);
    const dy = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;  // Firefox reports lines, not pixels
    zoomMapAt(cx, cy, Math.exp(-dy * 0.0015));
    redraw();
  }, { passive: false });
  c.addEventListener("pointerdown", (e) => {
    if (!MAP_STAGE) return;
    c.setPointerCapture(e.pointerId);
    ptrs.set(e.pointerId, [e.clientX, e.clientY]);
    if (ptrs.size === 1) { drag = { x: e.clientX, y: e.clientY, tx: MAP_VIEW.tx, ty: MAP_VIEW.ty }; c.classList.add("dragging"); }
    else { drag = null; pinch = null; }
  });
  c.addEventListener("pointermove", (e) => {
    if (!ptrs.has(e.pointerId)) return;
    ptrs.set(e.pointerId, [e.clientX, e.clientY]);
    if (ptrs.size === 2) {
      const [a, b] = [...ptrs.values()];
      const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], d = Math.hypot(a[0] - b[0], a[1] - b[1]) || 1;
      if (pinch) {
        const [cx, cy] = canvasPoint(mid[0], mid[1]);
        zoomMapAt(cx, cy, d / pinch.d);
        MAP_VIEW.tx += (mid[0] - pinch.mid[0]) * devicePixelRatio;
        MAP_VIEW.ty += (mid[1] - pinch.mid[1]) * devicePixelRatio;
      }
      pinch = { d, mid };
      redraw();
    } else if (drag) {
      MAP_VIEW.tx = drag.tx + (e.clientX - drag.x) * devicePixelRatio;
      MAP_VIEW.ty = drag.ty + (e.clientY - drag.y) * devicePixelRatio;
      redraw();
    }
  });
  const release = (e) => {
    ptrs.delete(e.pointerId);
    if (ptrs.size === 0) { drag = null; pinch = null; c.classList.remove("dragging"); }
    else if (ptrs.size === 1) { const [[x, y]] = ptrs.values(); drag = { x, y, tx: MAP_VIEW.tx, ty: MAP_VIEW.ty }; pinch = null; }
  };
  c.addEventListener("pointerup", release);
  c.addEventListener("pointercancel", release);
  c.addEventListener("dblclick", () => { resetMapView(); drawMap(); });
  $("#btn-map-reset").onclick = () => { resetMapView(); drawMap(); };
  addEventListener("resize", () => { if (MAP_STAGE) drawMap(); });
})();

// ---------------------------------------------------------------- config
let CONFIG_FIELDS = [];
const TIERS = [
  ["essential", "Essentials", "The few that matter. Start here.", true],
  ["more", "More", "Everything else you might want to touch.", false],
  ["expert", "Expert", "Internals. Normally leave these alone.", false],
];

async function loadConfig() {
  const { fields } = await api("/api/config");
  CONFIG_FIELDS = fields;
  renderConfig();
}

function renderConfig() {
  const q = ($("#config-search").value || "").trim().toLowerCase();
  const form = $("#config-form");
  form.innerHTML = "";
  TIERS.forEach(([tier, title, blurb, open]) => {
    const rows = CONFIG_FIELDS.filter((f) => f.tier === tier &&
      (!q || f.key.toLowerCase().includes(q) || f.label.toLowerCase().includes(q) || f.help.toLowerCase().includes(q)));
    if (!rows.length) return;
    const det = document.createElement("details");
    det.className = "card tier";
    det.open = open || !!q;
    det.innerHTML = `<summary><b>${title}</b> <span class="muted">${blurb}</span><span class="count">${rows.length}</span></summary>`;
    let section = null;
    rows.forEach((f) => {
      if (tier !== "essential" && f.section !== section) {
        section = f.section;
        const h = document.createElement("div"); h.className = "cfg-section"; h.textContent = section;
        det.appendChild(h);
      }
      det.appendChild(renderField(f));
    });
    form.appendChild(det);
  });
  bindConfigInputs(form);
}

function renderField(f) {
  const row = document.createElement("div");
  row.className = "cfg-row" + (f.overridden ? " over" : "");
  const esc = (s) => String(s ?? "").replace(/"/g, "&quot;");
  let input;
  if (f.options && f.options.length) {
    const cur = String(f.value ?? "");
    input = `<select data-key="${f.key}" data-type="${f.type}">${f.options.map((o) =>
      `<option value="${esc(o.value)}" ${String(o.value ?? "") === cur ? "selected" : ""}>${o.label}</option>`).join("")}</select>`;
  } else if (f.type === "bool") {
    input = `<input type="checkbox" data-key="${f.key}" data-type="bool" ${f.value ? "checked" : ""}>`;
  } else if (f.range && (f.type === "int" || f.type === "float")) {
    input = `<input type="range" data-key="${f.key}" data-type="${f.type}" min="${f.range[0]}" max="${f.range[1]}" step="${f.range[2]}" value="${f.value}"><output>${f.value}</output>`;
  } else if (f.type === "list") {
    input = `<input data-key="${f.key}" data-type="list" value="${esc((f.value || []).join(", "))}">`;
  } else {
    input = `<input data-key="${f.key}" data-type="${f.type}" value="${esc(f.value)}" ${f.type === "int" || f.type === "float" ? 'inputmode="decimal"' : ""}>`;
  }
  const badge =
    (f.needs_rebuild ? `<span class="badge" title="takes effect when you build or learn a stage">rebuild</span>` : "") +
    (f.needs_restart ? `<span class="badge restart" title="read only when the co-driver starts: press Stop, then Start on the Drive tab. No need to close the program.">stop + start</span>` : "");
  row.innerHTML =
    `<div class="cfg-key"><span class="dot"></span>${f.label}${badge}<div class="cfg-keyname">${f.key}</div></div>` +
    `<div class="cfg-input">${input}<button class="reset" data-key="${f.key}" title="back to default (${esc(f.default)})">reset</button></div>` +
    `<div class="cfg-help">${f.help}</div>`;
  return row;
}

function bindConfigInputs(form) {
  form.querySelectorAll("input[data-key], select[data-key]").forEach((inp) => {
    if (inp.type === "range") {
      inp.addEventListener("input", () => { inp.nextElementSibling.textContent = inp.value; });
    }
    inp.addEventListener("change", async () => {
      const value = inp.dataset.type === "bool" ? inp.checked : inp.value;
      try {
        await api("/api/config", "PUT", { key: inp.dataset.key, value, type: inp.dataset.type });
        inp.closest(".cfg-row").classList.add("over");
        inp.classList.add("saved"); setTimeout(() => inp.classList.remove("saved"), 600);
      } catch (x) { alert(x.message); }
    });
  });
  form.querySelectorAll("button.reset").forEach((b) =>
    b.addEventListener("click", async () => { await api(`/api/config/${b.dataset.key}`, "DELETE"); loadConfig(); }));
}
$("#config-search").addEventListener("input", renderConfig);

// ---------------------------------------------------------------- voice
$("#btn-voice-gen").onclick = () => { $("#voice-out").textContent = ""; api("/api/voice/generate", "POST", { lang: $("#voice-lang").value, engine: $("#voice-engine").value, name: $("#voice-name").value }).catch((x) => alert(x.message)); };
document.querySelectorAll("#voice-setup-actions button[data-gen-lang]").forEach((b) =>
  b.addEventListener("click", () => {
    $("#voice-setup-out").textContent = "";
    api("/api/voice/generate", "POST", { lang: b.dataset.genLang, engine: "edge" }).catch((x) => alert(x.message));
  }));
function onVoice(e) {
  // The same messages land on the Voice tab and on the Setup card.
  const outs = [$("#voice-out"), $("#voice-setup-out")];
  const log = (_, line) => outs.forEach((o) => { o.textContent += line + "\n"; o.scrollTop = o.scrollHeight; });
  const out = null;
  if (e.kind === "voice_started") log(out, `generating '${e.name}' (${e.lang}, ${e.engine})… ~20 s`);
  if (e.kind === "voice_done") {
    log(out, `done: ${e.clips} clips, ${e.seconds.toFixed(1)} s of audio, voice ${e.voice}.`);
    log(out, e.selected
      ? `'${e.name}' is now the active voice. If the co-driver is running, stop and start it again.`
      : `to use it, pick '${e.name}' under Config, Voice, then press Stop and Start on the Drive tab.`);
    refreshState();
  }
  if (e.kind === "error") log(out, "error: " + e.message);
}
$("#btn-say").onclick = () => api("/api/voice/say", "POST", { text: $("#say-text").value, pack: $("#say-pack").value }).catch((x) => alert(x.message));

// ---------------------------------------------------------------- go
refreshState();
connectWS();
setInterval(refreshState, 10000);
