/* 📷 Song from photos (Music Studio, Create): photos -> what's in them -> the story -> lyrics -> the normal Create song.
   Loaded after the page's own script: uses its $, esc, setEngine, saveForm, lyricStats, syncChips.
   Server side: studio/photosong.py (/api/photosong/...). The song is private to its maker and never on the radio. */
(function () {
  const box = $("photoWriter");
  if (!box) return;
  const ps = {id: null, d: null, busy: false, albumOpen: false, picked: new Set()};
  const PARTS = {verse1: "Verse 1", verse2: "Verse 2", chorus: "Chorus", bridge: "Bridge", outro: "Outro"};
  const KEY = "studio.photosong";
  const remember = (id) => { try { id ? localStorage.setItem(KEY, id) : localStorage.removeItem(KEY); } catch {} };
  const recall = () => { try { return localStorage.getItem(KEY); } catch { return null; } };

  document.head.insertAdjacentHTML("beforeend", `<style>
    .photos h4 { margin: 14px 0 6px; font-size: 14px; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
    .photos h4 .n { display: inline-grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; background: var(--accent); color: var(--accent-ink); font-size: 12px; }
    .photos .drop { display: block; border: 2px dashed var(--line); border-radius: 10px; padding: 14px; text-align: center; cursor: pointer; background: var(--panel); }
    .photos .drop.over { border-color: var(--accent); }
    .photos .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; margin-top: 10px; }
    .photos .card { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); overflow: hidden; display: flex; flex-direction: column; }
    .photos .card.skip { opacity: .45; }
    .photos .card img { width: 100%; aspect-ratio: 4/3; object-fit: cover; display: block; background: var(--soft); }
    .photos .card .b { padding: 6px 8px 8px; display: flex; flex-direction: column; gap: 4px; }
    .photos .card .when { font-size: 12px; color: var(--muted); }
    .photos .card textarea, .photos .card input { font-size: 13px; padding: 5px 7px; }
    .photos .card .tools { display: flex; gap: 6px; justify-content: space-between; }
    .photos .card .tools button { padding: 3px 8px; font-size: 13px; }
    .photos .card .tools [aria-pressed="true"] { border-color: var(--accent); color: var(--accent); }
    .photos .note { font-size: 12px; color: var(--warn); }
    .photos .moment { border: 1px solid var(--line); border-radius: 10px; padding: 8px; margin: 6px 0; background: var(--panel); display: grid; grid-template-columns: auto 1fr 130px; gap: 8px; align-items: start; }
    .photos .moment .th { display: flex; gap: 3px; } .photos .moment .th img { width: 38px; height: 38px; object-fit: cover; border-radius: 5px; }
    .photos .moment .w { font-size: 12px; color: var(--muted); }
    .photos .albumbar { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
    .photos .albumbar input { flex: 1 1 240px; }
    .photos .agrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(88px, 1fr)); gap: 6px; margin-top: 8px; max-height: 360px; overflow: auto; padding: 2px; }
    .photos .agrid button { position: relative; padding: 0; border: 2px solid transparent; border-radius: 8px; overflow: hidden; background: var(--soft); cursor: pointer; aspect-ratio: 1; }
    .photos .agrid button img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .photos .agrid button.on { border-color: var(--accent); }
    .photos .agrid button.on::after { content: "✓"; position: absolute; top: 4px; right: 4px; width: 20px; height: 20px; border-radius: 50%; background: var(--accent); color: var(--accent-ink); font-size: 13px; display: grid; place-items: center; }
    .photos .agrid button[disabled] { opacity: .35; cursor: default; }
    .photos .agrid .tag { position: absolute; left: 3px; bottom: 3px; font-size: 10px; background: rgba(0,0,0,.6); color: #fff; padding: 0 4px; border-radius: 4px; }
    @media (max-width: 560px) { .photos .moment { grid-template-columns: 1fr; } .photos .cards { grid-template-columns: 1fr 1fr; } }
  </style>`);

  async function api(method, path, body) {
    const r = await fetch("/api/photosong" + path, {method, headers: body ? {"Content-Type": "application/json"} : {}, body: body ? JSON.stringify(body) : undefined});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "Something went wrong");
    return j;
  }
  async function sse(url, body, onEvent) {
    const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || "Something went wrong"); }
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      buf += dec.decode(value, {stream: true});
      let i; while ((i = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
        if (!line.startsWith("data: ")) continue;
        const ev = JSON.parse(line.slice(6));
        if (ev.error) throw new Error(ev.error);
        onEvent(ev);
      }
    }
  }
  const status = (msg, bad) => { const s = $("psStatus"); if (s) { s.textContent = msg || ""; s.style.color = bad ? "var(--bad)" : ""; } };
  const thumb = (n) => `/api/photosong/${ps.id}/thumb/${n}.jpg`;
  const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString(undefined, {year: "numeric", month: "short", day: "numeric"}) : "no date";

  // ---------------- save edits (debounced)
  let saveT = null, pending = {};
  function queueSave(patch) {
    for (const [k, v] of Object.entries(patch)) {
      if (k === "photos") { pending.photos = pending.photos || {}; for (const p of v) pending.photos[p.n] = {...(pending.photos[p.n] || {}), ...p}; }
      else if (k === "moments") { pending.moments = pending.moments || {}; for (const m of v) pending.moments[m.id] = {...(pending.moments[m.id] || {}), ...m}; }
      else pending[k] = v;
    }
    clearTimeout(saveT); saveT = setTimeout(flush, 600);
  }
  async function flush() {
    clearTimeout(saveT);
    if (!ps.id || !Object.keys(pending).length) return;
    const b = {...pending}; pending = {};
    if (b.photos) b.photos = Object.values(b.photos);
    if (b.moments) b.moments = Object.values(b.moments);
    try { ps.d = await api("PUT", `/${ps.id}`, b); } catch (e) { status("Couldn't save: " + e.message, true); }
  }

  // ---------------- render
  function answersHTML(a) {
    const opt = (o, v) => Object.entries(o).map(([k, t]) => `<option value="${k}" ${k === v ? "selected" : ""}>${esc(t[0].toUpperCase() + t.slice(1))}</option>`).join("");
    return `<div class="grid2">
      <div><label class="f small" for="psFor">For <span class="hint">their name (optional)</span></label><input type="text" id="psFor" maxlength="60" value="${esc(a.for)}" placeholder="e.g. Ana"></div>
      <div><label class="f small" for="psOcc">Occasion</label><select id="psOcc">${opt(ps.d.occasions, a.occasion)}</select></div>
      <div><label class="f small" for="psFeel">Feel</label><select id="psFeel">${opt(ps.d.feels, a.feel)}</select></div>
      <div><label class="f small" for="psSince">Together since <span class="hint">(optional)</span></label><input type="text" id="psSince" maxlength="80" value="${esc(a.since)}" placeholder="e.g. met in 2019, married in 2023"></div>
    </div>
    <label class="f small" for="psInc">Must include <span class="hint">things no photo shows: inside jokes, what was said</span></label>
    <input type="text" id="psInc" maxlength="1500" value="${esc(a.include)}" placeholder="e.g. the night the car broke down in Big Sur; she calls me 'bear'">
    <label class="f small" for="psOut">Leave out <span class="hint">(optional)</span></label>
    <input type="text" id="psOut" maxlength="800" value="${esc(a.leave_out)}" placeholder="e.g. anything about work">`;
  }
  function answers() {
    return {for: $("psFor").value, occasion: $("psOcc").value, feel: $("psFeel").value, since: $("psSince").value,
            include: $("psInc").value, leave_out: $("psOut").value, language: (ps.d && ps.d.answers.language) || ""};
  }
  function cardHTML(p) {
    const child = p.child && ps.d.answers.occasion !== "other" ? `<div class="note">This one seems to show a child: maybe it belongs in a family song? ✕ leaves it out.</div>` : "";
    const err = p.vision_error ? `<div class="note">${esc(p.vision_error)}: press Read again.</div>` : "";
    const kind = p.kind && p.kind !== "photo" ? `<div class="note">Looks like a ${esc(p.kind)}, so it's left out (✕ to put it back).</div>` : "";
    return `<div class="card ${p.skip ? "skip" : ""}" data-n="${p.n}">
      <img src="${thumb(p.n)}" alt="" loading="lazy">
      <div class="b"><div class="when">${esc(fmtDate(p.date))}${p.date && !["camera", "Google Photos"].includes(p.date_src) ? ` <span title="from the ${esc(p.date_src)}">(≈)</span>` : ""}${p.place ? " · " + esc(p.place) : ""}</div>
        ${p.read ? `<textarea rows="2" data-k="caption" aria-label="What's in this photo">${esc(p.caption)}</textarea>
        <input type="text" data-k="detail" value="${esc(p.detail)}" aria-label="A detail for the song" placeholder="a detail for the song">` : `<div class="small muted">${p.vision_error ? "" : "Not read yet"}</div>`}
        ${child}${kind}${err}
        <div class="tools"><button class="btn small" data-t="star" aria-pressed="${!!p.star}" title="A favourite: the story leans on it, and it can be the cover">⭐</button>
          <button class="btn small" data-t="skip" aria-pressed="${!!p.skip}" title="Leave this photo out of the song">✕ Leave out</button>
          <button class="btn small" data-t="del" title="Remove the photo">🗑</button></div></div></div>`;
  }
  function albumHTML() {
    const a = ps.d && ps.d.album;
    if (!a) return `<div class="small muted" style="margin-top:4px">In Google Photos: open the album → Share → Create link, then paste it here. The originals come with their dates (and places, if the album shares them).</div>`;
    const room = 30 - ps.d.photos.length, left = a.items.filter(i => !i.added && !i.video).length;
    if (!ps.albumOpen) return `<div class="small muted" style="margin-top:6px">📁 ${esc(a.title || "Album")}: ${a.count} items, ${a.count - left - a.items.filter(i => i.video).length} added · <button class="linkbtn" type="button" id="psAlbumShow">Show the album</button></div>`;
    return `<div class="wbar" style="margin-top:8px"><b>📁 ${esc(a.title || "Album")}</b><span class="small muted">${a.count} items · tap photos to pick (room for ${Math.max(0, room)} more)</span></div>
      <div class="agrid" id="psAgrid">${a.items.map(i => `<button type="button" data-key="${esc(i.key)}" class="${ps.picked.has(i.key) ? "on" : ""}" ${i.added || i.video ? "disabled" : ""}
        title="${esc(i.date ? fmtDate(i.date) : "")}"><img src="${esc(i.thumb)}" alt="" loading="lazy">${i.added ? `<span class="tag">added</span>` : i.video ? `<span class="tag">video</span>` : ""}</button>`).join("")}</div>
      <div class="wbar"><button class="btn small accent" type="button" id="psAlbumAdd" ${ps.picked.size ? "" : "disabled"}>Add ${ps.picked.size || ""} picked</button>
        <button class="btn small" type="button" id="psAlbumSpread" ${left && room > 0 ? "" : "disabled"} title="Up to 20 photos spread evenly over the album's dates">✨ Pick for me</button>
        <button class="linkbtn" type="button" id="psAlbumHide">Hide the album</button></div>`;
  }
  function wireAlbum() {
    const show = $("psAlbumShow"), hide = $("psAlbumHide"), grid = $("psAgrid");
    if (show) show.onclick = () => { ps.albumOpen = true; $("psAlbum").innerHTML = albumHTML(); wireAlbum(); };
    if (hide) hide.onclick = () => { ps.albumOpen = false; $("psAlbum").innerHTML = albumHTML(); wireAlbum(); };
    if (grid) grid.onclick = (e) => {
      const b = e.target.closest("[data-key]"); if (!b || b.disabled) return;
      const k = b.dataset.key, room = 30 - ps.d.photos.length;
      if (ps.picked.has(k)) ps.picked.delete(k);
      else if (ps.picked.size < room) ps.picked.add(k);
      else return status(`There's room for ${room} more photo${room === 1 ? "" : "s"} (30 at most).`, true);
      b.classList.toggle("on", ps.picked.has(k));
      $("psAlbumAdd").disabled = !ps.picked.size; $("psAlbumAdd").textContent = `Add ${ps.picked.size || ""} picked`;
    };
    if ($("psAlbumAdd")) $("psAlbumAdd").onclick = () => importAlbum({keys: [...ps.picked]});
    if ($("psAlbumSpread")) $("psAlbumSpread").onclick = () => importAlbum({spread: true});
  }
  async function openAlbum() {
    const url = $("psAlbumUrl").value.trim();
    if (!url || ps.busy) return;
    ps.busy = true; await flush();
    const btn = $("psAlbumGo"); btn.disabled = true; btn.textContent = "Opening…";
    try {
      const {album} = await api("POST", `/${ps.id}/album`, {url});
      ps.d.album = album; ps.albumOpen = true; ps.picked = new Set();
      $("psAlbum").innerHTML = albumHTML(); wireAlbum(); $("psAlbumUrl").value = "";
      status(`${album.count} items in “${album.title || "the album"}”.`);
    } catch (e) { status(e.message, true); }
    finally { ps.busy = false; btn.disabled = false; btn.textContent = "Open another album"; }
  }
  async function importAlbum(body) {
    if (ps.busy) return; ps.busy = true; await flush();
    let summary = null;
    try {
      await sse(`/api/photosong/${ps.id}/album/import`, body, (ev) => {
        if (ev.importing) status(`Downloading the original ${ev.importing} of ${ev.of}…`);
        if (ev.done) summary = ev;
      });
      ps.picked = new Set(); ps.busy = false; ps.albumOpen = false;
      await open(ps.id);
      if (summary) {
        const sk = summary.skipped.length ? ` Skipped ${summary.skipped.length}: ${[...new Set(summary.skipped)].join("; ")}.` : "";
        status(`Added ${summary.added} photo${summary.added === 1 ? "" : "s"} from the album.${sk}`, summary.skipped.length > 0 && !summary.added);
      }
    } catch (e) { ps.busy = false; await open(ps.id); status(e.message, true); }
  }
  function momentsHTML() {
    const ms = ps.d.moments || [];
    if (!ms.length) return "";
    const st = ps.d.story;
    return `${st ? `<div class="grid2">
        <div><label class="f small" for="psTitle">Title</label><input type="text" id="psTitle" maxlength="80" value="${esc(ps.d.title || st.title)}"></div>
        <div><label class="f small" for="psHook">Chorus hook</label><input type="text" id="psHook" maxlength="200" value="${esc(st.hook)}"></div></div>` : ""}
      ${ms.map(m => `<div class="moment" data-m="${esc(m.id)}">
        <div class="th">${m.photos.slice(0, 4).map(n => `<img src="${thumb(n)}" alt="">`).join("")}</div>
        <div><div class="w">${esc(m.when || "date unknown")}${m.where ? " · " + esc(m.where) : ""} · ${m.photos.length} photo${m.photos.length > 1 ? "s" : ""}</div>
          ${st ? `<input type="text" data-k="summary" value="${esc(m.summary)}" aria-label="What this moment is">` : ""}</div>
        ${st ? `<select data-k="part" aria-label="Which part of the song">${Object.entries(PARTS).map(([k, t]) => `<option value="${k}" ${k === m.part ? "selected" : ""}>${t}</option>`).join("")}</select>` : ""}
      </div>`).join("")}`;
  }
  function render() {
    if (!ps.d) {
      box.innerHTML = `<div class="wbar" style="margin-top:0"><b>📷 A song from your photos</b><span class="small muted">made for love songs: anniversaries, Valentine's, weddings</span>
        <button class="btn small" type="button" id="psClose" style="margin-left:auto">Close</button></div>
        <div id="psDrafts" class="small"></div>
        <div class="wbar"><button class="btn primary" type="button" id="psNew">Start a new one</button><span class="small muted" id="psStatus"></span></div>`;
      $("psClose").onclick = () => { box.hidden = true; };
      $("psNew").onclick = startNew;
      listDrafts();
      return;
    }
    const d = ps.d, read = d.photos.filter(p => p.read).length, use = d.photos.filter(p => !p.skip).length;
    box.innerHTML = `<div class="wbar" style="margin-top:0"><b>📷 A song from your photos</b>
        <span class="small muted">🔒 private to you, never on the radio</span>
        <span style="margin-left:auto"><button class="linkbtn" type="button" id="psOthers">Other photo songs</button> ·
        <button class="linkbtn" type="button" id="psDelete" style="color:var(--bad)">Delete these photos</button> ·
        <button class="btn small" type="button" id="psClose">Close</button></span></div>
      <h4><span class="n">1</span> Who it's for</h4>${answersHTML(d.answers)}
      <h4><span class="n">2</span> Your photos <span class="small muted">${d.photos.length}/30 · JPEG, PNG, HEIC</span></h4>
      <label class="drop" id="psDrop">Drop photos here, or <b style="color:var(--accent)">pick them</b>
        <input type="file" id="psFile" accept="image/*,.heic,.heif" multiple hidden></label>
      <div class="albumbar"><input type="url" id="psAlbumUrl" placeholder="…or paste a Google Photos album link (photos.app.goo.gl/…)" aria-label="Google Photos album link">
        <button class="btn small" type="button" id="psAlbumGo">${d.album ? "Open another album" : "📁 Open album"}</button></div>
      <div id="psAlbum">${albumHTML()}</div>
      <div class="cards" id="psCards">${d.photos.map(cardHTML).join("")}</div>
      <div class="wbar"><button class="btn ${read < d.photos.length ? "primary" : ""}" type="button" id="psRead" ${d.photos.length ? "" : "disabled"}>
        ${read && read < d.photos.length ? `Read the ${d.photos.length - read} new photo${d.photos.length - read > 1 ? "s" : ""}` : read ? "Photos read ✓" : "Read the photos"}</button>
        <span class="small muted">${read ? `${read} read · ${use} in the song` : "about 5–10 seconds a photo"}</span></div>
      <h4><span class="n">3</span> The story</h4>
      <div id="psMoments">${momentsHTML()}</div>
      <div class="wbar"><button class="btn ${read && !d.story ? "primary" : ""}" type="button" id="psStory" ${read ? "" : "disabled"}>${d.story ? "Write the story again" : "Write the story"}</button>
        ${d.story ? `<span class="small muted">by ${esc(d.story.writer || "the writer")} · change any line, then write the lyrics</span>` : ""}</div>
      <h4><span class="n">4</span> The lyrics</h4>
      <div class="wbar"><button class="btn ${d.story ? "primary" : ""}" type="button" id="psLyrics" ${d.story ? "" : "disabled"}>${d.lyrics ? "Write them again" : "Write the lyrics"}</button>
        <span class="small muted" id="psStatus"></span></div>
      <pre id="psPreview" ${d.lyrics ? "" : "hidden"}>${esc(d.lyrics)}</pre>
      <div class="wbar" id="psUseBar" ${d.lyrics ? "" : "hidden"}>
        <button class="btn small accent" type="button" id="psUse">Use these lyrics and style</button>
        <input type="text" id="psFeedback" placeholder="Change something… e.g. more about Lisbon, a gentler chorus" style="flex:1 1 220px">
        <button class="btn small" type="button" id="psRewrite">Rewrite</button></div>`;
    wire();
  }

  async function listDrafts() {
    try {
      const {drafts} = await api("GET", "");
      ps.id = null;
      $("psDrafts").innerHTML = drafts.length ? `<div class="muted" style="margin:8px 0 4px">Your photo songs</div>` + drafts.map(x => `
        <div class="row" style="align-items:center;gap:8px;margin:4px 0">
          ${x.cover ? `<img src="/api/photosong/${esc(x.id)}/thumb/${x.cover}.jpg" alt="" style="width:40px;height:40px;object-fit:cover;border-radius:6px;flex:0 0 auto">` : ""}
          <span style="flex:1 1 auto">${esc(x.title || "Untitled")} <span class="muted">· ${x.photos} photos${x.jobs.length ? ` · ${x.jobs.length} song${x.jobs.length > 1 ? "s" : ""}` : ""}</span></span>
          <button class="btn small" type="button" data-open="${esc(x.id)}" style="flex:0 0 auto">Open</button></div>`).join("") : "";
      $("psDrafts").querySelectorAll("[data-open]").forEach(b => b.onclick = () => open(b.dataset.open));
    } catch (e) { status(e.message, true); }
  }
  async function startNew() {
    try {
      ps.d = await api("POST", "", {answers: {}}); ps.id = ps.d.id; remember(ps.id); render();
    } catch (e) { status(e.message, true); }
  }
  async function open(id) {
    try { ps.d = await api("GET", `/${id}`); ps.id = id; remember(id); render(); }
    catch (e) { remember(null); ps.d = null; render(); }
  }

  function wire() {
    $("psClose").onclick = async () => { await flush(); box.hidden = true; };
    $("psOthers").onclick = async () => { await flush(); ps.d = null; remember(null); render(); };
    $("psDelete").onclick = async () => {
      const songs = ps.d.jobs.length;
      if (!confirm(`Delete these ${ps.d.photos.length} photos and everything read from them?` + (songs ? `\n\nThe ${songs} song${songs > 1 ? "s" : ""} made from them stay${songs > 1 ? "" : "s"} in your library.` : ""))) return;
      try { await api("DELETE", `/${ps.id}?keep_song=1`); ps.d = null; remember(null); setPhotoSong(null); render(); } catch (e) { status(e.message, true); }
    };
    for (const id of ["psFor", "psOcc", "psFeel", "psSince", "psInc", "psOut"]) $(id).onchange = () => queueSave({answers: answers()});
    $("psAlbumGo").onclick = openAlbum;
    $("psAlbumUrl").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); openAlbum(); } };
    wireAlbum();
    const drop = $("psDrop"), file = $("psFile");
    file.onchange = () => upload([...file.files]);
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
    drop.ondragleave = () => drop.classList.remove("over");
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); upload([...e.dataTransfer.files].filter(f => /^image\//.test(f.type) || /\.hei[cf]$/i.test(f.name))); };
    $("psCards").oninput = (e) => {
      const k = e.target.dataset.k, c = e.target.closest(".card"); if (!k || !c) return;
      queueSave({photos: [{n: +c.dataset.n, [k]: e.target.value}]});
    };
    $("psCards").onclick = async (e) => {
      const b = e.target.closest("[data-t]"), c = e.target.closest(".card"); if (!b || !c) return;
      const n = +c.dataset.n, p = ps.d.photos.find(x => x.n === n);
      if (b.dataset.t === "del") {
        try { await api("DELETE", `/${ps.id}/photos/${n}`); ps.d.photos = ps.d.photos.filter(x => x.n !== n); ps.d.moments = []; render(); } catch (err) { status(err.message, true); }
        return;
      }
      p[b.dataset.t] = !p[b.dataset.t];
      queueSave({photos: [{n, [b.dataset.t]: p[b.dataset.t]}]});
      if (b.dataset.t === "skip") { await flush(); render(); } else b.setAttribute("aria-pressed", p.star);
    };
    $("psMoments").oninput = $("psMoments").onchange = (e) => {
      const k = e.target.dataset.k, m = e.target.closest(".moment");
      if (k && m) queueSave({moments: [{id: m.dataset.m, [k]: e.target.value}]});
      if (e.target.id === "psTitle") queueSave({title: e.target.value, story: {title: e.target.value}});
      if (e.target.id === "psHook") queueSave({story: {hook: e.target.value}});
    };
    $("psRead").onclick = readPhotos;
    $("psStory").onclick = writeStory;
    $("psLyrics").onclick = () => writeLyrics();
    $("psRewrite").onclick = () => { const f = $("psFeedback").value.trim(); if (f) writeLyrics(f); };
    $("psUse").onclick = useIt;
  }

  async function upload(files) {
    if (!files.length || ps.busy) return;
    ps.busy = true;
    await flush();
    let ok = 0, failed = [];
    for (const [i, f] of files.entries()) {
      status(`Adding photo ${i + 1} of ${files.length}…`);
      try {
        const r = await fetch(`/api/photosong/${ps.id}/photos`, {method: "POST", body: f, headers: {
          "Content-Type": f.type || "application/octet-stream", "X-Filename": encodeURIComponent(f.name), "X-Last-Modified": String(f.lastModified || "")}});
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error || "failed");
        ok++;
      } catch (e) { failed.push(`${f.name}: ${e.message}`); if (/Up to \d+ photos/.test(e.message)) break; }
    }
    ps.busy = false;
    await open(ps.id);
    status(failed.length ? `Added ${ok}. Not added: ${failed.join("; ")}` : `Added ${ok} photo${ok > 1 ? "s" : ""}.`, failed.length > 0);
  }

  async function readPhotos() {
    if (ps.busy) return; ps.busy = true; await flush();
    const btn = $("psRead"); btn.disabled = true;
    try {
      await sse(`/api/photosong/${ps.id}/analyze`, {}, (ev) => {
        if (ev.reading) btn.textContent = `Reading photo ${ev.i} of ${ev.of}…`;
        if (ev.photo) {
          const p = ps.d.photos.find(x => x.n === ev.photo.n);
          if (p) { Object.assign(p, ev.photo); const c = box.querySelector(`.card[data-n="${p.n}"]`); if (c) c.outerHTML = cardHTML(p); }
        }
      });
      ps.busy = false; await open(ps.id);
      $("psStory").focus();
    } catch (e) { ps.busy = false; await open(ps.id); status("Reading the photos stopped: " + e.message, true); }
  }

  async function writeStory() {
    if (ps.busy) return; ps.busy = true; await flush();
    const btn = $("psStory"); btn.disabled = true; btn.textContent = "Writing the story…";
    try { ps.d = await api("POST", `/${ps.id}/story`); ps.busy = false; render(); }
    catch (e) { ps.busy = false; btn.disabled = false; btn.textContent = "Write the story"; status(e.message, true); }
  }

  async function writeLyrics(feedback) {
    if (ps.busy) return; ps.busy = true; await flush();
    const pre = $("psPreview"), btn = $("psLyrics");
    const body = {action: "photos", photosong: ps.id, style: $("style").value.trim() || ps.d.style || "", title: ($("psTitle") || {}).value || ""};
    if (feedback) { body.feedback = feedback; body.lyrics = ps.d.lyrics; }
    pre.hidden = false; pre.textContent = ""; $("psUseBar").hidden = true; btn.disabled = true;
    status(feedback ? "Rewriting…" : "Writing…");
    let done = null;
    try {
      await sse("/api/lyrics", body, (ev) => {
        if (ev.source) status(`${feedback ? "Rewriting" : "Writing"} with ${ev.source}…`);
        if (ev.delta) { pre.textContent += ev.delta; pre.scrollTop = pre.scrollHeight; }
        if (ev.done) done = ev;
      });
      if (!done) throw new Error("the writer stopped early");
      ps.d.lyrics = done.lyrics; if (done.title) ps.d.title = done.title;
      pre.textContent = (done.title ? `Title: ${done.title}\n\n` : "") + done.lyrics;
      queueSave({lyrics: done.lyrics, ...(done.title ? {title: done.title} : {})}); await flush();
      status(`Written by ${done.source} in ${done.seconds}s`);
      $("psUseBar").hidden = false; $("psFeedback").value = "";
    } catch (e) { status(e.message, true); if (ps.d.lyrics) { pre.textContent = ps.d.lyrics; $("psUseBar").hidden = false; } }
    finally { ps.busy = false; btn.disabled = false; btn.textContent = "Write them again"; }
  }

  function useIt() {
    if (!ps.d.lyrics) return;
    $("lyrics").value = ps.d.lyrics;
    $("title").value = ps.d.title || (ps.d.story || {}).title || "";
    const suggested = ps.d.style || (ps.d.story || {}).style || "";
    if (suggested && (!$("style").value.trim() || confirm("Use the style suggested for these photos?\n\n" + suggested))) $("style").value = suggested;
    if (mode !== "song") setMode("song");
    setEngine("minimax");                                    // clearer vocals: the default for a song to give someone
    if ($("takes") && +$("takes").value < 2) $("takes").value = "2";
    setPhotoSong(ps.id, ps.d.photos.filter(p => !p.skip).length);
    syncChips(); lyricStats(); saveForm();
    box.hidden = true;
    $("go").scrollIntoView({behavior: "smooth", block: "center"});
  }

  // the form remembers which photo draft its lyrics came from (sent with Create song, cleared by Clear)
  window.setPhotoSong = function (id, n) {
    window.photoSong = id || null;
    try { id ? localStorage.setItem(KEY + ".form", JSON.stringify({id, n})) : localStorage.removeItem(KEY + ".form"); } catch {}   // survives a reload
    const note = $("photoNote");
    if (!note) return;
    note.hidden = !id;
    note.innerHTML = id ? `🔒📷 Made from ${n ? n + " of your photos" : "your photos"}: only you will see this song, and it never plays on the radio. MiniMax, 2 takes. <button class="linkbtn" type="button" id="photoNoteOff">Not from photos</button>` : "";
    if (id) $("photoNoteOff").onclick = () => setPhotoSong(null);
  };

  try { const f = JSON.parse(localStorage.getItem(KEY + ".form") || "null"); if (f && f.id) setPhotoSong(f.id, f.n); } catch {}

  $("openPhotos").onclick = async () => {
    box.hidden = !box.hidden;
    if (box.hidden) return flush();
    const last = recall();
    if (last && !ps.d) return open(last);
    render();
  };
})();
