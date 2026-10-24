// Agentora shared script: Now Playing (the sheet, one-song-at-a-time, the shared player), the app switcher and the now-playing bar, for every app. Served at /agentora/agentora.js by the
// radio (:7860) and Home (:7871). A page says which app it is with <html data-agentora-app="home|radio|music"> and puts <div data-agentora-apps></div> where
// the switcher goes. Agentora is one window: the radio page (:7860/radio) has the tabs Home · Stations · Library · Create,
// with Home and the Music Studio running inside it as panes (frames; AG_EMBED below). Switching changes the tab in place
// (the radio keeps playing); a pane asks the page around it to switch, and the Music Studio plays in the radio page's bar.
// ---- Now Playing, shared by every app:
//   agentoraLyrics(t)        a song's lyrics as labelled [Verse] / [Chorus] blocks
//   agentoraSolo             one song at a time: a registered media element that starts playing stops every other one, in
//                            this tab and in the other Agentora tabs (BroadcastChannel; a storage event where that's missing)
//   agentoraSheet(bar, src)  the Now Playing view: pull it up from the bar (or tap the bar), previous · now · next covers
//                            (tap or swipe), the song's details and the full controls. src says what to show and does what's pressed.
//   agentoraPlayer           the one player on Home and the Music Studio (the radio page has its own): agentoraPlay(list, i)
const agEsc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
// In a pane of the radio page: that page has the switcher, the ☰ panel, the now-playing bar and the player.
const AG_EMBED = window.top !== window;
if (AG_EMBED) document.documentElement.classList.add("agentora-embed");
const agTime = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

const AG_LY_SECTION = /^(verse|chorus|pre-?chorus|post-?chorus|bridge|intro|outro|hook|refrain|interlude|break|instrumental|solo|coda|drop|breakdown)\b/i;
window.agentoraLyrics = function (t) {
  const blocks = [];
  let cur = null, sung = 0;
  for (const raw of String((t && t.lyrics) || "").replace(/\r/g, "").split("\n")) {
    const line = raw.trim(), tag = line.match(/^\[([^\]]{1,120})\]$/);
    if (tag && AG_LY_SECTION.test(tag[1].trim())) { cur = {tag: tag[1].trim(), lines: []}; blocks.push(cur); continue; }
    if (!cur) { cur = {tag: null, lines: []}; blocks.push(cur); }
    if (tag) cur.lines.push(`<span class="lynote">${agEsc(tag[1])}</span>`);
    else if (line) { cur.lines.push(`<span class="lyl">${agEsc(line)}</span>`); sung++; }
  }
  if (!sung) return `<p class="lynone">${t && t.mode === "instrumental" ? "🎼 Instrumental: this song has no lyrics."
    : "No lyrics were saved for this song."}</p>`;
  return `<div class="lyrics">${blocks.filter(b => b.lines.length || b.tag).map(b => {
    const chorus = b.tag && /^(chorus|hook|refrain)/i.test(b.tag);
    return `<div class="lysec${chorus ? " chorus" : ""}">${b.tag ? `<div class="lytag">${agEsc(b.tag)}</div>` : ""}${b.lines.join("")}</div>`;
  }).join("")}</div>`;
};

window.agentoraSolo = (() => {
  // A tab that's playing notes it (agentora.audio.owner, refreshed every second). start() in another tab first asks that
  // tab to stop and waits for its answer (at most 400 ms), so two songs never overlap; with nothing playing elsewhere it
  // starts at once. A source that starts some other way (the lock screen, a media key) still stops the rest as it starts.
  const me = Math.random().toString(36).slice(2), els = [], OWNER = "agentora.audio.owner";
  let ch = null, waiting = null;
  try { ch = new BroadcastChannel("agentora-audio"); } catch (e) {}
  const send = (m) => { if (ch) ch.postMessage(m); else try { localStorage.setItem("agentora.audio", JSON.stringify({...m, n: Math.random()})); } catch (e) {} };
  const stopAll = (except) => els.forEach(([el, stop]) => { if (el !== except) stop(); });
  const busy = () => els.some(([el]) => !el.paused);
  const own = () => { if (busy()) try { localStorage.setItem(OWNER, JSON.stringify({tab: me, at: Date.now()})); } catch (e) {} };
  function hear(m) {
    if (!m || m.tab === me) return;
    if (m.stop) { stopAll(); send({tab: me, stopped: m.tab}); }
    if (m.stopped === me && waiting) { const go = waiting; waiting = null; go(); }
  }
  if (ch) ch.onmessage = (e) => hear(e.data);
  window.addEventListener("storage", (e) => { if (e.key === "agentora.audio" && e.newValue) try { hear(JSON.parse(e.newValue)); } catch (err) {} });
  setInterval(own, 1000);
  return {
    // stop() must be safe to call when the element isn't playing
    register(el, stop) {
      els.push([el, stop || (() => el.pause())]);
      el.addEventListener("play", () => { stopAll(el); if (!waiting) send({tab: me, stop: true}); own(); });
    },
    start(el, play) {
      stopAll(el); waiting = null;
      let o = null; try { o = JSON.parse(localStorage.getItem(OWNER) || "null"); } catch (e) {}
      if (!o || o.tab === me || Date.now() - o.at > 2500) return play();
      waiting = play; send({tab: me, stop: true});
      setTimeout(() => { if (waiting === play) { waiting = null; play(); } }, 400);
    },
  };
})();

// generated cover art, deterministic from a seed (same song, same cover): for songs without a drawn cover
window.agentoraPrng = function (seed) {
  let h = 2166136261; for (const c of String(seed)) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return () => { h += 0x6D2B79F5; let t = h; t = Math.imul(t ^ t >>> 15, t | 1); t ^= t + Math.imul(t ^ t >>> 7, t | 61); return ((t ^ t >>> 14) >>> 0) / 4294967296; };
};
const agArtCache = new Map();
window.agentoraSongArt = function (seed, baseHue) {
  const key = seed + "|" + baseHue; if (agArtCache.has(key)) return agArtCache.get(key);
  const r = agentoraPrng(seed), h = ((baseHue ?? r() * 360) + (r() - .5) * 70 + 360) % 360, h2 = (h + 25 + r() * 70) % 360, h3 = (h + 150 + r() * 60) % 360;
  const c = (hh, s, l, a = 1) => `hsla(${hh.toFixed(0)},${s}%,${l}%,${a})`;
  const style = Math.floor(r() * 4);
  let shapes = "";
  if (style === 0) {                                         // soft blobs
    for (let i = 0; i < 6; i++) shapes += `<circle cx="${r() * 300}" cy="${r() * 300}" r="${50 + r() * 110}" fill="${c([h, h2, h3][i % 3], 70 + r() * 20, 45 + r() * 25, .55 + r() * .3)}"/>`;
    shapes = `<g filter="url(#b)">${shapes}</g>`;
  } else if (style === 1) {                                  // sunset sun + horizon
    const y = 150 + r() * 60;
    shapes = `<circle cx="${80 + r() * 140}" cy="${y - 20}" r="${60 + r() * 40}" fill="${c(h3, 85, 62, .95)}"/>` +
      Array.from({length: 7}, (_, i) => `<rect x="0" y="${y + i * 16 + i * i}" width="300" height="${4 + i * 1.5}" fill="${c(h, 60, 14 + i * 3, .9)}"/>`).join("");
  } else if (style === 2) {                                  // waves
    for (let i = 0; i < 9; i++) {
      const y = 40 + i * 30, a = 10 + r() * 26, f = 1 + r() * 2.5;
      let d = `M0 ${y}`; for (let x = 0; x <= 300; x += 15) d += ` L${x} ${(y + Math.sin(x / 300 * Math.PI * 2 * f + i) * a).toFixed(1)}`;
      shapes += `<path d="${d}" fill="none" stroke="${c(i % 2 ? h2 : h3, 80, 70, .75)}" stroke-width="${2 + r() * 5}"/>`;
    }
  } else {                                                   // rings
    const cx = 60 + r() * 180, cy = 60 + r() * 180;
    for (let i = 14; i > 0; i--) shapes += `<circle cx="${cx}" cy="${cy}" r="${i * 22}" fill="none" stroke="${c(i % 2 ? h2 : h3, 75, 65, .5)}" stroke-width="${3 + (i % 3) * 3}"/>`;
  }
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300"><defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${c(h, 65, 42)}"/><stop offset="1" stop-color="${c(h2, 70, 18)}"/></linearGradient>
    <filter id="b"><feGaussianBlur stdDeviation="22"/></filter>
    <filter id="n"><feTurbulence type="fractalNoise" baseFrequency=".9" numOctaves="2" stitchTiles="stitch"/><feColorMatrix values="0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 .07 0"/></filter></defs>
    <rect width="300" height="300" fill="url(#g)"/>${shapes}<rect width="300" height="300" filter="url(#n)"/></svg>`;
  const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  agArtCache.set(key, url); return url;
};

const AG_ICONS = {
  prev: '<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M19 5v14l-9-7zM9 5H6v14h3z"/></svg>',
  next: '<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M5 5v14l9-7zM15 5h3v14h-3z"/></svg>',
  play: '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  pause: '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>',
  up: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M7 10v11H4V10h3ZM7 10l4.5-7a2 2 0 0 1 2.5 2.4L13 9h5.7a2 2 0 0 1 2 2.3l-1.2 7.6A2.5 2.5 0 0 1 17 21H7"/></svg>',
  down: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M17 14V3h3v11h-3ZM17 14l-4.5 7a2 2 0 0 1-2.5-2.4L11 15H5.3a2 2 0 0 1-2-2.3l1.2-7.6A2.5 2.5 0 0 1 7 3h10"/></svg>',
  add: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9.5"/><path d="M12 7.5v9M7.5 12h9"/></svg>',
  added: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" fill="currentColor" stroke="none"/><path d="m7.5 12.5 3 3 6-6.5" stroke="#fff"/></svg>',
  radio: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="3" y="8" width="18" height="12" rx="2"/><circle cx="15.5" cy="14" r="2.5"/><path d="M7 12h4M7 16h4M6 8l11-4"/></svg>',
};
window.agentoraIcons = AG_ICONS;

window.agentoraSheet = function (bar, src) {
  const q = (id) => document.getElementById(id);
  const fade = document.createElement("div"), el = document.createElement("section");
  fade.className = "npsfade"; fade.id = "npsFade"; fade.hidden = true;
  el.className = "nps"; el.id = "nps"; el.hidden = true;
  el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); el.setAttribute("aria-labelledby", "npsTitle");
  el.innerHTML = `<div class="nps-grab" aria-hidden="true"></div>
  <header class="nps-h">
    <button class="nps-btn" id="npsClose" type="button" title="Close (Esc)" aria-label="Close Now Playing"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></button>
    <div class="nps-ht"><div class="nps-label">Now Playing</div><a id="npsSt" href="#"></a></div>
    <a class="nps-btn" id="npsMore" href="#" title="Song details: lyrics, story, scores" aria-label="Song details"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9.5"/><path d="M12 11v6M12 7.5v.01"/></svg></a>
  </header>
  <div class="nps-b" id="npsB">
    <div class="nps-car" id="npsCar">
      <button class="nps-side prev" id="npsPrev" type="button"></button>
      <div class="nps-cover"><img id="npsCover" alt="" draggable="false"></div>
      <button class="nps-side next" id="npsNext" type="button"></button>
    </div>
    <div class="nps-caps"><button type="button" id="npsPrevCap"></button><button type="button" class="nx" id="npsNextCap"></button></div>
    <div class="nps-info"><h2 id="npsTitle"></h2><div class="nps-sub" id="npsSub"></div><div class="nps-style" id="npsStyle"></div>
      <div class="nps-chips" id="npsChips"></div><div class="nps-status" id="npsStatus"></div></div>
    <details class="nps-ly" id="npsLy"><summary class="nps-label">Lyrics</summary><div id="npsLyB"></div></details>
  </div>
  <footer class="nps-f">
    <div class="nps-prog"><span id="npsEl">0:00</span><div class="nps-track" id="npsTrack"><i id="npsProg"></i></div><span id="npsDur">0:00</span></div>
    <div class="nps-ctl" id="npsCtl"></div>
    <div class="nps-vol" id="npsVolRow"><span aria-hidden="true">🔈</span><input type="range" id="npsVol" min="0" max="1" step="0.01" aria-label="Volume"><span aria-hidden="true">🔊</span></div>
  </footer>`;
  document.body.append(fade, el);
  bar.classList.add("nps-bar");
  if (!bar.querySelector(".nps-handle")) bar.insertAdjacentHTML("afterbegin", '<div class="nps-handle" aria-hidden="true"></div>');
  const S = {open: false, sig: "", key: null, lyKey: null, ctl: "", around: {}, slide: null, show, hide, render, tick};
  let closeT = 0, eatClick = 0, inerted = [];
  const lyOpen = () => { try { return localStorage.getItem("agentora.npsLyrics") === "1"; } catch (e) { return false; } };
  q("npsLy").addEventListener("toggle", () => { try { localStorage.setItem("agentora.npsLyrics", q("npsLy").open ? "1" : "0"); } catch (e) {} });

  const sig = (a) => ["prev", "next"].map(d => a[d] ? `${a[d].key}|${a[d].cover}` : "").join("|") + (a.live ? "L" : "");
  const can = (dir) => !!S.around[dir] && S.around[dir === "prev" ? "canPrev" : "canNext"] !== false;
  function side(b, cap, t, dir, a) {
    const label = dir === "prev" ? "Previous" : "Up next", ok = can(dir);
    const none = (a.notes || {})[dir] || (dir === "prev" ? "Nothing before this" : "Nothing queued");
    const html = t ? (t.cover ? `<img src="${agEsc(t.cover)}" alt="" draggable="false">` : '<span aria-hidden="true">♪</span>')
      : `<span aria-hidden="true">${agEsc((a.icons || {})[dir] || (dir === "prev" ? "⏮" : "♪"))}</span>`;
    if (b.dataset.h !== html) { b.dataset.h = html; b.innerHTML = html; }
    b.classList.toggle("empty", !t);
    b.disabled = cap.disabled = !ok;
    const what = t ? `${label}: ${t.title}${t.sub ? " · " + t.sub : ""}` : `${label}: ${none}`;
    b.title = cap.title = ok ? `${what} (tap or swipe to play${a.live ? " — for everyone listening" : ""})` : what;
    b.setAttribute("aria-label", b.title);
    cap.innerHTML = `<span>${dir === "prev" ? "‹ " : ""}${label}${dir === "next" ? " ›" : ""}</span><b>${agEsc(t ? t.title : none)}</b>`;
  }
  function render() {
    if (!S.open) return;
    const i = src.info() || {}, a = S.around = src.around() || {};
    S.key = i.key;
    q("npsCover").src = i.cover || ""; q("npsCover").style.visibility = i.cover ? "" : "hidden";
    q("npsTitle").textContent = i.title || "";
    q("npsSub").textContent = i.sub || "";
    const k = q("npsSt"); k.textContent = i.kicker || ""; k.href = i.kickerHref || "#"; k.classList.toggle("off", !i.kickerHref);
    const m = q("npsMore"); m.style.visibility = i.more ? "" : "hidden"; if (i.more) m.href = i.more;
    q("npsStyle").textContent = i.style || "";
    q("npsChips").innerHTML = (i.chips || []).map(c => `<span class="nps-chip">${agEsc(c)}</span>`).join("");
    if (i.status != null) q("npsStatus").textContent = i.status;
    const ly = q("npsLy"); ly.hidden = i.lyrics == null;
    if (i.lyrics != null && S.lyKey !== i.key) { S.lyKey = i.key; q("npsLyB").innerHTML = i.lyrics; ly.open = lyOpen(); }
    side(q("npsPrev"), q("npsPrevCap"), a.prev, "prev", a);
    side(q("npsNext"), q("npsNextCap"), a.next, "next", a);
    S.sig = sig(a);
    const ctl = (src.controls() || []).map(c => `<button type="button" class="nps-btn${c.play ? " play" : ""}${c.on ? " on" : ""}" data-key="${agEsc(c.key)}"
      title="${agEsc(c.title || "")}" aria-label="${agEsc(c.label || c.title || "")}"${c.pressed != null ? ` aria-pressed="${c.pressed}"` : ""}${c.disabled ? " disabled" : ""}>${c.html}</button>`).join("");
    if (ctl !== S.ctl) { S.ctl = ctl; q("npsCtl").innerHTML = ctl; }
    el.style.setProperty("--npsWash", i.hue != null ? `radial-gradient(700px 420px at 50% 0%, hsla(${i.hue},70%,55%,.22), transparent 70%)` : "none");
    const v = src.volume; q("npsVolRow").hidden = !v;
    if (v && document.activeElement !== q("npsVol")) q("npsVol").value = v.get();
    if (S.slide && S.slide.went && S.slide.key !== i.key) unslide();
    tick();
  }
  function tick() {
    if (!S.open) return;
    const p = src.progress() || {};
    q("npsEl").textContent = p.el || "0:00"; q("npsDur").textContent = p.dur || "0:00";
    q("npsProg").style.width = Math.max(0, Math.min(100, (p.frac || 0) * 100)) + "%";
    q("npsTrack").classList.toggle("seek", !!p.seek); q("npsTrack").title = p.hint || "";
    if (sig(src.around() || {}) !== S.sig) render();    // a new "up next" pick or history entry
  }
  q("npsCtl").addEventListener("click", e => { const b = e.target.closest("[data-key]"); if (b && !b.disabled) src.control(b.dataset.key); });
  q("npsVol").oninput = () => src.volume && src.volume.set(+q("npsVol").value);
  const go = (dir) => { if (can(dir)) src.go(dir); };
  q("npsPrev").onclick = q("npsPrevCap").onclick = () => go("prev");
  q("npsNext").onclick = q("npsNextCap").onclick = () => go("next");
  function seek(e) {
    const p = src.progress() || {}; if (!p.seek) return;
    const r = q("npsTrack").getBoundingClientRect(); p.seek(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width))); tick();
  }
  q("npsTrack").addEventListener("pointerdown", e => { if (!(src.progress() || {}).seek) return; q("npsTrack").setPointerCapture(e.pointerId); seek(e); });
  q("npsTrack").addEventListener("pointermove", e => { if (q("npsTrack").hasPointerCapture(e.pointerId)) seek(e); });

  // ---- opening and closing. dragging==true: a finger is pulling it up from the bar (pose() follows, then settle() or hide())
  const phone = () => innerWidth <= 760;
  const span = () => phone() ? (el.offsetHeight || innerHeight) : innerHeight;    // how far the sheet travels
  function show(dragging) {
    if (S.open) return;
    clearTimeout(closeT); S.open = true; S.back = document.activeElement;
    el.hidden = fade.hidden = false;
    document.body.classList.add("nps-on"); document.documentElement.style.overflow = "hidden";
    q("npsB").scrollTop = 0; S.ctl = ""; S.lyKey = null;
    if (src.onOpen) src.onOpen();
    render();
    history.pushState({nps: 1}, "");                 // Android's back button closes it
    if (dragging === true) { drag(true); pose(0); }
    else requestAnimationFrame(() => requestAnimationFrame(settle));
  }
  function settle() {                                // all the way open, from wherever a drag left it
    if (!S.open) return;
    drag(false); el.classList.add("open"); fade.classList.add("open");
    // the rest of the page goes inert (not while a finger is still on the bar: it would lose the drag)
    inerted = [...document.body.children].filter(x => x !== el && x !== fade && !x.inert && !x.matches(".toast, [role=status], script, style, link, audio"));
    inerted.forEach(x => { x.inert = true; });
    q("npsClose").focus({preventScroll: true, focusVisible: false});
  }
  function drag(on) { for (const x of [el, fade]) { x.classList.toggle("drag", on); if (!on) x.style.transform = x.style.opacity = ""; } }
  function pose(p) {                                 // 0 = closed, down at the bar … 1 = open
    p = Math.max(0, Math.min(1, p));
    el.style.transform = phone() ? `translateY(${((1 - p) * 100).toFixed(2)}%)` : `translate(-50%, calc(-50% + ${((1 - p) * 100).toFixed(2)}vh))`;
    fade.style.opacity = String(p);
  }
  function hide(fromHistory) {
    if (!S.open) return;
    S.open = false; unslide();
    drag(false); el.classList.remove("open"); fade.classList.remove("open");
    document.body.classList.remove("nps-on"); document.documentElement.style.overflow = "";
    inerted.forEach(x => { x.inert = false; }); inerted = [];
    if (src.onClose) src.onClose();
    closeT = setTimeout(() => { el.hidden = fade.hidden = true; }, 300);
    if (S.back && S.back.focus) S.back.focus({preventScroll: true});
    if (!fromHistory && history.state && history.state.nps) history.back();
  }
  window.addEventListener("popstate", () => {
    hide(true);
    if (S.go) { const g = S.go; S.go = null; location.hash = g; }
  });
  q("npsClose").onclick = () => hide();
  fade.onclick = () => hide();
  document.addEventListener("keydown", e => { if (e.key === "Escape" && S.open && !document.querySelector("dialog[open]")) { e.preventDefault(); hide(); } });
  el.addEventListener("click", e => {                // a link to this page (station, song details) closes it, then goes there
    const a = e.target.closest("a[href]"); if (!a) return;
    if (a.classList.contains("off")) return e.preventDefault();
    if (!a.getAttribute("href").startsWith("#")) return;
    e.preventDefault(); const g = a.getAttribute("href");
    if (history.state && history.state.nps) { S.go = g; hide(); } else { hide(true); location.hash = g; }
  });

  // ---- gestures. The whole bar pulls up: a tap anywhere but its buttons opens the view; a vertical drag from anywhere
  // (a button too) pulls it up with the finger or mouse. Past a third of the way, or a quick flick, it opens; else it drops
  // back. The same in reverse closes it. Covers swipe sideways (only once the move is clearly sideways, never during a
  // vertical drag): past about a third of a cover, or a flick, goes to that song.
  window.addEventListener("click", e => { if (Date.now() < eatClick) { eatClick = 0; e.preventDefault(); e.stopPropagation(); } }, true);
  bar.addEventListener("click", e => {
    if (!S.open && (e.target.closest("[data-np-open]") || !e.target.closest("button, a, input, select, textarea"))) show();
  });
  const speed = (pts) => {                           // px/ms over the last 100 ms of movement
    const [tl, vl] = pts[pts.length - 1], [t, v] = pts.find(([t]) => tl - t <= 100);
    return (vl - v) / Math.max(16, tl - t);
  };
  function vertical(host, opening, ok) {
    let g = null;
    host.addEventListener("pointerdown", e => {
      if (g || e.button > 0 || (opening ? S.open : !S.open) || !ok(e)) return;
      g = {id: e.pointerId, x0: e.clientX, y0: e.clientY, drag: false, pts: [[e.timeStamp, e.clientY]]};
    });
    window.addEventListener("pointermove", e => {         // on the window: a fast mouse drag leaves the host at once
      if (!g || e.pointerId !== g.id) return;
      const d = opening ? g.y0 - e.clientY : e.clientY - g.y0, dx = Math.abs(e.clientX - g.x0);
      if (!g.drag) {
        if (d < 8 && dx < 8) return;
        if (d < 8 || dx > d) { g = null; return; }   // sideways, or the other way: not this gesture
        g.drag = true; try { host.setPointerCapture(e.pointerId); } catch (err) {}
        if (opening) show(true); else drag(true);
      }
      g.pts.push([e.timeStamp, e.clientY]); if (g.pts.length > 6) g.pts.shift();
      pose(opening ? d / span() : 1 - d / span());
    });
    const end = (e, cancel) => {
      if (!g || e.pointerId !== g.id) return;
      const was = g, d = opening ? was.y0 - e.clientY : e.clientY - was.y0;
      g = null;
      if (!was.drag) return;
      eatClick = Date.now() + 500;
      const v = speed(was.pts) * (opening ? -1 : 1), far = !cancel && d > span() / 3, fast = !cancel && v > .4 && d > 24;
      if (opening) (far || fast) ? settle() : hide(); else (far || fast) ? hide() : settle();
    };
    window.addEventListener("pointerup", e => end(e));
    window.addEventListener("pointercancel", e => end(e, true));
  }
  vertical(bar, true, e => !e.target.closest("input, select, textarea"));
  vertical(el, false, e => e.pointerType === "mouse" && !!e.target.closest(".nps-h, .nps-grab"));
  // a finger closing it: touch events, so a drag at the top of the scrolled-up sheet can stop the page's pull-to-refresh
  (() => {
    let y0 = null, x0 = 0, dy = 0, pts = [];
    el.addEventListener("touchstart", e => {
      if (e.touches.length !== 1 || !S.open || e.target.closest("input, .nps-track, .nps-f")) return (y0 = null);
      if (!e.target.closest(".nps-h, .nps-grab") && q("npsB").scrollTop > 0) return (y0 = null);
      y0 = e.touches[0].clientY; x0 = e.touches[0].clientX; dy = 0; pts = [[e.timeStamp, y0]];
    }, {passive: true});
    el.addEventListener("touchmove", e => {
      if (y0 == null) return;
      const y = e.touches[0].clientY, dx = Math.abs(e.touches[0].clientX - x0); dy = y - y0;
      if (dy < 8 && dx < 8) return;
      if (dy <= 0 || dx > dy) { y0 = null; if (el.classList.contains("drag")) settle(); return; }   // up, or sideways (a cover swipe)
      e.preventDefault(); drag(true); pose(1 - dy / span());
      pts.push([e.timeStamp, y]); if (pts.length > 6) pts.shift();
    }, {passive: false});
    el.addEventListener("touchend", () => {
      if (y0 == null) return; y0 = null;
      if (dy < 8) return;
      (dy > span() / 3 || (dy > 24 && speed(pts) > .4)) ? hide() : settle();
    });
  })();
  (() => {                                           // swipe the covers
    const car = q("npsCar"); let g = null;
    const width = () => (car.querySelector(".nps-cover").offsetWidth || 300) + 16;
    const set = (px, anim) => { car.classList.toggle("swiping", !anim); car.style.setProperty("--sw", px.toFixed(1) + "px"); };
    car.addEventListener("pointerdown", e => {
      if (!S.open || g || e.button > 0 || (S.slide && !S.slide.went)) return;
      unslide();                                     // the last swipe's song hasn't come on yet (live radio): swipe again from here
      g = {id: e.pointerId, x0: e.clientX, y0: e.clientY, lock: false, dx: 0, pts: [[e.timeStamp, e.clientX]]};
      if (e.pointerType === "mouse") e.preventDefault();
    });
    window.addEventListener("pointermove", e => {
      if (!g || e.pointerId !== g.id) return;
      const dx = e.clientX - g.x0, dy = e.clientY - g.y0;
      if (!g.lock) {
        if (Math.abs(dy) > 10 && Math.abs(dy) >= Math.abs(dx)) { g = null; return; }   // vertical: scrolling or closing, never a skip
        if (Math.abs(dx) < 14 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
        g.lock = true; try { car.setPointerCapture(e.pointerId); } catch (err) {}
      }
      g.pts.push([e.timeStamp, e.clientX]); if (g.pts.length > 6) g.pts.shift();
      g.dx = dx; set(can(dx > 0 ? "prev" : "next") ? dx : dx / 4, false);   // nothing that way: it resists
    });
    const end = (e, cancel) => {
      if (!g || e.pointerId !== g.id) return;
      const was = g; g = null;
      if (!was.lock) return;
      eatClick = Date.now() + 500;
      const dir = was.dx > 0 ? "prev" : "next", w = width(), v = speed(was.pts);
      const flick = Math.abs(v) > .5 && Math.sign(v) === Math.sign(was.dx) && Math.abs(was.dx) > 30;
      if (cancel || !can(dir) || !(Math.abs(was.dx) > w * .35 || flick)) return set(0, true);
      set(was.dx > 0 ? w : -w, true);
      S.slide = {key: S.key, went: false};
      setTimeout(() => {
        if (!S.slide) return;
        S.slide.went = true; src.go(dir);
        if (S.slide) S.slide.t = setTimeout(unslide, 4000);   // the live radio takes a moment to change songs
      }, 220);
    };
    window.addEventListener("pointerup", e => end(e));
    window.addEventListener("pointercancel", e => end(e, true));
  })();
  function unslide() {
    if (!S.slide) return;
    clearTimeout(S.slide.t); S.slide = null;
    const car = q("npsCar"); car.classList.add("swiping"); car.style.setProperty("--sw", "0px");
    void car.offsetWidth; car.classList.remove("swiping");
  }
  return S;
};

window.agentoraPlayer = (() => {
  const a = new Audio(); a.preload = "auto";
  const P = {queue: [], i: -1, audio: a, get t() { return this.queue[this.i] || null; }};
  const subs = new Set(), emit = () => subs.forEach(f => { try { f(); } catch (e) { console.error(e); } });
  P.on = (f) => subs.add(f);
  try { const v = localStorage.getItem("agentora.vol"); if (v != null) a.volume = +v; } catch (e) {}
  async function enrich(t) {   // cover, lyrics and credit from the radio's song API (on the radio's own server only)
    if (!t || t.enriched || location.port !== "7860") return; t.enriched = true;
    try {
      const r = await fetch(`/api/radio/tracks/${encodeURIComponent(t.id)}`);
      if (!r.ok) return;
      const j = await r.json();
      for (const k of ["cover", "lyrics", "mode", "station_name", "credit", "seconds", "style"]) if (j[k] != null && t[k] == null) t[k] = j[k];
      emit();
    } catch (e) {}
  }
  // Phones: with the screen off, Chrome freezes a page that has stopped playing, so the next song (or a reload after a
  // network hiccup) would never start. A looping silent clip plays through every gap (between songs, while loading,
  // while retrying), as on the radio page, and a failed load is retried from where it stopped.
  const bridge = new Audio(); bridge.loop = true; bridge.volume = 0.01;
  bridge.src = (() => {                                     // 1 s of silence as a tiny WAV
    const n = 8000, b = new ArrayBuffer(44 + n), v = new DataView(b), w = (o, t) => [...t].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
    w(0, "RIFF"); v.setUint32(4, 36 + n, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, 8000, true); v.setUint32(28, 8000, true); v.setUint16(32, 1, true); v.setUint16(34, 8, true); w(36, "data"); v.setUint32(40, n, true);
    for (let i = 0; i < n; i++) v.setUint8(44 + i, 128);
    return URL.createObjectURL(new Blob([b], {type: "audio/wav"}));
  })();
  const bridgeOn = () => { if (bridge.paused) bridge.play().catch(() => {}); };
  const bridgeOff = () => { if (!bridge.paused) bridge.pause(); };
  let want = false, switching = false, retry = {n: 0, timer: null}, lastPos = 0;   // switching: a new source is loading
  P.blocked = false;                                        // the phone refused to start a song: the UI shows ▶ and we try again on wake
  function playNow() {
    agentoraSolo.start(a, () => a.play().then(() => { P.blocked = false; retry.n = 0; }).catch(e => {
      if (e.name === "NotAllowedError") { P.blocked = true; switching = false; bridgeOff(); emit(); }
      else if (e.name !== "AbortError") recover();
    }));
  }
  function recover() {                                      // the song stopped loading (Wi-Fi napped, the server restarted): pick it up again
    if (!want || !P.t || retry.timer) return;
    if (retry.n >= 6) { want = false; bridgeOff(); emit(); return; }
    bridgeOn();
    const pos = Math.max(lastPos, a.currentTime || 0), src = P.t.audio;
    retry.timer = setTimeout(() => {
      retry.timer = null; retry.n++;
      if (!want || !P.t || P.t.audio !== src) return;
      switching = true; a.src = src + (src.includes("?") ? "&" : "?") + "r=" + Date.now();
      a.addEventListener("loadedmetadata", () => { if (pos > 1) try { a.currentTime = pos; } catch (e) {} }, {once: true});
      playNow();
    }, Math.min(15000, 1000 * 2 ** retry.n));
  }
  function load() {
    const t = P.t; if (!t) return P.stop();
    want = true; lastPos = 0; retry.n = 0; clearTimeout(retry.timer); retry.timer = null;
    bridgeOn(); switching = true; a.src = t.audio; playNow(); session();
    [P.queue[P.i - 1], t, P.queue[P.i + 1]].forEach(enrich); emit();
  }
  P.play = (list, i = 0) => { P.queue = (list || []).filter(t => t && t.audio); P.i = Math.max(0, Math.min(i, P.queue.length - 1)); load(); };
  P.go = (d) => { const j = P.i + d; if (j < 0 || j >= P.queue.length) return; P.i = j; load(); };
  const resume = () => { want = true; playNow(); };
  const pause = () => { want = false; switching = false; clearTimeout(retry.timer); retry.timer = null; bridgeOff(); a.pause(); };
  P.back = () => { if (a.currentTime > 3 || P.i <= 0) { a.currentTime = 0; resume(); } else P.go(-1); };
  P.toggle = () => { if (P.t) a.paused ? resume() : pause(); };
  P.stop = () => { pause(); a.removeAttribute("src"); a.load(); P.queue = []; P.i = -1; session(); emit(); };
  P.setVolume = (v) => { a.volume = v; try { localStorage.setItem("agentora.vol", v); } catch (e) {} };
  a.addEventListener("ended", () => { if (P.i < P.queue.length - 1) P.go(1); else { want = false; bridgeOff(); emit(); } });
  a.addEventListener("playing", () => { switching = false; bridgeOff(); retry.n = 0; session(); });
  a.addEventListener("waiting", () => { if (want) bridgeOn(); });      // buffering: keep the page "playing"
  a.addEventListener("timeupdate", () => { if (a.currentTime > 0) lastPos = a.currentTime; sessionPos(); });
  a.addEventListener("error", () => { if (want && a.getAttribute("src")) recover(); });
  a.addEventListener("stalled", () => { if (want && !a.paused) setTimeout(() => { if (want && a.readyState < 3 && !a.paused) recover(); }, 8000); });
  a.addEventListener("pause", () => {     // a pause from the phone (lock screen, a call, headphones out), not one from changing songs
    if (!a.ended && !switching && !retry.timer && a.getAttribute("src")) { want = false; bridgeOff(); }
    session();
  });
  for (const ev of ["play", "pause", "loadedmetadata"]) a.addEventListener(ev, emit);
  agentoraSolo.register(a, () => pause());
  // the phone refused to start the next song while the screen was off: try again when it's back on
  document.addEventListener("visibilitychange", () => { if (!document.hidden && P.blocked && P.t) resume(); });

  // lock screen, notification and Bluetooth controls (only once this player is used: the radio page has its own)
  const ms = "mediaSession" in navigator ? navigator.mediaSession : null;
  let msOn = false, msKey = "";
  function session() {
    if (!ms || (!msOn && !P.t)) return;
    if (!msOn) {
      msOn = true;
      const h = {play: () => resume(), pause: () => pause(), previoustrack: () => P.back(), nexttrack: () => P.go(1),
                 seekto: (d) => { if (a.duration && d.seekTime != null) a.currentTime = d.seekTime; }};
      for (const [k, f] of Object.entries(h)) try { ms.setActionHandler(k, f); } catch (e) {}
    }
    const t = P.t;
    ms.playbackState = !t ? "none" : a.paused ? "paused" : "playing";
    const key = t ? [t.id, t.cover].join("|") : "";
    if (key === msKey) return; msKey = key;
    if (!t) { ms.metadata = null; return; }
    const art = t.cover && !/^data:/.test(t.cover) ? [{src: new URL(t.cover, location.href).href, sizes: "768x768", type: "image/jpeg"}]
              : [{src: new URL("/icons/studio-180.png", location.href).href, sizes: "180x180", type: "image/png"}];
    try { ms.metadata = new MediaMetadata({title: t.title || "Untitled", artist: t.sub || t.credit || "Agentora", album: "Agentora", artwork: art}); } catch (e) {}
  }
  let posAt = 0;
  function sessionPos() {
    if (!ms || !msOn || !ms.setPositionState || !a.duration || !isFinite(a.duration) || Date.now() - posAt < 5000) return;
    posAt = Date.now();
    try { ms.setPositionState({duration: a.duration, position: Math.min(a.currentTime, a.duration), playbackRate: 1}); } catch (e) {}
  }
  P.on(() => { msKey = msKey && P.t ? msKey : ""; session(); });   // cover/credit arrive later (enrich): resend
  window.agentoraPlay = P.play;
  return P;
})();
if (AG_EMBED) try {                // the Create pane (same origin): ▶ plays in the radio page's bar, through its Library player
  const host = window.parent.agentoraHostPlayer;
  if (host) { window.agentoraPlayer = host; window.agentoraPlay = (list, i) => host.play(list, i); }
} catch (e) { /* Home's pane is another port: it has nothing to play */ }

(() => {
  const APPS = [["home", "🏠", "Home"], ["radio", "📻", "Radio"], ["music", "🎵", "Music"]];
  const THIS = document.documentElement.dataset.agentoraApp || "";
  const TAB = {home: "#home", radio: "#stations", music: "#create"};   // each app's tab on the radio page
  const url = (key, path = "") => `${location.protocol}//${location.hostname}:7860/radio${TAB[key]}${path ? "/" + path.replace(/^#/, "") : ""}`;

  // Never a new window: on the radio page switch the tab, from a pane ask the radio page to, anywhere else go there.
  function open(key, path) {
    if (!TAB[key]) return;
    if (AG_EMBED) { window.parent.postMessage({agentora: "open", key, path: path || ""}, "*"); return; }
    if (THIS === "radio") { location.hash = url(key, path).split("#")[1]; return; }
    location.href = url(key, path);
  }
  window.agentoraOpen = open;
  if (THIS === "radio" && !AG_EMBED) window.addEventListener("message", e => {
    const d = e.data;
    if (!d || d.agentora !== "open" || ![...document.querySelectorAll(".pane iframe")].some(f => f.contentWindow === e.source)) return;
    open(d.key, d.path);
  });

  // Assistant brain: <button data-agentora-brain> becomes a three-way slider. 🐹 Lemming = quick answers (always fits on the
  // GPU), ✨ Auto = the GPU broker picks AGI when it can have the card within a few seconds, else Lemming, 🧠 AGI = the
  // smartest local model (may wait for the card). Remembered per site; chats send agentoraBrain() ("lemming" | "auto" | "agi").
  const BRAINS = {lemming: ["🐹", "Lemming", "Quick answers (Qwen 3.5 9B), always on the GPU"],
                  auto: ["✨", "Auto", "The smartest model that can have the GPU right now"],
                  agi: ["🧠", "AGI", "The smartest local model (Qwen 3.6 27B); may wait a moment for the GPU"]};
  const ORDER = ["lemming", "auto", "agi"];
  const brain = () => { try { const v = localStorage.getItem("agentora.brain") || localStorage.getItem("b70.brain") /* its old name */; return BRAINS[v] ? v : "auto"; } catch (e) { return "auto"; } };
  window.agentoraBrain = brain;
  const drawBrain = () => document.querySelectorAll("[data-agentora-brain]").forEach(b => {
    const v = brain();
    b.classList.add("agentora-brain"); b.dataset.brain = v; b.type = "button"; b.title = `${BRAINS[v][1]}: ${BRAINS[v][2]}. Tap a name to switch.`;
    b.setAttribute("aria-label", `Assistant model: ${BRAINS[v][1]}. ${BRAINS[v][2]}. Press to switch.`);
    b.innerHTML = ORDER.map((k, i) => (i ? (i === 1 ? '<span class="track" aria-hidden="true"><span class="knob"></span></span>' : "") : "")
      + `<span class="opt${k === v ? " on" : ""}" data-v="${k}">${BRAINS[k][0]} ${BRAINS[k][1]}</span>`).join("");
  });
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-agentora-brain]"); if (!b) return;
    const opt = e.target.closest("[data-v]");
    const next = opt ? opt.dataset.v : ORDER[(ORDER.indexOf(brain()) + 1) % ORDER.length];   // a name picks it; the track cycles
    try { localStorage.setItem("agentora.brain", next); } catch (e) {}
    drawBrain();
  });
  drawBrain();

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  document.querySelectorAll("[data-agentora-apps]").forEach(el => {
    el.className = "agentora-apps"; el.setAttribute("role", "navigation"); el.setAttribute("aria-label", "Switch app");
    el.innerHTML = APPS.map(([k, ic, label], i) => `<a href="${esc(url(k))}" data-app="${k}" class="${k === THIS ? "on" : ""}" ${k === THIS ? 'aria-current="page"' : ""}
      title="${esc(label)} (Alt+${i})"><b aria-hidden="true">${ic}</b><span>${esc(label)}</span></a>`).join("");
  });
  document.addEventListener("click", e => {
    const a = e.target.closest("[data-app]"); if (!a || e.ctrlKey || e.metaKey || e.shiftKey) return;
    e.preventDefault(); open(a.dataset.app);
  });
  document.addEventListener("keydown", e => {
    if (e.altKey && !e.ctrlKey && !e.metaKey && /^[0-2]$/.test(e.key)) { e.preventDefault(); open(APPS[+e.key][0]); }
  });

  // ---- the now-playing bar on Home and the Music Studio: a song playing here (the shared player, agentoraPlay), else
  // the radio's song. Pull it up (or tap it) for the Now Playing view. The radio page has its own bar and player.
  if (THIS === "radio" || AG_EMBED) return;
  const onRadio = location.port === "7860", RADIO = `${location.protocol}//${location.hostname}:7860`, P = window.agentoraPlayer, I = window.agentoraIcons;
  const post = (body) => ({method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const api = onRadio ? {state: "/api/radio/state", act: (a, body) => fetch("/api/radio/" + a, post(body))}
                      : {state: "/api/nowplaying", act: (a, body) => fetch("/api/nowplaying/" + a, post(body))};
  const key = "agentora.np.closed." + THIS;
  let closedFor = null; try { closedFor = sessionStorage.getItem(key); } catch (e) {}
  const bar = document.createElement("div");
  bar.className = "agentora-np"; bar.hidden = true; bar.setAttribute("role", "region"); bar.setAttribute("aria-label", "Now playing");
  bar.innerHTML = `<img alt=""><div class="t" title="Now Playing: song info, previous and next song"><div class="lbl">Now Playing ⌃</div><b></b><span></span></div>
    <div class="bs"></div><i class="p"><i></i></i>`;
  document.body.appendChild(bar);
  let radio = null;                                 // the radio as heard by its listeners: now, prev, next, station

  function radioFrom(d, at) {
    const sv = d.stream || {}, live = sv.clock != null, base = live ? sv.clock : (sv.pos || 0);
    const heard = () => base + (live ? (performance.now() - at) / 1000 : 0);
    const aired = sv.aired || [], h = heard();
    let k = -1; aired.forEach((a, i) => { if (a.start_pos != null && a.start_pos <= h + .05) k = i; });
    const now = k >= 0 && aired[k].id ? aired[k] : null;
    let prev = null, next = null;
    if (now) {
      for (let i = k - 1; i >= 0 && !prev; i--) if (aired[i].id) prev = aired[i];
      for (let i = k + 1; i < aired.length && !next; i++) if (aired[i].id) next = aired[i];
    }
    if (!next && d.next && (!now || d.next.id !== now.id)) next = d.next;
    const st = d.station && typeof d.station === "object" ? d.station : d.station === "__reruns__" ? {id: d.station, name: "Reruns"}
      : (d.stations || []).find(s => s.id === d.station);
    return {on: d.on, now, prev, next, station: st && {id: st.id, name: st.name, art: st.art}, heard};
  }
  const cover = (c) => c ? (/^(https?:|data:)/.test(c) ? c : RADIO + c) : null;
  const stArt = (st) => st && st.art ? `${RADIO}/api/radio/stations/${encodeURIComponent(st.id)}/art?v=${st.art}` : null;
  const songUrl = (id) => url("radio", "#song/" + encodeURIComponent(id));
  const len = (t) => t && (t.duration || t.seconds);
  const chips = (t) => [len(t) ? agTime(len(t)) : null, t.mode === "instrumental" || (t.lyrics != null && !t.lyrics) ? "Instrumental" : t.lyrics ? "Vocals" : null,
                        t.feedback === "keep" ? "👍 Liked" : null, t.in_playlist ? "♥ In my playlist" : null].filter(Boolean);
  const elapsed = () => radio && radio.now ? Math.max(0, Math.min(radio.heard() - radio.now.start_pos, len(radio.now) || 1e9)) : 0;
  const view = (t, lib) => t && {key: t.id + "@" + (t.start_pos ?? ""), title: t.title, cover: lib ? t.cover || agentoraSongArt(t.id, 0) : cover(t.cover) || agentoraSongArt(t.id, 0), sub: lib ? t.sub || "" : t.station_name || ""};

  const local = {                                   // a song playing here
    info() {
      const t = P.t;
      return {key: `l:${t.id}:${P.i}`, title: t.title, cover: t.cover || agentoraSongArt(t.id, 0), sub: [...new Set([t.sub || (THIS === "music" ? "🎵 Music Studio" : ""), t.credit].filter(Boolean))].join(" · "),   // a cover's credit can be its sub too
              kicker: `Playing here · ${THIS === "music" ? "Music Studio" : "Home"}`, more: songUrl(t.id), style: t.style || "", chips: chips(t),
              lyrics: t.lyrics != null ? agentoraLyrics(t) : null, status: ""};
    },
    around: () => ({prev: view(P.queue[P.i - 1], true), next: view(P.queue[P.i + 1], true), notes: {next: "End of the list"}}),
    go: (d) => P.go(d === "prev" ? -1 : 1),
    controls() {
      const t = P.t;
      return [{key: "down", html: I.down, title: "Thumbs down — fewer like this (skips to the next song)", label: "Thumbs down"},
              {key: "back", html: I.prev, title: "Back: restart · press again for the previous song", label: "Back"},
              {key: "toggle", html: P.audio.paused ? I.play : I.pause, play: true, title: P.audio.paused ? "Play" : "Pause", label: P.audio.paused ? "Play" : "Pause"},
              {key: "next", html: I.next, title: "Next song in the list", label: "Next", disabled: P.i >= P.queue.length - 1},
              {key: "up", html: I.up, on: t.feedback === "keep", pressed: t.feedback === "keep", title: "Thumbs up — make more songs like this", label: "Thumbs up"},
              {key: "add", html: t.in_playlist ? I.added : I.add, on: !!t.in_playlist, pressed: !!t.in_playlist, title: t.in_playlist ? "In your playlist — press to remove" : "Add to my playlist", label: "Add to my playlist"}];
    },
    control(k) {                                  // 👍 👎 ⊕ teach the radio's taste and fill the playlist, as on the radio
      const t = P.t;
      if (k === "down") { t.feedback = "dislike"; act("feedback", {track: t.id, kind: "dislike"}); return P.i < P.queue.length - 1 ? P.go(1) : sheet.render(); }
      if (k === "up") { const on = t.feedback === "keep"; t.feedback = on ? null : "keep"; sheet.render(); return act("feedback", {track: t.id, kind: on ? "clear" : "keep"}); }
      if (k === "add") { t.in_playlist = !t.in_playlist; sheet.render(); return act("playlist", {track: t.id, on: t.in_playlist}); }
      return k === "back" ? P.back() : k === "toggle" ? P.toggle() : P.go(1);
    },
    progress() {
      const a = P.audio, dur = a.duration || P.t.seconds || 0;
      return {el: agTime(a.currentTime), dur: dur ? agTime(dur) : "—", frac: dur ? a.currentTime / dur : 0,
              seek: a.duration ? (f) => { a.currentTime = f * a.duration; } : null, hint: "Drag or tap to jump"};
    },
    volume: {get: () => P.audio.volume, set: (v) => P.setVolume(v)},
  };
  async function act(name, body) { try { await api.act(name, body); } catch (e) {} setTimeout(poll, 700); setTimeout(poll, 2000); }
  const onAir = {                                   // the radio's broadcast
    info() {
      const t = radio.now, st = radio.station;
      return {key: `r:${t.id}@${t.start_pos}`, title: t.title, cover: cover(t.cover) || agentoraSongArt(t.id, 0),
              sub: ["📻 " + (t.station_name || (st && st.name) || "Radio"), t.credit].filter(Boolean).join(" · "),
              kicker: "Live radio · everyone hears this", kickerHref: url("radio", "#stations"), more: songUrl(t.id), style: t.style || "", chips: chips(t),
              lyrics: t.lyrics != null ? agentoraLyrics(t) : null, status: ""};
    },
    around: () => ({prev: view(radio.prev), next: view(radio.next), live: true, notes: {next: "A new song is on the way"}}),
    go: (d) => d === "next" ? act("skip", {track: radio.now.id, kind: "skip"}) : act("back", {steps: 1}),
    controls() {
      const t = radio.now;
      return [{key: "down", html: I.down, title: "Thumbs down — fewer like this (skips it)", label: "Thumbs down"},
              {key: "back", html: I.prev, title: "Back: restart this song · near the start, the previous song (for everyone listening)", label: "Back"},
              {key: "listen", html: I.radio, play: true, title: "Listen: open the radio", label: "Open the radio to listen"},
              {key: "skip", html: I.next, title: "Skip this song (for everyone listening)", label: "Skip"},
              {key: "up", html: I.up, on: t.feedback === "keep", pressed: t.feedback === "keep", title: "Thumbs up — make more songs like this", label: "Thumbs up"},
              {key: "add", html: t.in_playlist ? I.added : I.add, on: !!t.in_playlist, pressed: !!t.in_playlist, title: t.in_playlist ? "In your playlist — press to remove" : "Add to my playlist", label: "Add to my playlist"}];
    },
    control(k) {
      const t = radio.now;
      if (k === "listen") return open("radio");
      if (k === "down") return act("skip", {track: t.id, kind: "dislike"});
      if (k === "skip") return act("skip", {track: t.id, kind: "skip"});
      if (k === "back") return act("back", elapsed() > 6 ? {track: t.id, steps: 0} : {steps: 1});
      if (k === "up") { const on = t.feedback === "keep"; t.feedback = on ? null : "keep"; sheet.render(); return act("feedback", {track: t.id, kind: on ? "clear" : "keep"}); }
      if (k === "add") { t.in_playlist = !t.in_playlist; sheet.render(); return act("playlist", {track: t.id, on: t.in_playlist}); }
    },
    progress() {
      const d = len(radio.now);
      return {el: agTime(elapsed()), dur: d ? agTime(d) : "—", frac: d ? elapsed() / d : 0, hint: "Live: everyone hears the same moment, so there's no seeking"};
    },
    volume: null,
  };
  const idle = {info: () => ({key: "none", title: "Nothing playing", kicker: "", status: ""}), around: () => ({}), go() {}, controls: () => [], control() {}, progress: () => ({}), volume: null};
  const mode = () => P.t ? local : radio && radio.on && radio.now ? onAir : idle;
  const sheet = agentoraSheet(bar, {
    info: () => mode().info(), around: () => mode().around(), go: (d) => mode().go(d), controls: () => mode().controls(),
    control: (k) => mode().control(k), progress: () => mode().progress(), get volume() { return mode().volume; },
  });
  window.agentoraNowPlaying = sheet;

  let barSig = "";
  function draw() {
    const t = P.t, now = radio && radio.on && radio.now, show = !!t || (!!now && closedFor !== now.id);
    bar.hidden = !show; document.body.classList.toggle("agentora-np-shown", show);
    if (!show) { if (sheet.open && !t && !now) sheet.hide(); return; }
    const img = t ? t.cover || agentoraSongArt(t.id, 0) : cover(now.cover) || stArt(radio.station) || agentoraSongArt(now.id, 0);
    if (bar.querySelector("img").getAttribute("src") !== (img || "")) bar.querySelector("img").setAttribute("src", img || "");
    bar.querySelector("b").textContent = t ? t.title : now.title;
    bar.querySelector(".t span").textContent = t ? (t.sub || "Playing here") : "On the radio" + (radio.station ? " · " + radio.station.name : "");
    const bs = t ? `<button data-np="back" title="Back" aria-label="Back">${I.prev}</button>
        <button data-np="toggle" title="${P.audio.paused ? "Play" : "Pause"}" aria-label="${P.audio.paused ? "Play" : "Pause"}">${P.audio.paused ? I.play : I.pause}</button>
        <button data-np="next" title="Next song" aria-label="Next song">${I.next}</button>
        <button class="x" data-np="stop" title="Stop" aria-label="Stop">✕</button>`
      : `<button data-np="like" class="${now.feedback === "keep" ? "on" : ""}" title="Thumbs up: more like this" aria-label="Thumbs up">👍</button>
        <button data-np="skip" title="Skip this song (for everyone listening)" aria-label="Skip">⏭</button>
        <button data-np="open" title="Go to the radio" aria-label="Go to the radio">📻</button>
        <button class="x" data-np="close" title="Hide until the next song" aria-label="Hide">✕</button>`;
    if (bs !== barSig) { barSig = bs; bar.querySelector(".bs").innerHTML = bs; }
    progress(); sheet.render();
  }
  function progress() {
    if (bar.hidden) return;
    const p = mode().progress();
    bar.querySelector(".p i").style.width = Math.max(0, Math.min(100, (p.frac || 0) * 100)) + "%";
    sheet.tick();
  }
  async function poll() {
    if (document.hidden) return;
    try { const at = performance.now(), r = await fetch(api.state, {cache: "no-store"}); if (!r.ok) throw 0; radio = radioFrom(await r.json(), at); }
    catch (e) { radio = null; }
    draw();
  }
  bar.addEventListener("click", async e => {
    const b = e.target.closest("[data-np]"); if (!b) return;
    const k = b.dataset.np;
    if (k === "back") return P.back();
    if (k === "toggle") return P.toggle();
    if (k === "next") return P.go(1);
    if (k === "stop") return P.stop();
    const now = radio && radio.now; if (!now) return;
    if (k === "open") return open("radio");
    if (k === "close") { closedFor = now.id; try { sessionStorage.setItem(key, now.id); } catch (e) {} return draw(); }
    if (k === "like") { const on = now.feedback === "keep"; now.feedback = on ? null : "keep"; draw(); return act("feedback", {track: now.id, kind: on ? "clear" : "keep"}); }
    if (k === "skip") return act("skip", {track: now.id, kind: "skip"});
  });
  P.on(draw);
  poll(); setInterval(poll, 5000); setInterval(progress, 500);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
})();

// ---- 👤 Account, from any app: Home (:7871) holds the accounts, so every page asks it who's signed in and signs
// out there (one account across the radio, Music Studio, Home and the Hermes dashboard). A page opts in with <div data-agentora-account>.
window.agentoraAccount = (() => {
  const home = `${location.protocol}//${location.hostname}:7871`, here = location.port === "7871";
  const url = (p) => here ? p : home + p;
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  let last = "";
  const who = () => fetch(url("/api/session"), {credentials: "include", cache: "no-store"}).then(r => r.ok ? r.json() : null).then(d => d && d.user).catch(() => undefined);
  const html = (u) => u === undefined ? `<p class="agentora-muted">Couldn't reach Agentora Home, which holds the accounts.</p>`
    : u ? `<p>👤 <b>${esc(u.name)}</b>${u.role === "admin" ? ' <span class="agentora-muted">· admin</span>' : ""}</p>
        <div class="agentora-links"><button type="button" class="agentora-btn" data-agentora-signout>Sign out</button></div>`
    : `<p class="agentora-muted">Not signed in.</p><div class="agentora-links"><a class="agentora-btn" href="${home}/home">Sign in</a></div>`;
  async function fill(el) { if (last) el.innerHTML = last; last = html(await who()); el.innerHTML = last; }
  const fillAll = () => document.querySelectorAll("[data-agentora-account]").forEach(fill);
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-agentora-signout]"); if (!b) return;
    b.disabled = true; b.textContent = "Signing out…";
    await fetch(url("/api/logout"), {method: "POST", credentials: "include"}).catch(() => {});
    if (here) return location.reload();          // a clean page: nothing of this account stays on screen
    last = ""; fillAll();                          // elsewhere (the radio) keep playing, just show it signed out
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fillAll); else fillAll();
  return {who, fill, fillAll};
})();

// ---- ☰ control panel for apps without their own (Home, Music Studio): radio, GPU, disk, apps.
// A page opts in with <button data-agentora-panel></button> where the ☰ goes. Works on both servers: the radio's (:7860)
// and Home's (:7871) expose the same facts under slightly different paths.
(() => {
  const btn = document.querySelector("[data-agentora-panel]");
  if (!btn || AG_EMBED) { if (btn) btn.hidden = true; return; }
  const onRadio = location.port === "7860";
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const get = (p) => fetch(p, {cache: "no-store"}).then(r => r.ok ? r.json() : Promise.reject(new Error(r.status)));
  const app = (port, path) => `${location.protocol}//${location.hostname}:${port}${path}`;
  const gb = (b) => (b / 2 ** 30).toFixed(1);

  async function facts() {
    if (onRadio) {
      const [st, g, p, d] = await Promise.all([get("/api/radio/state"), get("/api/radio/gpu").catch(() => ({})), get("/api/gpu/priority"), get("/api/disk")]);
      const s = (st.stations || []).find(x => x.id === st.station), sv = st.stream || {};
      return {radio: {on: st.on, station: s && s.name, now: sv.now && sv.now.title, listeners: sv.listeners || 0,
                      making: (st.making || []).length, gpu: (st.gpu || {}).detail || (st.gpu || {}).owner},
              vram: g.total_mb ? [g.used_mb, g.total_mb, g.name] : null, waiting: p.waiting, disk: d};
    }
    const signedIn = await get("/api/session").then(x => !!x.user).catch(() => false);   // disk details need a sign-in
    const [h, d] = await Promise.all([get("/api/home"), signedIn ? get("/api/disk").catch(() => null) : null]);
    const r = h.radio, sy = h.system || {};
    return {radio: r && {on: r.on, station: r.station && r.station.name, now: r.now && r.now.title, listeners: r.listeners || 0,
                         making: r.making || 0, gpu: r.detail || r.gpu},
            vram: sy.vram_total ? [sy.vram_used, sy.vram_total, "Graphics card"] : null,
            disk: d || (sy.disk_total ? {total: sy.disk_total, free: sy.disk_free, used: sy.disk_total - sy.disk_free} : null)};
  }

  btn.className = "agentora-panelbtn"; btn.type = "button"; btn.innerHTML = "☰"; btn.title = "Control panel: radio, GPU, disk and apps";
  btn.setAttribute("aria-label", "Open the control panel"); btn.setAttribute("aria-expanded", "false");
  const fade = document.createElement("div"); fade.className = "agentora-fade"; fade.hidden = true;
  const side = document.createElement("aside"); side.className = "agentora-side"; side.hidden = true; side.setAttribute("aria-label", "Control panel");
  document.body.append(fade, side);
  let timer = 0;
  const open = (on) => {
    side.hidden = fade.hidden = !on; btn.setAttribute("aria-expanded", on);
    clearInterval(timer); if (on) { render(); timer = setInterval(render, 5000); side.querySelector("button")?.focus(); }
  };
  btn.onclick = () => open(side.hidden);
  fade.onclick = () => open(false);
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !side.hidden) open(false); });

  async function render() {
    let f, msg = "";
    try { f = await facts(); } catch (e) { msg = "Couldn't reach the server."; }
    const card = (title, body) => `<section class="agentora-card"><h3>${title}</h3>${body}</section>`;
    const bar = (used, total, warn) => `<div class="agentora-meter"><i style="width:${Math.min(100, 100 * used / total).toFixed(1)}%"${warn ? ' class="warn"' : ""}></i></div>`;
    const r = f && f.radio;
    side.innerHTML = `<div class="agentora-sidehead"><b>Control panel</b><button type="button" class="agentora-x" aria-label="Close the control panel" title="Close">✕</button></div>
      ${msg ? `<p class="agentora-muted">${msg}</p>` : ""}
      ${card("👤 Account", `<div data-agentora-account></div>`)}
      ${f ? card("📻 Radio", r ? `<p>${r.on ? `<b>${esc(r.now || "Next song on the way…")}</b><br><span class="agentora-muted">${esc(r.station || "")} · ${r.listeners} listening</span>`
              : `<span class="agentora-muted">The radio is off.</span>`}</p>
            <p class="agentora-muted">${r.making ? `${r.making} song${r.making === 1 ? "" : "s"} being made` : "Nothing being made"}${r.gpu && r.gpu !== f.waiting ? ` · ${esc(r.gpu)}` : ""}</p>
            <a class="agentora-btn" href="${app(7860, "/radio")}" data-app="radio">Open the radio</a>` : `<p class="agentora-muted">The radio isn't answering.</p>`) : ""}
      ${f ? card("GPU", `${f.vram ? `${bar(f.vram[0], f.vram[1], f.vram[0] / f.vram[1] > 0.92)}<p class="agentora-muted">${(f.vram[0] / 1024).toFixed(1)} of ${Math.round(f.vram[1] / 1024)} GB in use · ${esc(f.vram[2])}</p>` : ""}
            ${f.waiting ? `<p class="agentora-muted">${esc(f.waiting)}</p>` : ""}`) : ""}
      ${f && f.disk ? card("Disk", `${bar(f.disk.used, f.disk.total, f.disk.free / f.disk.total < 0.1)}<p class="agentora-muted">${gb(f.disk.free)} GB free of ${Math.round(f.disk.total / 2 ** 30)} GB</p>`) : ""}
      ${card("Apps", `<div class="agentora-links">
          <a class="agentora-btn" href="${app(7871, "/home")}" data-app="home">🏠 Home</a><a class="agentora-btn" href="${app(7860, "/radio")}" data-app="radio">📻 Radio</a>
          <a class="agentora-btn" href="${app(7860, "/")}" data-app="music">🎵 Music</a>
          <a class="agentora-btn" href="${app(7880, "/")}" target="_blank" rel="noopener">☤ Hermes dashboard</a></div>`)}`;
    side.querySelector(".agentora-x").onclick = () => open(false);
    side.querySelectorAll("[data-agentora-account]").forEach(window.agentoraAccount.fill);
  }
  side.addEventListener("click", async (e) => {
    const a = e.target.closest("a[data-app]");
    if (a && window.agentoraOpen) { e.preventDefault(); open(false); return window.agentoraOpen(a.dataset.app); }
  });
})();
