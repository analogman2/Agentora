#!/usr/bin/env python3
"""Browser end-to-end test of the radio page, the Music Studio and the Home page, in headless Chromium (Playwright).

Fails on any JavaScript error or failed script load, and walks the main flows: Now Playing, the Library (All songs,
search, sort, select), a station, a song page, playing a song from the Library in the player bar, lyrics on the song page and Now Playing, the ☰ side panel,
⚙ settings, and the one-window app: Home and Create as tabs (no new windows, each pane loaded once), Create's Easy /
Advanced switch, its ▶ playing in the radio page's bar, 🎲 lyrics from the library, a station's style into Create, and the
old Home / Music Studio addresses landing on the radio page. Nothing is changed: the songs it plays only play in this headless browser.
Usage: tests/e2e_ui.py [--radio URL] [--home URL] [--chrome PATH] [--shots DIR]
"""
import argparse, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--radio", default="http://127.0.0.1:7860")
ap.add_argument("--home", default="http://127.0.0.1:7871")
ap.add_argument("--chrome", default=str(Path.home() / ".hermes/tools/chromium-1208/chrome-linux64/chrome"))
ap.add_argument("--shots", default="", help="save a screenshot of each step here")
A = ap.parse_args()
results, errors = [], []


def check(name, fn):
    try:
        detail = fn(); results.append(True); print(f"  ok   {name}  {detail or ''}")
    except Exception as e:
        results.append(False); print(f"  FAIL {name}  {repr(e)[:300]}")


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=A.chrome if Path(A.chrome).exists() else None,
                          args=["--autoplay-policy=no-user-gesture-required"])
    ctx = b.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(f"page error: {e}"))
    pg.on("console", lambda m: errors.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
    pg.on("requestfailed", lambda r: errors.append(f"request failed: {r.url}") if "/web/" in r.url or "/agentora/" in r.url else None)
    js = pg.evaluate

    def shot(name):
        if A.shots:
            Path(A.shots).mkdir(parents=True, exist_ok=True); pg.screenshot(path=f"{A.shots}/{name}.png")

    def go(url, ready="typeof R !== 'undefined' && R && R.stations"):
        pg.goto(url); pg.wait_for_function(ready, timeout=20000); time.sleep(1)

    print("radio")
    check("page loads, all scripts run", lambda: (go(A.radio + "/radio#now"), shot("radio-now"),
          must(js("[typeof songArt, typeof connect, typeof lpPlay, typeof renderSide, typeof updateMk].every(t => t === 'function')"), "functions missing"))[-1] or "core, player, library, panel, extras")
    check("Library", lambda: (js("location.hash = '#lib'"), time.sleep(1.5), shot("radio-lib"),
          must(js("document.querySelectorAll('.flist .frow').length") >= 4, "folders/stations missing"))[-1] or f"{js('document.querySelectorAll(\".flist .frow\").length')} rows")
    def all_songs():
        js("location.hash = '#lib/all'"); time.sleep(2)
        n = js("document.querySelectorAll('.rows .row').length"); must(n > 0, "no songs")
        js("(() => { const f = document.querySelector('.lfilter'); f.value = 'the'; f.dispatchEvent(new Event('input', {bubbles: true})); })()"); time.sleep(0.3)
        shown = js("document.querySelectorAll('.rows .row:not([hidden])').length"); must(0 < shown <= n, f"search shows {shown} of {n}")
        js("(() => { const s = document.querySelector('.lsort'); s.value = 'title'; s.dispatchEvent(new Event('change', {bubbles: true})); })()"); time.sleep(0.8)
        titles = js("[...document.querySelectorAll('.rows .row:not([hidden]) a.a')].slice(0, 5).map(a => a.textContent)")
        must(titles == sorted(titles, key=str.lower) or len(titles) < 2, f"not A–Z: {titles}")
        js("document.querySelector('[data-act=sel-on]').click()"); time.sleep(0.3)
        js("document.querySelector('[data-act=sel-all]').click()"); time.sleep(0.4)
        sel = js("document.querySelector('.selbar .small').textContent")
        js("document.querySelector('[data-act=sel-off]').click()")
        js("(() => { const s = document.querySelector('.lsort'); s.value = 'as-is'; s.dispatchEvent(new Event('change', {bubbles: true})); })()")
        shot("radio-all")
        return f"{n} songs, search 'the' -> {shown}, A–Z ok, select all -> {sel}"
    check("All songs: search, sort, select", all_songs)
    def station():
        sid = js("R.stations[0].id"); js(f"location.hash = '#lib/{sid}'"); time.sleep(2); shot("radio-station")
        must(js("!!document.querySelector('.shead h1') && !!document.querySelector('.lctl')"), "station header or list controls missing")
        return js("document.querySelector('.shead h1').textContent")
    check("a station", station)
    def song_page():
        js("document.querySelector('.rows .row .b').click()"); time.sleep(2); shot("radio-song")
        must(js("!!document.querySelector('.songpage h1')"), "no song page")
        return js("document.querySelector('.crumbs').innerText.replace(/\\n/g, ' ')")
    check("a song page (path bar)", song_page)
    def library_play():
        js("location.hash = '#lib/all'"); time.sleep(2)
        js("document.querySelectorAll('.rows .row')[1].querySelector('.thumb').click()"); time.sleep(3)
        bar = js("$('bTitle').textContent + ' | ' + $('bStation').textContent")
        must("from your library" in bar and js("!pv.paused"), f"bar: {bar}")
        js("$('bSkip').click()"); time.sleep(2); nxt = js("$('bTitle').textContent")
        js("$('bLink').click()"); time.sleep(1); shot("radio-nowplaying")         # the Now Playing sheet: previous · now · next
        must(js("nps.open && $('npsTitle').textContent === $('bTitle').textContent"), "Now Playing sheet didn't open on the song")
        must(js("!!$('npsPrev').querySelector('img') && !$('npsPrev').disabled"), "no previous cover in the sheet")
        js("$('npsPrev').click()"); time.sleep(1.5); back = js("$('npsTitle').textContent")
        must(back == bar.split(" | ")[0], f"previous cover played {back}")
        js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))"); time.sleep(0.5); must(not js("nps.open"), "Escape didn't close the sheet")
        js("lpStop()")
        return f"{bar} -> ⏭ {nxt} -> sheet ‹ {back}"
    check("play from the Library in the bar", library_play)
    def lyrics():
        ids = js("(async () => { const all = await loadAll(); const sung = all.find(t => (t.lyrics || '').includes('[')), inst = all.find(t => t.mode === 'instrumental' && !(t.lyrics || '').split('\\n').some(l => l.trim() && !/^\\[.*\\]$/.test(l.trim()))); return [sung && sung.id, inst && inst.id]; })()")
        must(ids[0], "no song with lyrics in the library")
        js(f"location.hash = '#lib/all/{ids[0]}'"); time.sleep(2); shot("radio-lyrics")
        secs = js("document.querySelectorAll('.songpage .lyrics .lysec .lytag').length"); must(secs > 0, "song page: no [section] labels")
        if ids[1]:                                       # Now Playing shows the lyrics and follows the song (here: to an instrumental)
            js(f"(async () => {{ const all = await loadAll(); lp.queue = [all.find(t => t.id === '{ids[0]}'), all.find(t => t.id === '{ids[1]}')]; lpPlay(0); location.hash = '#now'; }})()"); time.sleep(2.5)
            must(js("document.querySelectorAll('.nply .lyl').length") > 0, "Now Playing: no lyrics")
            js("$('bSkip').click()"); time.sleep(2.5)
            must(js("!!document.querySelector('.nply .lynone')"), "Now Playing didn't follow to the instrumental")
            js("lpStop()")
        return f"{secs} sections; Now Playing follows the song"
    check("lyrics: song page and Now Playing", lyrics)
    def side_panel():
        js("$('sideBtn').click()"); time.sleep(1.5); shot("radio-panel")
        must(js("$('side').classList.contains('open')"), "panel didn't open")
        js("setSide(false, false)"); return "opened"
    check("☰ side panel", side_panel)
    def settings():
        js("$('sideBtn').click()"); time.sleep(0.5)
        btn = js("(() => { const b = [...document.querySelectorAll('#side button')].find(x => /settings/i.test(x.textContent)); if (b) b.click(); return !!b; })()")
        time.sleep(2); shot("radio-settings")
        must(btn and js("$('dlg').open && document.querySelectorAll('#dlgB [data-key], #dlgB input, #dlgB select').length > 5"), "settings dialog empty")
        js("$('dlg').close(); setSide(false, false)"); return "dialog with fields"
    check("⚙ settings", settings)

    print("one window: Home and Create are tabs of the radio page")
    def pane(name):                                     # the frame of a pane, once it has loaded
        js(f"location.hash = '#{name}'")
        el = pg.wait_for_selector(f"#pane-{name} iframe", state="attached", timeout=10000)
        f = el.content_frame(); must(f, f"no {name} pane")
        f.wait_for_load_state(timeout=30000); f.wait_for_function("document.body && typeof agentoraOpen === 'function'", timeout=30000)
        time.sleep(1); return f
    def one_window():
        js("window.__opens = 0; window.open = () => { window.__opens++; return null; }; 0")   # ; 0: don't hand Playwright a function
        home = pane("home"); shot("tab-home")
        must(home.evaluate("getComputedStyle(document.querySelector('.nav')).display === 'none'"), "Home's own nav shows inside the pane")
        home.evaluate("document.querySelector('[data-app=music]').click()"); time.sleep(1.5)
        must(js("location.hash") == "#create", f"Home's Create button went to {js('location.hash')}")
        pane("create"); shot("tab-create")
        js("location.hash = '#lib'"); time.sleep(1.5); js("location.hash = '#home'"); time.sleep(1)
        frames = js("[document.querySelectorAll('#pane-home iframe').length, document.querySelectorAll('#pane-create iframe').length]")
        must(frames == [1, 1] and js("window.__opens") == 0, f"frames {frames}, window.open called {js('window.__opens')}×")
        return "Home → Create → Library → Home in place, each pane loaded once, no window.open"
    check("switch tabs in one window", one_window)
    def music():
        f = pane("create")
        f.evaluate("document.querySelector('[data-ui=advanced]').click()"); time.sleep(0.4)
        adv = f.evaluate("!document.body.classList.contains('easy')")
        f.evaluate("document.querySelector('[data-ui=easy]').click()"); time.sleep(0.4)
        easy = f.evaluate("document.body.classList.contains('easy') && getComputedStyle(document.getElementById('adv')).display === 'none'")
        must(adv and easy, f"advanced={adv} easy={easy}")
        must(f.evaluate("window.agentoraPlayer === window.parent.agentoraHostPlayer"), "Create has its own player")
        return "Easy/Advanced switch, uses the radio page's player"
    check("Create (Music Studio)", music)
    def create_play():                                  # plays only in this headless browser
        f = pane("create"); f.wait_for_function("document.querySelector('[data-act=play]')", timeout=15000)
        f.evaluate("document.querySelector('[data-act=play]').click()"); time.sleep(3)
        got = js("[lp.t && lp.t.title, !pv.paused, $('bTitle').textContent]"); js("pv.pause()")
        must(got[0] and got[1] and got[2] == got[0], f"bar: {got}")
        return f"“{got[0]}” in the radio page's bar"
    check("Create: ▶ plays in the one player bar", create_play)
    def library_lyrics():                               # this headless browser's own form; nothing is saved on the server
        f = pane("create"); ev = f.evaluate
        ev("setMode('song'); $('lyrics').value = 'my own words'")
        got = []
        for _ in range(3):
            ev("$('libLyrics').click()"); time.sleep(1); got.append(ev("$('lyrics').value"))
        note = ev("$('libLyricsNote').textContent")
        must(len(set(got)) == 3 and "my own words" not in got and note.startswith("Lyrics from"), f"3 presses -> {len(set(got))} different; {note[:80]}")
        ev("$('libLyricsUndo').click()"); must(ev("$('lyrics').value") == "my own words", "undo didn't bring my lyrics back")
        return note.split(".")[0]
    check("Create: 🎲 lyrics from the library, undo", library_lyrics)
    def station_style():                                # a station's sound into Create's style box, then ↩ Undo
        sid = js("R.stations.find(s => s.ready).id"); js(f"location.hash = '#lib/{sid}'"); time.sleep(3)
        js("document.querySelector('[data-act=station-like]').click()"); time.sleep(4)
        f = pane("create"); note = f.evaluate("$('enhanceNote').textContent")
        must(js("location.hash") == "#create" and note.startswith("📻 Style from"), f"{js('location.hash')} {note[:80]}")
        f.evaluate("$('likeUndo').click()"); return note.split(" on the radio")[0]
    check("Station → ✏️ Write my own in this style → Create", station_style)
    def old_addresses():
        pg.goto(A.home + "/home"); time.sleep(2); h = pg.url
        pg.goto(A.radio + "/"); time.sleep(2); m = pg.url
        must(h.endswith("/radio#home") and m.endswith("/radio#create"), f"{h} {m}"); return "Home → #home, Music Studio → #create"
    check("old addresses open the one page", old_addresses)
    def home_panel():
        go(A.radio + "/radio#home"); js("$('sideBtn').click()"); time.sleep(1.5)
        must(js("$('side').classList.contains('open') && !$('pane-home').hidden"), "panel over the Home tab"); js("setSide(false, false)")
        return "☰ panel over the Home tab"
    check("Home tab: ☰ panel", home_panel)

    b.close()

print("browser errors")
errors = [e for e in errors if "favicon" not in e]
check("no JavaScript errors", lambda: must(not errors, " | ".join(errors[:5])) or "none")
print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)
