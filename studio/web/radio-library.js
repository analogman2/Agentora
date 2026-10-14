// Agentora radio page, part 3 of 5: The pages: routes, the path bar, song / station / Library / list pages, the list controls (search, sort, select), and playing from the Library.
// Plain scripts loaded in order by radio.html; they share one global scope.
// ---------------- views, laid out like folders (hash routes):
//   #now                         what's playing
//   #lib                         Library: ♥ My playlist, 🕘 Listen history, and every station
//   #lib/playlist  #lib/history  a song list
//   #lib/<station id>            a station: all its songs, then "About this station"
//   #lib/<folder>/<song id>      a song, inside the folder you opened it from (playlist, history or its station)
//   #search/<q>
// Older addresses (#browse, #collection/..., #station/<id>, #song/<id>) still work.
const view = {tab: "now", sub: "playlist", id: null, parent: null, q: ""};
function route() {
  let [tab, a, b] = decodeURIComponent(location.hash.slice(1) || "now").split("/");
  if (tab === "browse") { tab = "lib"; a = undefined; }
  if (tab === "collection") { tab = "lib"; a = a === "stations" ? undefined : (a || "playlist"); }
  if (tab === "station") { tab = "lib"; b = undefined; }
  view.parent = null; sel.on = false; sel.ids.clear();
  if (tab === "song") { view.tab = "song"; view.id = a; }
  else if (tab === "lib") {
    if (!a) view.tab = "lib";
    else if (b) { view.tab = "song"; view.id = b; view.parent = a; }
    else if (a === "playlist" || a === "history" || a === "all") { view.tab = "collection"; view.sub = a; }
    else { view.tab = "station"; view.id = a; }
  } else if (tab === "search") { view.tab = "search"; view.q = a || ""; }
  else view.tab = "now";
  const on = view.tab === "now" ? "now" : view.tab === "search" ? "" : "lib";
  document.querySelectorAll(".tabs a").forEach(x => x.classList.toggle("on", x.dataset.tab === on));
  if (view.tab !== "search") $("q").value = "";
  window.scrollTo(0, 0); renderView();
}
window.addEventListener("hashchange", route);
let renderSeq = 0;
async function renderView() {
  const seq = ++renderSeq, page = $("page");
  if (!R) { page.innerHTML = `<p class="empty">Tuning in…</p>`; return; }
  if (document.querySelector(".row.previewing")) return;          // don't interrupt a preview
  let html = "";
  if (view.tab === "now") html = nowView();
  else if (view.tab === "lib") html = await libView();
  else if (view.tab === "station") html = await stationView(view.id);
  else if (view.tab === "collection") html = await collectionView();
  else if (view.tab === "search") html = await searchView(view.q);
  else if (view.tab === "song") html = await songView(view.id);
  if (seq !== renderSeq) return;
  const typing = document.activeElement && document.activeElement.classList.contains("lfilter") ? document.activeElement : null;
  const caret = typing && [typing.dataset.list, typing.selectionStart, typing.selectionEnd];
  page.innerHTML = html;
  lpMark();                                        // the row of the library song that's playing
  if (caret) {                                     // a refresh while typing in a list's search box keeps the cursor
    const f = page.querySelector(`.lfilter[data-list="${CSS.escape(caret[0])}"]`);
    if (f) { f.focus(); f.setSelectionRange(caret[1], caret[2]); }
  }
  if (view.tab === "now") status(statusText);
}

// ---------- the path bar: Library › Station › Song, every step clickable, ↑ goes up one level
const FOLDERS = {all: ["🎵 All songs", "#lib/all"], playlist: ["♥ My playlist", "#lib/playlist"], history: ["🕘 Listen history", "#lib/history"]};
function folderCrumb(f) {
  if (FOLDERS[f]) return FOLDERS[f];
  const s = stOf(f);
  return [s ? s.name : "Station", `#lib/${encodeURIComponent(f)}`];
}
function crumbs(...steps) {
  const all = [["Library", "#lib"], ...steps], up = all.length > 1 ? all[all.length - 2][1] : null;
  return `<nav class="crumbs" aria-label="Where you are">${up ? `<a class="up" href="${up}" title="Up to ${esc(all[all.length - 2][0])}" aria-label="Up one level">↑</a>` : ""}
    ${all.map(([label, href], i) => i === all.length - 1 ? `<span class="here" aria-current="page">${esc(label)}</span>` : `<a href="${href}">${esc(label)}</a>`).join('<span class="sep" aria-hidden="true">›</span>')}</nav>`;
}
const songHref = (t, folder) => (folder || t.station) ? `#lib/${encodeURIComponent(folder || t.station)}/${encodeURIComponent(t.id)}` : `#song/${encodeURIComponent(t.id)}`;

// ---------- song page: cover, lyrics, story, scores and actions for one song
async function songView(id) {
  let t;
  try { t = await api(`/api/radio/tracks/${encodeURIComponent(id)}`); } catch (e) { return crumbs(["Song", "#"]) + `<p class="empty">${esc(e.message)}</p>`; }
  const folder = view.parent || t.station, st = stOf(t.station), c = t.critic || {};
  // the songs around this one in the folder it was opened from, for ‹ previous / next ›
  let sibs = [];
  if (folder === "all") sibs = cache.all || await loadAll();
  else if (folder === "playlist") sibs = cache.playlist || await loadPlaylist();
  else if (folder === "history") sibs = cache.listened || [];
  else if (folder) sibs = cache.station[folder] || await loadStation(folder);
  const lkey = folder === "all" || folder === "playlist" || folder === "history" ? folder : "station:" + folder;
  const lsort = store.get("sort." + lkey, "as-is");
  if (lsort !== "as-is" && LIST_SORTS[lsort]) sibs = [...sibs].sort(LIST_SORTS[lsort][1]);   // the order you sorted the list in
  const k = sibs.findIndex(x => x.id === id), prev = k > 0 ? sibs[k - 1] : null, next = k >= 0 && k < sibs.length - 1 ? sibs[k + 1] : null;
  const facts = [
    t.story ? ["Story idea", t.story] : null,
    t.wildcard ? ["🎲 Variety twist", t.wildcard] : null,
    t.writer ? ["Written by", t.writer] : null,
    c.score != null ? ["Quality score", `${c.score.toFixed(3)}${c.bar > 0 ? ` (station bar ${c.bar.toFixed(3)})` : ""}${c.takes ? ` · best of ${c.takes} take${c.takes === 1 ? "" : "s"}` : ""}`] : null,
    t.seconds ? ["Length", mmss(t.seconds)] : null,
    t.played_at ? ["Last played", whenText(t.played_at) + (t.airings > 1 ? ` · aired ${t.airings} times` : "")] : ["Status", t.status === "approved" ? "Ready, not aired yet" : t.status || ""],
    t.requested ? ["Made", t.liked_from ? "On request — more like another song" : "On request"] : null,
  ].filter(Boolean);
  return crumbs(...(folder ? [folderCrumb(folder)] : []), [t.title, "#"]) + `<section class="songpage">
    <div class="cover"><img src="${trackArt(t)}" alt=""></div>
    <div class="info">
      ${st ? `<a class="st" href="#lib/${esc(st.id)}">📻 ${esc(st.name)}</a>` : ""}
      <h1>${esc(t.title)}</h1>
      <div class="style">${esc(t.style)}</div>
      <div class="acts">
        <button class="pill primary" data-act="song-play" data-audio="${esc(t.audio)}">▶ Preview</button>
        <button class="pill ${t.feedback === "keep" ? "primary" : ""}" data-act="song-fb" data-kind="${t.feedback === "keep" ? "clear" : "keep"}" title="Make more like this" aria-pressed="${t.feedback === "keep"}">👍</button>
        <button class="pill ${t.feedback === "dislike" ? "primary" : ""}" data-act="song-fb" data-kind="${t.feedback === "dislike" ? "clear" : "dislike"}" title="Fewer like this" aria-pressed="${t.feedback === "dislike"}">👎</button>
        <button class="pill ${t.in_playlist ? "primary" : ""}" data-act="song-plus" data-on="${t.in_playlist ? 1 : 0}">${t.in_playlist ? "✓ In my playlist" : "+ Playlist"}</button>
        <a class="pill" href="/#rewrite=${esc(t.id)}" data-act="song-rewrite" data-id="${esc(t.id)}" title="Improve this song in the Music Studio: everything is filled in and the AI suggests changes">✏️ Rewrite</a>
        ${st ? `<button class="pill" data-act="song-more" data-station="${esc(st.id)}" title="Write and record a new song in the spirit of this one">🎵 More like this</button>` : ""}
        <button class="pill" data-act="song-art" title="Draw a new cover">🎨 New cover</button>
        <a class="pill" href="${esc(t.audio)}?download=1">⤓ Download</a>
      </div>
      <dl class="facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      ${t.lyrics ? `<p class="label" style="margin-top:18px">Lyrics</p><pre class="ly songly">${esc(t.lyrics)}</pre>` : `<p class="small muted" style="margin-top:18px">Instrumental</p>`}
    </div></section>
    ${prev || next ? `<nav class="sibs" aria-label="Other songs in ${esc(folderCrumb(folder)[0])}">
      ${prev ? `<a href="${songHref(prev, folder)}"><span class="small muted">‹ Previous</span><b>${esc(prev.title)}</b></a>` : "<span></span>"}
      <span class="small muted">${k + 1} of ${sibs.length}</span>
      ${next ? `<a class="nx" href="${songHref(next, folder)}"><span class="small muted">Next ›</span><b>${esc(next.title)}</b></a>` : "<span></span>"}</nav>` : ""}`;
}
function stationTile(s) {
  const air = R.on && R.station === s.id;
  const sub = s.id === RERUNS ? s.meta : s.kind === "folder" && s.analysis.status !== "done" ? "learning from your music" : `${s.ready} ready · ${s.stats.played} played`;
  return `<a class="tile" href="#lib/${esc(s.id)}"><div class="art"><img src="${stationArt(s)}" alt="" loading="lazy">${air ? '<span class="on">ON AIR</span>' : ""}</div>
    <div class="t1">${esc(s.name)}</div><div class="t2">${esc(sub)}</div></a>`;
}
const newTile = `<button class="tile" data-act="new"><div class="art"><div class="new">+</div></div><div class="t1">Create station</div><div class="t2">describe it, or learn from your music</div></button>`;
// ☑ Select: tick songs in a list (or all of them) and act on them together
const sel = {on: false, ids: new Set(), list: []};
function selBar(list) {
  sel.list = list.map(t => t.id);
  if (!sel.on) return `<button class="pill" data-act="sel-on" title="Tick songs to play next, add to your playlist or delete together">☑ Select</button>`;
  const n = sel.ids.size, all = list.length > 0 && list.every(t => sel.ids.has(t.id)), off = n ? "" : "disabled";
  return `<div class="selbar"><label><input type="checkbox" data-act="sel-all" ${all ? "checked" : ""}> Select all</label>
    <span class="small muted">${n} selected</span>
    <button class="pill" data-act="sel-next" ${off} title="Play these next on the radio, for everyone listening">📻 Play next</button>
    <button class="pill" data-act="sel-plus" ${off}>+ Playlist</button>
    <button class="pill" data-act="sel-minus" ${off}>− Playlist</button>
    <button class="pill danger" data-act="sel-del" ${off}>Delete</button>
    <button class="pill" data-act="sel-off">Done</button></div>`;
}
async function selAction(act) {
  const ids = sel.list.filter(id => sel.ids.has(id));           // in the order shown
  if (!ids.length) return;
  try {
    if (act === "sel-next") { await post("/api/radio/upnext", {ids}); toast(`📻 ${ids.length} song${ids.length === 1 ? "" : "s"} will play next.`); }
    if (act === "sel-plus" || act === "sel-minus") {
      for (const id of ids) await post("/api/radio/playlist", {track: id, on: act === "sel-plus"});
      cache.playlist = null; toast(act === "sel-plus" ? `Added ${ids.length} to your playlist.` : `Removed ${ids.length} from your playlist.`);
    }
    if (act === "sel-del") {
      if (!confirm(`Delete ${ids.length} song${ids.length === 1 ? "" : "s"} for good? This removes the recordings too.`)) return;
      for (const id of ids) await api(`/api/jobs/${encodeURIComponent(id)}`, {method: "DELETE"});
      cache.station = {}; cache.playlist = null; cache.all = null; sel.ids.clear(); toast(`Deleted ${ids.length} song${ids.length === 1 ? "" : "s"}.`);
    }
  } catch (e) { toast(e.message); }
  loadHistory(); refresh(); renderView();
}
function songRow(t, opts = {}) {
  const fb = t.feedback === "keep" ? "👍" : t.feedback === "dislike" ? "👎" : t.feedback === "skip" ? "⏭" : "";
  const sub = [opts.when, opts.noStation ? "" : t.station_name, opts.extra, t.style].filter(Boolean).join(" · ");
  return `<li class="row${sel.on && sel.ids.has(t.id) ? " picked" : ""}" data-id="${esc(t.id)}" data-audio="${esc(t.audio)}">
    ${sel.on && opts.select !== false ? `<input type="checkbox" class="selbox" data-act="sel-one" aria-label="Select “${esc(t.title)}”" ${sel.ids.has(t.id) ? "checked" : ""}>` : ""}
    <div class="thumb" data-act="preview" title="Preview this song"><img src="${trackArt(t)}" alt="" loading="lazy"><span class="pv">▶</span></div>
    <div class="tx"><a class="a" href="${songHref(t, opts.folder)}" title="Open “${esc(t.title)}”">${esc(t.title)}</a><div class="b" title="${esc(t.style)}">${esc(sub)}</div></div>
    <span class="fb">${fb}</span><span class="dur">${t.seconds ? mmss(t.seconds) : ""}</span>
    <span class="acts"><button class="plus ${t.in_playlist ? "in" : ""}" data-act="plus" data-on="${t.in_playlist ? 1 : 0}" title="${t.in_playlist ? "In your playlist — click to remove" : "Add to my playlist"}" aria-label="${t.in_playlist ? "Remove from playlist" : "Add to playlist"}">${t.in_playlist ? "✓" : "+"}</button>
      <a class="dl" href="${esc(t.audio)}?download=1" title="Download MP3" aria-label="Download">⤓</a></span></li>`;
}
function whenText(ts) {
  const d = new Date(ts * 1000), now = new Date(), time = d.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"});
  if (d.toDateString() === now.toDateString()) return time;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  return d.toDateString() === y.toDateString() ? "Yesterday " + time : d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
}

function nowView() {
  const t = current, st = stOf(t ? t.station : selected);
  if (!t && !lid) {
    const stations = [rerunStation(), ...R.stations];
    return `<section class="section"><p class="label">Start listening</p><h1 style="font-size:30px;margin:0 0 6px">Pick a station</h1>
      <p class="muted" style="margin:0 0 24px">Every station writes and sings brand-new songs. Everyone listening hears the same broadcast.
        Press a station to play it, or open the <a href="#lib">Library</a> to look through stations and songs.</p>
      <p style="margin:0 0 20px"><button class="pill primary" data-act="random" title="Play a station picked at random">🎲 Random station</button></p>
      <div class="grid">${stations.map(s => stationTile(s).replace('class="tile"', `class="tile" data-play="${esc(s.id)}"`)).join("")}${newTile}</div></section>`;
  }
  const chips = t ? [t.airing === "rerun" ? "Rerun" : t.airing === "runner-up" ? "Runner-up take" : t.airing === "history" ? "Replay" : t.airing === "request" ? "Requested" : null, t.seconds ? mmss(t.seconds) : null,
    t.mode === "instrumental" || !t.lyrics ? "Instrumental" : "Vocals"].filter(Boolean) : [];
  const stId = t ? t.station : st && st.id;
  return `<section class="np">
    <div class="cover"><img src="${t ? trackArt(t) : stationArt(st)}" alt=""></div>
    <div>
      <a class="st" href="#lib/${esc(stId || "")}">📻 ${esc(t ? (t.station_name || (st && st.name)) : st ? st.name : "")}${t && t.airing === "rerun" ? " · via Reruns" : ""}</a>
      <h1>${t ? `<a href="${songHref(t)}" style="color:inherit;text-decoration:none" title="Lyrics, story and details">${esc(t.title)}</a>` : "Next song on the way…"}</h1>
      <div class="style">${t ? esc(t.style) : ""}</div>
      <div class="meta">${chips.map(c => `<span class="chip">${esc(c)}</span>`).join("")}</div>
      <div class="acts">${t ? `<button class="pill ${t.in_playlist ? "primary" : ""}" data-act="np-plus">${t.in_playlist ? "✓ In my playlist" : "+ Add to my playlist"}</button>
        <a class="pill" href="${songHref(t)}">Song details</a>
        <a class="pill" href="${esc(t.audio)}?download=1">⤓ Download</a>
        <button class="pill" data-act="np-art" title="Draw a new cover for this song">🎨 New cover</button>` : ""}
        <button class="pill" data-act="random" title="Play a station picked at random">🎲 Random</button></div>
      ${t && t.lyrics ? `<details><summary>Lyrics</summary><pre class="ly">${esc(t.lyrics)}</pre></details>` : ""}
      <div class="status" id="npStatus"></div>
    </div></section>
    ${cache.listened && cache.listened.length ? `<section class="section"><p class="label"><a href="#lib/history" style="color:inherit">Recently played ›</a></p><ul class="rows">${cache.listened.slice(0, 5).map(x => songRow(x, {when: whenText(x.listened_at), folder: "history"})).join("")}</ul></section>` : ""}`;
}

// ---------- Library: the top folder
async function libView() {
  const pl = cache.playlist || await loadPlaylist(), h = cache.listened || [], tiles = store.get("libTiles", false);
  const sts = [rerunStation(), ...R.stations];
  const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const stRow = (s) => {
    const air = R.on && R.station === s.id, playing = air && lid;
    const sub = s.id === RERUNS ? s.meta : s.kind === "folder" && s.analysis.status !== "done" ? "learning from your music"
      : `${count(s.stats.played, "song", "songs")} played${s.ready ? ` · ${s.ready} ready` : ""}${s.stats.kept ? ` · 👍 ${s.stats.kept}` : ""}`;
    return `<li><a class="frow" href="#lib/${esc(s.id)}"><img class="fart" src="${stationArt(s)}" alt="" loading="lazy">
      <span class="tx"><b>${esc(s.name)}</b><span>${esc(sub)}</span></span>${air ? '<span class="air">ON AIR</span>' : ""}
      <button class="fplay" data-play="${esc(s.id)}" title="${playing ? "Playing now" : "Play this station"}" aria-label="Play ${esc(s.name)}">${playing ? "≋" : "▶"}</button><span class="chev" aria-hidden="true">›</span></a></li>`;
  };
  const all = cache.all;
  if (!all) loadAll().then(() => { if (view.tab === "lib") renderView(); });   // fill in the count
  return crumbs() + `<ul class="flist">
      <li><a class="frow" href="#lib/all"><span class="fic">🎵</span><span class="tx"><b>All songs</b><span>${all ? `${count(all.length, "song", "songs")} · every station and the Music Studio` : "every song from every station and the Music Studio"}</span></span><span class="chev" aria-hidden="true">›</span></a></li>
      <li><a class="frow" href="#lib/playlist"><span class="fic">♥</span><span class="tx"><b>My playlist</b><span>${count(pl.length, "song", "songs")} you saved</span></span><span class="chev" aria-hidden="true">›</span></a></li>
      <li><a class="frow" href="#lib/history"><span class="fic">🕘</span><span class="tx"><b>Listen history</b><span>${h.length ? `${count(h.length, "song", "songs")} played recently` : "nothing played yet"}</span></span><span class="chev" aria-hidden="true">›</span></a></li></ul>
    <section class="section"><div class="lhead"><span style="display:flex;align-items:center;gap:12px"><p class="label">📻 Stations · ${sts.length}</p>
      <button class="pill" data-act="random" title="Play a station picked at random">🎲 Random</button></span>
      <span class="seg" role="group" aria-label="Show stations as"><button data-act="lib-layout" data-v="0" aria-pressed="${!tiles}" title="List">☰ List</button><button data-act="lib-layout" data-v="1" aria-pressed="${tiles}" title="Covers">▦ Covers</button></span></div>
      ${tiles ? `<div class="grid">${sts.map(stationTile).join("")}${newTile}</div>`
        : `<ul class="flist">${sts.map(stRow).join("")}<li><button class="frow" data-act="new"><span class="fic">＋</span><span class="tx"><b>Create station</b><span>describe it, or learn from your music</span></span></button></li></ul>`}</section>`;
}

// ---------- a station: its songs first, then what it is and its settings
async function stationView(id) {
  const s = stOf(id); if (!s) return crumbs(["Station", "#"]) + `<p class="empty">That station doesn't exist any more. <a href="#lib">Back to the Library</a></p>`;
  const tracks = id === RERUNS ? (cache.listened || []) : (cache.station[id] || await loadStation(id));
  const p = s.profile, air = R.on && R.station === id, playing = air && lid;
  const sub = id === RERUNS ? s.meta : `${tracks.filter(t => !t.up_next).length} songs · ${s.ready} ready to air · 👍 ${s.stats.kept}`;
  const row = (t) => songRow(t, {folder: id === RERUNS ? "history" : id, noStation: id !== RERUNS,
    when: t.up_next ? "up next" : t.played_at ? whenText(t.played_at) : "", extra: t.airings > 1 ? `played ${t.airings}×` : ""});
  return crumbs([s.name, "#"]) + `<section class="shead">
      <div class="art"><img src="${stationArt(s)}" alt=""></div>
      <div class="tx"><h1>${esc(s.name)}</h1><div class="muted small">${esc(sub)}${air ? ' · <b class="onair">ON AIR</b>' : ""}</div>
        <div class="acts">
          <button class="pill primary" data-play="${esc(id)}">${playing ? "≋ Playing" : "▶ Play station"}</button>
          ${id !== RERUNS ? `<button class="pill" data-act="make-song" title="Write and record a new song for this station right away">🎵 Create a song now</button>
          <button class="pill" data-act="edit" title="Edit station">✎ Edit</button>` : ""}
        </div>
        <div class="small muted" id="mkStat" role="status"></div>
        ${air ? "" : `<div class="small muted">Playing it retunes the radio for everyone listening.</div>`}</div>
    </section>
    <section class="section"><p class="label">Songs</p>
      ${tracks.length ? songList("station:" + id, tracks, row, [["as-is", "Newest first"], "oldest", "top", "played", "title"])
        : `<p class="muted small">No songs yet — press play and the first one is written for you.</p>`}</section>
    <details class="section aboutbox" ${tracks.length ? "" : "open"}><summary>About this station${id !== RERUNS ? " & settings" : ""}</summary>
      <div class="about">
        ${s.description ? `<p>${esc(s.description)}</p>` : ""}
        ${s.theme ? `<p><b>Lyrics theme:</b> ${esc(s.theme)}</p>` : ""}
        ${id !== RERUNS ? `<p><b>🎲 Variety:</b> ${esc(varietyName(s.variety ?? 25))} (${s.variety ?? 25}/100)</p>` : ""}
        ${id !== RERUNS ? `<p><b>Vocals:</b> ${esc(s.vocals)}${s.language ? " · " + esc(s.language) : ""} · <b>${s.kind === "folder" ? "Learns from your music" : "Defined by its description"}</b></p>` : ""}
        ${p ? `<p><b>Learned:</b> ${esc(p.top.genre.join(", "))} · ${esc(p.top.mood.join(", "))} · ${p.bpm_range ? p.bpm_range.join("–") + " BPM" : ""}</p>` : ""}
        ${s.problem ? `<p class="err">${esc(s.problem)}</p>` : ""}
      </div>
      ${id !== RERUNS ? `<div class="taste">${s.taste ? `<p class="label">What it has learned from you</p><pre>${esc(s.taste)}</pre>
          <button class="btn" data-act="forget-taste" title="Clear these notes and the thumbs behind them">Forget</button>` : `<p class="small muted">Thumbs up, thumbs down and playlist saves teach this station what you like.</p>`}
        ${s.banked ? `<p class="small muted">✍ ${s.banked} song${s.banked === 1 ? "" : "s"} pre-written by the GPU lyricist, ready to record</p>` : ""}</div>` : ""}
      ${s.kind === "folder" ? `<div class="section"><p class="label">Learn from your music</p>${folderTools(s)}</div>` : ""}
      <p class="small"><a href="/radio/stream.mp3" target="_blank">📡 Open the live stream in another player</a></p>
      ${id !== RERUNS ? `<div class="section"><button class="pill danger" data-act="delete">Delete station</button></div>` : ""}
    </details>`;
}
$("page").addEventListener("change", (e) => {
  if (e.target.classList.contains("lsort")) { store.set("sort." + e.target.dataset.list, e.target.value); renderView(); }
});
$("page").addEventListener("input", (e) => { if (e.target.classList.contains("lfilter")) filterList(e.target); });

// ---------- the controls on every song list: search these songs, sort them, ☑ Select
const songWhen = (t) => t.listened_at || t.added_at || t.played_at || t.created || 0;
const songLove = (t) => (t.feedback === "keep" ? 2 : 0) + (t.in_playlist ? 1 : 0) - (t.feedback === "dislike" ? 3 : 0);
const LIST_SORTS = {
  newest: ["Newest first", (a, b) => songWhen(b) - songWhen(a)],
  oldest: ["Oldest first", (a, b) => songWhen(a) - songWhen(b)],
  top: ["Your favourites first", (a, b) => songLove(b) - songLove(a) || ((b.critic || {}).score || 0) - ((a.critic || {}).score || 0)],
  played: ["Most played", (a, b) => (b.airings || 0) - (a.airings || 0)],
  title: ["Title A–Z", (a, b) => a.title.localeCompare(b.title)],
  station: ["By station", (a, b) => (a.station_name || "~").localeCompare(b.station_name || "~") || songWhen(b) - songWhen(a)],
};
const listQ = {};                                  // what's typed in each list's search box (kept while you browse)
const songText = (t) => [t.title, t.station_name, t.style, t.story].filter(Boolean).join(" ").toLowerCase();
// sorts: keys of LIST_SORTS, or ["as-is", label] for the list's own order (listening order, date added...)
function sortedSongs(key, tracks, keys) {
  const sort = keys.includes(store.get("sort." + key, keys[0])) ? store.get("sort." + key, keys[0]) : keys[0];
  return [sort, sort === "as-is" ? tracks : [...tracks].sort(LIST_SORTS[sort][1])];
}
function songList(key, tracks, row, sorts) {
  const [sort, list] = sortedSongs(key, tracks, sorts.map(x => Array.isArray(x) ? x[0] : x));
  const q = (listQ[key] || "").trim().toLowerCase();
  const rows = list.map(t => row(t).replace('<li class="row', `<li data-s="${esc(songText(t))}"${q && !songText(t).includes(q) ? " hidden" : ""} class="row`));
  const shown = q ? list.filter(t => songText(t).includes(q)).length : list.length;
  return `<div class="lctl">
      <label class="lsearch"><span aria-hidden="true">🔍</span><input type="search" class="lfilter" data-list="${esc(key)}" value="${esc(listQ[key] || "")}"
        placeholder="Search these ${list.length} songs" aria-label="Search these songs"></label>
      <label class="small muted">Sort <select class="lsort" data-list="${esc(key)}" aria-label="Sort songs">${sorts.map(x => {
        const [k, l] = Array.isArray(x) ? x : [x, LIST_SORTS[x][0]];
        return `<option value="${k}" ${k === sort ? "selected" : ""}>${l}</option>`; }).join("")}</select></label>
      ${selBar(list)}</div>
    <p class="small muted lcount" data-list="${esc(key)}">${q ? `${shown} of ${list.length} songs match` : `${list.length} song${list.length === 1 ? "" : "s"}`}</p>
    <ul class="rows">${rows.join("")}</ul>`;
}
function filterList(input) {
  const key = input.dataset.list, q = input.value.trim().toLowerCase(); listQ[key] = input.value;
  let shown = 0, total = 0;
  document.querySelectorAll(".rows .row[data-s]").forEach(r => { total++; r.hidden = !!q && !r.dataset.s.includes(q); if (!r.hidden) shown++; });
  const c = document.querySelector(`.lcount[data-list="${CSS.escape(key)}"]`);
  if (c) c.textContent = q ? (shown ? `${shown} of ${total} songs match` : `No songs match “${input.value.trim()}”`) : `${total} song${total === 1 ? "" : "s"}`;
}

// ---------- 🎵 All songs / ♥ My playlist / 🕘 Listen history
async function collectionView() {
  if (view.sub === "all") {
    const all = cache.all || await loadAll();
    return crumbs(FOLDERS.all) + (all.length ? songList("all", all, t => songRow(t, {folder: "all",
        when: t.up_next ? "up next" : t.played_at ? whenText(t.played_at) : t.created ? whenText(t.created) : "",
        extra: t.source !== "radio" ? "Music Studio" : t.airings > 1 ? `played ${t.airings}×` : ""}),
      [["as-is", "Newest first"], "oldest", "top", "played", "title", "station"]) : `<p class="empty">No songs yet.</p>`);
  }
  if (view.sub === "history") {
    const h = cache.listened || [];
    return crumbs(FOLDERS.history) + (h.length ? songList("history", h, t => songRow(t, {folder: "history", when: whenText(t.listened_at), extra: t.via_reruns ? "rerun" : ""}),
      [["as-is", "Order played"], "top", "title", "station"]) : `<p class="empty">Nothing played yet.</p>`);
  }
  const pl = cache.playlist || await loadPlaylist();
  return crumbs(FOLDERS.playlist) + (pl.length ? songList("playlist", pl, t => songRow(t, {folder: "playlist", when: "added " + whenText(t.added_at)}),
      [["as-is", "Recently added"], "oldest", "top", "played", "title", "station"])
    : `<p class="empty">Your playlist is empty. Press <b>⊕</b> in the player while a song plays, or <b>+</b> next to any song, to save it here.</p>`);
}
async function searchView(q) {
  const s = q.trim().toLowerCase(); if (!s) return `<p class="empty">Type to search stations and songs.</p>`;
  const hit = (...xs) => xs.some(x => x && String(x).toLowerCase().includes(s));
  const sts = [rerunStation(), ...R.stations].filter(x => hit(x.name, x.description, x.theme));
  const pool = new Map(); [...(cache.playlist || await loadPlaylist()), ...(cache.listened || []), ...Object.values(cache.station).flat()].forEach(t => pool.set(t.id, t));
  const songs = [...pool.values()].filter(t => hit(t.title, t.style, t.station_name, t.lyrics)).slice(0, 40);
  return `<section class="section"><p class="label">Stations</p>${sts.length ? `<div class="grid">${sts.map(stationTile).join("")}</div>` : `<p class="muted small">No stations match “${esc(q)}”.</p>`}</section>
    <section class="section"><p class="label">Songs</p>${songs.length ? `<ul class="rows">${songs.map(t => songRow(t)).join("")}</ul>` : `<p class="muted small">No songs match “${esc(q)}”.</p>`}</section>`;
}
let qT = 0;
$("q").addEventListener("input", () => { clearTimeout(qT); qT = setTimeout(() => { const v = $("q").value; location.hash = v ? "#search/" + encodeURIComponent(v) : "#lib"; setTimeout(() => $("q").focus(), 0); }, 250); });

// page clicks: play a station, preview a song, playlist +, station actions
$("page").addEventListener("click", async (e) => {
  const playEl = e.target.closest("[data-play]");
  if (playEl) { e.preventDefault(); return playStation(playEl.dataset.play); }
  const a = e.target.closest("[data-act]");
  if (!a) {                                         // a click anywhere else on a song row opens the song, like a file
    const r = e.target.closest(".row"), link = r && r.querySelector("a.a");
    if (sel.on && r && !e.target.closest("button, input, select, .thumb")) {   // select mode: a row click ticks it
      e.preventDefault(); sel.ids.has(r.dataset.id) ? sel.ids.delete(r.dataset.id) : sel.ids.add(r.dataset.id); return renderView();
    }
    if (link && !e.target.closest("a, button, input, select")) location.hash = link.getAttribute("href");
    return;
  }
  const act = a.dataset.act, row = a.closest(".row");
  if (act === "new") return openNewStation();
  if (act === "random") return playRandom();
  if (act === "sel-on") { sel.on = true; return renderView(); }
  if (act === "sel-off") { sel.on = false; sel.ids.clear(); return renderView(); }
  if (act === "sel-all") {                       // the songs you can see (a search narrows it)
    const vis = [...document.querySelectorAll(".rows .row:not([hidden])")].map(r => r.dataset.id), all = vis.every(id => sel.ids.has(id));
    vis.forEach(id => all ? sel.ids.delete(id) : sel.ids.add(id)); return renderView();
  }
  if (act === "sel-one") { const id = row.dataset.id; sel.ids.has(id) ? sel.ids.delete(id) : sel.ids.add(id); return renderView(); }
  if (act.startsWith("sel-")) return selAction(act);
  if (act === "lib-layout") { store.set("libTiles", a.dataset.v === "1"); return renderView(); }
  if (act === "plus") { try { await setPlaylist(row.dataset.id, a.dataset.on !== "1"); } catch (err) { toast(err.message); } return; }
  if (act === "np-plus") return $("bAdd").click();
  if (act === "np-art" && current) { post("/api/radio/art", {track: current.id}).then(() => hint("Drawing a new cover — it appears in a minute or two (between songs).")).catch(e => hint(e.message)); return; }
  if (act === "preview" || act === "preview-feature") return preview(row ? row.dataset.audio : a.dataset.audio, row, row ? row.dataset.id : a.dataset.id);
  if (act === "edit") return editStation(stOf(view.id));
  if (act === "forget-taste") { if (!confirm("Forget what this station has learned from your thumbs?")) return; await api(`/api/radio/stations/${encodeURIComponent(view.id)}/taste`, {method: "DELETE"}).catch(e => toast(e.message)); await refresh(); renderView(); return; }
  if (act === "song-fb") { await post("/api/radio/feedback", {track: view.id, kind: a.dataset.kind}).catch(e => toast(e.message)); toast(a.dataset.kind === "keep" ? "👍 Noted — the station learns from this." : a.dataset.kind === "dislike" ? "👎 Noted — fewer like this." : "Cleared."); renderView(); return; }
  if (act === "song-plus") { try { await setPlaylist(view.id, a.dataset.on !== "1"); } catch (err) { toast(err.message); } renderView(); return; }
  if (act === "song-art") { post("/api/radio/art", {track: view.id}).then(() => toast("Drawing a new cover — it appears in a minute or two.")).catch(e => toast(e.message)); return; }
  if (act === "song-more") { post(`/api/radio/stations/${encodeURIComponent(a.dataset.station)}/song`, {like: view.id}).then(() => toast("Writing a new song in the spirit of this one — it's recorded next.")).catch(e => toast(e.message)); return; }
  if (act === "song-play") return preview(a.dataset.audio, null, view.id);
  if (act === "song-rewrite") { if (window.agentoraOpen) { e.preventDefault(); agentoraOpen("music", "#rewrite=" + a.dataset.id); } return; }
  if (act === "make-song") {
    a.disabled = true;
    post(`/api/radio/stations/${encodeURIComponent(view.id)}/song`).then(() => { hint("Writing a new song for this station — it's recorded next, ahead of everything else."); refresh().then(updateMk); })
      .catch(e => hint(e.message)).finally(() => { a.disabled = false; });
    return;
  }
  if (act === "delete") {
    const s = stOf(view.id); if (!s || !confirm(`Delete “${s.name}” and its songs?`)) return;
    await api(`/api/radio/stations/${s.id}`, {method: "DELETE"}).catch(err => toast(err.message));
    if (selected === s.id) { selected = null; store.set("station", null); }
    location.hash = "#lib"; refresh();
  }
});
// ---------------- playing from the Library: the song (and the rest of its list) plays in the player bar; ⏮ ⏭ move
// through that list, ▶ on a station goes back to the live broadcast
function lpTrack(id) {
  for (const list of [cache.all, cache.playlist, cache.listened, ...Object.values(cache.station)]) {
    const t = (list || []).find(x => x.id === id); if (t) return t;
  }
  return null;
}
function lpQueueFrom(row) {                        // the songs you can see in that list, in the order shown
  const ul = row && row.closest(".rows");
  return ul ? [...ul.querySelectorAll(".row:not([hidden])")].map(r => lpTrack(r.dataset.id)
    || {id: r.dataset.id, audio: r.dataset.audio, title: r.querySelector("a.a").textContent}) : [];
}
function lpMark() {
  document.querySelectorAll(".row").forEach(r => {
    const on = !!lp.t && r.dataset.id === lp.t.id && !pv.paused;
    r.classList.toggle("previewing", on); const b = r.querySelector(".pv"); if (b) b.textContent = on ? "❚❚" : "▶";
  });
}
function lpPlay(i) {
  const t = lp.queue[i]; if (!t) return lpStop();
  lp.i = i; lp.t = t;
  if (lid) { wantPlay = false; disconnect(); toast("Playing from your library — press ▶ on a station to go back to the radio."); }
  pv.src = t.audio; pv.play().catch(err => toast(err.message));
  syncNow(); lpMark();
}
function lpBack() { if (pv.currentTime > 3 || lp.i <= 0) { pv.currentTime = 0; pv.play().catch(() => {}); } else lpPlay(lp.i - 1); }
function lpStop(quiet) {
  lp.t = null; lp.queue = []; lp.i = -1; pv.pause(); pv.removeAttribute("src");
  current = null; shownId = undefined; lpMark();
  if (!quiet) { syncNow(); renderBar(); setPlayingUI(); if (view.tab === "now") renderView(); }
}
async function preview(src, row, id) {
  id = id || (row && row.dataset.id);
  if (lp.t && lp.t.id === id) { pv.paused ? pv.play().catch(e => toast(e.message)) : pv.pause(); return; }   // same song: play / pause
  let queue = lpQueueFrom(row);
  if (!queue.length) {                             // a song page or a tile: just that song
    let t = lpTrack(id);
    if (!t && id) t = await api(`/api/radio/tracks/${encodeURIComponent(id)}`).catch(() => null);
    queue = [t || {id, audio: src, title: "Song"}];
  }
  lp.queue = queue; lpPlay(Math.max(0, queue.findIndex(t => t.id === id)));
}
pv.addEventListener("play", () => { setPlayingUI(); lpMark(); });
pv.addEventListener("pause", () => { setPlayingUI(); lpMark(); });
pv.addEventListener("ended", () => { if (lp.t) lpPlay(lp.i + 1); });   // on to the next song in the list

// station dialogs need these names
function openDialog(title, html) { $("dlgT").textContent = title; $("dlgB").innerHTML = html; $("dlg").showModal(); }
$("dlgX").onclick = () => $("dlg").close();
function openStation(id) { if ($("dlg").open) $("dlg").close(); location.hash = "#lib/" + id; renderView(); }
function hint(t) { toast(t); }

