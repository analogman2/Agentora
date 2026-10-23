// Agentora radio page, part 1 of 5: Shared helpers and state, generated cover art, and this device's sound (EQ, balance, mono).
// Plain scripts loaded in order by radio.html; they share one global scope.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const mmss = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const api = async (path, opts = {}) => { const r = await fetch(path, opts); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.statusText); return j; };
const post = (path, body) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
const store = { get(k, d) { try { const v = localStorage.getItem("radio." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
                set(k, v) { try { localStorage.setItem("radio." + k, JSON.stringify(v)); } catch {} } };
const RERUNS = "__reruns__";
const met = {stalls: 0, reconnects: 0};
const lp = {t: null, queue: [], i: -1};          // a song playing from the Library (see 'playing from the Library')
let R = null, current = null, wantPlay = false, actx = null, devNodes = null, shelfSig = "";
let selected = store.get("station", null);
const audio = $("audio"), pv = $("pv");
audio.crossOrigin = "anonymous";                   // the stream comes from the relay's port; Web Audio (EQ, meters) needs CORS
audio.volume = pv.volume = store.get("vol", 0.85); $("vol").value = audio.volume;
$("vol").oninput = () => { audio.volume = pv.volume = +$("vol").value; store.set("vol", audio.volume); };   // the live stream and Library songs alike

const lyricsHTML = agentoraLyrics;                 // [Verse] / [Chorus] blocks (shared with every app's Now Playing)

// ---------------- generated cover art (deterministic from a seed: same song, same cover)
const prng = agentoraPrng;
function hueOf(st) { let h = 0; for (const ch of (st ? (st.id || "") + (st.name || "") : "agentora")) h = (h * 31 + ch.charCodeAt(0)) % 360; return h; }
const artCache = new Map(), songArt = agentoraSongArt;   // generated covers (shared with every app's Now Playing)
// drawn cover art when it exists (made by the Image & Video Studio's picture model), else the generated pattern
const stationArt = (st) => st && st.art && st.id !== RERUNS ? `/api/radio/stations/${encodeURIComponent(st.id)}/art?v=${st.art}` : patternArt(st);
function patternArt(st) {
  if (!st) return songArt("agentora", 220);
  if (st.id === RERUNS) return songArt("reruns-mix", 350);
  const key = "st|" + st.id + st.name; if (artCache.has(key)) return artCache.get(key);
  const h = hueOf(st), r = prng(st.id + st.name), cx = 150 + (r() - .5) * 60, cy = 150 + (r() - .5) * 60;
  let rings = ""; for (let i = 26; i > 0; i--) rings += `<circle cx="${cx}" cy="${cy}" r="${i * 9}" fill="none" stroke="hsla(${(h + i * 4) % 360},70%,${55 + (i % 4) * 7}%,.55)" stroke-width="${1 + (i % 3)}"/>`;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300"><defs><radialGradient id="g" cx=".4" cy=".35"><stop offset="0" stop-color="hsl(${h},55%,40%)"/><stop offset="1" stop-color="hsl(${(h + 40) % 360},60%,12%)"/></radialGradient></defs>
    <rect width="300" height="300" fill="url(#g)"/>${rings}<circle cx="${cx}" cy="${cy}" r="34" fill="hsl(${(h + 180) % 360},80%,70%)" opacity=".9"/></svg>`;
  const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  artCache.set(key, url); return url;
}
const stOf = (id) => id === RERUNS ? rerunStation() : R && R.stations.find(s => s.id === id);
const trackArt = (t) => t ? (t.cover || songArt(t.id, hueOf(stOf(t.station) || {id: t.station || "", name: t.station_name || ""}))) : stationArt(stOf(selected));
const trackHue = (t) => hueOf(stOf(t && t.station) || {id: (t && t.station) || "", name: (t && t.station_name) || ""});
function rerunStation() {
  const r = (R && R.reruns) || {songs: 0, stations: 0, favorites: 0};
  return {id: RERUNS, name: "Reruns", kind: "reruns", ready: r.songs, stats: {played: 0, kept: r.favorites}, analysis: {}, problem: null,
          description: "Shuffles songs from every station — thumbs-up and playlist songs more often. Nothing new is generated, so the GPU stays free.",
          meta: r.songs ? `${r.songs} songs · ${r.stations} stations` : "plays songs once stations have aired some"};
}

// ---------------- this device's sound (EQ / balance / mono through Web Audio; off = plain playback)
// Phones get plain playback by default: Bluetooth (car, headphones) route changes can silently suspend Web Audio there.
const PHONE = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const DEV_DEFAULT = {low: 0, mid: 0, high: 0, balance: 0, mono: false, fx: !PHONE};
let dev = Object.assign({}, DEV_DEFAULT, store.get("device", {}));
if (PHONE && !dev.fxChosen) dev.fx = false;          // older saved settings never chose: use the phone default
function applyDevice() {
  if (!devNodes) return;
  devNodes.low.gain.value = dev.low; devNodes.mid.gain.value = dev.mid; devNodes.high.gain.value = dev.high;
  devNodes.mono.channelCount = dev.mono ? 1 : 2; devNodes.pan.pan.value = dev.balance / 100;
}
function ensureAudioGraph() {
  if (actx || !dev.fx) return;
  try {
    actx = new (window.AudioContext || window.webkitAudioContext)({latencyHint: "playback"});   // big buffers: smooth on phones
    const src = actx.createMediaElementSource(audio);
    const low = actx.createBiquadFilter(), mid = actx.createBiquadFilter(), high = actx.createBiquadFilter();
    low.type = "lowshelf"; low.frequency.value = 120; mid.type = "peaking"; mid.frequency.value = 1000; mid.Q.value = 0.9;
    high.type = "highshelf"; high.frequency.value = 8000;
    const mono = actx.createGain(); mono.channelCountMode = "explicit"; mono.channelInterpretation = "speakers";
    const pan = actx.createStereoPanner();
    src.connect(low); low.connect(mid); mid.connect(high); high.connect(mono); mono.connect(pan); pan.connect(actx.destination);
    devNodes = {low, mid, high, mono, pan}; applyDevice();
    actx.onstatechange = () => { if (wantPlay && lid && !audio.paused && actx.state !== "running") actx.resume().catch(() => {}); };
  } catch (e) { console.warn("no Web Audio", e); }
}

