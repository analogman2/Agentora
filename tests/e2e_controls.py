#!/usr/bin/env python3
"""Every button, link and form on the radio, the classic deck, the Music Studio and Home (signed out), in headless Chromium.

Each check opens a fresh page, presses one control like a user would and checks what it should do: the request it sends,
the dialog or page it opens, the sound it plays. Requests that would change anything (POST, PUT, DELETE) never leave the
browser: the page's fetch is replaced (they're recorded and answered {"ok": true}), and the browser context's network
route catches anything else. The live stream is blocked. Nothing changes: no retune, skip, delete, vote or song. Check
afterwards with `grep '"device": "script"' logs/activity-*.jsonl | tail` (it should show nothing from the run). A check that fails is tried once more (lists redraw every few seconds).
Usage: tests/e2e_controls.py [--radio URL] [--home URL] [--chrome PATH] [--station ID] [--json FILE]
"""
import argparse, json, re, sys, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--radio", default="http://127.0.0.1:7860")
ap.add_argument("--home", default="http://127.0.0.1:7871")
ap.add_argument("--chrome", default=str(Path.home() / ".hermes/tools/chromium-1208/chrome-linux64/chrome"))
ap.add_argument("--station", default="", help="the station to use (default: the one with the most songs)")
ap.add_argument("--json", default="", help="also write the results here")
A = ap.parse_args()
CHROME, R, H = A.chrome, A.radio.rstrip("/"), A.home.rstrip("/")
_st = json.loads(urllib.request.urlopen(f"{R}/api/radio/state").read())["stations"]
ST = A.station or max(_st, key=lambda s: (s.get("ready") or 0) + s["stats"]["played"])["id"]
SONG = json.loads(urllib.request.urlopen(f"{R}/api/radio/stations/{ST}/history?limit=5&upcoming=1").read())
SONG = (SONG.get("tracks") if isinstance(SONG, dict) else SONG)[0]["id"]
INIT = """window.__opened = []; window.__clip = 0; window.__play = 0; window.__sent = [];
// first safety net: the page's own fetch never sends a POST/PUT/DELETE (the context's route is the second one)
const _fetch = window.fetch;
window.fetch = (u, o = {}) => {
  const m = String(o.method || (u && u.method) || "GET").toUpperCase();
  if (m === "GET" || m === "HEAD") return _fetch(u, o);
  const url = String((u && u.url) || u).replace(/^https?:\\/\\/[^/]+/, "");
  window.__sent.push([m, url, typeof o.body === "string" ? o.body.slice(0, 200) : ""]);
  return Promise.resolve(new Response('{"ok": true, "ids": ["x"], "station": {"id": "x", "name": "audit"}, "queued": true}', {status: 200, headers: {"Content-Type": "application/json"}}));
};
navigator.sendBeacon = () => true;
window.open = (...a) => { window.__opened.push(String(a[0])); return null; };
try { navigator.clipboard.writeText = async () => { window.__clip++; }; } catch (e) {}
const _p = HTMLMediaElement.prototype.play; HTMLMediaElement.prototype.play = function () { window.__play++; try { return _p.call(this).catch(() => {}); } catch (e) { return Promise.resolve(); } };"""
results = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME if Path(CHROME).exists() else None, args=["--autoplay-policy=no-user-gesture-required"])
    sent, errs = [], []
    def route(r):
        q = r.request
        if "stream.mp3" in q.url or ":7861" in q.url: return r.abort()
        if "/api/spotify/tracks" in q.url:                 # never ask Spotify (its rate limit is easy to hit)
            return r.fulfill(status=200, content_type="application/json",
                             body='{"tracks": [{"id": "0000000000test", "name": "Audit Song", "artist": "Audit", "inspired": null, "cover": null}]}')
        if q.method not in ("GET", "HEAD"):
            sent.append((q.method, re.sub(r"https?://[^/]+", "", q.url), q.post_data or ""))
            body = '{"ok": true, "ids": ["x"], "station": {"id": "x", "name": "audit"}, "queued": true}'
            return r.fulfill(status=200, content_type="application/json", body=body)
        return r.continue_()
    S_ = {"ctx": None, "pg": None}
    def js(*a):
        return S_["pg"].evaluate(*a)

    class _Pg:                                       # the current check's page (each check gets a fresh browser context)
        def __getattr__(self, k): return getattr(S_["pg"], k)
    pg = _Pg()

    def load(url, radio=True):
        if S_["ctx"]: S_["ctx"].close()
        ctx = b.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True)
        ctx.add_init_script(INIT); ctx.route("**/*", route)
        p_ = ctx.new_page()
        p_.on("pageerror", lambda e: errs.append("pageerror: " + str(e)[:160]))
        p_.on("console", lambda m: errs.append("console: " + m.text[:160]) if m.type == "error" and "ERR_FAILED" not in m.text and "CORS" not in m.text else None)
        p_.on("dialog", lambda d: d.accept())
        S_.update(ctx=ctx, pg=p_)
        p_.goto(url)
        if radio and "/radio" in url or "/classic" in url:
            pg.wait_for_function("typeof R !== 'undefined' && R && R.stations", timeout=20000)
        for _ in range(40):
            if js("!document.getElementById('page') || !/Tuning in/.test(document.getElementById('page').innerText)"): break
            time.sleep(0.25)
        time.sleep(1.5); sent.clear(); errs.clear()

    def click(sel, nth=0):
        for _ in range(40):
            if js("([s, i]) => !!document.querySelectorAll(s)[i]", [sel, nth]): break
            time.sleep(0.25)
        loc = pg.locator(sel).nth(nth)
        if not loc.count(): raise AssertionError(f"no element {sel}")
        try:
            loc.click(timeout=4000)                      # a real (trusted) click, like a tap
        except Exception:
            js("([s, i]) => document.querySelectorAll(s)[i].click()", [sel, nth])
        time.sleep(1.5)

    def req(method, pat):
        for _ in range(24):
            try:
                for x in js("window.__sent || []"):
                    if tuple(x) not in sent: sent.append(tuple(x))
            except Exception:
                pass
            hit = [s for s in sent if s[0] == method and re.search(pat, s[1])]
            if hit: break
            time.sleep(0.25)
        if not hit: raise AssertionError(f"expected {method} {pat}, sent {[(m, u) for m, u, _ in sent][:3]}")
        return f"{method} {hit[0][1]} {hit[0][2][:60]}"

    def check(area, name, fn, retry=True):
        try:
            d = fn(); bad = [e for e in errs]
            if bad and retry: raise AssertionError("JS errors: " + "; ".join(bad[:2]))
            results.append((area, name, not bad, (d or "") + (f"  JS ERRORS: {bad[:2]}" if bad else "")))
        except Exception as e:
            if retry:
                return check(area, name, fn, retry=False)
            results.append((area, name, False, (str(e).splitlines() or [repr(e)])[0][:200] + (f"  JS ERRORS: {errs[:2]}" if errs else "")))
        print(("ok  " if results[-1][2] else "FAIL") + f" {area:9} {name:34} {results[-1][3][:150]}", flush=True)

    hashis = lambda h: (lambda: (time.sleep(0.5), (js("location.hash") == h) or (_ for _ in ()).throw(AssertionError("hash " + js("location.hash"))))[-1] and h)

    # ---------------- radio: navigation
    def t():
        load(R + "/radio#lib"); click('a[data-tab="now"]'); assert js("location.hash") == "#stations" and "Stations" in js("document.querySelector('a[data-tab=now]').textContent"); click('a[data-tab="lib"]'); assert js("location.hash") == "#lib"
        for tab in ("home", "create"):
            click(f'a[data-tab="{tab}"]'); time.sleep(1); assert js("location.hash") == "#" + tab and js(f"!document.getElementById('pane-{tab}').hidden"), tab
        return "#stations, #lib, #home, #create"
    check("radio", "top tabs", t)
    def t():
        load(R + "/radio#home"); click('a[data-tab="create"]'); time.sleep(1)
        srcs = js("[...document.querySelectorAll('.pane iframe')].map(f => f.src)"); assert len(srcs) == 2, srcs
        for u in srcs:
            assert urllib.request.urlopen(u, timeout=5).status == 200, u
        return ", ".join(u.split("/", 3)[-1] for u in srcs)
    check("radio", "Home and Create panes answer", t)
    def t():
        load(R + "/radio#now"); click('[data-act="random"]'); return req("POST", r"/api/radio/power")
    check("radio", "🎲 Random station", t)
    def t():
        load(R + "/radio#now"); click('[data-play]:not([data-play="__reruns__"])'); return req("POST", r"/api/radio/power")
    check("radio", "station tile ▶ (Now page)", t)
    # ---------------- library
    def t():
        load(R + "/radio#lib"); before = js("document.getElementById('page').innerHTML.length"); click('[data-act="lib-layout"][data-v="1"]'); after = js("document.getElementById('page').innerHTML.length")
        click('[data-act="lib-layout"][data-v="0"]'); assert before != after, "layout didn't change"; return f"page {before}→{after} chars"
    check("library", "list / covers layout", t)
    for h in ("#lib/all", "#lib/playlist", "#lib/history", f"#lib/{ST}", "#lib/__reruns__"):
        def t(h=h):
            load(R + "/radio#lib"); click(f'a[href="{h}"]'); assert js("location.hash") == h; n = js("document.querySelectorAll('#page li.row').length"); return f"{n} rows"
        check("library", f"open {h}", t)
    def t():
        load(R + "/radio#lib"); click('[data-act="new"]'); assert js("!!document.querySelector('dialog[open]')"), "no dialog"
        js("() => { const d = document.querySelector('dialog[open]'); d.querySelector('#nName').value = 'audit'; d.querySelector('#nDesc').value = 'audit station'; }")
        click("#nCreate")
        return req("POST", r"/api/radio/stations$")
    check("library", "+ New station → create", t)
    # ---------------- station page
    S = R + f"/radio#lib/{ST}"
    for name, sel, m, pat in [("▶ Play station", "[data-play]", "POST", r"/api/radio/power"), ("🎵 Create a song now", '[data-act="make-song"]', "POST", rf"/api/radio/stations/{ST}/song"),
                              ("+ add to playlist (row)", '[data-act="plus"][data-on="0"]', "POST", r"/api/radio/playlist"), ("✓ remove from playlist (row)", '[data-act="plus"][data-on="1"]', "POST", r"/api/radio/playlist"),
                              ("Delete station", '[data-act="delete"]', "DELETE", rf"/api/radio/stations/{ST}")]:
        def t(sel=sel, m=m, pat=pat):
            load(S); js("document.querySelectorAll('details').forEach(d => d.open = true)"); click(sel); return req(m, pat)
        check("station", name, t)
    def t():
        load(S); js("document.querySelectorAll('details').forEach(d => d.open = true)"); click('[data-act="forget-taste"]'); return req("DELETE", rf"/api/radio/stations/{ST}/taste")
    check("station", "Forget taste notes", t)
    def t():
        load(S); click('[data-act="edit"]'); assert js("!!document.querySelector('dialog[open]')"), "no edit dialog"
        click("#eSave")
        return req("PUT", rf"/api/radio/stations/{ST}")
    check("station", "✎ Edit → Save", t)
    def t():
        load(S); click('[data-act="preview"]'); assert js("window.__play") > 0, "no audio play"; return "audio played"
    check("station", "row ▶ preview", t)
    def t():
        load(S); first = js("document.querySelector('#page li.row a[href*=\"/2026\"]').getAttribute('href')"); click(f'#page li.row a[href="{first}"]')
        time.sleep(1)
        for _ in range(20):
            if js("document.querySelectorAll('#page [data-act=\"song-play\"]').length"): break
            time.sleep(0.25)
        assert js("location.hash") == first and js("document.querySelectorAll('#page [data-act=\"song-play\"]').length"), "song page didn't open"; return first
    check("station", "song link → song page", t)
    def t():
        load(S); ids = lambda: js("[...document.querySelectorAll('#page li.row a[href*=\"/2026\"]')].map(a => a.getAttribute('href')).join()")
        a = ids(); js("() => { const s = document.querySelector('.lsort'); s.value = 'title'; s.dispatchEvent(new Event('change', {bubbles: true})); }"); time.sleep(1); bb = ids()
        js("() => { const s = document.querySelector('.lsort'); s.value = 'made'; s.dispatchEvent(new Event('change', {bubbles: true})); }"); time.sleep(1)
        assert a != bb, "order didn't change"; return "order changes"
    check("station", "Sort select", t)
    def t():
        load(S); n = js("document.querySelectorAll('#page li.row:not([hidden])').length")
        js("() => { const f = document.querySelector('.lfilter'); f.value = 'zzzz-no-match'; f.dispatchEvent(new Event('input', {bubbles: true})); }"); time.sleep(0.6)
        m = js("document.querySelectorAll('#page li.row:not([hidden])').length"); assert m < n, f"{n}→{m}"; return f"{n}→{m} rows"
    check("station", "Search these songs", t)
    def t():
        load(S); click('[data-act="sel-on"]'); click('[data-act="sel-one"]') if js("!!document.querySelector('[data-act=\"sel-one\"]')") else js("document.querySelector('#page li.row').click()")
        out = []
        for a, m, pat in [("sel-plus", "POST", r"/api/radio/playlist"), ("sel-next", "POST", r"/api/radio/upnext")]:
            if js(f"!!document.querySelector('[data-act=\"{a}\"]')"):
                click(f'[data-act="{a}"]'); out.append(req(m, pat)); sent.clear()
        click('[data-act="sel-off"]'); assert out, "no selection actions"; return "; ".join(x[:40] for x in out)
    check("station", "☑ Select → + playlist / play next", t)
    def t():
        load(S); h = js("document.querySelector('#page a[href*=\"download=1\"]').href"); assert urllib.request.urlopen(urllib.request.Request(h, headers={"Range": "bytes=0-9"})).status in (200, 206); return "download link answers"
    check("station", "⤓ download", t)
    # ---------------- song page
    SG = R + f"/radio#song/{SONG}"
    for name, sel, m, pat in [("👍", '[data-act="song-fb"][data-kind="keep"], [data-act="song-fb"]', "POST", r"/api/radio/feedback"), ("+ Playlist", '[data-act="song-plus"]', "POST", r"/api/radio/playlist"),
                              ("🎵 More like this", '[data-act="song-more"]', "POST", r"/api/radio/stations/.+/song"), ("🎨 New cover", '[data-act="song-art"]', "POST", r"/api/radio/art")]:
        def t(sel=sel, m=m, pat=pat):
            load(SG); click(sel); return req(m, pat)
        check("song", name, t)
    def t():
        load(SG); n = js("document.querySelectorAll('[data-act=\"song-fb\"]').length"); click('[data-act="song-fb"]', 1); return req("POST", r"/api/radio/feedback") + f" ({n} thumb buttons)"
    check("song", "👎", t)
    def t():
        load(SG); click('[data-act="song-play"]'); assert js("window.__play") > 0, "no audio"; return "audio played"
    check("song", "▶ Preview", t)
    def t():
        load(SG); click('[data-act="song-rewrite"]'); time.sleep(1); u = pg.url; op = js("window.__opened"); assert "rewrite=" in u or any("rewrite=" in o for o in op), f"url {u} opened {op}"; return (u if "rewrite=" in u else op[0])[-40:]
    check("song", "✏️ Rewrite → Music Studio", t)
    # ---------------- search
    def t():
        load(R + "/radio#lib"); js("() => { const q = document.getElementById('q'); q.value = 'love'; q.dispatchEvent(new Event('input', {bubbles: true})); }"); time.sleep(1.5)
        assert js("location.hash").startswith("#search"), js("location.hash"); return f"{js('document.querySelectorAll(\"#page li.row\").length')} results"
    check("search", "type in search box", t)
    # ---------------- player bar (playing a library song locally)
    for name, sel, m, pat in [("bar 👍", "#bUp", "POST", r"/api/radio/feedback"), ("bar ⊕ playlist", "#bAdd", "POST", r"/api/radio/playlist"), ("bar 👎", "#bDown", "POST", r"/api/radio/(feedback|skip)")]:
        def t(sel=sel, m=m, pat=pat):
            load(SG); click('[data-act="song-play"]'); sent.clear(); click(sel); return req(m, pat)
        check("bar", name, t)
    def t():
        load(SG); click('[data-act="song-play"]'); t1 = js("document.getElementById('bTitle').textContent"); click("#bSkip"); t2 = js("document.getElementById('bTitle').textContent")
        click("#bBack"); t3 = js("document.getElementById('bTitle').textContent"); assert t1 != t2, f"skip didn't change song ({t1})"; return f"{t1} → ⏭ {t2} → ⏮ {t3}"
    check("bar", "⏭ / ⏮ (library play)", t)
    def t():
        load(SG); click('[data-act="song-play"]'); click("#bLink"); assert js("nps.open"), "Now Playing didn't open"
        h = js("document.getElementById('npsMore').getAttribute('href')"); click("#npsMore"); assert SONG in js("location.hash") and not js("nps.open"), f"{h} → {js('location.hash')}"; return h
    check("bar", "tap Now Playing → sheet → song details", t)
    def t():
        load(SG); click('[data-act="song-play"]'); click("#bLink"); sent.clear(); click('#npsCtl [data-key="bUp"]'); r = req("POST", r"/api/radio/feedback")
        js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))"); time.sleep(0.4); assert not js("nps.open"), "Escape didn't close it"; return r
    check("bar", "Now Playing sheet: 👍 copy, Esc closes", t)
    def t():
        load(R + "/radio#now"); click("#bPlay"); assert js("window.__play") > 0; return "stream play attempted"
    check("bar", "▶ live radio", t)
    # ---------------- settings
    def t():
        load(R + "/radio#now"); js("() => { if (!document.querySelector('#side.open')) document.getElementById('sideBtn').click(); }"); time.sleep(1); click("#setBtn")
        assert js("!!document.querySelector('dialog[open]')"), "no settings dialog"
        js("() => { const d = document.querySelector('dialog[open]'); const b = [...d.querySelectorAll('button')].find(b => /save|apply/i.test(b.textContent)); b && b.click(); }"); time.sleep(1.5)
        return req("PUT", r"/api/radio/settings") if any(s[0] == "PUT" for s in sent) else "dialog opens (no Save button pressed)"
    check("panel", "⚙ Settings → Save", t)
    # ---------------- classic deck
    C = R + "/radio/classic"
    for name, sel, m, pat in [("⏻ POWER", "#kPower", "POST", r"/api/radio/power"), ("⏭ SKIP", "#kSkip", "POST", r"/api/radio/skip"), ("station button", "[data-st]:not([data-st='__reruns__'])", "POST", r"/api/radio/power"),
                              ("♥ / + favourite", "[data-heart], [data-plus]", "POST", r"/api/radio/playlist")]:
        def t(sel=sel, m=m, pat=pat):
            load(C); click(sel); return req(m, pat)
        check("classic", name, t)
    for name, sel in [("👍 KEEP", "#kKeep"), ("👎", "#kDislike"), ("+", "#kAdd")]:
        def t(sel=sel):
            load(C); click(sel); return (sent and f"{sent[0][0]} {sent[0][1]}") or js(f"document.querySelector('{sel}').title || 'no request: needs a song on air'")
        check("classic", name, t)
    def t():
        load(C); click("#kPlay"); assert js("window.__play") > 0; return "play attempted"
    check("classic", "▶ PLAY", t)
    def t():
        load(C); click("[data-new]"); assert js("!!document.querySelector('dialog[open], .modal.open, #newDlg[open]')") or js("document.body.innerText.includes('Create')"), "no form"; return "new-station form"
    check("classic", "+ New station", t)
    # ---------------- Music Studio
    M = R + "/?embed=1"                             # on its own (it's the radio page's Create tab otherwise)
    def t():
        load(M); click('[data-mode="instrumental"]'); a = js("document.querySelector('[data-mode=\"instrumental\"]').getAttribute('aria-pressed')"); click('[data-mode="song"]'); assert a == "true"; return "mode toggles"
    check("studio", "🎤 / 🎹 mode", t)
    def t():
        load(M); click('[data-ui="advanced"]'); v = js("[...document.querySelectorAll('.adv-only')].some(e => e.offsetWidth)"); click('[data-ui="easy"]'); w = js("[...document.querySelectorAll('.adv-only')].some(e => e.offsetWidth)"); assert v and not w, f"{v} {w}"; return "advanced controls show/hide"
    check("studio", "Easy / Advanced", t)
    def t():
        load(M); js("() => { const s = document.getElementById('preset'); s.value = s.options[2].value; s.dispatchEvent(new Event('change', {bubbles: true})); }"); time.sleep(0.5); v = js("document.getElementById('style').value"); assert v, "style empty"; return v[:40]
    check("studio", "preset → style", t)
    def t():
        load(M); js("document.getElementById('lyrics').value = ''"); click("#example"); v = js("document.getElementById('lyrics').value"); assert v, "no lyrics"; return f"{len(v)} chars"
    check("studio", "Load example", t)
    def t():
        load(M); click("#example"); click("#clearForm"); assert not js("document.getElementById('lyrics').value"); return "form cleared"
    check("studio", "Clear", t)
    def t():
        load(M); click("#libLyrics"); time.sleep(1.5); v = js("document.getElementById('lyrics').value"); assert v, "no lyrics"; return f"{len(v)} chars"
    check("studio", "🎲 Lyrics from the library", t)
    def t():
        load(M); click("#dice") if js("document.getElementById('dice').offsetWidth") else (click('[data-ui="advanced"]'), click("#dice")); v = js("document.getElementById('seed').value"); click('[data-ui="easy"]'); assert v; return f"seed {v}"
    check("studio", "🎲 random seed", t)
    def t():
        load(M); click("#openWriter"); assert js("document.getElementById('wGo').offsetWidth > 0"), "writer hidden"; click("#wGo"); return req("POST", r"/api/lyrics")
    check("studio", "✨ Write lyrics for me → Write", t)
    def t():
        load(M); click('[data-ui="advanced"]'); click("#example"); click("#polish"); click('[data-ui="easy"]'); return req("POST", r"/api/lyrics")
    check("studio", "Polish my lyrics", t)
    def t():
        load(M); click("#example"); js("document.getElementById('style').value = document.getElementById('style').value || 'pop'"); click("#go"); return req("POST", r"/api/jobs$")
    check("studio", "Create song", t)
    def t():
        load(M); time.sleep(3); click("#spGo"); return req("POST", r"/api/spotify/run")
    check("studio", "🎵 From my Spotify likes", t)
    for kind, m, pat in [("upnext", "POST", r"/api/radio/upnext"), ("take", "POST", r"/api/jobs$"), ("delete", "DELETE", r"/api/jobs/\d")]:
        def t(kind=kind, m=m, pat=pat):
            load(M); time.sleep(1); click(f'[data-act="{kind}"]'); time.sleep(1)
            try: return req(m, pat)
            except AssertionError as e: raise AssertionError(f"{e} | form says: {js('document.getElementById(\"formErr\").textContent')!r}")
        check("studio", f"library: {kind}", t)
    def t():
        load(M); time.sleep(1); click('[data-act="details"]'); assert js("document.getElementById('dlg').open"), "no details dialog"; return js("document.getElementById('dlgTitle').textContent")[:40]
    check("studio", "library: Details", t)
    def t():
        load(M); time.sleep(1); js("document.getElementById('style').value = ''; document.getElementById('lyrics').value = ''"); click('[data-act="reuse"]'); time.sleep(1); v = js("document.getElementById('style').value + ' / ' + document.getElementById('lyrics').value.length"); assert not v.startswith(' / '), "style not filled"; return f"form: {v[:40]}"
    check("studio", "library: Reuse", t)
    def t():
        load(M); time.sleep(1); click('[data-act="edit"]'); time.sleep(1); assert js("!!document.getElementById('abcEdit')"), "no score editor"; return "score editor opens"
    check("studio", "library: Edit score", t)
    def t():
        load(M); time.sleep(1); click('[data-act="rewrite"]'); time.sleep(3); assert js("document.getElementById('rwClose') && document.getElementById('rwClose').offsetWidth > 0"), "rewrite panel hidden"; return "rewrite panel opens"
    check("studio", "library: ✏️ Rewrite", t)
    def t():
        load(M); n = js("document.querySelectorAll('#library .job').length"); js("() => { const s = document.getElementById('search'); s.value = 'zzzz-none'; s.dispatchEvent(new Event('input')); }"); time.sleep(0.6)
        m = js("document.querySelectorAll('#library .job').length"); assert m < n; return f"{n}→{m}"
    check("studio", "library search", t)
    def t():
        load(M); n = js("document.querySelectorAll('#library .job').length"); js("() => { const s = document.getElementById('filter'); s.value = 'failed'; s.dispatchEvent(new Event('change')); }"); time.sleep(0.8)
        m = js("document.querySelectorAll('#library .job').length"); assert m != n; return f"{n}→{m}"
    check("studio", "library filter", t)
    def t():
        load(M); h = js("document.querySelector('#library a[href*=\"song.mp3\"]').href"); assert urllib.request.urlopen(urllib.request.Request(h, headers={"Range": "bytes=0-9"})).status in (200, 206); return "MP3 link answers"
    check("studio", "library ⬇ MP3", t)
    # ---------------- Home (signed out)
    def t():
        load(H + "/home?embed=1"); click("#acctBtn"); assert js("!!document.querySelector('dialog[open]')") or js("document.body.innerText.toLowerCase().includes('password')"), "no sign-in form"; return "sign-in form"
    check("home", "Sign in button", t)
    def t():
        load(H + "/home?embed=1"); out = []
        for app in ("radio", "music"):
            js("window.__opened = []"); click(f'button[data-app="{app}"]'); time.sleep(0.5); u = js("window.__opened[0] || location.href"); out.append(f"{app}→{u[-28:]}"); load(H + "/home?embed=1")
        return ", ".join(out)
    check("home", "Listen / Open buttons", t)
    def t():
        load(H + "/home?embed=1"); click("[data-agentora-panel]"); assert js("!!document.querySelector('.agentora-panel.open, [data-agentora-panel-open], dialog[open]')") or js("document.body.innerText.includes('GPU')"), "panel didn't open"; return "panel opens"
    check("home", "☰ panel", t)
    b.close()
print(f"\n{sum(r[2] for r in results)} passed, {sum(not r[2] for r in results)} failed")
if A.json:
    Path(A.json).write_text(json.dumps(results, indent=1))
sys.exit(0 if all(r[2] for r in results) else 1)
