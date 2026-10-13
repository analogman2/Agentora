"""Agentora: infinite AI radio stations for the Music Studio (inspired by CharlesMod/infinite-tapedeck).

Stations are text-defined ("late-night lo-fi") or learned from a folder of songs.
While the radio is on, a spool manager keeps a few approved songs ready per
station: an LLM writes each song's spec (title, style, lyrics) from the station
description, its learned profile and listener feedback; YuE2 renders it through
the studio's GPU pool; a CLAP critic scores the take against the station (its
songs, or its description) and drops the weakest. Keep / skip / dislike feedback
steers both the prompts and the critic.

Everything runs on the CPU here (CLAP, librosa); the GPUs stay with YuE2.
"""

# The radio is split by job (see each module's docstring); this module starts it up, routes /api/radio/*, and keeps
# `radio.NAME` working for app.py, art.py and settings.py by forwarding to the module that owns NAME.
import collections, json, os, queue, random, re, shutil, subprocess, sys, threading, time, traceback, urllib.request, uuid
from pathlib import Path
import numpy as np
import art                          # cover art for stations and songs (drawn by ComfyUI between songs)

import app
import radio_state as rs, radio_stations as rst, radio_songwriter as rsw, radio_spool as rsp, radio_broadcast as rbc, radio_library as rlb


def init(studio_module):
    global app
    app = studio_module
    art.init(sys.modules[__name__])
    rs.RADIO = app.DATA.parent / "radio"
    (rs.RADIO / "stations").mkdir(parents=True, exist_ok=True)
    try:
        rs.state.update(json.loads((rs.RADIO / "state.json").read_text()))
    except Exception:
        pass
    if os.environ.get("RADIO_AHEAD"):                 # songs to keep ready or in the making (overrides the saved value)
        rs.state["target_ahead"] = int(os.environ["RADIO_AHEAD"])
    import settings
    settings.init(app, sys.modules[__name__])        # saved ⚙ settings win over env vars and built-ins
    # resume only after a quick service restart (studio was alive < 2 min ago); after a reboot the radio starts off
    rs.state["on"] = bool(rs.state.get("on")) and time.time() - (rs.state.get("heartbeat") or 0) < 120 and rs.state.get("station") is not None
    if rs.state["on"]:
        app.log("radio: resuming the broadcast after a quick restart")
    for f in (rs.RADIO / "stations").glob("*/station.json"):
        try:
            s = json.loads(f.read_text())
            if s.get("analysis", {}).get("status") == "running":
                s["analysis"]["status"] = "interrupted"
            rs.stations[s["id"]] = s
        except Exception:
            pass
    if not rs.stations:
        for name, desc, vocals in [
                ("Late Night Lo-fi", "Mellow late-night lo-fi hip hop and chillhop: dusty piano, warm Rhodes, soft boom-bap drums, rain on the window, mostly instrumental with the occasional soft vocal.", "mix"),
                ("Indie Road Trip", "Sunny indie rock and indie folk for driving with the windows down: jangly guitars, big singalong choruses, male and female vocals, nostalgic and hopeful.", "vocals"),
                ("Neon Nights", "80s-inspired synthwave and synth-pop: analog synths, gated drums, arpeggiated bass, dreamy vocals, neon city at night.", "mix")]:
            rst.create_station({"name": name, "kind": "text", "description": desc, "vocals": vocals})
    rs.save_state()
    threading.Thread(target=rsp.spool_loop, daemon=True, name="radio-spool").start()
    threading.Thread(target=rsw.bank_loop, daemon=True, name="radio-lyricist").start()
    threading.Thread(target=rbc.playout_loop, daemon=True, name="radio-playout").start()


def handle(h, method, path):
    """Route /api/radio/* requests. Returns True if handled."""
    q = {}
    if "?" in h.path:
        q = dict(x.split("=", 1) for x in h.path.split("?", 1)[1].split("&") if "=" in x)
    try:
        if method == "GET" and path == "/api/radio/favorites":
            return h.send(200, {"tracks": rlb.favorites()}) or True
        if method == "GET" and path == "/api/radio/songs":
            return h.send(200, {"tracks": rlb.all_songs()}) or True
        if method == "GET" and path == "/api/radio/playlist":
            return h.send(200, {"tracks": rlb.playlist()}) or True
        if method == "POST" and path == "/api/radio/playlist":
            b = h.body(); return h.send(200, rlb.set_playlist(b.get("track", ""), bool(b.get("on")))) or True
        if method == "GET" and path == "/api/radio/ping":
            return h.send(200, {"t": time.time()}) or True
        if method == "GET" and path == "/api/radio/speedtest":
            n = max(1, min(int(q.get("mb") or 2), 20)) * 2**20
            return h.send(200, rbc._noise(n), "application/octet-stream") or True
        if method == "GET" and path == "/api/radio/listeners":
            return h.send(200, {"listeners": rbc.listeners_view(q.get("me"))}) or True
        if method == "GET" and path == "/api/radio/gpu":
            return h.send(200, rlb.gpu_memory()) or True
        if method == "GET" and path == "/api/radio/settings":
            import settings
            return h.send(200, settings.view() | {"models": rsw.writer_models()}) or True
        if method == "PUT" and path == "/api/radio/settings":
            import settings
            return h.send(200, settings.update(h.body()) | {"models": rsw.writer_models()}) or True
        if method == "POST" and path == "/api/radio/history/clear":
            return h.send(200, rlb.clear_history()) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)", path)
        if method == "PUT" and m:
            return h.send(200, {"station": rst.update_station(m.group(1), h.body())}) or True
        if method == "GET" and path == "/api/radio/listened":
            return h.send(200, {"tracks": rlb.listened(min(500, int(q.get("limit") or 100)))}) or True
        if method == "GET" and path == "/api/radio/state":
            return h.send(200, rlb.api_state(q.get("listener"))) or True
        if method == "POST" and path == "/api/radio/upnext":
            b = h.body(); return h.send(200, rbc.play_next(b.get("ids") or [])) or True
        if method == "POST" and path == "/api/radio/back":
            b = h.body(); return h.send(200, rbc.back(b.get("track"), int(b.get("steps") or 0))) or True
        if method == "POST" and path == "/api/radio/skip":
            b = h.body(); return h.send(200, rbc.skip(b.get("track"), "dislike" if b.get("kind") == "dislike" else "skip")) or True
        if method == "GET" and path == "/api/radio/next":
            t = rlb.next_track(q.get("station") or rs.state["station"], mark=q.get("peek") != "1")
            return h.send(200, {"track": t}) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/history", path)
        if method == "GET" and m:
            return h.send(200, {"tracks": rlb.history(m.group(1), min(1000, int(q.get("limit") or 40)), q.get("upcoming") == "1")}) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/art", path)
        if method == "GET" and m:
            f = art.station_path(m.group(1))
            if not f.is_file():
                return h.send(404, {"error": "No art yet"}) or True
            return h.send(200, f.read_bytes(), "image/jpeg", {"Cache-Control": "private, max-age=604800"}) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/song", path)
        if method == "POST" and m:
            return h.send(200, rsp.request_song(m.group(1), (h.body() or {}).get("like"))) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/taste", path)
        if method == "DELETE" and m:
            return h.send(200, rsw.forget_taste(m.group(1))) or True
        m = re.fullmatch(r"/api/radio/tracks/([\w-]+)", path)
        if method == "GET" and m:
            return h.send(200, rlb.track_details(m.group(1))) or True
        if method == "POST" and path == "/api/radio/art":
            b = h.body()
            return h.send(200, art.request("station", b["station"]) if b.get("station") else art.request("song", str(b.get("track") or ""))) or True
        if method == "POST" and path == "/api/radio/stations/assist":
            return h.send(200, rst.station_assist(h.body())) or True
        if method == "POST" and path == "/api/radio/stations":
            return h.send(200, {"station": rst.create_station(h.body())}) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)", path)
        if method == "DELETE" and m:
            rst.delete_station(m.group(1)); return h.send(200, {"ok": True}) or True
        if method == "POST" and path == "/api/radio/power":
            b = h.body(); return h.send(200, rlb.set_power(b.get("on"), b.get("station"))) or True
        if method == "POST" and path == "/api/radio/feedback":
            b = h.body(); return h.send(200, rlb.feedback(b.get("track", ""), b.get("kind", ""))) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/analyze", path)
        if method == "POST" and m:
            return h.send(200, rst.start_analysis(m.group(1))) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/files/(.+)", path)
        if method == "PUT" and m:
            n = int(h.headers.get("Content-Length") or 0)
            if n > 200 * 2**20:
                raise app.BadRequest("File too large (200 MB max)")
            from urllib.parse import unquote
            return h.send(200, rst.upload(m.group(1), unquote(m.group(2)), h.rfile.read(n))) or True
    except app.BadRequest as e:
        return h.send(400, {"error": str(e)}) or True
    except json.JSONDecodeError:
        return h.send(400, {"error": "Invalid JSON"}) or True
    return False


_PARTS = (rs, rlb, rbc, rsp, rsw, rst)


def __getattr__(name):
    """radio.NAME for anything that moved into a part (read live: settings change rs.* at run time)."""
    for part in _PARTS:
        if hasattr(part, name):
            return getattr(part, name)
    raise AttributeError(f"module 'radio' has no attribute {name!r}")
