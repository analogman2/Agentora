// b70 shared script: the app switcher and the radio's now-playing bar, for every app. Served at /b70/b70.js by both servers.
// A page says which app it is with <html data-b70-app="home|radio|studio|music"> and puts <div data-b70-apps></div> where
// the switcher goes. Each app keeps one browser tab; switching jumps to that tab without reloading it (the radio keeps playing).
(() => {
  const APPS = [["home", "🏠", "Home", ":7870/home"], ["radio", "📻", "Radio", ":7860/radio"],
                ["studio", "🖼", "Image & Video", ":7870/"], ["music", "🎵", "Music", ":7860/"]];
  const THIS = document.documentElement.dataset.b70App || "";
  const url = (key, path = "") => `${location.protocol}//${location.hostname}${APPS.find(a => a[0] === key)[3]}${path}`;
  if (!window.name || window.name.startsWith("b70-")) window.name = "b70-" + THIS;

  function open(key, path) {
    if (key === THIS && !path) return;
    if (key === THIS) { location.href = url(key, path); return; }
    const w = window.open("", "b70-" + key);
    if (!w) { location.href = url(key, path); return; }          // pop-ups blocked: go there in this tab
    let blank = false;
    try { blank = !w.location.href || w.location.href === "about:blank"; } catch (e) { /* another app's page: already open */ }
    if (blank || path) w.location.href = url(key, path);
    w.focus();
  }
  window.b70Open = open;

  // Assistant brain: <button data-b70-brain> becomes a two-way toggle. 🐹 Lemming = quick answers, 🧠 AGI = the smartest
  // local model (slower). Remembered per site; chats send b70Brain() as the model ("lemming" | "agi").
  const BRAINS = {lemming: ["🐹", "Lemming", "Quick answers (Qwen 3.6 35B-A3B). Click for AGI."],
                  agi: ["🧠", "AGI", "Smartest local model, slower (Qwen 3.6 27B). Click for Lemming."]};
  const brain = () => { try { const v = localStorage.getItem("b70.brain"); return BRAINS[v] ? v : "agi"; } catch (e) { return "agi"; } };
  window.b70Brain = brain;
  const drawBrain = () => document.querySelectorAll("[data-b70-brain]").forEach(b => {
    const on = brain() === "agi";
    b.classList.add("b70-brain"); b.dataset.brain = brain(); b.type = "button"; b.title = BRAINS[brain()][2];
    b.setAttribute("role", "switch"); b.setAttribute("aria-checked", on);
    b.setAttribute("aria-label", "Assistant model: " + BRAINS[brain()][1] + " (switch on for AGI)");
    b.innerHTML = `<span class="l">🐹 Lemming</span><span class="track" aria-hidden="true"><span class="knob"></span></span><span class="r">🧠 AGI</span>`;
  });
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-b70-brain]"); if (!b) return;
    try { localStorage.setItem("b70.brain", brain() === "agi" ? "lemming" : "agi"); } catch (e) {}
    drawBrain();
  });
  drawBrain();

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  document.querySelectorAll("[data-b70-apps]").forEach(el => {
    el.className = "b70-apps"; el.setAttribute("role", "navigation"); el.setAttribute("aria-label", "Switch app");
    el.innerHTML = APPS.map(([k, ic, label], i) => `<a href="${esc(url(k))}" data-app="${k}" class="${k === THIS ? "on" : ""}" ${k === THIS ? 'aria-current="page"' : ""}
      title="${esc(label)} (Alt+${i})"><b aria-hidden="true">${ic}</b><span>${esc(label)}</span></a>`).join("");
  });
  document.addEventListener("click", e => {
    const a = e.target.closest("[data-app]"); if (!a || e.ctrlKey || e.metaKey || e.shiftKey) return;
    e.preventDefault(); open(a.dataset.app);
  });
  document.addEventListener("keydown", e => {
    if (e.altKey && !e.ctrlKey && !e.metaKey && /^[0-3]$/.test(e.key)) { e.preventDefault(); open(APPS[+e.key][0]); }
  });

  // ---- now playing: the radio's current song, on every page except the radio's own (it has its player)
  if (THIS === "radio") return;
  const onRadio = location.port === "7860";
  const api = onRadio ? {state: "/api/radio/state", act: (a, body) => fetch("/api/radio/" + a, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})}
                      : {state: "/api/nowplaying", act: (a, body) => fetch("/api/nowplaying/" + a, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})};
  const key = "b70.np.closed." + THIS;
  let closedFor = null; try { closedFor = sessionStorage.getItem(key); } catch (e) {}
  const bar = document.createElement("div");
  bar.className = "b70-np"; bar.hidden = true; bar.setAttribute("role", "region"); bar.setAttribute("aria-label", "Now playing on the radio");
  bar.innerHTML = `<img alt=""><div class="t" title="Open the radio"><b></b><span></span></div>
    <button data-np="like" title="Thumbs up: more like this" aria-label="Thumbs up">👍</button>
    <button data-np="skip" title="Skip this song (for everyone listening)" aria-label="Skip">⏭</button>
    <button data-np="open" title="Go to the radio" aria-label="Go to the radio">📻</button>
    <button class="x" data-np="close" title="Hide until the next song" aria-label="Hide">✕</button>`;
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(bar));
  if (document.body) document.body.appendChild(bar);
  let cur = null;

  function nowFrom(d) {                                            // radio state -> {id, title, station, cover, art}
    if (d.now !== undefined) return d;                              // already shaped by the Studio's /api/nowplaying
    const sv = d.stream || {}, aired = (sv.aired || []).filter(a => a.start_pos != null && a.start_pos <= (sv.pos || 0));
    const a = aired[aired.length - 1], st = (d.stations || []).find(s => s.id === d.station);
    return {on: d.on, now: a && a.id ? {id: a.id, title: a.title, cover: a.cover, feedback: a.feedback} : null,
            station: st ? {id: st.id, name: st.name, art: st.art} : null};
  }
  async function poll() {
    if (document.hidden) return;
    let d;
    try { const r = await fetch(api.state, {cache: "no-store"}); if (!r.ok) throw 0; d = nowFrom(await r.json()); } catch (e) { bar.hidden = true; return; }
    cur = d.now;
    const show = d.on && d.now && closedFor !== d.now.id;
    bar.hidden = !show; document.body.classList.toggle("b70-np-shown", !!show);
    if (!show) return;
    const radio = `${location.protocol}//${location.hostname}:7860`;
    bar.querySelector("img").src = d.now.cover ? radio + d.now.cover : d.station && d.station.art ? `${radio}/api/radio/stations/${encodeURIComponent(d.station.id)}/art?v=${d.station.art}` : "";
    bar.querySelector("b").textContent = d.now.title;
    bar.querySelector(".t span").textContent = "On the radio" + (d.station ? " · " + d.station.name : "");
    bar.querySelector('[data-np="like"]').classList.toggle("on", d.now.feedback === "keep");
  }
  bar.addEventListener("click", async e => {
    const b = e.target.closest("[data-np]"), t = e.target.closest(".t");
    if (t) return open("radio", cur ? "#song/" + encodeURIComponent(cur.id) : "");
    if (!b || !cur) return;
    if (b.dataset.np === "open") return open("radio");
    if (b.dataset.np === "close") { closedFor = cur.id; try { sessionStorage.setItem(key, cur.id); } catch (e) {} bar.hidden = true; document.body.classList.remove("b70-np-shown"); return; }
    if (b.dataset.np === "like") { await api.act("feedback", {track: cur.id, kind: b.classList.contains("on") ? "clear" : "keep"}).catch(() => {}); }
    if (b.dataset.np === "skip") { await api.act("skip", {track: cur.id, kind: "skip"}).catch(() => {}); }
    setTimeout(poll, 600);
  });
  poll(); setInterval(poll, 10000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
})();

// ---- ☰ control panel for apps without their own (Home, Music Studio): radio, GPU and priority, disk, apps.
// A page opts in with <button data-b70-panel></button> where the ☰ goes. Works on both servers: the radio's (:7860)
// and the Studio's (:7870) expose the same facts under slightly different paths.
(() => {
  const btn = document.querySelector("[data-b70-panel]");
  if (!btn) return;
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
              vram: g.total_mb ? [g.used_mb, g.total_mb, g.name] : null, priority: p.priority, choices: p.choices, waiting: p.waiting, disk: d};
    }
    const signedIn = await get("/api/session").then(x => !!x.user).catch(() => false);   // priority and disk details need a sign-in
    const [h, p, d] = await Promise.all([get("/api/home"), signedIn ? get("/api/gpu-priority").catch(() => ({})) : {},
                                         signedIn ? get("/api/disk").catch(() => null) : null]);
    const r = h.radio, sy = h.system || {};
    return {radio: r && {on: r.on, station: r.station && r.station.name, now: r.now && r.now.title, listeners: r.listeners || 0,
                         making: r.making || 0, gpu: r.detail || r.gpu},
            vram: sy.vram_total ? [sy.vram_used, sy.vram_total, "Graphics card"] : null, priority: p.priority, choices: p.choices,
            disk: d || (sy.disk_total ? {total: sy.disk_total, free: sy.disk_free, used: sy.disk_total - sy.disk_free} : null)};
  }
  const setPriority = (v) => fetch(onRadio ? "/api/gpu/priority" : "/api/gpu-priority", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({priority: v})});

  btn.className = "b70-panelbtn"; btn.type = "button"; btn.innerHTML = "☰"; btn.title = "Control panel: radio, GPU, disk and apps";
  btn.setAttribute("aria-label", "Open the control panel"); btn.setAttribute("aria-expanded", "false");
  const fade = document.createElement("div"); fade.className = "b70-fade"; fade.hidden = true;
  const side = document.createElement("aside"); side.className = "b70-side"; side.hidden = true; side.setAttribute("aria-label", "Control panel");
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
    const card = (title, body) => `<section class="b70-card"><h3>${title}</h3>${body}</section>`;
    const bar = (used, total, warn) => `<div class="b70-meter"><i style="width:${Math.min(100, 100 * used / total).toFixed(1)}%"${warn ? ' class="warn"' : ""}></i></div>`;
    const r = f && f.radio;
    side.innerHTML = `<div class="b70-sidehead"><b>Control panel</b><button type="button" class="b70-x" aria-label="Close the control panel" title="Close">✕</button></div>
      ${msg ? `<p class="b70-muted">${msg}</p>` : ""}
      ${f ? card("📻 Radio", r ? `<p>${r.on ? `<b>${esc(r.now || "Next song on the way…")}</b><br><span class="b70-muted">${esc(r.station || "")} · ${r.listeners} listening</span>`
              : `<span class="b70-muted">The radio is off.</span>`}</p>
            <p class="b70-muted">${r.making ? `${r.making} song${r.making === 1 ? "" : "s"} being made` : "Nothing being made"}${r.gpu && r.gpu !== f.waiting ? ` · ${esc(r.gpu)}` : ""}</p>
            <a class="b70-btn" href="${app(7860, "/radio")}" data-app="radio">Open the radio</a>` : `<p class="b70-muted">The radio isn't answering.</p>`) : ""}
      ${f ? card("GPU", `${f.vram ? `${bar(f.vram[0], f.vram[1], f.vram[0] / f.vram[1] > 0.92)}<p class="b70-muted">${(f.vram[0] / 1024).toFixed(1)} of ${Math.round(f.vram[1] / 1024)} GB in use · ${esc(f.vram[2])}</p>` : ""}
            ${f.choices ? `<p class="b70-label">Who goes first</p>
            <div class="b70-prio" role="group" aria-label="GPU priority">${Object.entries(f.choices || {}).map(([k, l]) =>
              `<button type="button" data-prio="${k}" aria-pressed="${k === f.priority}">${esc(l)}</button>`).join("")}</div>` : ""}
            ${f.waiting ? `<p class="b70-muted">${esc(f.waiting)}</p>` : ""}<p class="b70-muted" data-prio-msg></p>`) : ""}
      ${f && f.disk ? card("Disk", `${bar(f.disk.used, f.disk.total, f.disk.free / f.disk.total < 0.1)}<p class="b70-muted">${gb(f.disk.free)} GB free of ${Math.round(f.disk.total / 2 ** 30)} GB</p>`) : ""}
      ${card("Apps", `<div class="b70-links">
          <a class="b70-btn" href="${app(7870, "/home")}" data-app="home">🏠 Home</a><a class="b70-btn" href="${app(7860, "/radio")}" data-app="radio">📻 Radio</a>
          <a class="b70-btn" href="${app(7870, "/")}" data-app="studio">🖼 Image &amp; Video</a><a class="b70-btn" href="${app(7860, "/")}" data-app="music">🎵 Music</a>
          <a class="b70-btn" href="${app(7880, "/")}" target="_blank" rel="noopener">☤ Hermes dashboard</a></div>`)}`;
    side.querySelector(".b70-x").onclick = () => open(false);
  }
  side.addEventListener("click", async (e) => {
    const a = e.target.closest("a[data-app]");
    if (a && window.b70Open) { e.preventDefault(); open(false); return window.b70Open(a.dataset.app); }
    const p = e.target.closest("[data-prio]"); if (!p) return;
    const r = await setPriority(p.dataset.prio).catch(() => null);
    if (!r || !r.ok) { const m = side.querySelector("[data-prio-msg]"); if (m) m.textContent = r && r.status === 403 ? "Only an admin can change this." : "Couldn't change it."; return; }
    render();
  });
})();
