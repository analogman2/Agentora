#!/usr/bin/env python3
"""Agentora stream relay: holds the radio's listeners so a restart of the radio doesn't cut the music.

The radio (studio/app.py + radio_*.py) chooses, encodes and paces songs and feeds the MP3 bytes here over a local
socket; this process fans them out to every listener on GET /stream.mp3 (port 7861). It owns what has to survive a
restart of the radio: the listener connections, the burst buffer a new listener starts with (the 15 s cushion), and
the stream position (byte offset), which the radio uses to tell players which song is audible. If the feed stops for
almost a whole cushion (the radio restarting takes ~10 s), it plays silence until the radio is back, so players never
run dry or disconnect.

Feed protocol (127.0.0.1:7862, one feeder at a time): a message is 1 type byte + 4-byte big-endian length + payload.
  A <mp3 bytes>          audio: append, send to listeners          -> 8-byte stream position after it
  X                      cut: drop the buffered audio (⏭ / ⏮ / retune) -> position
  T <8-byte keep>        trim the buffer to its last `keep` bytes   -> position
  F {"bitrate", "burst"} format: a new bitrate drops listeners and restarts the position; burst = cushion bytes -> position
  S                      status                                      -> 4-byte length + JSON
Stdlib only. Usage: stream_relay.py [--port 7861] [--feed-port 7862] [--host 127.0.0.1]
"""
import argparse, collections, json, logging, queue, re, socket, struct, subprocess, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=7861)
ap.add_argument("--feed-port", type=int, default=7862)
A = ap.parse_args()

lock = threading.Lock()
S = {"bitrate": 192, "bps": 24000, "frame": 576, "burst": 15 * 24000, "buf": bytearray(), "pos": 0,
     "last_audio": 0.0, "fed": False, "silence": b"", "bridging": False}
listeners = {}                                  # lid -> {"q", "p0", "since", "device", "agent", "peer", "sent", "samples"}
departures = collections.deque(maxlen=200)      # (lid, left at, joined at)
STALE = 60                                      # after this long without audio the buffer is old news


import logsetup                                 # logging for every component: journal + logs/stream.log


def log(*a):
    msg = " ".join(str(x) for x in a)
    logging.getLogger("stream").log(logsetup.level_of(msg), msg)


def make_silence(bitrate):
    try:
        return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                               "-t", "1.152", "-vn", "-ac", "2", "-ar", "48000", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
                               "-id3v2_version", "0", "-write_xing", "0", "-map_metadata", "-1", "-f", "mp3", "-"], capture_output=True, timeout=30).stdout
    except Exception as e:
        log("couldn't make silence frames:", e)
        return b""


def push(chunk):
    """Append audio to the stream: the burst buffer, the position, every listener's queue. Caller holds the lock."""
    S["pos"] += len(chunk)
    S["buf"] += chunk
    del S["buf"][:-S["burst"] or None]
    for lid, li in list(listeners.items()):
        try:
            li["q"].put_nowait(chunk)
        except queue.Full:                      # a listener that cannot keep up gets dropped (it reconnects)
            listeners.pop(lid, None)


def status():
    now = time.time()
    out = {"pos": S["pos"], "bitrate": S["bitrate"], "burst": S["burst"], "buffered": len(S["buf"]), "fed": S["fed"],
           "bridging": S["bridging"], "since_audio": round(now - S["last_audio"], 1) if S["last_audio"] else None,
           "listeners": {}, "departures": list(departures)}
    for lid, li in listeners.items():
        recent = [x for x in li["samples"] if now - x[0] <= 10]
        rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0]) if len(recent) > 1 and recent[-1][0] > recent[0][0] else 0
        out["listeners"][lid] = {"p0": li["p0"], "since": li["since"], "device": li["device"], "agent": li["agent"],
                                 "sent": li["sent"], "kbps": round(rate * 8 / 1000), "backlog_s": round(li["q"].qsize() * 8192 / S["bps"], 1)}
    return out


# ---------------------------------------------------------------- the feed from the radio
def read_exact(conn, n):
    b = bytearray()
    while len(b) < n:
        part = conn.recv(n - len(b))
        if not part:
            raise ConnectionError("feeder went away")
        b += part
    return bytes(b)


def feeder(conn):
    log("radio connected")
    S["fed"] = True
    try:
        while True:
            kind, n = struct.unpack(">cI", read_exact(conn, 5))
            payload = read_exact(conn, n) if n else b""
            with lock:
                if kind == b"A":
                    if S["bridging"]:
                        S["bridging"] = False; log("radio is back: bridge off")
                    push(payload); S["last_audio"] = time.time()
                elif kind == b"X":
                    S["buf"].clear()
                elif kind == b"T":
                    keep = struct.unpack(">Q", payload)[0]
                    if 0 < keep < len(S["buf"]):
                        del S["buf"][:len(S["buf"]) - keep]
                elif kind == b"F":
                    cfg = json.loads(payload)
                    if cfg.get("bitrate") and cfg["bitrate"] != S["bitrate"]:
                        S["bitrate"] = int(cfg["bitrate"]); S["bps"] = S["bitrate"] * 1000 // 8; S["frame"] = 144 * S["bitrate"] * 1000 // 48000
                        listeners.clear(); S["buf"].clear(); S["pos"] = 0         # players reconnect at the new bitrate
                        S["silence"] = b""
                        log(f"now {S['bitrate']} kbit/s")
                    if cfg.get("burst"):
                        S["burst"] = int(cfg["burst"]) // S["frame"] * S["frame"]
                elif kind == b"S":
                    body = json.dumps(status()).encode()
                    conn.sendall(struct.pack(">I", len(body)) + body)
                    continue
                pos = S["pos"]
            conn.sendall(struct.pack(">Q", pos))
    except (ConnectionError, OSError, ValueError) as e:
        log(f"radio disconnected ({type(e).__name__})")
    finally:
        S["fed"] = False
        conn.close()


def feed_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", A.feed_port)); srv.listen(2)
    while True:
        conn, _ = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=feeder, args=(conn,), daemon=True, name="feeder").start()


def bridge():
    """No audio for almost a whole cushion while people listen (the radio is restarting): play silence in real time."""
    next_at = 0.0
    while True:
        time.sleep(0.05)
        with lock:
            if not listeners or not S["last_audio"]:
                continue
            quiet = time.time() - S["last_audio"]
            cushion = S["burst"] / S["bps"]
            if quiet < max(2.0, cushion - 3):
                continue
            if not S["bridging"]:
                S["bridging"] = True; next_at = time.time(); log(f"no audio for {quiet:.0f} s: bridging with silence")
            if not S["silence"]:
                S["silence"] = make_silence(S["bitrate"])
            if time.time() >= next_at and S["silence"]:
                piece = S["silence"][:8 * S["frame"]]
                push(piece)
                next_at += len(piece) / S["bps"]


# ---------------------------------------------------------------- listeners
def device(ua):
    for key, name in (("VLC", "VLC"), ("Android", "Android"), ("iPhone", "iPhone"), ("Windows", "Windows"), ("Macintosh", "Mac"),
                      ("Linux", "Linux"), ("curl", "curl"), ("Python", "script")):
        if key in ua:
            return name + (" Chrome" if "Chrome" in ua and key != "VLC" else "")
    return ua[:30] or "unknown"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        logging.getLogger("stream.http").debug("%s %s", self.address_string(), fmt % a)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/status":
            with lock:
                body = json.dumps(status()).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path not in ("/stream.mp3", "/radio/stream.mp3", "/stream"):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        q_ = self.path.split("?", 1)[1] if "?" in self.path else ""
        lid = re.sub(r"\W", "", (q_.split("l=", 1)[1] if "l=" in q_ else "").split("&")[0])[:40] or uuid.uuid4().hex[:12]
        q = queue.Queue(maxsize=400)                     # ~2 minutes of slack before a stuck client is dropped
        ua = self.headers.get("User-Agent") or ""
        with lock:
            if time.time() - S["last_audio"] > STALE:
                S["buf"].clear()
            burst = bytes(S["buf"])
            li = listeners[lid] = {"q": q, "p0": (S["pos"] - len(burst)) / S["bps"], "since": time.time(), "device": device(ua),
                                   "agent": ua[:80], "sent": 0, "samples": collections.deque(maxlen=64)}
            bps = S["bps"]
        joined = time.time()
        peer = self.headers.get("X-Forwarded-For") or logsetup.real_ip(*self.client_address[:2])
        log(f"listener {lid[:6]} joined from {peer} ({logsetup.device(peer, ua)}), {len(burst) / bps:.0f} s cushion")
        logsetup.activity("stream", "start listening", None, ip=peer, ua=ua, every=600)   # ⏭/⏮ reconnect: once per 10 min
        why = "left"
        try:
            self.send_response(200)
            for k, v in (("Content-Type", "audio/mpeg"), ("Cache-Control", "no-cache, no-store"), ("Connection", "close"),
                         ("Access-Control-Allow-Origin", "*"), ("icy-name", "Agentora"), ("X-Content-Type-Options", "nosniff")):
                self.send_header(k, v)
            self.end_headers()
            if burst:
                self.wfile.write(burst); li["sent"] += len(burst); li["samples"].append((time.time(), li["sent"]))
            while listeners.get(lid) is li:
                try:
                    chunk = q.get(timeout=5)
                except queue.Empty:
                    continue
                self.wfile.write(chunk)
                li["sent"] += len(chunk); li["samples"].append((time.time(), li["sent"]))
            else:
                why = "dropped (too slow to keep up, or a format change)"
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            why = f"connection closed ({type(e).__name__})"
        finally:
            with lock:
                if listeners.get(lid) is li:
                    listeners.pop(lid, None)
                departures.append((lid, time.time(), joined))
            log(f"listener {lid[:6]} {why} after {time.time() - joined:.0f} s")
            if time.time() - joined >= 60:
                logsetup.activity("stream", "listened", None, ip=peer, ua=ua, minutes=round((time.time() - joined) / 60, 1))
        self.close_connection = True


class Server(logsetup.QuietBrokenPipes, ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    logsetup.setup("stream")
    threading.Thread(target=feed_server, daemon=True, name="feed").start()
    threading.Thread(target=bridge, daemon=True, name="bridge").start()
    log(f"listeners on http://{A.host}:{A.port}/stream.mp3, radio feed on 127.0.0.1:{A.feed_port}")
    Server((A.host, A.port), Handler).serve_forever()
