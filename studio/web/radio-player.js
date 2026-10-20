// Agentora radio page, part 2 of 5: The live broadcast: connecting and recovering, ⏭ ⏮ and play next, the player bar, the Now Playing sheet, the lock screen, loading data.
// Plain scripts loaded in order by radio.html; they share one global scope.
// ---------------- the broadcast: one shared live stream, every listener hears the same song
let lid = null, shownId = undefined, prevOn = null;
function streamPos() { return R && R.stream && lid && R.stream.p0 != null ? R.stream.p0 + audio.currentTime : null; }
function hearing() {
  const s = R && R.stream; if (!s) return null;
  const pos = streamPos(); if (pos == null) return lid ? s.now : null;
  let e = null; for (const a of s.aired) if (a.start_pos <= pos + 0.05) e = a;
  return e && e.id ? e : null;
}
// A broken stream (radio restarting, network drop) is retried with backoff until it plays again; a real pause (Bluetooth
// off, a call, ⏸) has a loaded stream and no error, and is left alone.
let retry = {n: 0, at: 0};
// The stream relay (its own service) serves the broadcast, so a restart of the radio doesn't disconnect anyone.
const streamUrl = (id) => `${location.protocol}//${location.hostname}:${(R && R.stream && R.stream.relay_port) || 7861}/stream.mp3?l=${id}`;
function connect() {
  lid = Math.random().toString(36).slice(2, 12);
  audio.src = streamUrl(lid);
  audio.play().then(() => { retry.n = 0; }).catch(e => {
    if (e.name === "NotAllowedError") { status("Tap ▶ to resume — the phone blocked playing in the background."); toast("Tap ▶ to resume the radio."); }
    else scheduleRetry();                                  // the stream didn't load (e.g. the radio is restarting)
  });
  setTimeout(refresh, 400);
}
// While the stream reconnects, a looping silent clip keeps the page "playing", so a phone doesn't freeze the background
// tab (and its retries) or drop the lock-screen controls.
const bridge = new Audio();
bridge.loop = true; bridge.volume = 0.01;
bridge.src = (() => {                                     // 1 s of silence as a tiny WAV
  const n = 8000, b = new ArrayBuffer(44 + n), v = new DataView(b), w = (o, t) => [...t].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + n, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 8000, true); v.setUint32(28, 8000, true); v.setUint16(32, 1, true); v.setUint16(34, 8, true); w(36, "data"); v.setUint32(40, n, true);
  for (let i = 0; i < n; i++) v.setUint8(44 + i, 128);
  return URL.createObjectURL(new Blob([b], {type: "audio/wav"}));
})();
function bridgeOn() { if (bridge.paused) bridge.play().catch(() => {}); }
function bridgeOff() { if (!bridge.paused) bridge.pause(); }
function scheduleRetry() {
  if (!wantPlay || !lid) return;
  bridgeOn();
  const wait = Math.min(30000, 2000 * 2 ** retry.n);
  if (Date.now() < retry.at) return;                      // one retry at a time
  retry.n++; retry.at = Date.now() + wait; met.reconnects++;
  status(`Stream dropped — reconnecting${retry.n > 1 ? ` (try ${retry.n})` : ""}…`);
  setTimeout(() => { retry.at = 0; if (wantPlay && lid) connect(); }, wait);
}
function disconnect() { lid = null; bridgeOff(); audio.pause(); audio.removeAttribute("src"); audio.load(); setPlayingUI(); }
const reconnect = () => scheduleRetry();
audio.addEventListener("error", () => { if (audio.getAttribute("src")) reconnect(); });
audio.addEventListener("ended", reconnect);
audio.addEventListener("play", setPlayingUI); audio.addEventListener("pause", setPlayingUI);
let pausedAt = 0;                                  // the phone paused us (Bluetooth dropped, a call, the car's ⏸)
audio.addEventListener("pause", () => { pausedAt = Date.now(); });
audio.addEventListener("playing", () => { retry.n = 0; retry.at = 0; bridgeOff(); });
audio.addEventListener("play", () => {
  if (actx && actx.state !== "running") actx.resume().catch(() => {});
  if (wantPlay && lid && pausedAt && Date.now() - pausedAt > 8000) { pausedAt = 0; connect(); }   // stale or dead connection: rejoin live
  pausedAt = 0;
});
let stallT = -1, stallSince = Date.now();          // phones can leave a dead stream "playing" with no error
setInterval(() => {
  if (actx && wantPlay && lid && !audio.paused && actx.state !== "running") actx.resume().catch(() => {});   // Bluetooth route change
  if (wantPlay && lid && (audio.error || (audio.paused && audio.readyState === 0 && audio.networkState !== 2))) scheduleRetry();   // broken, not paused
  if (!wantPlay || !lid || audio.paused || audio.currentTime !== stallT) { stallT = audio.currentTime; stallSince = Date.now(); return; }
  if (Date.now() - stallSince > 12000) { stallSince = Date.now(); met.reconnects++; status("Stream stalled — reconnecting…"); connect(); }
}, 2000);
async function playStation(id) {
  ensureAudioGraph(); if (actx && actx.state === "suspended") await actx.resume();
  if (lp.t) lpStop(true);                          // back to the live broadcast
  pv.pause(); document.querySelectorAll(".row.previewing").forEach(r => r.classList.remove("previewing"));
  if (!id) return toast("Pick a station first.");
  selected = id; store.set("station", id);
  wantPlay = true;
  if (!R || !R.on || R.station !== id) {
    try { await post("/api/radio/power", {on: true, station: id}); prevOn = true; } catch (e) { wantPlay = false; return toast(e.message); }
    if (R && R.on && R.station && R.station !== id) toast("Retuned for everyone listening.");
  }
  if (!lid) connect();
  location.hash = "#lib/" + id; refresh();        // straight to the station: its songs, with the player bar below
}
// 🎲 Random: tune to a station picked at random (not the one on now, and not Reruns, which is already a shuffle)
function playRandom() {
  const pool = R.stations.filter(s => s.id !== R.station && s.analysis.status !== "running");
  const list = pool.length ? pool : R.stations;
  if (!list.length) return toast("Create a station first.");
  const s = list[Math.floor(Math.random() * list.length)];
  toast(`🎲 ${s.name}`);
  return playStation(s.id);
}
async function togglePlay() {
  if (lp.t) { pv.paused ? pv.play().catch(e => toast(e.message)) : pv.pause(); return; }   // a library song is in the player
  ensureAudioGraph(); if (actx && actx.state === "suspended") await actx.resume();
  if (lid) { wantPlay = false; disconnect(); return; }
  playStation(selected || (R && R.station) || (R && R.stations[0] && R.stations[0].id));
}
$("bPlay").onclick = togglePlay;
document.addEventListener("keydown", e => {
  if (e.code === "Space" && !/INPUT|TEXTAREA|SELECT|BUTTON/.test(document.activeElement.tagName) && !$("dlg").open) { e.preventDefault(); togglePlay(); }
});

// ---------------- player bar + now playing
function setPlayingUI() {
  const playing = !!lid && !audio.paused, on = lp.t ? !pv.paused : !!lid;
  $("bPlayIcon").innerHTML = on ? '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>' : '<path d="M8 5v14l11-7z"/>';
  $("bPlay").setAttribute("aria-label", on ? "Pause" : "Play");
  $("favicon").href = playing && hearing() ? "/icons/radio-live.svg" : "/icons/radio.svg";
  updateMediaSession(); npsMirror();
}
function syncNow() {
  if (lp.t) {                                      // a library song: the bar follows it, not the broadcast
    if (current !== lp.t) { current = lp.t; shownId = undefined; renderBar(); if (view.tab === "now") renderView(); setPlayingUI(); }
    const el = pv.currentTime || 0, dur = pv.duration || lp.t.seconds || 0;
    $("bEl").textContent = mmss(el); $("bDur").textContent = dur ? mmss(dur) : "—";
    $("bProg").style.width = dur ? Math.min(100, 100 * el / dur) + "%" : "0";
    return npsProgress();
  }
  const h = hearing(), id = h ? h.id : null;
  if (id !== shownId) { shownId = id; current = h ? {...h} : null; renderBar(); if (view.tab === "now") renderView(); setPlayingUI(); loadHistory(); }
  if (current && lid) {
    const el = Math.max(0, streamPos() - current.start_pos), dur = current.duration || current.seconds || 0;
    $("bEl").textContent = mmss(Math.min(el, dur || el)); $("bDur").textContent = dur ? mmss(dur) : "—";
    $("bProg").style.width = dur ? Math.min(100, 100 * el / dur) + "%" : "0";
  } else { $("bEl").textContent = "0:00"; $("bDur").textContent = "0:00"; $("bProg").style.width = "0"; }
  npsProgress();
  if (view.tab === "now") status(lid && !current && R ? waitText() : "");
}
function renderBar() {
  const t = current, st = stOf(selected);
  $("bCover").src = t ? trackArt(t) : stationArt(st);
  $("bTitle").textContent = t ? t.title : lid ? "Next song on the way…" : st ? st.name : "Pick a station";
  $("bStation").textContent = (lp.t ? `${t.station_name || "Music Studio"} · from your library` : t ? (t.station_name || (st && st.name) || "") + (t.airing === "rerun" ? " · rerun" : t.airing === "runner-up" ? " · runner-up" : "") : st ? (lid ? "tuning in…" : "press play") : "Agentora")
    + (t && t.credit ? " · " + t.credit : "");
  $("bUp").classList.toggle("on-up", !!t && t.feedback === "keep");
  setAddKey(!!t && !!t.in_playlist);
  ["bUp", "bDown", "bAdd", "bSkip", "bBack"].forEach(k => $(k).disabled = !t);
  $("wash").style.background = t ? `radial-gradient(1200px 600px at 20% 0%, hsla(${trackHue(t)},70%,55%,.16), transparent 70%)` : "none";
  npsRender();
}
function waitText() {
  const st = R && stOf(selected), g = R && R.gpu;
  if (selected === RERUNS) return st && st.ready ? "Cueing up a rerun…" : "No songs have aired yet — play a station first.";
  if (g && g.owner === "switching") return g.detail;
  if (st && st.kind === "folder" && st.analysis.status !== "done") return "This station is still learning its songs.";
  if (st && st.problem) return "Generation paused: " + st.problem;
  const run = (R ? R.making.filter(m => m.station === selected) : []).find(m => m.status === "running");
  if (run) return `Recording “${run.title}” — ${run.stage || ""}${run.progress && run.progress.music_seconds ? ` (${mmss(run.progress.music_seconds)} of music so far)` : ""}`;
  if (R && R.judging) return "The critic is listening to a fresh take…";
  return "Writing the first song for this station…";
}
let statusText = "";
function status(t) {
  statusText = t || "";
  for (const el of [$("npStatus"), $("npsStatus")]) if (el && el.textContent !== statusText) el.textContent = statusText;
}
let toastT = 0;
function toast(t) { const el = $("toast"); el.textContent = t; el.classList.add("show"); clearTimeout(toastT); toastT = setTimeout(() => el.classList.remove("show"), 3500); }

// feedback
$("bUp").onclick = async () => {
  if (!current) return;
  const kind = current.feedback === "keep" ? "clear" : "keep";
  try { await post("/api/radio/feedback", {track: current.id, kind}); } catch (e) { return toast(e.message); }
  current.feedback = kind === "keep" ? "keep" : null; renderBar(); if (view.tab === "now") renderView();
  toast(kind === "keep" ? "👍 Got it — more songs like this one." : "Thumbs up removed.");
};
// A skip makes the server drop the buffered old song ("cut"); reconnecting then starts right on the next song.
let myCut;
function followCut(cut) {
  if (cut == null) return;
  if (myCut !== undefined && cut > myCut && wantPlay && lid) connect();   // lower: the radio restarted (the stream didn't)
  myCut = cut;
}
async function skipNow(kind) {
  try { const r = await post("/api/radio/skip", {track: current ? current.id : null, kind}); followCut(r.cut); }
  catch (e) { toast(e.message); }
}
$("bDown").onclick = () => {
  if (lp.t) { post("/api/radio/feedback", {track: lp.t.id, kind: "dislike"}).catch(() => {}); toast("👎 Noted — fewer like this."); return lpPlay(lp.i + 1); }
  toast("👎 Noted — fewer like this. Skipping…"); skipNow("dislike");
};
$("bSkip").onclick = () => { if (lp.t) return lpPlay(lp.i + 1); toast("Skipping for everyone listening…"); skipNow("skip"); };
// ⏮ like any player: more than a few seconds into a song it restarts it; near the start (or pressed again quickly) it goes
// to the previous song, and so on back through the history
let backAt = 0;
function secondsIntoSong() {
  const h = hearing(), pos = streamPos();
  return h && pos != null && h.start_pos != null ? pos - h.start_pos : null;
}
async function backNow(prev) {                    // prev: straight to the previous song (Now Playing's left cover)
  const into = secondsIntoSong();
  const again = prev || Date.now() - backAt < 4000 || (into != null && into < 6); backAt = Date.now();
  toast(again ? "⏮ Previous song…" : "⏮ Back to the start of this song…");
  try {
    const r = await post("/api/radio/back", again ? {steps: 1} : {track: current ? current.id : null, steps: 0});
    backAt = Date.now();
    if (r.error) toast(r.error); else { followCut(r.cut); if (again) toast(`⏮ ${r.title}${r.first ? " — the oldest song in this session's history" : ""}`); }
  } catch (e) { toast(e.message); }
}
$("bBack").onclick = () => lp.t ? lpBack() : backNow();
function setAddKey(on) {
  $("bAdd").classList.toggle("inlist", on); $("bAdd").setAttribute("aria-pressed", on);
  $("bAdd").title = on ? "In your playlist — press to remove" : "Add to my playlist";
  $("bAdd").querySelector("svg").innerHTML = on ? '<circle cx="12" cy="12" r="10" fill="currentColor" stroke="none"/><path d="m7.5 12.5 3 3 6-6.5" stroke="#fff"/>' : '<circle cx="12" cy="12" r="9.5"/><path d="M12 7.5v9M7.5 12h9"/>';
}
async function setPlaylist(id, on) {
  await post("/api/radio/playlist", {track: id, on});
  if (current && current.id === id) { current.in_playlist = on; setAddKey(on); }
  cache.playlist = null; toast(on ? "Added to your playlist." : "Removed from your playlist.");
  loadHistory(); if (view.tab !== "now") renderView();
}
$("bAdd").onclick = async () => { if (current) try { await setPlaylist(current.id, !current.in_playlist); } catch (e) { toast(e.message); } };

// ---------------- Now Playing sheet: tap the bar. The current cover in the middle, the previous song's peeking in on the
// left and the next one's on the right (tap one to go there), the song's details, and the bar's controls at the bottom.
// It shows the same state as the bar (current, lp, R); its buttons are copies of the bar's and press them.
const NPS_KEYS = ["bDown", "bBack", "bPlay", "bSkip", "bUp", "bAdd"];
const nps = {open: false, around: {prev: null, next: null}};
$("npsCtl").innerHTML = NPS_KEYS.map(k => $(k).outerHTML.replace(`id="${k}"`, `data-proxy="${k}"`).replace(/ id="bPlayIcon"/, "")).join("");
// the bar is inert while the sheet is open, so call its handlers rather than click it
$("npsCtl").addEventListener("click", e => { const b = e.target.closest("[data-proxy]"), src = b && $(b.dataset.proxy); if (src && !src.disabled) src.onclick(); });
function npsMirror() {                              // the copies follow the bar's buttons: icon, on/off, disabled, labels
  if (!nps.open) return;
  for (const k of NPS_KEYS) {
    const src = $(k), b = $("npsCtl").querySelector(`[data-proxy="${k}"]`), html = src.innerHTML.replace(' id="bPlayIcon"', "");
    if (b.innerHTML !== html) b.innerHTML = html;
    b.className = src.className; b.disabled = src.disabled; b.title = src.title;
    for (const a of ["aria-label", "aria-pressed"]) if (src.hasAttribute(a)) b.setAttribute(a, src.getAttribute(a));
  }
}
// previous / next: a Library list plays through that list; the broadcast uses the songs aired around the one this device
// hears (the stream runs ahead of the ear), then the listen history, and the radio's own pick for what airs next
function npsAround() {
  if (lp.t) return {prev: lp.queue[lp.i - 1] || null, next: lp.queue[lp.i + 1] || null, lib: true};
  const aired = (R && R.stream && R.stream.aired) || [], k = current ? aired.findIndex(a => a.id === current.id && a.start_pos === current.start_pos) : -1;
  let prev = null, next = null;
  if (k >= 0) {
    for (let i = k - 1; i >= 0 && !prev; i--) if (aired[i].id) prev = aired[i];
    for (let i = k + 1; i < aired.length && !next; i++) if (aired[i].id) next = aired[i];
  }
  if (!prev && cache.listened) prev = cache.listened.find(t => !current || t.id !== current.id) || null;
  if (!next && R && R.next && (!current || R.next.id !== current.id)) next = R.next;
  return {prev, next};
}
function npsSide(el, cap, t, dir, live) {
  const label = dir === "prev" ? "Previous" : "Up next";
  // a cover can be pressed when there's somewhere to go: the Library list, or the broadcast this device is tuned to
  const can = !!t && (lp.t || (live && current));
  const none = dir === "prev" ? "Nothing before this" : lp.t ? "End of the list" : selected === RERUNS ? "Shuffled at random" : lid || (R && R.on) ? "A new song is on the way" : "Nothing queued";
  el.classList.toggle("empty", !t);
  el.innerHTML = t ? `<img src="${trackArt(t)}" alt="">` : `<span aria-hidden="true">${dir === "prev" ? "⏮" : selected === RERUNS && !lp.t ? "🔀" : "♪"}</span>`;
  el.disabled = cap.disabled = !can;
  const what = t ? `${label}: ${t.title}${t.station_name ? " · " + t.station_name : ""}` : `${label}: ${none}`;
  el.title = can ? `${what} (tap to play${lp.t ? "" : " — for everyone listening"})` : what;
  el.setAttribute("aria-label", el.title);
  cap.innerHTML = `<span>${dir === "prev" ? "‹ " : ""}${label}${dir === "next" ? " ›" : ""}</span><b>${esc(t ? t.title : none)}</b>`;
  cap.title = el.title;
}
function npsRender() {
  if (!nps.open) return;
  const t = current, st = stOf(t ? t.station : selected), live = !!lid;
  $("npsCover").src = t ? trackArt(t) : stationArt(st);
  $("npsTitle").textContent = $("bTitle").textContent;
  const stName = t ? (t.station_name || (st && st.name) || (lp.t ? "Music Studio" : "")) : "";   // no song: the title is the station
  $("npsSub").textContent = [stName && "📻 " + stName, t && t.credit].filter(Boolean).join(" · ");
  $("npsSt").textContent = lp.t ? "From your library" : live ? "Live radio · everyone hears this" : t ? "" : "Press ▶ to tune in";
  $("npsSt").href = t && t.station ? `#lib/${encodeURIComponent(t.station)}` : "#now";
  $("npsMore").style.visibility = t ? "" : "hidden";
  if (t) $("npsMore").href = songHref(t);
  $("npsStyle").textContent = t ? t.style || "" : "";
  const chips = t ? [t.airing === "rerun" ? "Rerun" : t.airing === "runner-up" ? "Runner-up take" : t.airing === "history" ? "Replay" : t.airing === "request" ? "Requested" : null,
    (t.duration || t.seconds) ? mmss(t.duration || t.seconds) : null, t.mode === "instrumental" || (t.lyrics != null && !t.lyrics) ? "Instrumental" : t.lyrics ? "Vocals" : null,
    t.feedback === "keep" ? "👍 Liked" : null, t.in_playlist ? "♥ In my playlist" : null].filter(Boolean) : [];
  $("npsChips").innerHTML = chips.map(c => `<span class="chip">${esc(c)}</span>`).join("");
  const ly = $("npsLy");
  ly.hidden = !t || t.lyrics == null;
  if (t && ly.dataset.id !== t.id) { ly.dataset.id = t.id; $("npsLyB").innerHTML = lyricsHTML(t); ly.open = store.get("npsLyrics", false); }
  const a = nps.around = npsAround(); nps.sig = npsSig(a);
  npsSide($("npsPrev"), $("npsPrevCap"), a.prev, "prev", live);
  npsSide($("npsNext"), $("npsNextCap"), a.next, "next", live);
  $("nps").style.setProperty("--npsWash", t ? `radial-gradient(700px 420px at 50% 0%, hsla(${trackHue(t)},70%,55%,.22), transparent 70%)` : "none");
  status(statusText); npsMirror(); npsProgress();
}
function npsProgress() {
  if (!nps.open) return;
  $("npsEl").textContent = $("bEl").textContent; $("npsDur").textContent = $("bDur").textContent;
  $("npsProg").style.width = $("bProg").style.width;
  $("npsTrack").classList.toggle("seek", !!lp.t);
  $("npsTrack").title = lp.t ? "Drag or tap to jump" : lid ? "Live: everyone hears the same moment, so there's no seeking" : "";
  if (!lp.t && nps.sig !== npsSig(npsAround())) npsRender();   // a new "up next" pick or history entry
}
const npsSig = (a) => [a.prev && a.prev.id, a.prev && a.prev.cover, a.next && a.next.id, a.next && a.next.cover].join("|");
function npsGo(dir) {
  const a = nps.around;
  if (!a[dir]) return;
  if (lp.t) return lpPlay(lp.i + (dir === "prev" ? -1 : 1));
  if (!current) return;
  if (dir === "next") $("bSkip").onclick(); else backNow(true);
}
$("npsPrev").onclick = $("npsPrevCap").onclick = () => npsGo("prev");
$("npsNext").onclick = $("npsNextCap").onclick = () => npsGo("next");
$("npsLy").addEventListener("toggle", () => store.set("npsLyrics", $("npsLy").open));
$("npsVol").oninput = () => { $("vol").value = $("npsVol").value; $("vol").oninput(); };
// seeking only makes sense for a Library song: the broadcast is live
function npsSeek(e) {
  if (!lp.t || !pv.duration) return;
  const r = $("npsTrack").getBoundingClientRect();
  pv.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * pv.duration; syncNow();
}
$("npsTrack").addEventListener("pointerdown", e => { if (!lp.t) return; $("npsTrack").setPointerCapture(e.pointerId); npsSeek(e); });
$("npsTrack").addEventListener("pointermove", e => { if ($("npsTrack").hasPointerCapture(e.pointerId)) npsSeek(e); });
let npsCloseT = 0;
function npsOpen() {
  if (nps.open) return;
  clearTimeout(npsCloseT); nps.open = true; nps.back = document.activeElement;
  $("nps").hidden = $("npsFade").hidden = false;
  document.body.classList.add("nps-on"); document.documentElement.style.overflow = "hidden";
  for (const el of [document.querySelector(".nav"), $("page"), $("bar")]) el.inert = true;
  $("bLink").setAttribute("aria-expanded", "true");
  $("npsVol").value = $("vol").value; $("npsB").scrollTop = 0;
  npsRender();
  history.pushState({nps: 1}, "");                 // Android's back button closes the sheet
  requestAnimationFrame(() => requestAnimationFrame(() => { $("nps").classList.add("open"); $("npsFade").classList.add("open"); }));
  $("npsClose").focus({preventScroll: true});
}
function npsShut(fromHistory) {
  if (!nps.open) return;
  nps.open = false;
  $("nps").classList.remove("open", "drag"); $("nps").style.transform = ""; $("npsFade").classList.remove("open");
  document.body.classList.remove("nps-on"); document.documentElement.style.overflow = "";
  for (const el of [document.querySelector(".nav"), $("page"), $("bar")]) el.inert = false;
  $("bLink").setAttribute("aria-expanded", "false");
  npsCloseT = setTimeout(() => { $("nps").hidden = $("npsFade").hidden = true; }, 260);
  if (nps.back && nps.back.focus) nps.back.focus({preventScroll: true});
  if (!fromHistory && history.state && history.state.nps) history.back();
}
window.addEventListener("popstate", () => {
  npsShut(true);
  if (nps.go) { const go = nps.go; nps.go = null; location.hash = go; }
});
$("bLink").onclick = npsOpen;
$("npsClose").onclick = () => npsShut();
$("npsFade").onclick = () => npsShut();
document.addEventListener("keydown", e => { if (e.key === "Escape" && nps.open && !$("dlg").open) { e.preventDefault(); npsShut(); } });
// a link inside the sheet (station, song details) closes it first, then opens that page
$("nps").addEventListener("click", e => {
  const a = e.target.closest("a[href^='#']"); if (!a) return;
  e.preventDefault(); const go = a.getAttribute("href");
  if (history.state && history.state.nps) { nps.go = go; npsShut(); }   // back out of the sheet's history entry, then go
  else { npsShut(true); location.hash = go; }
});
// swipe down to close (phone): from the handle or header, or anywhere once the sheet is scrolled to the top
(() => {
  const el = $("nps"); let y0 = null, x0 = 0, dy = 0, t0 = 0;
  el.addEventListener("touchstart", e => {
    if (e.touches.length !== 1 || innerWidth > 760 || e.target.closest("input, .track, .nps-car, .nps-f")) return (y0 = null);
    if (!e.target.closest(".nps-h, .nps-grab") && $("npsB").scrollTop > 0) return (y0 = null);
    y0 = e.touches[0].clientY; x0 = e.touches[0].clientX; dy = 0; t0 = Date.now();
  }, {passive: true});
  el.addEventListener("touchmove", e => {
    if (y0 == null) return;
    dy = e.touches[0].clientY - y0;
    if (dy <= 0 || Math.abs(e.touches[0].clientX - x0) > dy) { if (dy < -4) y0 = null; return; }
    e.preventDefault(); el.classList.add("drag"); el.style.transform = `translateY(${dy}px)`;
  }, {passive: false});
  el.addEventListener("touchend", () => {
    if (y0 == null) return; y0 = null; el.classList.remove("drag");
    if (dy > 120 || (dy > 40 && dy / (Date.now() - t0) > 0.6)) npsShut(); else el.style.transform = "";
  });
})();

// lock screen / notification controls
const ms = "mediaSession" in navigator ? navigator.mediaSession : null;
let msKey = null;
const PAGE_TITLE = document.title;
function updateMediaSession(force) {
  const t = current, st = R && stOf(selected), on = !!(lid || lp.t);
  // the tab title too: Android shows it on the lock screen and over Bluetooth when it has no song details
  document.title = on ? `${t ? t.title : "Next song on the way…"} · ${(t && t.station_name) || (st && st.name) || "Agentora"}` : PAGE_TITLE;
  if (!ms) return;
  ms.playbackState = lp.t ? (pv.paused ? "paused" : "playing") : !lid ? "none" : audio.paused ? "paused" : "playing";
  const key = [on && "on", t && t.id, t && t.cover, st && st.name, st && st.art].join("|");
  if (key === msKey && !force) return; msKey = key;
  if (!on) { ms.metadata = null; return; }
  ms.metadata = new MediaMetadata({title: t ? t.title : "Next song on the way…", artist: (t && t.station_name) || (st && st.name) || "Agentora",
    album: "Agentora", artwork: t && t.cover ? [{src: location.origin + t.cover, sizes: "768x768", type: "image/jpeg"}]
      : st && st.art ? [{src: location.origin + stationArt(st), sizes: "768x768", type: "image/jpeg"}] : [{src: "/icons/radio-180.png", sizes: "180x180", type: "image/png"}]});
}
// Chrome on Android ties the lock-screen / Bluetooth song details to the playing media: a new stream source (every
// ⏭ ⏮, cut or reconnect) or the silent bridge clip can leave them blank. Send them again whenever playback (re)starts
// and when the page comes back into view.
const resendMeta = () => { updateMediaSession(true); setTimeout(() => updateMediaSession(true), 1500); };
audio.addEventListener("playing", resendMeta);
pv.addEventListener("playing", resendMeta);
document.addEventListener("visibilitychange", () => { if (!document.hidden) updateMediaSession(true); });
if (ms) {
  const act = (a, f) => { try { ms.setActionHandler(a, f); } catch {} };
  act("play", () => { if (!lid) return togglePlay(); wantPlay = true; audio.paused ? connect() : audio.play(); });   // live: rejoin, don't resume stale audio
  act("pause", () => { if (lid || lp.t) togglePlay(); });
  act("stop", () => { if (lp.t) return lpStop(); if (lid) togglePlay(); });
  act("nexttrack", () => $("bSkip").click());
  act("previoustrack", () => lp.t ? lpBack() : backNow());
}

// ---------------- data
const cache = {playlist: null, listened: null, station: {}};
async function loadHistory() {
  const raw = (await api("/api/radio/listened?limit=150").catch(() => ({tracks: cache.listened || []}))).tracks;
  cache.listened = raw.filter((t, i) => !(i && raw[i - 1].title === t.title && Math.abs(raw[i - 1].listened_at - t.listened_at) < 900));
  if (view.tab === "collection" && view.sub === "history") renderView();
}
async function loadPlaylist() { cache.playlist = (await api("/api/radio/playlist").catch(() => ({tracks: []}))).tracks; return cache.playlist; }
async function loadAll() { cache.all = (await api("/api/radio/songs").catch(() => ({tracks: []}))).tracks; return cache.all; }
async function loadStation(id) { cache.station[id] = (await api(`/api/radio/stations/${id}/history?limit=500&upcoming=1`).catch(() => ({tracks: []}))).tracks; return cache.station[id]; }

async function refresh() {
  try {
    R = await api("/api/radio/state" + (lid ? `?listener=${lid}` : "")); R._at = performance.now();
    if (R.on && R.station && R.station !== selected) { selected = R.station; store.set("station", selected); }
    else if (!selected && R.station) selected = R.station;
    if (prevOn && !R.on && lid) { wantPlay = false; disconnect(); toast("The radio was switched off."); }
    prevOn = R.on;
    if (R.stream) followCut(R.stream.cut);            // someone else skipped, or the station changed
    const n = R.stream ? R.stream.listeners : 0;
    $("lisN").textContent = n; $("lisChip").textContent = n === 1 ? "person listening now" : "listening now";
    $("sideBadge").textContent = `📡 ${n}`; $("sideBadge").classList.toggle("live", n > 0);
    renderSideRadio(); syncNow();
    const sig = JSON.stringify([R.on, R.station, R.stations.map(s => [s.id, s.name, s.ready, s.stats.played, s.stats.kept, s.description, s.analysis.status]), R.reruns]);
    if (sig !== shelfSig) { shelfSig = sig; if (view.tab === "lib" || view.tab === "station") renderView(); if (!current) renderBar(); }
  } catch (e) { $("gpuChip").textContent = "radio unreachable"; }
}
function renderSideRadio() {
  const st = stOf(R.station), g = R.gpu, ws = g.workers || [], dead = ws.filter(w => w.disabled).length, busy = ws.filter(w => w.job).length;
  $("radioState").innerHTML = !R.on ? "Off — nothing is being made." : `On · <b>${esc(st ? st.name : "?")}</b>${st && st.ready != null && st.id !== RERUNS ? ` · ${st.ready} song${st.ready === 1 ? "" : "s"} ready` : ""}`;
  $("powerBtn").textContent = R.on ? "Turn radio off" : "Turn radio on";
  $("gpuChip").textContent = g.owner === "studio" ? (busy ? `Radio: recording a song${dead ? ` (${dead} card faulted)` : ""}` : "Radio: music model loaded, idle")
    : g.owner === "llm" ? "Radio: GPU lent to the LLM" : g.owner === "switching" || g.owner === "waiting" ? g.detail : "Radio: not using the GPU";
}
$("powerBtn").onclick = async () => {
  const on = !R.on;
  if (on && !selected) return toast("Pick a station first.");
  if (!on) { wantPlay = false; disconnect(); }
  try { await post("/api/radio/power", {on, station: selected}); prevOn = on; toast(on ? "Radio on." : "Radio off — the GPU is freed once the current take finishes."); } catch (e) { toast(e.message); }
  refresh();
};

