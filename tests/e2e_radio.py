#!/usr/bin/env python3
"""End-to-end test of a running radio + Music Studio (and the Image & Video Studio's views of it).

Run it while nobody is listening: it plays the stream, skips, goes back and queues a song, which moves the
broadcast for everyone. Everything it changes (a setting, a playlist entry, a thumb) is put back.
Usage: tests/e2e_radio.py [--radio http://127.0.0.1:7860] [--home http://127.0.0.1:7871] [--quick]
--quick skips the broadcast tests (stream, skip, back, play next); --full adds the AI helpers and makes one song
start to finish (lyrics, GPU recording, critic) - several minutes.
"""
import argparse, json, re, subprocess, sys, threading, time, urllib.error, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--radio", default="http://127.0.0.1:7860")
ap.add_argument("--home", default="http://127.0.0.1:7871")
ap.add_argument("--quick", action="store_true")
ap.add_argument("--allow-listeners", action="store_true")
ap.add_argument("--full", action="store_true", help="also the AI helpers and one song made start to finish (several minutes)")
ap.add_argument("--restart", action="store_true", help="restart the radio while listening: the stream must not stop")
A = ap.parse_args()
R, S = A.radio, A.home          # S: Agentora Home (accounts, /home, now playing)
results = []


def req(base, path, body=None, method=None, raw=False, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data, {"Content-Type": "application/json", "Origin": base} if data else {},
                               method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        out = resp.read()
        return (resp.status, out) if raw else json.loads(out)


def check(name, fn):
    t0 = time.time()
    try:
        detail = fn()
        results.append((True, name, detail or "")); print(f"  ok   {name}  {detail or ''}  ({time.time() - t0:.1f}s)")
    except Exception as e:
        results.append((False, name, repr(e)[:300])); print(f"  FAIL {name}  {repr(e)[:300]}")


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


start = time.strftime("%Y-%m-%d %H:%M:%S")
st = req(R, "/api/radio/state")
listeners = req(R, "/api/radio/listeners")["listeners"]
if listeners and not A.quick and not A.allow_listeners:
    sys.exit(f"{len(listeners)} listening right now: run with --quick, or wait (the broadcast tests move the stream)")

print("pages")
for base, path in ((R, "/radio"), (R, "/"), (R, "/radio/classic"), (R, "/agentora/agentora.js"), (R, "/agentora/agentora.css"), (S, "/home"), (S, "/agentora/agentora.js")):
    check(f"GET {base[-5:]}{path}", lambda base=base, path=path: must(req(base, path, raw=True)[0] == 200, "not 200"))

print("radio state and library")
SID = st["station"] or st["stations"][0]["id"]
check("state", lambda: (must(st["stations"], "no stations"), f"{len(st['stations'])} stations, on={st['on']}, station={SID}")[1])
songs = []
def all_songs():
    songs.extend(req(R, "/api/radio/songs")["tracks"]); must(len(songs) > 0, "no songs")
    return f"{len(songs)} songs ({sum(1 for t in songs if t['source'] != 'radio')} from the Music Studio)"
check("all songs", all_songs)
check("station songs", lambda: f"{len(req(R, f'/api/radio/stations/{SID}/history?limit=500&upcoming=1')['tracks'])} on {SID}")
def variety_setting():   # 🎲 Variety: saved on create and edit, shown in the station list, bad values refused
    sid = req(R, "/api/radio/stations", {"name": "e2e variety test", "description": "test station, warm lo-fi piano", "variety": 70})["station"]["id"]
    try:
        must(next(s for s in req(R, "/api/radio/state")["stations"] if s["id"] == sid)["variety"] == 70, "variety not saved on create")
        must(req(R, f"/api/radio/stations/{sid}", {"variety": 0}, method="PUT")["station"]["variety"] == 0, "variety not saved on edit")
        try:
            req(R, f"/api/radio/stations/{sid}", {"variety": "lots"}, method="PUT"); raise AssertionError("bad variety accepted")
        except urllib.error.HTTPError as e:
            must(e.code == 400, f"bad variety gave {e.code}")
        return "create 70, edit 0, 'lots' refused"
    finally:
        req(R, f"/api/radio/stations/{sid}", method="DELETE")
check("🎲 station variety", variety_setting)
check("playlist", lambda: f"{len(req(R, '/api/radio/playlist')['tracks'])} saved")
check("listen history", lambda: f"{len(req(R, '/api/radio/listened?limit=50')['tracks'])} recent")
check("favorites", lambda: f"{len(req(R, '/api/radio/favorites')['tracks'])} kept")
check("song page", lambda: req(R, f"/api/radio/tracks/{songs[0]['id']}")["title"])
check("gpu / disk / priority", lambda: "{} MiB used, {:.0f} GB free, priority {}".format(
    req(R, "/api/radio/gpu").get("used_mb"), req(R, "/api/disk")["free"] / 2 ** 30, req(R, "/api/gpu/priority")["priority"]))
check("listeners", lambda: f"{len(req(R, '/api/radio/listeners')['listeners'])} connected")

print("settings (changed and put back)")
def settings_roundtrip():   # the stream buffer: settings.py -> the running radio (reported back as the stream's lead)
    v = req(R, "/api/radio/settings")["values"]; old = v["buffer_seconds"]; new = old - 1
    req(R, "/api/radio/settings", {"buffer_seconds": new}, method="PUT")
    lead = req(R, "/api/radio/state")["stream"]["lead"]
    req(R, "/api/radio/settings", {"buffer_seconds": old}, method="PUT")
    back = req(R, "/api/radio/state")["stream"]["lead"]
    must(abs(lead - new) < 0.1 and abs(back - old) < 0.1, f"lead {lead} (wanted {new}), then {back} (wanted {old})")
    return f"buffer {old} s -> {new} s -> {old} s, live in the stream; bitrate {v['stream_bitrate']}, takes {v['takes']}"
check("settings change + revert", settings_roundtrip)

print("playlist and feedback (put back)")
def playlist_roundtrip():
    t = next(x for x in songs if not x["in_playlist"])
    req(R, "/api/radio/playlist", {"track": t["id"], "on": True})
    on = any(x["id"] == t["id"] for x in req(R, "/api/radio/playlist")["tracks"])
    req(R, "/api/radio/playlist", {"track": t["id"], "on": False})
    off = not any(x["id"] == t["id"] for x in req(R, "/api/radio/playlist")["tracks"])
    must(on and off, f"on={on} off={off}"); return t["title"]
check("playlist add + remove", playlist_roundtrip)
def feedback_roundtrip():   # a Music Studio song with no station: no taste notes get rewritten
    t = next((x for x in songs if x["source"] != "radio" and not x.get("feedback") and not x.get("station")), None)
    if not t:
        return "skipped: no unrated Music Studio song to try it on (a radio song would rewrite a station's taste notes)"
    req(R, "/api/radio/feedback", {"track": t["id"], "kind": "keep"})
    kept = req(R, f"/api/radio/tracks/{t['id']}")["feedback"] == "keep"
    req(R, "/api/radio/feedback", {"track": t["id"], "kind": "clear"})
    cleared = req(R, f"/api/radio/tracks/{t['id']}")["feedback"] is None
    must(kept and cleared, f"kept={kept} cleared={cleared}"); return t["title"]
check("thumbs up + clear", feedback_roundtrip)

print("GPU broker")
B = "http://127.0.0.1:7890"
def broker_status():
    d = req(B, "/status"); must(d["devices"], "no GPUs seen")
    return f"priority {d['priority']}, " + ", ".join(f"GPU {x['index']} {x['free_mb'] // 1024} GB free" for x in d["devices"]) + f", {len(d['leases'])} leases"
check("broker sees the GPU", broker_status)
def broker_yield():   # a person waiting (a Studio lease) makes the radio step aside; releasing it lets the radio go on
    y0 = req(B, "/yield?client=radio")                 # (another lease may already hold the card)
    lease = req(B, "/lease", {"client": "studio", "kind": "interactive", "mem_mb": 20000, "wait_s": 0, "ttl_s": 30})
    y1 = req(B, "/yield?client=radio")
    req(B, f"/lease/{lease['id']}", method="DELETE")
    y2 = req(B, "/yield?client=radio")
    must(lease["granted"] and y1["yield"], f"lease {lease}, yield while held {y1}")
    must(y2["yield"] == y0["yield"], f"after release {y2}, before {y0}")
    return f"radio waits while the Studio holds the card ({y1['reason']}), and goes on after"
check("leases make the radio step aside", broker_yield)
def broker_cards():   # Hermes's 35B lives on the Radeon (when its Ollama is up) and an unload through the proxy leaves it there
    d = req(B, "/status")
    amd = next((x for x in d["devices"] if x.get("vendor") == "amd"), None)
    if not amd or not amd["up"]:
        return "skipped: no Radeon Ollama"
    must(d["placement"].get("qwen3.6-agent-moe") == "amd", f"35B placed on {d['placement'].get('qwen3.6-agent-moe')}")
    before = "qwen3.6-agent-moe" in d["loaded"].get("amd", [])
    r = req("http://127.0.0.1:11435", "/api/generate", {"model": "qwen3.6-agent-moe", "keep_alive": 0})
    must(r.get("done_reason") == "unload", f"unload answered {r}")
    loaded = req(B, "/status")["loaded"].get("amd", [])
    must("qwen3.6-agent-moe" in loaded or not before, f"35B was unloaded from the Radeon: {loaded}")
    return f"35B on the Radeon ({amd['free_mb'] // 1024} GB free there), kept loaded after an unload request"
check("Radeon runs Hermes's model", broker_cards)
check("Ollama proxy answers (Hermes's way in)",lambda: (lambda d: (must(any(m["id"] == "agentora-auto" for m in d["data"]), "no auto model"), f"{len(d['data'])} models incl. agentora-auto")[1])(req("http://127.0.0.1:11435", "/v1/models")))

print("speech service (voice on the GPU under a lease, listening on the CPU)")
SP = "http://127.0.0.1:7895"
def speech_roundtrip():
    sentence = "The quick brown fox jumps over the lazy dog."
    r = urllib.request.Request(SP + "/v1/audio/speech", json.dumps({"input": sentence, "voice": "agentora", "response_format": "mp3"}).encode(),
                               {"Content-Type": "application/json"})
    t0 = time.time(); audio = urllib.request.urlopen(r, timeout=300).read(); spoke = time.time() - t0
    must(len(audio) > 5000, f"only {len(audio)} bytes of audio")
    boundary = "e2eboundary"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n").encode() \
        + audio + f"\r\n--{boundary}--\r\n".encode()
    r = urllib.request.Request(SP + "/v1/audio/transcriptions", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    text = json.loads(urllib.request.urlopen(r, timeout=120).read())["text"]
    words = lambda x: set(re.findall(r"[a-z]+", x.lower()))
    overlap = len(words(text) & words(sentence)) / len(words(sentence))
    must(overlap >= 0.8, f"heard {text!r}")
    leased = any(l["client"] == "speech" for l in req("http://127.0.0.1:7890", "/status")["leases"])
    must(leased, "the voice isn't holding a GPU lease")
    return f"spoke in {spoke:.1f} s, heard back {overlap:.0%} of the words, voice under a GPU lease"
check("speak, then listen (round trip)", speech_roundtrip)

print("Home's views of the radio")
check("home summary", lambda: (lambda h: (must(h.get("radio") is not None, "no radio in /api/home"), f"radio on={h['radio']['on']}, {h['radio']['listeners']} listening")[1])(req(S, "/api/home")))
check("now-playing bar", lambda: str(req(S, "/api/nowplaying").get("on")))

if not A.quick:
    print("broadcast (plays, skips, goes back, queues a song)")
    if not st["on"]:
        req(R, "/api/radio/power", {"on": True, "station": SID})
    got = {"bytes": 0, "t0": 0.0, "stop": False}
    def listen():
        r = urllib.request.urlopen(R + "/radio/stream.mp3?l=e2etest", timeout=60)
        got["t0"] = time.time()
        while not got["stop"]:
            chunk = r.read(8192)
            if not chunk:
                break
            got["bytes"] += len(chunk)
    th = threading.Thread(target=listen, daemon=True); th.start()
    def on_air():
        return ((req(R, "/api/radio/state").get("stream") or {}).get("now") or {}).get("title")
    def wait_on_air(max_s=60):
        t0 = time.time()
        while time.time() - t0 < max_s:
            if on_air():
                return on_air()
            time.sleep(1)
        raise AssertionError("nothing on air")
    check("stream starts", lambda: wait_on_air())
    def rate():
        b0, t0 = got["bytes"], time.time(); time.sleep(20)
        kbps = (got["bytes"] - b0) * 8 / 1000 / (time.time() - t0)
        must(kbps > 150, f"{kbps:.0f} kbit/s"); return f"{kbps:.0f} kbit/s over 20 s"
    check("stream delivers in real time", rate)
    def skip():
        before = on_air(); cut = req(R, "/api/radio/state")["stream"].get("cut")
        r = req(R, "/api/radio/skip", {"track": None})
        time.sleep(2); after = on_air()
        must(after and after != before and r.get("cut") != cut, f"{before!r} -> {after!r}, cut {cut} -> {r.get('cut')}")
        return f"{before!r} -> {after!r}"
    check("skip", skip)
    def back():
        cur = on_air(); time.sleep(3)
        req(R, "/api/radio/back", {"steps": 0}); time.sleep(2); again = on_air()
        req(R, "/api/radio/back", {"steps": 1}); time.sleep(2); prev = on_air()
        must(again == cur and prev and prev != cur, f"restart {cur!r} -> {again!r}, back -> {prev!r}")
        return f"restart {again!r}, previous {prev!r}"
    check("back (restart, previous)", back)
    def play_next():
        t = next(x for x in songs if x["source"] != "radio")
        req(R, "/api/radio/upnext", {"ids": [t["id"]]})
        req(R, "/api/radio/skip", {"track": None}); time.sleep(3)
        must(on_air() == t["title"], f"on air {on_air()!r}, wanted {t['title']!r}"); return t["title"]
    check("play next", play_next)
    got["stop"] = True

if A.full:
    print("AI helpers and a whole song (several minutes)")
    check("✨ Improve a station description", lambda: (lambda d: (must((d.get("improved") or {}).get("description"), d), d["improved"]["description"][:60])[1])(
        req(R, "/api/radio/stations/assist", {"mode": "improve", "name": "Test", "description": "chill jazz for rainy days"}, timeout=240)))
    check("✏️ rewrite ideas", lambda: (lambda d: (must(d.get("suggestions"), d), f"{len(d['suggestions'])} ideas via {d.get('source')}")[1])(
        req(R, "/api/rewrite/suggest", {"id": songs[0]["id"]}, timeout=240)))
    def whole_song():
        t0 = time.strftime("%Y-%m-%d %H:%M:%S")
        req(R, f"/api/radio/stations/{SID}/song", {})
        deadline = time.time() + 1500
        while time.time() < deadline:
            log = subprocess.run(["journalctl", "--user", "-u", "yue2-studio", "--since", t0, "--no-pager"], capture_output=True, text=True).stdout
            done = [l for l in log.splitlines() if "radio critic" in l and ("approved" in l or "rejected" in l or "runner-up" in l)]
            if done:
                return done[-1].split("radio critic", 1)[1].strip()[:120]
            time.sleep(15)
        raise AssertionError("no song was judged within 25 minutes")
    check("a song: lyrics -> GPU takes -> critic", whole_song)

if A.restart:
    print("restart the radio while listening (the stream relay keeps it going)")
    def survives_restart():
        times, state = [], {"closed": None}
        def listen():
            try:
                r = urllib.request.urlopen(R + "/radio/stream.mp3?l=e2erestart", timeout=60)
                while not state.get("stop"):
                    chunk = r.read(4096)
                    if not chunk:
                        state["closed"] = "server closed the stream"; return
                    times.append((time.time(), len(chunk)))
            except Exception as e:
                state["closed"] = repr(e)
        th = threading.Thread(target=listen, daemon=True); th.start()
        time.sleep(20)                                     # past the initial burst: steady real time
        t_restart = time.time()
        subprocess.run(["systemctl", "--user", "restart", "yue2-studio"], check=True)
        time.sleep(45)
        state["stop"] = True
        after = [t for t, n in times if t > t_restart]
        gaps = [b - a for a, b in zip(after, after[1:])]
        got = sum(n for t, n in times if t > t_restart)
        must(state["closed"] is None, f"listener disconnected: {state['closed']}")
        must(after and max(gaps or [0]) < 3, f"audio stopped for {max(gaps or [99]):.1f} s")
        kbps = got * 8 / 1000 / (time.time() - t_restart)
        must(kbps > 150, f"only {kbps:.0f} kbit/s during the restart")
        for _ in range(60):                                # and the radio is back on air
            try:
                if ((req(R, "/api/radio/state").get("stream") or {}).get("now") or {}).get("title"):
                    break
            except Exception:
                pass
            time.sleep(2)
        else:
            raise AssertionError("nothing on air 2 minutes after the restart")
        return f"stayed connected, longest pause {max(gaps or [0]):.1f} s, {kbps:.0f} kbit/s through the restart"
    check("stream survives a radio restart", survives_restart)

print("server log")
def log_clean():
    out = subprocess.run(["journalctl", "--user", "-u", "yue2-studio", "--since", start, "--no-pager"], capture_output=True, text=True).stdout
    bad = [l for l in out.splitlines() if "Traceback" in l or ("Error" in l and "radio" in l and "connection closed (" not in l
                                                                 and "BrokenPipe" not in l and "muxer" not in l)]
    must(not bad, " | ".join(bad[:3])); return f"{len(out.splitlines())} lines, no tracebacks"
check("no errors since the test started", log_clean)

fails = [r for r in results if not r[0]]
print(f"\n{len(results) - len(fails)} passed, {len(fails)} failed")
sys.exit(1 if fails else 0)
