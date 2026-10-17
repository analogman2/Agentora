// Agentora radio page, part 4 of 5: The ☰ side panel, connection metrics, and ⚙ settings.
// Plain scripts loaded in order by radio.html; they share one global scope.
// ---------------- control sidebar: hide / pop out / pin (remembered per browser)
const side = { open: store.get("side.open", false), pinned: store.get("side.pinned", false) };
const wide = () => window.matchMedia("(min-width: 1100px)").matches;
function renderSide() {
  const pinned = side.pinned && wide(), open = side.open || pinned;
  $("side").classList.toggle("open", open); document.body.classList.toggle("pinned", pinned);
  $("sideFade").classList.toggle("show", open && !pinned);
  $("sideBtn").setAttribute("aria-expanded", open); $("pinBtn").setAttribute("aria-pressed", side.pinned);
  $("pinBtn").title = side.pinned ? "Unpin: hide the panel when you click away" : "Keep the panel open beside the radio";
  if (open) { loadGpuMem(); if (typeof updateMetrics === "function") updateMetrics(); }
}
function setSide(open, pinned = side.pinned) { side.open = open; side.pinned = pinned; store.set("side.open", open); store.set("side.pinned", pinned); renderSide(); }
$("sideBtn").onclick = () => setSide(true);
$("sideClose").onclick = () => setSide(false, false);
$("sideFade").onclick = () => setSide(false);
$("pinBtn").onclick = () => setSide(true, !side.pinned);
document.addEventListener("keydown", e => { if (e.key === "Escape" && side.open && !(side.pinned && wide()) && !$("dlg").open) setSide(false); });
window.matchMedia("(min-width: 1100px)").addEventListener("change", renderSide);
$("hermesDash").href = `${location.protocol}//${location.hostname}:7880/`;
$("copyStream").onclick = async () => {
  const url = new URL("/radio/stream.mp3", location.href).href;
  try { await navigator.clipboard.writeText(url); $("copyStream").textContent = "Copied ✓"; }
  catch { prompt("Stream address:", url); }
  setTimeout(() => $("copyStream").textContent = "Copy link", 2000);
};
async function loadGpuMem() {
  const g = await api("/api/radio/gpu").catch(() => ({}));
  if (!g.total_mb) { $("vramText").textContent = "memory: not available"; return; }
  $("vramBar").style.width = (100 * g.used_mb / g.total_mb) + "%";
  $("vramText").textContent = `${(g.used_mb / 1024).toFixed(1)} of ${Math.round(g.total_mb / 1024)} GB in use · ${g.name}`;
}
setInterval(() => { if ($("side").classList.contains("open") && !document.hidden) loadGpuMem(); }, 5000);
renderSide();

// ---------------- connection metrics: this device + every listener
audio.addEventListener("waiting", () => { if (wantPlay && lid && audio.currentTime > 2) met.stalls++; });
const cls = (el, v, good, warn) => { el.className = v <= good ? "good" : v <= warn ? "warn" : "bad"; };
async function measurePing() {
  let best = Infinity;
  for (let i = 0; i < 3; i++) {
    const t = performance.now();
    try { await fetch("/api/radio/ping", {cache: "no-store"}); best = Math.min(best, performance.now() - t); } catch { return null; }
  }
  return best;
}
async function updateMetrics() {
  const ping = await measurePing();
  $("mPing").textContent = ping == null ? "unreachable" : `${Math.round(ping)} ms`;
  if (ping != null) cls($("mPing"), ping, 80, 250);
  const r = await api("/api/radio/listeners" + (lid ? `?me=${lid}` : "")).catch(() => null);
  if (!r) return;
  const me = r.listeners.find(x => x.me), s = R && R.stream;
  $("mRate").textContent = s && s.bitrate ? `${s.bitrate} kbps` : "—";
  if (me && lid) {
    $("mRecv").textContent = `${me.kbps} kbps`; cls($("mRecv"), -(me.kbps / (s.bitrate || 192)), -0.95, -0.7);
    const cushion = me.sent / ((s.bitrate || 192) * 125) - audio.currentTime;
    $("mCush").textContent = `${Math.max(0, cushion).toFixed(1)} s`; cls($("mCush"), -cushion, -8, -3);
    const clockNow = s.clock != null ? s.clock + (performance.now() - R._at) / 1000 : null;
    const behind = clockNow != null && s.p0 != null ? clockNow - (s.p0 + audio.currentTime) : null;
    $("mBehind").textContent = behind == null ? "—" : `${Math.max(0, behind).toFixed(1)} s`;
  } else {
    ["mRecv", "mCush", "mBehind"].forEach(k => { $(k).textContent = "not playing"; $(k).className = ""; });
  }
  $("mStall").textContent = `${met.stalls} · ${met.reconnects}`;
  $("lisList").innerHTML = r.listeners.map(x => `<li><span>${x.device === "VLC" ? "📻" : /Android|iPhone/.test(x.device) ? "📱" : "💻"} ${esc(x.device)}${x.me ? " <b>(you)</b>" : ""}</span>
    <span class="muted">${x.kbps} kbps · ${x.seconds < 90 ? x.seconds + " s" : Math.round(x.seconds / 60) + " min"}${x.backlog_s > 1 ? ` · <span class="warn">${x.backlog_s} s queued</span>` : ""}</span></li>`).join("");
}
$("speedBtn").onclick = async () => {
  $("speedBtn").disabled = true; $("mSpeed").textContent = "testing…"; $("mSpeed").className = "";
  try {
    const t = performance.now(), b = await (await fetch("/api/radio/speedtest?mb=4", {cache: "no-store"})).arrayBuffer();
    const mbps = b.byteLength * 8 / ((performance.now() - t) / 1000) / 1e6;
    $("mSpeed").textContent = `${mbps >= 100 ? Math.round(mbps) : mbps.toFixed(1)} Mbps`;
    cls($("mSpeed"), -mbps, -5, -1);             // the stream needs 0.2–0.3 Mbps; under 1 Mbps leaves little headroom
  } catch { $("mSpeed").textContent = "failed"; }
  $("speedBtn").disabled = false;
};
setInterval(() => { if ($("side").classList.contains("open") && !document.hidden) updateMetrics(); }, 2500);

// ---------------- ⚙ settings: every radio setting, applied live and saved on the server
let SET = null;
const fmtVal = (f, v) => f.type === "pct" ? Math.round(v * 100) : f.type === "json" ? JSON.stringify(v) : v;
function writerRows(list) {
  const models = SET.models || {};
  return list.map((w, i) => `<div class="wr" data-i="${i}">
      <input type="text" data-w="url" value="${esc(w.url)}" placeholder="http://localhost:11434/v1" aria-label="Server URL">
      <input type="text" data-w="model" value="${esc(w.model)}" list="ml${i}" placeholder="model" aria-label="Model">
      <datalist id="ml${i}">${(models[w.url] || []).map(m => `<option value="${esc(m)}">`).join("")}</datalist>
      <input type="text" data-w="label" value="${esc(w.label)}" placeholder="label" aria-label="Label">
      <button class="btn" data-up="${i}" title="Try earlier" ${i ? "" : "disabled"}>↑</button>
      <button class="btn danger" data-rm="${i}" title="Remove" ${list.length > 1 ? "" : "disabled"}>✕</button></div>`).join("");
}
function readWriters() {
  return [...document.querySelectorAll("#setW .wr")].map(r => Object.fromEntries([...r.querySelectorAll("[data-w]")].map(e => [e.dataset.w, e.value.trim()])));
}
function renderSettings(msg) {
  const slider = (k, label, lo, hi, unit, help) => `<div class="f"><label for="d_${k}">${label} <span class="small muted" id="dv_${k}">${dev[k]}${unit}</span><input type="range" id="d_${k}" data-dev="${k}" data-unit="${unit}" min="${lo}" max="${hi}" step="1" value="${dev[k]}"></label><div class="hlp">${help}</div></div>`;
  let html = `<h3>This device</h3><p class="small muted" style="margin:0 0 4px">Only changes how it sounds in this browser. Saved here and applied right away.</p>`
    + slider("low", "Bass", -12, 12, " dB", "Low shelf at 120 Hz.") + slider("mid", "Mids", -12, 12, " dB", "Voices and guitars, around 1 kHz.")
    + slider("high", "Treble", -12, 12, " dB", "High shelf at 8 kHz.") + slider("balance", "Balance", -100, 100, "", "Left (−) / right (+).")
    + `<div class="f"><label for="d_mono">Mono<input type="checkbox" id="d_mono" data-dev="mono" ${dev.mono ? "checked" : ""}></label><div class="hlp">Both channels in each ear — handy with one earbud.</div></div>
       <div class="f"><label for="d_fx">VU meters &amp; sound controls<input type="checkbox" id="d_fx" data-dev="fx" ${dev.fx ? "checked" : ""}></label><div class="hlp">Turn off for plain playback — the most robust on phones if the sound stutters. Bass, mids, treble, balance and mono then have no effect, and the meters stay still. Takes effect after a reload.</div></div>
       <button class="btn" id="devReset" style="margin-top:6px">Reset this device</button>`;
  let group = "";
  for (const f of SET.schema) {
    if (f.group !== group) { group = f.group; html += `<h3>${esc(group)}</h3>`; }
    const v = SET.values[f.key], d = SET.defaults[f.key], changed = JSON.stringify(v) !== JSON.stringify(d);
    const def = changed && f.type !== "writers" ? ` · <span class="def" data-def="${f.key}" title="Use the default">default ${esc(String(fmtVal(f, d)))}</span>` : "";
    if (f.type === "writers") {
      html += `<div class="f"><label>${esc(f.label)}</label><div class="hlp">${esc(f.help)}</div><div id="setW">${writerRows(v)}</div>
        <button class="btn" id="addW" style="margin-top:6px">+ Add model</button></div>`;
    } else if (f.type === "choice") {
      html += `<div class="f"><label for="s_${f.key}">${esc(f.label)}<select id="s_${f.key}" data-k="${f.key}">${f.options.map(o => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label><div class="hlp">${esc(f.help)}${def}</div></div>`;
    } else if (f.type === "bool") {
      html += `<div class="f"><label for="s_${f.key}">${esc(f.label)}<input type="checkbox" id="s_${f.key}" data-k="${f.key}" ${v ? "checked" : ""}></label><div class="hlp">${esc(f.help)}${def}</div></div>`;
    } else if (f.type === "json") {
      html += `<div class="f"><label for="s_${f.key}">${esc(f.label)}</label><textarea id="s_${f.key}" data-k="${f.key}">${esc(JSON.stringify(v))}</textarea><div class="hlp">${esc(f.help)}${def}</div></div>`;
    } else {
      const pct = f.type === "pct", step = f.type === "int" || pct ? 1 : "any";
      html += `<div class="f"><label for="s_${f.key}">${esc(f.label)}<input type="number" id="s_${f.key}" data-k="${f.key}" value="${fmtVal(f, v)}" min="${f.min}" max="${f.max}" step="${step}"></label><div class="hlp">${esc(f.help)} (${f.min}–${f.max})${def}</div></div>`;
    }
  }
  html += `<h3>Data</h3><div class="f"><label>Listen history<button class="btn danger" id="clrHist">Clear history</button></label><div class="hlp">Removes the list of what was played. Songs and your playlist stay.</div></div>
    <p class="small muted" style="margin-top:14px">Set in the radio's service file instead (they need a restart): GPU, port, data folder and network access.</p>
    <div class="setbar"><button class="btn primary" id="saveSet">Save</button><button class="btn" id="resetSet" title="Reset every setting to its default">Reset all</button><span class="small" id="setMsg">${msg || ""}</span></div>`;
  $("dlgB").innerHTML = `<div class="set">${html}</div>`;
  $("dlgB").querySelectorAll("[data-dev]").forEach(e => e.oninput = () => {
    const k = e.dataset.dev; dev[k] = e.type === "checkbox" ? e.checked : +e.value;
    if ($("dv_" + k)) $("dv_" + k).textContent = dev[k] + e.dataset.unit;
    store.set("device", dev); applyDevice();
    if (k === "fx") { dev.fxChosen = true; store.set("device", dev); }
    if (k === "fx" && (actx ? !dev.fx : dev.fx) && confirm("Reload the page now to switch playback mode?")) location.reload();
  });
  $("devReset").onclick = () => { dev = Object.assign({}, DEV_DEFAULT); store.set("device", dev); applyDevice(); renderSettings("This device reset."); };
  $("dlgB").querySelectorAll("[data-def]").forEach(e => e.onclick = () => {
    const f = SET.schema.find(x => x.key === e.dataset.def), inp = $("s_" + f.key);
    if (f.type === "bool") inp.checked = SET.defaults[f.key];
    else inp.value = f.type === "json" ? JSON.stringify(SET.defaults[f.key]) : fmtVal(f, SET.defaults[f.key]);
    $("setMsg").textContent = "Press Save to apply.";
  });
  $("setW").onclick = (e) => {
    const list = readWriters(), up = e.target.dataset.up, rm = e.target.dataset.rm;
    if (up) { const i = +up; [list[i - 1], list[i]] = [list[i], list[i - 1]]; }
    else if (rm) list.splice(+rm, 1); else return;
    $("setW").innerHTML = writerRows(list); $("setMsg").textContent = "Press Save to apply.";
  };
  $("addW").onclick = () => { const list = readWriters(); list.push({url: list.length ? list[list.length - 1].url : "http://localhost:11434/v1", model: "", label: ""}); $("setW").innerHTML = writerRows(list); };
  $("saveSet").onclick = async () => {
    const body = {writers: readWriters()};
    document.querySelectorAll("#dlgB [data-k]").forEach(e => { body[e.dataset.k] = e.type === "checkbox" ? e.checked : e.value; });
    $("saveSet").disabled = true;
    try { SET = await api("/api/radio/settings", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}); renderSettings("Saved ✓ live now"); refresh(); }
    catch (err) { $("setMsg").innerHTML = `<span class="err">${esc(err.message)}</span>`; }
    $("saveSet").disabled = false;
  };
  $("resetSet").onclick = async () => {
    if (!confirm("Reset every setting to its default?")) return;
    SET = await api("/api/radio/settings", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({reset: true})}); renderSettings("Defaults restored ✓"); refresh();
  };
  $("clrHist").onclick = async () => {
    if (!confirm("Clear the whole listen history?")) return;
    await post("/api/radio/history/clear"); loadHistory(); $("setMsg").textContent = "Listen history cleared.";
  };
}
$("setBtn").onclick = async () => {
  openDialog("Settings", `<p class="muted">Loading…</p>`);
  try { SET = await api("/api/radio/settings"); renderSettings(); } catch (e) { $("dlgB").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
};
function editStation(s) {
  const opt = (v, l) => `<option value="${v}" ${s.vocals === v ? "selected" : ""}>${l}</option>`;
  openDialog("Edit " + s.name, `<label class="f" for="eN">Name</label><input type="text" id="eN" value="${esc(s.name)}" maxlength="60">
    <label class="f" for="eD">Description <span>${s.kind === "folder" ? "(optional for learned stations)" : "— the sound the songwriter and critic aim for"}</span></label><textarea id="eD" rows="4" maxlength="2000">${esc(s.description || "")}</textarea>
    ${assistBar()}
    <label class="f" for="eV">Vocals</label><select id="eV">${opt("mix", "Mix of songs and instrumentals")}${opt("vocals", "Always vocals")}${opt("instrumental", "Instrumental only")}</select>
    <label class="f" for="eL">Language <span>(blank = any)</span></label><input type="text" id="eL" value="${esc(s.language || "")}" maxlength="40">
    <label class="f" for="eT">Lyrics theme <span>(optional)</span></label><textarea id="eT" rows="2" maxlength="1000">${esc(s.theme || "")}</textarea>
    ${varietyField("eVar", s.variety ?? 25)}
    <p class="small muted">${s.kind === "text" ? "Changing the description restarts the station's quality scores, since the critic measures songs against it. " : ""}Songs already made keep their old style; new ones follow the changes.</p>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap"><button class="btn primary" id="eSave">Save</button><button class="btn" id="eBack">Cancel</button>
      <button class="btn" id="eArt" type="button" title="Draw new station art from its description">🎨 New art</button><span class="small" id="eMsg"></span></div>`);
  $("eArt").onclick = async () => { try { await post("/api/radio/art", {station: s.id}); $("eMsg").textContent = "Drawing new art — it appears in a minute or two (between songs)."; } catch (e) { $("eMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; } };
  wireAssist({name: "eN", desc: "eD", vocals: "eV", lang: "eL", theme: "eT"}, false);
  wireVariety("eVar");
  $("eBack").onclick = () => openStation(s.id);
  $("eSave").onclick = async () => {
    try {
      await api(`/api/radio/stations/${s.id}`, {method: "PUT", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name: $("eN").value, description: $("eD").value, vocals: $("eV").value, language: $("eL").value, theme: $("eT").value, variety: +$("eVar").value})});
      shelfSig = ""; await refresh(); openStation(s.id);
    } catch (e) { $("eMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  };
}

function folderTools(s) {
  return `<div class="drop" id="drop">Drop songs here (mp3, flac, wav, ogg, m4a) or <label style="color:var(--red);cursor:pointer"><input type="file" id="files" multiple accept="audio/*" hidden>choose files</label><div class="small" id="upl">${s.files || 0} songs uploaded</div><div class="dprog" hidden id="uplBar"><i style="width:0"></i></div></div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:8px"><button class="btn primary" id="analyze">Learn from these songs</button><span class="small muted" id="anaStat">${s.analysis.status === "done" ? "Learned ✓ — add more songs and learn again anytime." : s.analysis.status === "running" ? `Learning ${s.analysis.done}/${s.analysis.total}…` : ""}</span></div>`;
}
function wireFolderTools(s) {
  const drop = $("drop");
  const send = async (files) => {
    const list = [...files].filter(f => /\.(mp3|flac|wav|ogg|m4a|aac|opus)$/i.test(f.name)); if (!list.length) return;
    $("uplBar").hidden = false; let i = 0;
    for (const f of list) {
      $("upl").textContent = `Uploading ${++i}/${list.length}: ${f.name}`;
      try { await fetch(`/api/radio/stations/${s.id}/files/${encodeURIComponent(f.name)}`, {method: "PUT", body: f}); } catch (e) { $("upl").textContent = e.message; }
      $("uplBar").firstElementChild.style.width = `${100 * i / list.length}%`;
    }
    $("upl").textContent = `Uploaded ${list.length} songs. Press “Learn from these songs”.`; refresh();
  };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); send(e.dataTransfer.files); };
  $("files").onchange = () => send($("files").files);
  $("analyze").onclick = async () => { await post(`/api/radio/stations/${s.id}/analyze`).catch(e => ($("anaStat").textContent = e.message)); $("anaStat").textContent = "Learning… (about a second per song after a short warm-up)"; };
}
