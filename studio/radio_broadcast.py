"""The live broadcast: chooses and encodes songs into one real-time MP3 stream, skip / ⏮ history / 📻 play next,
clean cuts, and the listening signals (heard to the end / tuned out).

The listeners themselves are held by the stream relay (stream_relay.py, its own service on port 7861): this module
paces the audio and feeds it there, so restarting the radio doesn't disconnect anyone. The relay owns the stream
position and the burst buffer; rs.bc["bytes"] mirrors its position and rs.bc["listeners"] its listeners (polled).
"""
import collections, json, os, queue, random, re, shutil, subprocess, sys, threading, time, traceback, urllib.request, uuid
from pathlib import Path
import numpy as np
import art                          # cover art for stations and songs (drawn by ComfyUI between songs)

import app
import radio_state as rs, radio_stations as rst, radio_songwriter as rsw, radio_spool as rsp, radio_library as rlb
import socket, struct

RELAY_PORT = int(os.environ.get("RADIO_RELAY_PORT", "7861"))        # where players connect (stream_relay.py)
RELAY_FEED = ("127.0.0.1", int(os.environ.get("RADIO_RELAY_FEED", "7862")))


class Relay:
    """The radio's side of the feed to stream_relay.py; reconnects by itself. Every call returns None if it's down."""

    def __init__(self):
        self.sock, self.lock, self.retry_at = None, threading.Lock(), 0.0

    def _call(self, kind, payload=b"", reply="pos"):
        with self.lock:
            for attempt in (1, 2):
                if self.sock is None:
                    if time.time() < self.retry_at:
                        return None
                    try:
                        self.sock = socket.create_connection(RELAY_FEED, timeout=5)
                        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        self._send(b"F", json.dumps({"bitrate": rs.ACTIVE_BITRATE, "burst": rs.BURST}).encode())
                        self._recv(8)
                        app.log(f"radio: feeding the stream relay on {RELAY_FEED[0]}:{RELAY_FEED[1]}")
                    except OSError as e:
                        self.sock = None; self.retry_at = time.time() + 2
                        app.log(f"radio: stream relay not reachable ({e})")
                        return None
                try:
                    self._send(kind, payload)
                    if reply == "pos":
                        return struct.unpack(">Q", self._recv(8))[0]
                    n = struct.unpack(">I", self._recv(4))[0]
                    return json.loads(self._recv(n))
                except (OSError, ValueError, struct.error):
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.sock = None
            return None

    def _send(self, kind, payload):
        self.sock.sendall(struct.pack(">cI", kind, len(payload)) + payload)

    def _recv(self, n):
        b = bytearray()
        while len(b) < n:
            part = self.sock.recv(n - len(b))
            if not part:
                raise OSError("relay closed the feed")
            b += part
        return bytes(b)

    def audio(self, chunk):
        return self._call(b"A", chunk)

    def cut(self):
        return self._call(b"X")

    def trim(self, keep):
        return self._call(b"T", struct.pack(">Q", keep))

    def fmt(self):
        return self._call(b"F", json.dumps({"bitrate": rs.ACTIVE_BITRATE, "burst": rs.BURST}).encode())

    def status(self):
        return self._call(b"S", reply="json")


relay = Relay()
relay_view = {"listeners": {}}                    # the relay's last status: per-listener metrics for listeners_view()


def relay_poll():
    """Mirror the relay's listeners into rs.bc["listeners"] (who's listening, their stream offsets) and collect the
    departures for the listening signals."""
    seen = set()
    while True:
        st = relay.status()
        if st is not None:
            with rs.bc_lock:
                was = bool(rs.bc["listeners"])
                rs.bc["listeners"] = st["listeners"]
                if not st["bridging"]:
                    rs.bc["bytes"] = max(rs.bc["bytes"], st["pos"]) if st["pos"] >= rs.bc["bytes"] - 10 * rs.BPS else st["pos"]
            relay_view["listeners"] = st["listeners"]
            for d in st["departures"]:
                key = (d[0], d[1])
                if key not in seen:
                    seen.add(key); _departures.append(tuple(d))
            if rs.bc["listeners"] and not was:
                rs.bc["wake"].set()
        time.sleep(1)


def set_buffer(seconds):
    rs.BURST = int(seconds) * rs.BPS // rs.FRAME * rs.FRAME
    rs.LEAD = rs.BURST / rs.BPS
    if relay.sock is not None:
        relay.fmt()                                  # the relay keeps that much for each new listener


def _switch_format():
    """Apply a new BITRATE between songs: listeners are dropped (players reconnect by themselves) and the stream
    clock restarts, because byte offsets map to time differently at another bitrate."""
    seconds = rs.LEAD
    with rs.bc_lock:
        rs.BPS, rs.FRAME, rs.ACTIVE_BITRATE = rs.BITRATE * 1000 // 8, 144 * rs.BITRATE * 1000 // 48000, rs.BITRATE
        rs.ENCODE[rs.ENCODE.index("-b:a") + 1] = f"{rs.BITRATE}k"
        set_buffer(seconds)
        rs.bc["listeners"] = {}
        rs.bc.update(bytes=relay.fmt() or 0, paced=0, epoch=time.time(), aired=[], now=None)   # the relay drops its listeners
    _make_silence()
    app.log(f"radio: stream now {rs.BITRATE} kbit/s")


def _make_silence():
    try:
        rs.bc["silence"] = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                        "anullsrc=r=48000:cl=stereo", "-t", "1.152", *rs.ENCODE], capture_output=True, timeout=30).stdout
    except Exception:
        app.log("radio: could not make the silence frames:", traceback.format_exc())


COMPRESSORS = {"light": "acompressor=threshold=-18dB:ratio=2:attack=20:release=250:makeup=2",
               "medium": "acompressor=threshold=-20dB:ratio=3:attack=10:release=200:makeup=4",
               "heavy": "acompressor=threshold=-24dB:ratio=6:attack=5:release=150:makeup=7"}


def audio_filters(seconds):
    """ffmpeg -af chain for one song: tone, stereo width, compression, loudness, limiter, fades."""
    f = []
    if rs.BASS_DB:
        f.append(f"bass=g={rs.BASS_DB:g}:f=100")
    if rs.TREBLE_DB:
        f.append(f"treble=g={rs.TREBLE_DB:g}:f=8000")
    if rs.STEREO_WIDTH == 0:
        f.append("pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1")
    elif rs.STEREO_WIDTH != 1:
        f.append(f"extrastereo=m={rs.STEREO_WIDTH:g}:c=false")
    if rs.COMPRESSION in COMPRESSORS:
        f.append(COMPRESSORS[rs.COMPRESSION])
    if rs.LOUDNESS is not None:
        f.append(f"loudnorm=I={rs.LOUDNESS:g}:TP=-1.5:LRA=11")
    if rs.LIMITER:
        f.append("alimiter=limit=0.95:level=false")
    if rs.FADE_IN:
        f.append(f"afade=t=in:d={rs.FADE_IN:g}")
    if rs.FADE_OUT and seconds and seconds > rs.FADE_OUT * 2:
        f.append(f"afade=t=out:st={seconds - rs.FADE_OUT:.2f}:d={rs.FADE_OUT:g}")
    return ["-af", ",".join(f)] if f else []


def _broadcast(chunk):
    """Pace the stream at real time (bytes paced == elapsed seconds * BPS), then feed the chunk to the stream relay.
    The stream runs LEAD seconds ahead of the clock, so every player - also the first one after the stream was
    idle, or one that just reconnected - holds that much audio in its buffer."""
    ahead = (rs.bc["paced"] + len(chunk)) / rs.BPS - (time.time() - rs.bc["epoch"]) - rs.LEAD
    if ahead > 0:
        time.sleep(ahead)
    with rs.bc_lock:
        if ahead < -1.0 - rs.LEAD:         # we were idle (no listeners) or just started: restart the clock, send LEAD at once
            rs.bc["epoch"] = time.time() - (rs.bc["paced"] + len(chunk)) / rs.BPS
        rs.bc["paced"] += len(chunk)
        rs.bc["last"] = time.time()
    pos = relay.audio(chunk)                    # the relay's stream position after this chunk (None: relay is down)
    with rs.bc_lock:
        rs.bc["bytes"] = pos if pos is not None else rs.bc["bytes"] + len(chunk)


def _air(entry):
    with rs.bc_lock:
        entry = dict(entry or {"id": None}, start_pos=round(rs.bc["bytes"] / rs.BPS, 3))
        rs.bc["now"] = entry if entry.get("id") else None
        rs.bc["aired"] = (rs.bc["aired"] + [entry])[-8:]
    if entry.get("id") and entry.get("airing") != "history":
        with rs.nav_lock:
            if not rs.nav["hist"] or rs.nav["hist"][-1] != entry["id"]:     # the same song twice in a row is one step back
                rs.nav["hist"].append(entry["id"])
    if entry.get("id"):
        rlb.log_listen(entry)               # playout only runs while someone listens, so every airing was heard


def _play_track(t):
    d = app.DATA / t["id"]
    src = d / "song.mp3"
    if not src.is_file():
        src = d / "song" / "audio.flac"
    proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src), *audio_filters(t.get("seconds")),
                             *rs.ENCODE], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    rs.bc["skip"].clear()
    if t.get("tuned", t.get("station")) == rs.state["station"]:
        rs.bc["retuned"] = False                  # the new station's own song is on: nothing left to cut over
    if rs.bc["cut_next"]:                     # skipped or retuned: drop the old song's buffered audio and refill LEAD at once,
        pos = relay.cut()                     # so players that reconnect start right on this song with a full cushion
        with rs.bc_lock:
            rs.bc["epoch"] = time.time() - rs.bc["paced"] / rs.BPS
            if pos is not None:
                rs.bc["bytes"] = pos
            rs.bc["cut"] += 1; rs.bc["cut_next"] = False
    _air(t | {"duration": t.get("seconds")})
    app.log("radio on air:", t["title"], f"({len(rs.bc['listeners'])} listening)")
    checked, start, pending, empty_since = 0.0, rs.bc["bytes"], b"", None
    aired_at, present, finished = time.time(), set(rs.bc["listeners"]), False
    try:
        while True:
            chunk = os.read(proc.stdout.fileno(), 8192)
            if not chunk:
                finished = True                    # the whole song went out
                break
            pending += chunk                       # only send whole MP3 frames so a skip never leaves a torn frame
            n = len(pending) // rs.FRAME * rs.FRAME
            if not n:
                continue
            _broadcast(pending[:n]); pending = pending[n:]
            if rs.bc["skip"].is_set() or not rs.state["on"]:
                rs.bc["cut_next"] = rs.state["on"]
                break
            if rs.bc["listeners"]:
                empty_since = None
            elif empty_since is None:
                empty_since = time.time()
            elif time.time() - empty_since > 5:    # everyone left (a player reconnecting after ⏮/⏭ is back within a second)
                if (rs.bc["bytes"] - start) / rs.BPS < 30 and t.get("airing") == "new":   # barely started -> back on the spool
                    with app.lock:
                        j = app.jobs.get(t["id"])
                        if j:
                            j.update(radio_status="approved", played_at=None); app.save_job(j)
                break
            if time.time() - checked > 1:          # retuned to another station: cut over once it has a song ready
                checked = time.time()               # (only a real retune: a filler rerun plays to its end when a new song is ready)
                if rs.bc.get("retuned") and t.get("tuned", t.get("station")) != rs.state["station"] and rsp.station_tracks(rs.state["station"]):
                    rs.bc["retuned"] = False
                    rs.bc["cut_next"] = True
                    break
    finally:
        proc.kill(); proc.wait()
        try:
            _listening_signal(t, aired_at, present, finished and not rs.bc["skip"].is_set())
        except Exception:
            app.log("radio listening-signal error:", traceback.format_exc())


# ----------------------------------------------------------------------------- implicit feedback
# Few listeners press the thumbs, so how people listen counts as mild feedback: a song someone heard through to the
# end is a soft like ("heard"); tuning out 10-60 s into a song, after listening for a while, is a soft skip ("left").
# Both feed the taste notes (more weakly than thumbs); explicit thumbs on a song always win.
IMPLICIT_EVERY = 5                      # rewrite the taste notes after this many new listening signals
_departures = collections.deque(maxlen=200)   # (listener id, left at, joined at)


def _listening_signal(t, aired_at, present, finished):
    if not t.get("id") or not present:
        return
    stayed = [lid for lid in present if lid in rs.bc["listeners"]]
    lo, hi = aired_at + rs.LEAD + 10, aired_at + rs.LEAD + 60        # listeners hear the stream LEAD seconds late
    left = [lid for lid, at, joined in list(_departures)
            if lid in present and lid not in rs.bc["listeners"] and lo <= at <= hi and aired_at - joined >= 120]
    if finished and stayed:
        implicit(t["id"], "heard")
    elif left and not rs.bc["skip"].is_set():
        implicit(t["id"], "left")


def implicit(track_id, kind):
    with app.lock:
        j = app.jobs.get(track_id)
    if not j or j.get("source") != "radio" or j.get("radio_feedback"):
        return                                  # thumbs already given: they speak louder
    s = rs.stations.get(j.get("station"))
    if not s:
        return
    with rs.lock:
        s["implicit"] = ([f for f in s.get("implicit", []) if not (f["track"] == track_id and f["kind"] == kind)]
                         + [{"track": track_id, "kind": kind, "t": time.time()}])[-200:]
        s["stats"][kind] = s["stats"].get(kind, 0) + 1
        s["implicit_new"] = s.get("implicit_new", 0) + 1
        due = s["implicit_new"] >= IMPLICIT_EVERY
        if due:
            s["implicit_new"] = 0
        rs.save_station(s)
    with app.lock:
        sig = j.setdefault("listen_signals", {})
        sig[kind] = sig.get(kind, 0) + 1
    app.save_job(j)
    app.log(f"radio: listening signal '{kind}' for {j['title']}")
    if due:
        threading.Thread(target=rsw.update_taste, args=(s["id"],), daemon=True).start()


def _play_silence():
    """Dead air while the radio is off or the next song is still being made (keeps listener connections open)."""
    _air(None)
    while rs.bc["listeners"]:
        for i in range(0, len(rs.bc["silence"]), 8 * rs.FRAME):
            _broadcast(rs.bc["silence"][i:i + 8 * rs.FRAME])
        if rs.nav["queued"] or _upnext_job() or (rs.state["on"] and rs.state["station"] and rlb.next_track(rs.state["station"], mark=False)):   # ⏮, a new song, runner-up or rerun
            return


def playout_loop():
    rs.bc.setdefault("paced", 0)
    _make_silence()
    st = relay.status()                       # carry on from the relay's stream position (it outlives a restart of the radio)
    if st is not None:
        rs.bc["bytes"] = st["pos"]
    threading.Thread(target=relay_poll, daemon=True, name="radio-relay-poll").start()
    while True:
        try:
            if not rs.bc["listeners"]:                 # nobody listening: don't burn songs into the void
                if rs.bc["now"] is not None:
                    _air(None)
                rs.bc["wake"].wait(2); rs.bc["wake"].clear()
                continue
            if rs.BITRATE != rs.ACTIVE_BITRATE:
                _switch_format()
                continue
            t = (_take_queued() or _take_upnext() or _nav_next() or rlb.next_track(rs.state["station"])) if rs.state["on"] and rs.state["station"] else None
            if t:
                _play_track(t)
                for _ in range(round(rs.GAP / 1.152)):       # a little silence between songs
                    if not rs.bc["listeners"] or rs.bc["cut_next"]:     # skipped: straight to the next song
                        break
                    for i in range(0, len(rs.bc["silence"]), 8 * rs.FRAME):
                        _broadcast(rs.bc["silence"][i:i + 8 * rs.FRAME])
            else:
                _play_silence()
        except Exception:
            app.log("radio playout error:", traceback.format_exc()); time.sleep(1)


def stream(h):
    """GET /radio/stream.mp3: the broadcast is served by the stream relay (stream_relay.py) on its own port, so a
    restart of the radio doesn't disconnect listeners. Old links and players (VLC, the classic deck) follow this."""
    query = h.path.split("?", 1)[1] if "?" in h.path else ""
    host = (h.headers.get("Host") or "127.0.0.1").rsplit(":", 1)[0]
    h.send_response(307)
    h.send_header("Location", f"//{host}:{RELAY_PORT}/stream.mp3" + (f"?{query}" if query else ""))   # same http/https
    h.send_header("Content-Length", "0"); h.send_header("Cache-Control", "no-store")
    h.end_headers()


def listeners_view(me=None):
    """Per-listener metrics from the relay: what it delivered over the last ~10 s and how much is still queued."""
    now, out = time.time(), []
    for lid, li in list(relay_view["listeners"].items()):
        out.append({"id": lid[:6], "me": lid == me, "device": li.get("device", "?"), "seconds": round(now - li["since"]),
                    "kbps": li["kbps"], "sent_mb": round(li["sent"] / 2**20, 1), "sent": li["sent"], "backlog_s": li["backlog_s"]})
    return sorted(out, key=lambda x: (not x["me"], x["seconds"]))


_noise_cache = b""


def _noise(n):
    """Incompressible bytes for the speed test (cached)."""
    global _noise_cache
    if len(_noise_cache) < n:
        _noise_cache = os.urandom(n)
    return _noise_cache[:n]


def skip(track_id, kind="skip"):
    """Skip (or NOPE) the song on air - for everyone, it's one broadcast. After ⏮, ⏭ steps forward through the history."""
    if track_id and (kind != "skip" or rs.nav["cursor"] is None):      # stepping through the history isn't a skip vote
        rlb.feedback(track_id, kind)
    now, cut = rs.bc["now"], rs.bc["cut"]
    if now and track_id and now["id"] != track_id and now.get("start_pos") is not None:
        # the listener still hears the song before (the stream runs LEAD ahead) and the next one is already on air:
        # drop the old song's tail from the buffer, so a reconnecting player starts on the current song
        keep = round((rs.bc["bytes"] / rs.BPS - now["start_pos"]) * rs.BPS / rs.FRAME) * rs.FRAME
        if 0 < keep < rs.BURST and relay.trim(keep) is not None:
            with rs.bc_lock:
                rs.bc["cut"] += 1
        return {"ok": True, "cut": rs.bc["cut"]}
    if now and (not track_id or now["id"] == track_id):
        rs.bc["skip"].set()
        return _wait_cut(cut)
    return {"ok": True, "cut": rs.bc["cut"]}


def _wait_cut(cut):
    t0 = time.time()
    while rs.bc["cut"] == cut and time.time() - t0 < 8:      # the next song is on air: players can reconnect to it now
        time.sleep(0.1)
    return {"ok": True, "cut": rs.bc["cut"]}


def _hist_track(i):
    with app.lock:
        j = app.jobs.get(rs.nav["hist"][i])
    if not j or j.get("status") != "done":
        return None
    return rlb.track_view(j) | {"airing": "history", "tuned": rs.state["station"]}


def _upnext_job(take=False):
    """📻 Play next: the first song on the list that has finished recording (songs still recording wait their turn;
    failed or deleted ones drop off). take=True removes it from the list."""
    with rs.lock:
        ids = rs.state.setdefault("upnext", [])
        for jid in list(ids):
            j = app.jobs.get(jid)                     # a plain dict read: no app.lock while holding ours
            if not j or j.get("status") in ("failed", "cancelled"):
                ids.remove(jid); rs.save_state(); continue
            if j.get("status") == "done" and (app.DATA / jid / "song.mp3").is_file():
                if take:
                    ids.remove(jid); rs.save_state()
                return j
    return None


def _take_upnext():
    j = _upnext_job(take=True)
    if j:
        app.log(f"radio: playing next on request: {j['title']!r}")
        return rlb.track_view(j) | {"airing": "request", "tuned": rs.state["station"]}
    return None


def play_next(ids):
    """Add songs (any song in the library, e.g. a Music Studio rewrite) to the front of the broadcast, in order."""
    with app.lock:
        ok = [i for i in ids if isinstance(i, str) and i in app.jobs]
    if not ok:
        raise app.BadRequest("No such song")
    with rs.lock:
        lst = rs.state.setdefault("upnext", [])
        lst.extend(i for i in ok if i not in lst)
        rs.save_state()
    rs.bc["wake"].set()
    return {"ok": True, "upnext": upnext_view()}


def upnext_view():
    out = []
    for jid in rs.state.get("upnext") or []:
        with app.lock:
            j = app.jobs.get(jid)
        if j:
            out.append({"id": jid, "title": j["title"], "ready": j.get("status") == "done", "status": j.get("status")})
    return out


def peek_next():
    """The song the playout loop would air next, without taking it (Now Playing's "up next" cover). Same order as
    playout_loop: a ⏮ pick, 📻 Play next, the history walk, then the station's spool. Random picks (Reruns, a filler
    rerun) aren't known in advance: None."""
    with rs.nav_lock:
        t = rs.nav["queued"]
        c = rs.nav["cursor"]
        if not t and c is not None and c < len(rs.nav["hist"]) - 1:
            t = _hist_track(c + 1)
    if not t:
        j = _upnext_job()
        t = rlb.track_view(j) | {"airing": "request"} if j else None
    sid = rs.state["station"]
    if not t and rs.state["on"] and sid and sid != rs.RERUNS:
        t = rlb.next_track(sid, mark=False)
        if t and t["airing"] not in ("new", "runner-up"):
            t = None
    return {k: v for k, v in t.items() if k not in ("lyrics", "critic")} if t else None


def _take_queued():
    with rs.nav_lock:
        t, rs.nav["queued"] = rs.nav["queued"], None
    return t


def _nav_next():
    """The next song while walking the history (after ⏮); None once past the newest one: new songs again."""
    with rs.nav_lock:
        while rs.nav["cursor"] is not None and rs.nav["cursor"] < len(rs.nav["hist"]) - 1:
            rs.nav["cursor"] += 1
            t = _hist_track(rs.nav["cursor"])
            if t:
                return t
        rs.nav["cursor"] = None
    return None


def back(track_id=None, steps=0):
    """⏮: steps=0 restarts the song (the one the caller hears), steps=N goes N songs further back."""
    with rs.nav_lock:
        hist = rs.nav["hist"]
        if len(hist) < 2 and rs.state["station"]:          # just restarted: rebuild the history from the station's play log
            older, keep = [t["id"] for t in reversed(rlb.history(rs.state["station"], 50))], list(hist)   # oldest first
            hist.clear(); hist.extend([x for x in older if x not in keep] + keep)
        if not hist:
            return {"ok": False, "cut": rs.bc["cut"], "error": "Nothing has played yet"}
        if rs.nav["cursor"] is None or (track_id and hist[rs.nav["cursor"]] != track_id):
            idx = [k for k, x in enumerate(hist) if x == track_id]
            rs.nav["cursor"] = idx[-1] if idx else len(hist) - 1
        c = max(0, rs.nav["cursor"] - max(0, steps))
        t = _hist_track(c)
        while not t and c > 0:                          # a deleted song: one further back
            c -= 1; t = _hist_track(c)
        if not t:
            return {"ok": False, "cut": rs.bc["cut"], "error": "That song isn't available any more"}
        rs.nav["cursor"], rs.nav["queued"] = c, t
    app.log(f"radio: ⏮ steps={steps} heard={track_id} -> {t['title']!r} ({c + 1} of {len(hist)} in the history)")
    cut = rs.bc["cut"]
    rs.bc["cut_next"] = True
    rs.bc["skip"].set()
    out = _wait_cut(cut)
    return out | {"title": t["title"], "first": c == 0}


def stream_view(listener=None):
    with rs.bc_lock:
        li = rs.bc["listeners"].get(listener) if listener else None
        return {"pos": round(rs.bc["bytes"] / rs.BPS, 3), "listeners": len(rs.bc["listeners"]), "aired": rs.bc["aired"],
                "now": rs.bc["now"], "p0": li["p0"] if li else None, "url": "/radio/stream.mp3",
                "clock": round(time.time() - rs.bc["epoch"], 3) if rs.bc["listeners"] else None,   # real-time position
                "bitrate": rs.ACTIVE_BITRATE, "lead": round(rs.LEAD, 1), "cut": rs.bc["cut"], "relay_port": RELAY_PORT}
