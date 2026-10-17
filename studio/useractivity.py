"""User activity for the radio and Music Studio (port 7860), written to the shared activity log (logsetup.activity).
The radio has no accounts of its own: the Studio's sign-in cookie (one per machine name, shared across ports) tells
who's here, by asking the Studio (cached). Not signed in shows as user null, with the device."""
import json, re, threading, time, urllib.request

import logsetup

STUDIO = "http://127.0.0.1:7871"     # Agentora Home holds the accounts
_cache, _lock = {}, threading.Lock()


def user_of(h):
    m = re.search(r"(?:^|;\s*)gs_session=([^;]+)", h.headers.get("Cookie") or "")
    if not m:
        return None
    tok = m.group(1)
    with _lock:
        hit = _cache.get(tok)
        if hit and time.time() - hit[1] < 120:
            return hit[0]
    try:
        r = urllib.request.Request(STUDIO + "/api/session", headers={"Cookie": "gs_session=" + tok})
        name = (json.loads(urllib.request.urlopen(r, timeout=3).read()).get("user") or {}).get("name")
    except Exception:
        name = hit[0] if hit else None
    with _lock:
        _cache[tok] = (name, time.time())
        if len(_cache) > 500:
            _cache.clear()
    return name


def title(track):
    import app
    with app.lock:
        j = app.jobs.get(track or "")
        return j and j.get("title")


def station(sid):
    import radio_state as rs
    s = rs.stations.get(sid or "")
    return s and s["name"]


def _event(method, path, b):
    """(event, details) for a request that changes something, else None."""
    if method == "POST":
        if path == "/api/radio/feedback":
            return {"keep": "👍", "dislike": "👎", "skip": "⏭ skip", "clear": "clear thumb"}.get(b.get("kind"), "feedback " + str(b.get("kind"))), {"song": title(b.get("track"))}
        if path == "/api/radio/skip":
            return "⏭ skip", {"song": title(b.get("track"))}
        if path == "/api/radio/back":
            return "⏮ back", {}
        if path == "/api/radio/power":
            return ("tune in" if b.get("on") else "turn the radio off"), {"station": station(b.get("station"))}
        if path == "/api/radio/upnext":
            return "play next", {"songs": len(b.get("ids") or []), "song": title((b.get("ids") or [""])[0])}
        if path == "/api/radio/playlist":
            return ("add to playlist" if b.get("on") else "remove from playlist"), {"song": title(b.get("track"))}
        if path == "/api/radio/stations":
            return "create a station", {"station": b.get("name")}
        if path == "/api/radio/stations/assist":
            return "station ideas (" + str(b.get("mode")) + ")", {}
        if path == "/api/radio/art":
            return "new cover", {"song": title(b.get("track")), "station": station(b.get("station"))}
        if path == "/api/radio/history/clear":
            return "clear listen history", {}
        m = re.fullmatch(r"/api/radio/stations/(\w+)/(song|analyze)", path)
        if m:
            return {"song": "create a song now", "analyze": "learn from music"}[m.group(2)], {"station": station(m.group(1))}
        if path == "/api/jobs":
            return "make a song (Music Studio)", {"rewrite_of": title(b.get("rewrite_of"))}
        if path == "/api/lyrics":
            return "write lyrics", {}
        if path == "/api/rewrite/suggest":
            return "rewrite ideas", {"song": title(b.get("id"))}
        if path.startswith("/api/assistant/"):
            return "chat with Hermes", {"model": b.get("model")}
        if path == "/api/gpu/priority":
            return "set GPU priority", {"priority": b.get("priority")}
        if path == "/api/gpu/release":
            return "free the GPU", {}
        if re.fullmatch(r"/api/jobs/[\w-]+/cancel", path):
            return "cancel a song", {}
    if method == "PUT":
        m = re.fullmatch(r"/api/radio/stations/(\w+)", path)
        if m:
            return "edit a station", {"station": station(m.group(1)), "variety": b.get("variety")}
        if path == "/api/radio/settings":
            return "change radio settings", {}
    if method == "DELETE":
        m = re.fullmatch(r"/api/radio/stations/(\w+)(/taste)?", path)
        if m:
            return ("forget taste" if m.group(2) else "delete a station"), {"station": station(m.group(1))}
        if re.fullmatch(r"/api/jobs/[\w-]+", path):
            return "delete a song", {}
    return None


def note(h, code):
    """Called once per answered request (Handler.send)."""
    if getattr(h, "_noted", False):
        return
    h._noted = True
    path, _, query = h.path.partition("?")
    method = h.command
    w = {"ip": logsetup.real_ip(*h.client_address[:2]), "ua": h.headers.get("User-Agent")}
    if method == "GET":
        if path in ("/", "/radio", "/radio/classic"):
            return logsetup.activity("radio", "open", user_of(h), **w, every=600, key=path,
                                     page={"/": "Music Studio", "/radio": "Radio", "/radio/classic": "Radio (classic)"}[path])
        if path == "/api/radio/listeners" and "me=" in query:       # an open radio page with the stream playing
            return logsetup.activity("radio", "listening", user_of(h), **w, every=600, key="listening",
                                     listener=query.split("me=", 1)[1][:12])
        if path.startswith("/api/") and h.headers.get("Cookie"):
            return logsetup.activity("radio", "active", user_of(h), **w, every=600)
        return
    ev = _event(method, path, getattr(h, "_json", None) or {})
    if ev:
        logsetup.activity("radio", ev[0], user_of(h), **w, status=code if code >= 400 else None, **ev[1])
