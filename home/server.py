#!/usr/bin/env python3
"""Agentora Home: the Home page, accounts and sign-in for every Agentora app, and the Hermes chat.

The radio and Music Studio (:7860), the Hermes gate (:7880) and the Jim gate (:7885) ask this server who's signed in.
Also serves the shared look (/agentora/agentora.css, .js) and the now-playing bar's API. Listens on 127.0.0.1:7871.
The Image & Video Studio used to live here too; it is its own app now (~/genstudio, :7870) with its own accounts.
"""
import asyncio, hmac, json, os, re, shutil, time
from pathlib import Path

import logging
import aiohttp
from aiohttp import web

import logsetup                     # logging for every component: journal + logs/<component>.log
import auth

HOST = os.environ.get("HOME_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOME_PORT", "7871"))
RADIO = os.environ.get("RADIO_URL", "http://127.0.0.1:7860")
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
HERE = Path(__file__).resolve().parent
http: aiohttp.ClientSession = None


# ---------- GPU and disk, for the Home page and the ☰ panel ----------

async def gpu_state():
    state = {"used_mb": None, "total_mb": None, "radio": None, "ollama": []}
    try:
        p = await asyncio.create_subprocess_exec("nvidia-smi", "--query-gpu=memory.used,memory.total",
                                                 "--format=csv,noheader,nounits", stdout=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), 5)
        used, total = (int(x) for x in out.decode().split("\n")[0].split(","))
        state.update(used_mb=used, total_mb=total)
    except Exception:
        pass
    try:
        async with http.get(RADIO + "/api/radio/state", timeout=aiohttp.ClientTimeout(total=2)) as r:
            d = await r.json()
            state["radio"] = {"on": d.get("on"), "holding": d.get("gpu", {}).get("owner") in ("studio", "switching")}
    except Exception:
        pass
    try:
        async with http.get(OLLAMA + "/api/ps", timeout=aiohttp.ClientTimeout(total=2)) as r:
            state["ollama"] = [{"name": m["name"], "vram_mb": m.get("size_vram", 0) // 2**20}
                               for m in (await r.json()).get("models", [])]
    except Exception:
        pass
    return state


PRIORITY_FILE = Path.home() / ".config" / "ai-gpu-priority.json"


PRIORITIES = {"shared": "Shared — take turns", "studio": "Image & Video Studio", "radio": "Radio", "assistant": "Assistant (Hermes)",
              "clipsmith": "Clipsmith (Designer Studio)", "gaming": "Gaming — keep the 4090 free"}


def gpu_priority():
    try:
        v = json.loads(PRIORITY_FILE.read_text()).get("priority")
    except (OSError, ValueError):
        v = None
    return v if v in PRIORITIES else "shared"


async def get_priority(request):
    return web.json_response({"priority": gpu_priority(), "choices": PRIORITIES})


async def set_priority(request):
    require_admin(request)
    v = (await request.json()).get("priority")
    if v not in PRIORITIES:
        raise web.HTTPBadRequest(text="priority must be one of: " + ", ".join(PRIORITIES))
    PRIORITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRIORITY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"priority": v, "set": time.time()}))
    tmp.replace(PRIORITY_FILE)
    return web.json_response({"priority": v, "choices": PRIORITIES})


DISK_ROOT = Path.home()
DISK_PARTS = [("ComfyUI models", Path.home() / "ComfyUI" / "models"), ("Chat models (Ollama)", Path("/usr/share/ollama/.ollama/models")),
              ("Creations (Image & Video Studio)", Path.home() / "ComfyUI" / "output"), ("Radio songs", HERE.parent / "library")]
_disk = {"t": 0, "parts": None, "busy": False}


def _tree_bytes(root):
    """Bytes actually used under a folder (symlinked model copies cost nothing, so they aren't followed)."""
    total, stack = 0, [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_blocks * 512
                    except OSError:
                        pass
        except OSError:
            pass
    return total


async def disk_usage(request):
    """The disk the models live on: total / used / free, plus what the big folders take (measured every few minutes)."""
    du = shutil.disk_usage(DISK_ROOT)
    if (_disk["parts"] is None or time.time() - _disk["t"] > 300) and not _disk["busy"]:
        _disk["busy"] = True
        try:
            loop = asyncio.get_running_loop()
            sizes = await asyncio.gather(*(loop.run_in_executor(None, _tree_bytes, d) for _, d in DISK_PARTS))
            _disk.update(t=time.time(), parts=[{"label": l, "bytes": b} for (l, d), b in zip(DISK_PARTS, sizes) if d.exists()])
        finally:
            _disk["busy"] = False
    return web.json_response({"total": du.total, "used": du.used, "free": du.free, "parts": _disk["parts"] or [],
                              "measured": _disk["t"]})


# ---------- the shared look, the Home page and the radio's now-playing bar ----------

SHARED = HERE / "shared"


async def shared_file(request):
    """The look and script shared by every app (switcher, now-playing bar); the radio serves the same files."""
    name = request.match_info["name"]
    if name not in ("agentora.css", "agentora.js"):
        raise web.HTTPNotFound()
    return web.FileResponse(SHARED / name, headers={"Cache-Control": "no-cache"})


async def nowplaying(request):
    """What's on the radio, for the now-playing bar and Now Playing view on Home: the songs that aired around now (the
    page works out which one listeners hear), the radio's pick for what airs next, and the station."""
    try:
        async with http.get(RADIO + "/api/radio/state", timeout=aiohttp.ClientTimeout(total=3)) as r:
            d = await r.json()
    except Exception:
        raise web.HTTPServiceUnavailable(text="The radio isn't answering")
    sv = d.get("stream") or {}
    aired = [a for a in sv.get("aired", []) if a.get("start_pos") is not None and a["start_pos"] <= (sv.get("pos") or 0)]
    a = aired[-1] if aired and aired[-1].get("id") else None
    st = next((x for x in d.get("stations", []) if x["id"] == d.get("station")), None)
    if not st and d.get("station") == "__reruns__":
        st = {"id": "__reruns__", "name": "Reruns"}
    keep = ("id", "title", "cover", "style", "lyrics", "mode", "seconds", "duration", "station", "station_name", "credit",
            "feedback", "in_playlist", "airing", "start_pos")
    return web.json_response({"on": d.get("on"), "now": a and {"id": a["id"], "title": a.get("title"), "cover": a.get("cover"), "feedback": a.get("feedback")},
                              "station": st and {"id": st["id"], "name": st["name"], "art": st.get("art")},
                              "stream": {"pos": sv.get("pos"), "clock": sv.get("clock"), "listeners": sv.get("listeners", 0),
                                         "aired": [{k: x.get(k) for k in keep} for x in sv.get("aired", [])]},
                              "next": d.get("next") and {k: d["next"].get(k) for k in keep}})


async def nowplaying_action(request):
    """The now-playing bar and Now Playing view: 👍 👎 ⏭ ⏮ and the playlist, passed on to the radio (which has no sign-in of its own)."""
    action = request.match_info["action"]
    if action not in ("feedback", "skip", "back", "playlist"):
        raise web.HTTPNotFound()
    b = await request.json()
    track = str(b.get("track") or "")[:64]
    if action == "feedback":
        body = {"track": track, "kind": b.get("kind") if b.get("kind") in ("keep", "clear", "dislike") else "keep"}
    elif action == "skip":
        body = {"track": track, "kind": "dislike" if b.get("kind") == "dislike" else "skip"}
    elif action == "back":
        body = {"steps": 1} if b.get("steps") != 0 else {"track": track or None, "steps": 0}
    else:
        body = {"track": track, "on": bool(b.get("on"))}
    async with http.post(f"{RADIO}/api/radio/{action}", json=body, timeout=aiohttp.ClientTimeout(total=8)) as r:
        return web.json_response(await r.json(content_type=None), status=r.status)


async def home_page(request):
    return web.FileResponse(HERE / "home.html", headers={"Cache-Control": "no-cache"})


async def home_summary(request):
    """Everything the Home page shows. Open to anyone who can reach the site: what's on the radio and the machine;
    a signed-in person also gets their name."""
    out = {"radio": None, "system": {}, "user": None}
    try:
        async with http.get(RADIO + "/api/radio/state", timeout=aiohttp.ClientTimeout(total=3)) as r:
            d = await r.json()
        st = next((x for x in d.get("stations", []) if x["id"] == d.get("station")), None)
        sv = d.get("stream") or {}
        aired = [a for a in sv.get("aired", []) if a.get("start_pos") is not None and a["start_pos"] <= (sv.get("pos") or 0)]
        now = aired[-1] if aired and aired[-1].get("id") else None
        out["radio"] = {"on": d.get("on"), "station": st and {"id": st["id"], "name": st["name"], "art": st.get("art"), "ready": st.get("ready")},
                        "now": now and {"id": now["id"], "title": now.get("title"), "style": (now.get("style") or "")[:120], "cover": now.get("cover")},
                        "listeners": sv.get("listeners", 0), "gpu": (d.get("gpu") or {}).get("owner"), "detail": (d.get("gpu") or {}).get("detail"),
                        "making": len(d.get("making") or [])}
    except Exception:
        pass
    g = await gpu_state()
    du = shutil.disk_usage(DISK_ROOT)
    out["system"] = {"vram_used": g["used_mb"], "vram_total": g["total_mb"], "disk_free": du.free, "disk_total": du.total, "priority": gpu_priority()}
    me = request.get("user")
    if me:
        out["user"] = {"name": me["name"], "admin": me["role"] == "admin"}
    return web.json_response(out)


# ---------- the assistant (Hermes) ----------

HERMES = "http://127.0.0.1:8642"
INSTRUCTIONS = ("You are being used from Agentora's Assistant panel in a web browser. Keep replies short and "
                "conversational, use plain text or light markdown, and prefer the radio_* tools for the radio and the "
                "studio_* tools for the Image & Video Studio. When you make pictures or videos, mention they appear in the Studio.")


def _hermes_key():
    for line in (Path.home() / ".hermes" / ".env").read_text().splitlines():
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("The Hermes API server isn't configured")


def _tool_names(item):
    name, args = item.get("name") or "", item.get("arguments") or "{}"
    try:
        args = json.loads(args) if isinstance(args, str) else args
    except ValueError:
        args = {}
    if name == "tool_describe":
        return []
    names = [c.get("name", "") for c in args.get("calls", [])] if name == "tool_call" else [name]
    return [n.replace("mcp__local_apps__", "").replace("_", " ") for n in names if n]


async def assistant_chat(request):
    """Stream a Hermes turn to the browser as simple server-sent events."""
    require_admin(request)
    body = await request.json()
    text = str(body.get("message") or "").strip()[:8000]
    if not text:
        raise web.HTTPBadRequest(text="Write a message first")
    prev = body.get("previous") if re.fullmatch(r"resp_[\w-]{6,80}", str(body.get("previous") or "")) else None

    model = body.get("model") if body.get("model") in ("lemming", "auto", "agi") else "auto"    # Hermes model routes (config.yaml)
    chat_body = {"model": model, "input": text, "instructions": INSTRUCTIONS, "store": True, "stream": True}
    if prev:
        chat_body["previous_response_id"] = prev
    req = HERMES + "/v1/responses"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + _hermes_key()}
    data = json.dumps(chat_body).encode()
    timeout = aiohttp.ClientTimeout(total=1800)

    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    await resp.prepare(request)

    async def emit(obj):
        await resp.write(("data: " + json.dumps(obj) + "\n\n").encode())

    try:
        async with http.post(req, data=data, headers=headers, timeout=timeout) as r:   # the shared session: nothing left open
            event = None
            if r.status != 200:
                await emit({"error": f"Hermes said {r.status}: {(await r.text())[:300]}"})
                return resp
            async for raw in r.content:                    # one SSE line at a time, even when a chunk splits a line
                for line in [raw.decode(errors="replace").strip()]:
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        d = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    t = d.get("type") or event
                    if t == "response.output_text.delta":
                        await emit({"delta": d.get("delta", "")})
                    elif t == "response.output_item.done" and (d.get("item") or {}).get("type") == "function_call":
                        for n in _tool_names(d["item"]):
                            await emit({"tool": n})
                    elif t == "response.completed":
                        rid = (d.get("response") or {}).get("id")
                        await emit({"done": rid})
                    elif t in ("response.failed", "error"):
                        err = (d.get("response") or {}).get("error") or d.get("error") or d
                        await emit({"error": (err.get("message") if isinstance(err, dict) else str(err))[:400]})
    except (ConnectionResetError, aiohttp.ClientConnectionError):
        pass  # browser went away
    except Exception as e:
        await emit({"error": "Couldn't reach Hermes: " + str(e)[:400]})
    return resp


HERMES_BIN = str(Path.home() / ".local" / "bin" / "hermes")


NET_TOOLSETS = ("web",)


OFFLINE_ALSO = ("terminal",)          # shell commands and scripts could reach the internet too: off with it


async def _hermes_toolsets():
    async with http.get(HERMES + "/v1/toolsets", headers={"Authorization": "Bearer " + _hermes_key()},
                        timeout=aiohttp.ClientTimeout(total=10)) as r:
        d = await r.json()
    items = d.get("toolsets", d.get("data", [])) if isinstance(d, dict) else d
    return {t.get("name"): bool(t.get("enabled")) for t in items}


async def assistant_internet(request):
    """Admins: is the assistant allowed on the internet (web search and reading pages)? POST {on} to change it.
    Hermes reads its tool settings on every request, so this applies from the next message, in every app's chat panel."""
    require_admin(request)
    if request.method == "POST":
        on = bool((await request.json()).get("on"))
        p = await asyncio.create_subprocess_exec(HERMES_BIN, "tools", "enable" if on else "disable", *NET_TOOLSETS, *OFFLINE_ALSO, "--platform", "api_server",
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), 60)
        if p.returncode:
            raise web.HTTPBadGateway(text="Hermes couldn't change it: " + out.decode(errors="replace")[-300:])
    try:
        ts = await _hermes_toolsets()
    except Exception as e:
        raise web.HTTPServiceUnavailable(text=f"Hermes isn't answering ({e})")
    return web.json_response({"on": all(ts.get(n) for n in NET_TOOLSETS), "tools": {n: ts.get(n) for n in NET_TOOLSETS + OFFLINE_ALSO}})


# ---------- accounts: one account across the radio, Music Studio, Home and the gates ----------

PUBLIC_PATHS = {"/", "/home", "/api/home", "/api/session", "/api/setup", "/api/login"}

# ---- user activity (logsetup.activity): what each signed-in person does, with their device
ACTIONS = {   # (method, route) -> event name
    ("POST", "/api/password"): "change password", ("POST", "/api/users"): "add a user", ("PUT", "/api/users/{name}"): "change a user",
    ("DELETE", "/api/users/{name}"): "delete a user", ("POST", "/api/assistant/chat"): "chat with Hermes",
    ("POST", "/api/assistant/internet"): "switch Hermes internet", ("POST", "/api/nowplaying/{action}"): "radio",
    ("POST", "/api/gpu-priority"): "set GPU priority"}
PAGES = {"/home": "Home"}


def who(request):
    peer = request.transport.get_extra_info("peername") if request.transport else None
    ip = logsetup.real_ip(*(peer[:2] if peer else (request.remote, None)))
    if ip in ("127.0.0.1", "::1") and request.headers.get("X-Agentora-Client"):   # the Hermes gate, passing on its visitor
        ip = request.headers["X-Agentora-Client"]
    return {"ip": ip, "ua": request.headers.get("User-Agent")}


async def note_activity(request, resp):
    user = (request.get("user") or {}).get("name")
    by_hermes = (request.get("user") or {}).get("via") == "assistant"     # Hermes's tools acting for this account
    route = request.match_info.route.resource.canonical if request.match_info.route.resource else request.path
    ev = ACTIONS.get((request.method, route))
    w = who(request)
    if ev:
        details = {}
        try:
            b = await request.json() if request.content_type == "application/json" else {}
        except Exception:
            b = {}
        if ev == "radio":
            ev = "radio " + request.match_info.get("action", "")
        elif ev == "chat with Hermes":
            details = {"model": b.get("model")}
        elif ev in ("change a user", "delete a user", "add a user"):
            details = {"target": request.match_info.get("name") or b.get("username"), "role": b.get("role")}
        elif ev == "set GPU priority":
            details = {"priority": b.get("priority")}
        if "{id}" in route:
            details["id"] = request.match_info.get("id")
        logsetup.activity("home", ev, user, **w, status=resp.status if resp.status >= 400 else None,
                          via="Hermes" if by_hermes else None, **details)
    elif request.method == "GET" and request.path in PAGES:
        logsetup.activity("home", "open", user, **w, every=600, key=request.path, page=PAGES[request.path])
    elif user and not by_hermes and request.path.startswith("/api/"):
        logsetup.activity("home", "active", user, **w, every=600)


# Agentora's own apps on this machine (radio + Music Studio, Home, the Hermes and Jim gates) may ask who's signed in and
# sign out: one account across every app. Any other site is refused.
OWN_PORTS = {"7860", "7871", "7880", "7885"}
CORS_PATHS = {"/api/session", "/api/logout"}


def own_origin(origin, request):
    hostport = origin.split("://", 1)[-1]
    host, _, port = hostport.rpartition(":")
    return hostport == request.host or (host == request.host.rpartition(":")[0] and port in OWN_PORTS)


@web.middleware
async def guard(request, handler):
    """Every request: who is it (session cookie), are they signed in, and is a change coming from this site."""
    request["user"] = auth.user_for(request.cookies.get(auth.COOKIE))
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("Origin")
        if origin and not (origin.split("://", 1)[-1] == request.host or (request.path in CORS_PATHS and own_origin(origin, request))):
            raise web.HTTPForbidden(text="Cross-site request refused")      # another website trying to act as the user
    if request.path not in PUBLIC_PATHS and not request.path.startswith(("/agentora/", "/api/nowplaying")) and not request["user"]:
        raise web.HTTPUnauthorized(text="Please sign in")
    try:
        resp = await handler(request)
    except auth.AuthError as e:
        raise web.HTTPBadRequest(text=str(e))
    except web.HTTPException as e:
        if e.status in (401, 403) and request.method != "GET" and request.path not in ("/api/login", "/api/setup"):
            logsetup.activity("home", "refused", (request.get("user") or {}).get("name"), **who(request), path=request.path, status=e.status)
        raise
    try:
        if request["user"]:
            w = who(request)
            auth.note_client(request.cookies.get(auth.COOKIE), w["ip"], logsetup.device(w["ip"], w["ua"]))
        await note_activity(request, resp)
    except Exception:
        logging.getLogger("activity").warning("couldn't note the activity", exc_info=True)
    origin = request.headers.get("Origin")
    if origin and request.path in CORS_PATHS and own_origin(origin, request):   # the radio's and the gate's account card
        resp.headers.update({"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"})
    return resp


def require_admin(request):
    if request["user"]["role"] != "admin":
        raise web.HTTPForbidden(text="Only an admin can do that")


def session_response(data, token=None, clear=False):
    resp = web.json_response(data)
    if token:
        resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="Lax", path="/")
    if clear:
        resp.del_cookie(auth.COOKIE, path="/")
    return resp


async def session_info(request):
    return web.json_response({"user": request["user"], "setup": auth.needs_setup()})


async def setup(request):
    """First run only: create the admin account."""
    if not auth.needs_setup():
        raise web.HTTPForbidden(text="Setup is already done")
    body = await request.json()
    name = auth.create_user(body.get("username"), body.get("password"), "admin")
    return session_response({"user": auth.user_for_name(name)}, token=auth.new_session(name))


async def login(request):
    body = await request.json()
    try:
        name = auth.verify(body.get("username"), body.get("password"))
    except auth.AuthError:
        logsetup.activity("home", "sign-in failed", None, **who(request), tried=str(body.get("username") or "")[:40])
        await asyncio.sleep(0.6)                       # slows down password guessing
        raise
    w = who(request)
    logsetup.activity("home", "sign in", name, **w)
    return session_response({"user": auth.user_for_name(name)},
                            token=auth.new_session(name, w["ip"], logsetup.device(w["ip"], w["ua"]) if w["ip"] else logsetup.browser(w["ua"])))


async def logout(request):
    if request["user"]:
        logsetup.activity("home", "sign out", request["user"]["name"], **who(request),
                          via={"7860": "radio", "7880": "Hermes gate", "7885": "Jim gate"}.get(str(request.headers.get("Origin", "")).rpartition(":")[2]))
    auth.end_session(request.cookies.get(auth.COOKIE))
    return session_response({"ok": True}, clear=True)


async def change_password(request):
    body = await request.json()
    me = request["user"]["name"]
    auth.verify(me, body.get("current"))
    auth.set_password(me, body.get("new"))
    auth.end_user_sessions(me, keep_token=request.cookies.get(auth.COOKIE))   # sign out other devices
    return web.json_response({"ok": True})


async def list_users(request):
    require_admin(request)
    last = last_activity()
    users = []
    for u in auth.listing():
        a = last.get(u["name"]) or {}
        if a.get("ts", 0) > (u["last_seen"] or 0):      # the radio and the gates see people too
            u["last_seen"] = a["ts"]
        users.append({k: v for k, v in u.items() if k != "adult"} | {"last_device": a.get("device"), "last_app": a.get("app"),
                     "last_event": a.get("event"), "last_ip": a.get("ip") or next((s["ip"] for s in u["sign_ins"] if s["ip"]), None)})
    return web.json_response({"users": users, "now": time.time()})


def last_activity():
    """Each user's latest line in the activity log (this month and last), from every app."""
    out = {}
    for f in sorted(logsetup.ACTIVITY_DIR.glob("activity-*.jsonl"))[-2:]:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("user"):
                    r["ts"] = time.mktime(time.strptime(r["t"], "%Y-%m-%d %H:%M:%S"))
                    out[r["user"]] = r
        except OSError:
            pass
    return out


async def add_user(request):
    require_admin(request)
    body = await request.json()
    name = auth.create_user(body.get("username"), body.get("password"), body.get("role", "user"))
    return web.json_response({"ok": True, "name": name})


async def update_user(request):
    require_admin(request)
    name, body = request.match_info["name"], await request.json()
    if name not in auth.users:
        raise web.HTTPNotFound()
    if body.get("password"):
        auth.set_password(name, body["password"])
        auth.end_user_sessions(name, keep_token=request.cookies.get(auth.COOKIE) if name == request["user"]["name"] else None)
    if body.get("role"):
        auth.set_role(name, body["role"])
    return web.json_response({"ok": True})


async def remove_user(request):
    require_admin(request)
    name = request.match_info["name"]
    if name not in auth.users:
        raise web.HTTPNotFound()
    if name == request["user"]["name"]:
        raise web.HTTPBadRequest(text="You can't delete your own account while signed in to it")
    auth.delete_user(name)
    return web.json_response({"ok": True})


async def root(request):
    raise web.HTTPFound("/home")


async def on_startup(app):
    global http
    http = aiohttp.ClientSession()


async def on_cleanup(app):
    await http.close()


def main():
    auth.load(HERE)
    app = web.Application(middlewares=[guard])
    app.router.add_get("/", root)
    app.router.add_get("/home", home_page)
    app.router.add_get("/api/home", home_summary)
    app.router.add_get("/agentora/{name}", shared_file)
    app.router.add_get("/api/session", session_info)
    app.router.add_post("/api/setup", setup)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_post("/api/password", change_password)
    app.router.add_get("/api/users", list_users)
    app.router.add_post("/api/users", add_user)
    app.router.add_put("/api/users/{name}", update_user)
    app.router.add_delete("/api/users/{name}", remove_user)
    app.router.add_get("/api/gpu-priority", get_priority)
    app.router.add_post("/api/gpu-priority", set_priority)
    app.router.add_get("/api/disk", disk_usage)
    app.router.add_get("/api/nowplaying", nowplaying)
    app.router.add_post("/api/nowplaying/{action}", nowplaying_action)
    app.router.add_post("/api/assistant/chat", assistant_chat)
    app.router.add_get("/api/assistant/internet", assistant_internet)
    app.router.add_post("/api/assistant/internet", assistant_internet)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    logging.getLogger("home").info(f"Agentora Home on http://{HOST}:{PORT}/home")
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    logsetup.setup("home", Path(__file__).resolve().parent.parent / "logs")
    logging.getLogger("aiohttp.access").setLevel(logging.DEBUG if os.environ.get("LOG_LEVEL", "").upper() == "DEBUG" else logging.WARNING)
    main()
