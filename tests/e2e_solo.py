#!/usr/bin/env python3
"""One song at a time: the Music Studio's ▶ Play goes through the shared Now Playing player, and starting a song anywhere
stops whatever else plays, in that tab and in the other Agentora tabs (radio, Music Studio).

In headless Chromium: plays two Music Studio songs in a row, then a radio Library song in a second tab, then the Studio
again. A sampler checks every 50 ms that at most one audio source plays across both tabs. Nothing changes on the server:
requests that would (POST, PUT, DELETE) are answered in the browser, and the live stream is blocked.
Usage: tests/e2e_solo.py [--radio URL] [--chrome PATH]
"""
import argparse, sys, threading, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--radio", default="http://127.0.0.1:7860")
ap.add_argument("--chrome", default=str(Path.home() / ".hermes/tools/chromium-1208/chrome-linux64/chrome"))
A = ap.parse_args()
R = A.radio.rstrip("/")
PLAYING = """(() => { const els = [...document.querySelectorAll('audio, video')];
  if (window.agentoraPlayer) els.push(agentoraPlayer.audio);
  return [...new Set(els)].filter(e => !e.paused && !e.ended && e.currentSrc && e.volume > 0.05).map(e => e.currentSrc.split('/').slice(-2).join('/')); })()"""
results = []


def check(name, fn):
    try:
        detail = fn(); results.append(True); print(f"  ok   {name}  {detail or ''}")
    except Exception as e:
        results.append(False); print(f"  FAIL {name}  {repr(e)[:300]}")


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


def route(r):
    q = r.request
    if "stream.mp3" in q.url or ":7861" in q.url:
        return r.abort()
    if q.method not in ("GET", "HEAD"):
        return r.fulfill(status=200, content_type="application/json", body='{"ok": true, "cut": 0}')
    return r.continue_()


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=A.chrome if Path(A.chrome).exists() else None, args=["--autoplay-policy=no-user-gesture-required"])
    ctx = b.new_context(viewport={"width": 390, "height": 844}); ctx.route("**/*", route)
    errs = []
    def page(url):
        pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e))); pg.goto(url); time.sleep(3); return pg
    studio, radio = page(R + "/"), page(R + "/radio#lib/all")
    worst, lock, stop = [], threading.Lock(), threading.Event()
    def playing():
        return studio.evaluate(PLAYING) + radio.evaluate(PLAYING)

    def sample(seconds):                             # at most one source playing, checked every 50 ms
        end = time.time() + seconds
        while time.time() < end:
            now = playing()
            if len(now) > 1: worst.append(now)
            time.sleep(0.05)

    def studio_plays_through_shared_player():
        must(studio.evaluate("document.querySelectorAll('audio[controls]').length") == 0, "the Studio still has native audio players")
        btns = studio.evaluate("[...document.querySelectorAll('[data-act=play]')].map(b => b.dataset.id)")
        must(len(btns) >= 2, f"need two songs with a ▶ Play button, found {len(btns)}")
        studio.click(f'[data-act="play"][data-id="{btns[0]}"]'); sample(2)
        first = studio.evaluate("agentoraPlayer.t.id")
        studio.click(f'[data-act="play"][data-id="{btns[1]}"]'); sample(2)
        now = playing()
        must(studio.evaluate("agentoraPlayer.t.id") == btns[1] and len(now) == 1, f"after the second ▶: {now}")
        must(studio.evaluate("agentoraNowPlaying && document.getElementById('npsTitle') !== null"), "no Now Playing view in the Studio")
        return f"{first} then {btns[1]}; playing: {now}"
    check("Studio: two songs in a row, one player", studio_plays_through_shared_player)

    def radio_tab_stops_studio():
        radio.evaluate("(async () => { const all = await loadAll(); lp.queue = all.slice(0, 3); lpPlay(0); })()"); sample(2.5)
        now = playing()
        must(studio.evaluate("agentoraPlayer.audio.paused"), "the Studio kept playing")
        must(not radio.evaluate("pv.paused") and len(now) == 1, f"playing: {now}")
        return f"radio tab plays {now[0]}, Studio paused"
    check("another tab: the radio stops the Studio", radio_tab_stops_studio)

    def studio_stops_radio_tab():
        studio.evaluate("agentoraPlayer.toggle()"); sample(2.5)
        now = playing()
        must(radio.evaluate("pv.paused"), "the radio tab kept playing")
        must(len(now) == 1, f"playing: {now}")
        return f"Studio plays {now[0]}, radio tab paused"
    check("another tab: the Studio stops the radio", studio_stops_radio_tab)

    def studio_sheet_follows_list():
        studio.evaluate("agentoraPlayer.go(1)"); time.sleep(1)
        studio.click(".agentora-np .t"); time.sleep(0.8)
        prev = studio.evaluate("document.getElementById('npsPrevCap').innerText.split('\\n')[1]")
        want = studio.evaluate("agentoraPlayer.queue[agentoraPlayer.i - 1].title")
        must(prev == want, f"previous cover shows {prev!r}, the list has {want!r}")
        studio.keyboard.press("Escape"); studio.evaluate("agentoraPlayer.stop()"); radio.evaluate("lpStop()")
        return f"previous = {prev}"
    check("Studio Now Playing follows the list it played from", studio_sheet_follows_list)

    check("never two at once", lambda: must(not worst, f"overlaps: {worst[:3]}") or "max 1 source in every sample")
    check("no JavaScript errors", lambda: must(not errs, errs[:3]) or "none")
    b.close()

print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)
