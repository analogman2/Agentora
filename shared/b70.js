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
    const [icon, name, tip] = BRAINS[brain()];
    b.classList.add("b70-brain"); b.dataset.brain = brain(); b.type = "button"; b.title = tip;
    b.setAttribute("aria-label", "Assistant model: " + name + ". " + tip);
    b.innerHTML = `<span aria-hidden="true">${icon}</span> ${name}`;
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
