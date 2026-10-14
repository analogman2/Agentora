"""Gate in front of the Hermes web dashboard (http://127.0.0.1:9119), on port 7880.

The dashboard binds to loopback with no login of its own, and it can read and write Hermes's keys and run the agent.
This gate lets only signed-in Studio *admins* through, reusing the Studio's sign-in (its gs_session cookie is shared
across ports on the same host), and forwards everything else, WebSockets included, to the dashboard.
"""
import asyncio, json, os, time

import aiohttp
from aiohttp import web

DASH = os.environ.get("HERMES_DASHBOARD", "http://127.0.0.1:9119")
STUDIO = os.environ.get("GEN_STUDIO", "http://127.0.0.1:7870")
PORT = int(os.environ.get("GATE_PORT", "7880"))
COOKIE = "gs_session"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
       "upgrade", "host", "content-length", "content-encoding"}
seen = {}                                   # session cookie -> (checked at, user or None): avoids a Studio call per asset
http: aiohttp.ClientSession = None

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Dashboard</title><style>
:root{--bg:#f4f5f7;--card:#fff;--text:#1d1f23;--muted:#6b7079;--line:#e6e8ec;--accent:#3157e0;--ink:#fff;--err:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#15181d;--text:#e9eaee;--muted:#9ba1ab;--line:#272b33;--accent:#7d97ff;--ink:#0d1226;--err:#ff7b72}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif;padding:16px;box-sizing:border-box}
form{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;width:100%;max-width:340px;display:flex;flex-direction:column;gap:10px}
h1{font-size:20px;margin:0}p{margin:0;color:var(--muted);font-size:14px}
input{font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text)}
button{font:inherit;font-weight:600;padding:10px;border:0;border-radius:10px;background:var(--accent);color:var(--ink);cursor:pointer}
#e{color:var(--err);font-size:14px;min-height:1em}</style></head><body>
<form id="f"><h1>☤ Hermes Dashboard</h1><p>Sign in with a Studio <b>admin</b> account. The dashboard can change the agent's settings and keys.</p>
<input id="u" autocomplete="username" placeholder="Studio username" aria-label="Studio username" required>
<input id="p" type="password" autocomplete="current-password" placeholder="Password" aria-label="Password" required>
<button>Sign in</button><div id="e" role="alert"></div></form>
<script>
document.getElementById("f").onsubmit = async (ev) => {
  ev.preventDefault(); const e = document.getElementById("e"); e.textContent = "";
  const r = await fetch("/gate/login", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({username: document.getElementById("u").value, password: document.getElementById("p").value})});
  const j = await r.json().catch(() => ({}));
  if (r.ok) location.reload(); else e.textContent = j.error || "Couldn't sign in";
};
</script></body></html>"""


async def studio_user(cookie):
    if not cookie:
        return None
    hit = seen.get(cookie)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    try:
        async with http.get(STUDIO + "/api/session", headers={"Cookie": f"{COOKIE}={cookie}"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
            user = (await r.json()).get("user") if r.status == 200 else None
    except Exception:
        return None                          # Studio down: fail closed
    if len(seen) > 500:
        seen.clear()
    seen[cookie] = (time.time(), user)
    return user


def who(request):
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return {"ip": logsetup.real_ip(*(peer[:2] if peer else (request.remote, None))), "ua": request.headers.get("User-Agent")}


def same_site(request):
    origin = request.headers.get("Origin")
    return not origin or origin.split("://", 1)[-1] == request.host


async def login(request):
    if not same_site(request):
        raise web.HTTPForbidden(text="Cross-site request refused")
    b = await request.json()
    w = who(request)                                 # the Studio logs the sign-in with this device, not the gate's
    async with http.post(STUDIO + "/api/login", json={"username": b.get("username"), "password": b.get("password")},
                         headers={"X-Agentora-Client": w["ip"] or "", "User-Agent": w["ua"] or ""}) as r:
        text = await r.text()
        if r.status != 200:
            return web.json_response({"error": text[:200] or "Wrong username or password"}, status=400)
        user = json.loads(text)["user"]
        if user["role"] != "admin":
            logsetup.activity("hermes-gate", "refused: not an admin", user["name"], **w)
            return web.json_response({"error": "That account isn't a Studio admin"}, status=403)
        resp = web.json_response({"ok": True})
        for c in r.headers.getall("Set-Cookie", []):
            resp.headers.add("Set-Cookie", c)
        return resp


def forward_headers(request):
    h = {k: v for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "cookie"}
    cookies = "; ".join(f"{k}={v}" for k, v in request.cookies.items() if k != COOKIE)
    if cookies:
        h["Cookie"] = cookies
    h["Host"] = DASH.split("://", 1)[1]               # the dashboard only answers to its loopback address
    if "Origin" in h:
        h["Origin"] = DASH
    if "Referer" in h:
        h["Referer"] = DASH + "/"
    return h


async def proxy(request):
    user = await studio_user(request.cookies.get(COOKIE))
    if not user or user.get("role") != "admin":
        if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
            return web.Response(text=LOGIN_PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})
        raise web.HTTPUnauthorized(text="Sign in with a Studio admin account")
    if not same_site(request):
        raise web.HTTPForbidden(text="Cross-site request refused")
    logsetup.activity("hermes-gate", "use the Hermes dashboard", user["name"], **who(request), every=600)
    url = DASH + request.rel_url.path_qs
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await bridge(request, url)
    body = await request.read() if request.can_read_body else None
    async with http.request(request.method, url, headers=forward_headers(request), data=body, allow_redirects=False,
                            timeout=aiohttp.ClientTimeout(total=None, sock_read=600)) as r:
        resp = web.StreamResponse(status=r.status, headers={k: v for k, v in r.headers.items()
                                                            if k.lower() not in HOP or k.lower() == "content-encoding"})
        await resp.prepare(request)
        async for chunk in r.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp


async def bridge(request, url):
    protos = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
    hdrs = {k: v for k, v in forward_headers(request).items() if not k.lower().startswith("sec-websocket")}
    async with http.ws_connect(url.replace("http", "ws", 1), headers=hdrs, protocols=protos, max_msg_size=0, autoping=True) as up:
        down = web.WebSocketResponse(protocols=protos, max_msg_size=0, autoping=True)
        await down.prepare(request)

        async def pump(src, dst):
            async for m in src:
                if m.type == aiohttp.WSMsgType.TEXT:
                    await dst.send_str(m.data)
                elif m.type == aiohttp.WSMsgType.BINARY:
                    await dst.send_bytes(m.data)
                else:
                    break
            await dst.close()
        await asyncio.gather(pump(down, up), pump(up, down), return_exceptions=True)
        return down


async def start(app):
    global http
    http = aiohttp.ClientSession(auto_decompress=False)


async def stop(app):
    await http.close()


app = web.Application(client_max_size=50 * 2**20)
app.on_startup.append(start)
app.on_cleanup.append(stop)
app.router.add_post("/gate/login", login)
app.router.add_route("*", "/{path:.*}", proxy)

if __name__ == "__main__":
    import logging, logsetup
    from pathlib import Path
    logsetup.setup("hermes-gate", Path(__file__).resolve().parent / "logs")
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None, print=lambda *_: logging.getLogger("hermes-gate").info(f"Hermes dashboard gate on :{PORT}"))
