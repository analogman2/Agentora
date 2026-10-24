// Agentora radio page, part 5 of 5: ✨ Improve / 💡 Suggest, resizable text boxes, disk usage, and Create-a-song progress.
// Plain scripts loaded in order by radio.html; they share one global scope.
// ---------- ✨ Improve / 💡 Suggest for a station's description, with the radio's own songwriter model
const assistBar = () => `<div class="assist"><button type="button" class="btn" data-assist="improve" title="Rewrite the description into a richer brief for the songwriter">✨ Improve with AI</button>
  <button type="button" class="btn" data-assist="suggest" title="Three different directions to pick from">💡 Suggest ideas</button><span class="small muted" id="aMsg" role="status"></span></div><div id="aIdeas"></div>`;
function wireAssist(ids, creating) {
  const f = (k) => $(ids[k]), msg = $("aMsg");
  const fields = () => ({name: f("name").value, description: f("desc").value, vocals: f("vocals").value, language: f("lang").value, theme: f("theme").value});
  const fill = (d, all) => {
    if (d.description) f("desc").value = d.description;
    if (d.name && (all || !f("name").value.trim())) f("name").value = d.name;
    if (d.theme && (all || f("theme").value.trim() || !f("theme").value)) f("theme").value = d.theme;
    if (d.vocals && all) f("vocals").value = d.vocals;
    [f("desc"), f("theme")].forEach(t => t.dispatchEvent(new Event("input")));
  };
  document.querySelectorAll("[data-assist]").forEach(b => b.onclick = async () => {
    const mode = b.dataset.assist, before = fields();
    document.querySelectorAll("[data-assist]").forEach(x => x.disabled = true);
    msg.textContent = (mode === "improve" ? "Improving" : "Thinking up ideas") + "… usually a few seconds on the GPU.";
    $("aIdeas").innerHTML = "";
    try {
      const r = await post("/api/radio/stations/assist", {mode, ...before});
      if (mode === "improve") {
        fill(r.improved, false);
        msg.innerHTML = `Improved ✓ <a href="#" id="aUndo">Undo</a>`;
        $("aUndo").onclick = (e) => { e.preventDefault(); f("desc").value = before.description; f("name").value = before.name; f("theme").value = before.theme; msg.textContent = "Back to your version."; };
      } else {
        msg.textContent = "Pick one to use it" + (creating ? "" : " (Save to keep it)") + ":";
        $("aIdeas").innerHTML = r.ideas.map((d, i) => `<div class="idea"><b>${esc(d.name || "Idea " + (i + 1))}</b>${d.vocals ? ` <span class="small muted">· ${esc(d.vocals)}</span>` : ""}
          <p>${esc(d.description)}</p>${d.theme ? `<p class="small muted">Lyrics: ${esc(d.theme)}</p>` : ""}<button type="button" class="btn" data-idea="${i}">Use this</button></div>`).join("");
        $("aIdeas").querySelectorAll("[data-idea]").forEach(x => x.onclick = () => {
          fill(r.ideas[+x.dataset.idea], true); $("aIdeas").innerHTML = "";
          msg.innerHTML = `Using “${esc(r.ideas[+x.dataset.idea].name || "that idea")}” <a href="#" id="aUndo">Undo</a>`;
          $("aUndo").onclick = (e) => { e.preventDefault(); Object.entries({name: "name", desc: "description", vocals: "vocals", theme: "theme"}).forEach(([k, v]) => f(k).value = before[v]); msg.textContent = "Back to your version."; };
        });
      }
    } catch (e) { msg.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    document.querySelectorAll("[data-assist]").forEach(x => x.disabled = false);
  });
}
// 🎲 Variety slider (station setting, 0-100): how far songs may wander from the station's core sound
const varietyName = (v) => v < 10 ? "Core sound only" : v < 35 ? "A little variety" : v < 65 ? "Some surprises" : v < 90 ? "Lots of variety" : "Adventurous";
const varietyHelp = (v) => v < 10 ? "Every song sticks to the station's core sound."
  : v < 35 ? "Songs vary tempo, topic or instruments, all within the station's sound."
  : v < 65 ? `Songs vary more, and about ${Math.round((v - 25))}% borrow one element from a neighbouring style.`
  : `Songs vary a lot, and about ${Math.round(v - 25)}% borrow one element from a neighbouring style. They still sound like this station.`;
const varietyField = (id, v = 25) => `<label class="f" for="${id}">🎲 Variety <span id="${id}N">— ${varietyName(v)}</span></label>
  <input type="range" id="${id}" min="0" max="100" step="5" value="${v}" style="width:100%" aria-describedby="${id}H">
  <div class="small muted" style="display:flex;justify-content:space-between"><span>Stay in the genre</span><span>More variety</span></div>
  <p class="small muted" id="${id}H" style="margin-top:2px">${varietyHelp(v)}</p>`;
function wireVariety(id) {
  $(id).oninput = () => { const v = +$(id).value; $(id + "N").textContent = "— " + varietyName(v); $(id + "H").textContent = varietyHelp(v); };
}
const openNewStation = () => {
  openDialog("New station", `<div class="seg" role="group"><button type="button" data-k="text" aria-pressed="true">Describe it</button><button type="button" data-k="folder" aria-pressed="false">Learn from my music</button></div>
    <label class="f" for="nName">Name</label><input type="text" id="nName" maxlength="60" placeholder="e.g. Sunday Morning Jazz">
    <label class="f" for="nDesc">Sound <span id="nDescHint">— genres, moods, instruments, era, vocals</span></label>
    <textarea id="nDesc" rows="3" placeholder="e.g. Warm Sunday-morning jazz: brushed drums, upright bass, piano trio, occasional soft female vocal, coffee-shop calm"></textarea>
    ${assistBar()}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px"><div><label class="f" for="nVocals">Vocals</label><select id="nVocals"><option value="mix">Mix of both</option><option value="vocals">Mostly vocals</option><option value="instrumental">Instrumental only</option></select></div>
    <div><label class="f" for="nLang">Language <span>(vocals)</span></label><select id="nLang"><option value="">Any / from description</option><option>English</option><option>Mandarin</option><option>Japanese</option><option>Korean</option><option>Spanish</option><option>French</option><option>German</option></select></div></div>
    <label class="f" for="nTheme">Lyrics theme <span>— optional, e.g. "Halloween horror stories" or "songs about the sea"</span></label>
    <input type="text" id="nTheme" maxlength="1000" placeholder="what every song's lyrics should be about">
    ${varietyField("nVar")}
    <p class="small muted" id="nFolderNote" hidden>After creating it, open the station tape to drop in your songs, then press “Learn from these songs”. The radio analyzes tempo, key, energy and sound (CLAP), and its critic keeps the takes that sound most like them.</p>
    <div style="margin-top:12px;display:flex;gap:8px;align-items:center"><button class="btn primary" id="nCreate">Create station</button><span class="err" id="nErr"></span></div>`);
  wireAssist({name: "nName", desc: "nDesc", vocals: "nVocals", lang: "nLang", theme: "nTheme"}, true);
  wireVariety("nVar");
  let kind = "text";
  document.querySelectorAll("[data-k]").forEach(b => b.onclick = () => { kind = b.dataset.k; document.querySelectorAll("[data-k]").forEach(x => x.setAttribute("aria-pressed", x === b)); $("nFolderNote").hidden = kind !== "folder"; $("nDescHint").textContent = kind === "folder" ? "— optional; your songs define it" : "— genres, moods, instruments, era, vocals"; });
  $("nCreate").onclick = async () => {
    try {
      const r = await post("/api/radio/stations", {kind, name: $("nName").value, description: $("nDesc").value, vocals: $("nVocals").value, language: $("nLang").value, theme: $("nTheme").value, variety: +$("nVar").value});
      selected = r.station.id; store.set("station", selected); shelfSig = ""; await refresh(); $("dlg").close();
      if (kind === "folder") openStation(selected); else hint(`“${r.station.name}” is ready — press PLAY.`);
    } catch (e) { $("nErr").textContent = e.message; }
  };
};

// wire the folder tools after a station page renders
const _render = renderView;
renderView = async function () { await _render(); const s = view.tab === "station" && stOf(view.id); if (s && s.kind === "folder" && $("drop")) wireFolderTools(s); };

route();
refresh().then(() => { renderBar(); renderView(); });
loadHistory();
setInterval(refresh, 2500);
setInterval(syncNow, 1000);
setInterval(() => { cache.playlist = null; if (view.tab === "collection" && view.sub === "playlist") renderView(); }, 30000);

// ---------- resizable text boxes: grow while typing, drag the bar underneath (mouse or touch), ⤢ for a tall editor
(() => {
  const MAX = () => Math.round(innerHeight * 0.7);
  const fit = (ta) => { if (ta.dataset.manual) return; ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight + 2, MAX()) + "px"; };
  function enhance(ta) {
    if (ta.dataset.rz || !ta.parentNode) return;
    ta.dataset.rz = "1";
    const wrap = document.createElement("div"); wrap.className = "tawrap";
    ta.parentNode.insertBefore(wrap, ta); wrap.appendChild(ta);
    const big = document.createElement("button"); big.type = "button"; big.className = "tabig"; big.textContent = "⤢";
    big.title = "Make this box bigger (again to shrink)"; big.setAttribute("aria-label", "Toggle a bigger text box");
    const grip = document.createElement("div"); grip.className = "tagrip"; grip.title = "Drag to resize · double-click to fit the text";
    wrap.append(big, grip);
    big.onclick = () => {
      const on = !wrap.classList.contains("big"); wrap.classList.toggle("big", on);
      ta.dataset.manual = on ? "1" : ""; ta.style.height = on ? Math.round(Math.max(innerHeight * 0.8, ta.offsetHeight + 160)) + "px" : "";
      if (!on) fit(ta); ta.focus();
    };
    grip.addEventListener("pointerdown", (e) => {
      e.preventDefault(); grip.setPointerCapture(e.pointerId); grip.classList.add("drag");
      const y0 = e.clientY, h0 = ta.offsetHeight;
      const move = (ev) => { ta.dataset.manual = "1"; ta.style.height = Math.max(44, h0 + ev.clientY - y0) + "px"; };
      const up = () => { grip.classList.remove("drag"); grip.removeEventListener("pointermove", move); grip.removeEventListener("pointerup", up); grip.removeEventListener("pointercancel", up); };
      grip.addEventListener("pointermove", move); grip.addEventListener("pointerup", up); grip.addEventListener("pointercancel", up);
    });
    grip.addEventListener("dblclick", () => { ta.dataset.manual = ""; wrap.classList.remove("big"); fit(ta); });
    ta.addEventListener("input", () => fit(ta));
    ta.addEventListener("focus", () => fit(ta));
    requestAnimationFrame(() => fit(ta));
  }
  const scan = (root) => { if (root.tagName === "TEXTAREA") enhance(root); else if (root.querySelectorAll) root.querySelectorAll("textarea").forEach(enhance); };
  scan(document.body);
  new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach(scan))).observe(document.body, {childList: true, subtree: true});
})();

// ---------- disk usage (side panel)
const diskSize = (n) => n >= 2 ** 40 ? (n / 2 ** 40).toFixed(2) + " TB" : n >= 2 ** 30 ? (n / 2 ** 30).toFixed(n >= 100 * 2 ** 30 ? 0 : 1) + " GB" : Math.round(n / 2 ** 20) + " MB";
async function loadDisk() {
  try {
    const d = await api("/api/disk"), pct = 100 * d.used / d.total, low = d.free < 15 * 2 ** 30;
    $("diskBar").style.width = pct.toFixed(1) + "%"; $("diskBar").className = low ? "full" : pct > 85 ? "warn" : "";
    $("diskMeter").setAttribute("aria-valuenow", Math.round(pct));
    $("diskText").textContent = `${diskSize(d.free)} free of ${diskSize(d.total)}` + (low ? " · almost full" : "");
    $("diskParts").innerHTML = d.parts.filter(p => p.bytes > 0).sort((a, b) => b.bytes - a.bytes).map(p => `<li><span>${esc(p.label)}</span><b>${diskSize(p.bytes)}</b></li>`).join("");
  } catch (e) {}
}
loadDisk(); setInterval(() => { if (!document.hidden) loadDisk(); }, 60000);


// ---------- "Create a song now": progress for the station page (writing -> recording -> ready)
function updateMk() {
  const el = $("mkStat"); if (!el || view.tab !== "station" || !R) return;
  const s = stOf(view.id); if (!s) return;
  const mine = (R.making || []).filter(m => m.station === s.id);
  const parts = [];
  if (s.asked) parts.push(`✍ writing ${s.asked === 1 ? "a song" : s.asked + " songs"} (the songwriter drafts and edits it: a few minutes)`);
  for (const m of mine) parts.push(`${m.status === "running" ? "🎙 recording" : "⏳ waiting to record"} “${esc(m.title)}”${m.stage ? " · " + esc(m.stage) : ""}${m.requested ? "" : " (radio queue)"}`);
  el.innerHTML = parts.length ? parts.join("<br>") : (s.ready ? `${s.ready} song${s.ready === 1 ? "" : "s"} ready to play` : "");
}
setInterval(updateMk, 2000);
