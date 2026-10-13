"""The radio's Assistant panel: chat with the local Hermes agent.

Hermes can run commands on this machine, so only a signed-in Studio *admin* may use it. Sign-in is the Studio's own
(its gs_session cookie is shared across ports on the same host); the radio only relays it. The Hermes API key stays
on the server.
"""
import json, re, urllib.error, urllib.request
from pathlib import Path

STUDIO = "http://127.0.0.1:7870"
HERMES = "http://127.0.0.1:8642"
COOKIE = "gs_session"
INSTRUCTIONS = ("You are being used from the AI radio's Assistant panel in a web browser. Keep replies short and "
                "conversational, use plain text or light markdown, and prefer the radio_* and studio_* tools for the radio "
                "and the Image & Video Studio. When you make pictures or videos, mention they appear in the Studio.")


def _hermes_key():
    for line in (Path.home() / ".hermes" / ".env").read_text().splitlines():
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("The Hermes API server isn't configured")


def _cookie(h):
    m = re.search(r"(?:^|;\s*)" + COOKIE + r"=([^;]+)", h.headers.get("Cookie") or "")
    return m.group(1) if m else None


def _studio(path, body=None, cookie=None):
    req = urllib.request.Request(STUDIO + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Cookie": f"{COOKIE}={cookie}"} if cookie else {})},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}"), r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:300]}, []


def who(h):
    c = _cookie(h)
    if not c:
        return None
    code, d, _ = _studio("/api/session", cookie=c)
    return d.get("user") if code == 200 else None


def _same_site(h):
    """Refuse changes started by another website (the browser always sends Origin on cross-site POSTs)."""
    origin = h.headers.get("Origin")
    return not origin or origin.split("://", 1)[-1] == h.headers.get("Host")


def handle(h, method, path):
    if not path.startswith("/api/assistant/"):
        return False
    if method == "POST" and not _same_site(h):
        return h.send(403, {"error": "Cross-site request refused"}) or True
    if method == "GET" and path == "/api/assistant/session":
        u = who(h)
        return h.send(200, {"user": u, "allowed": bool(u and u.get("role") == "admin")}) or True
    if method == "POST" and path == "/api/assistant/login":
        b = h.body()
        code, d, cookies = _studio("/api/login", {"username": b.get("username"), "password": b.get("password")})
        if code != 200:
            return h.send(400, {"error": d.get("error") or "Wrong username or password"}) or True
        extra = {"Set-Cookie": cookies[0]} if cookies else {}
        return h.send(200, {"user": d["user"], "allowed": d["user"]["role"] == "admin"}, extra=extra) or True
    if method == "POST" and path == "/api/assistant/logout":
        c = _cookie(h)
        if c:
            _studio("/api/logout", {}, cookie=c)
        return h.send(200, {"ok": True}, extra={"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"}) or True
    if method == "POST" and path == "/api/assistant/chat":
        u = who(h)
        if not u or u.get("role") != "admin":
            return h.send(403, {"error": "Sign in with a Studio admin account to use the assistant"}) or True
        b = h.body()
        text = str(b.get("message") or "").strip()[:8000]
        if not text:
            return h.send(400, {"error": "Write a message first"}) or True
        prev = b.get("previous") if re.fullmatch(r"resp_[\w-]{6,80}", str(b.get("previous") or "")) else None
        chat(h, text, prev, b.get("model") if b.get("model") in ("lemming", "auto", "agi") else "auto")
        return True
    return h.send(404, {"error": "not found"}) or True


def _tool_names(item):
    """Friendly names for the tools a function_call item ran (Hermes wraps MCP calls in tool_call)."""
    name, args = item.get("name") or "", item.get("arguments") or "{}"
    try:
        args = json.loads(args) if isinstance(args, str) else args
    except ValueError:
        args = {}
    if name == "tool_describe":
        return []
    names = [c.get("name", "") for c in args.get("calls", [])] if name == "tool_call" else [name]
    return [n.replace("mcp__local_apps__", "").replace("_", " ") for n in names if n]


def chat(h, text, prev, model="auto"):
    """Stream a Hermes turn to the browser as simple server-sent events: tool / delta / done / error."""
    body = {"model": model, "input": text, "instructions": INSTRUCTIONS, "store": True, "stream": True}
    if prev:
        body["previous_response_id"] = prev
    req = urllib.request.Request(HERMES + "/v1/responses", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + _hermes_key()})
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream"); h.send_header("Cache-Control", "no-store")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()

    def emit(obj):
        h.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n"); h.wfile.flush()

    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            event = None
            for raw in r:
                line = raw.decode(errors="replace").rstrip("\n")
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
                    emit({"delta": d.get("delta", "")})
                elif t == "response.output_item.done" and (d.get("item") or {}).get("type") == "function_call":
                    for n in _tool_names(d["item"]):
                        emit({"tool": n})
                elif t == "response.completed":
                    emit({"done": (d.get("response") or {}).get("id")})
                elif t in ("response.failed", "error"):
                    err = (d.get("response") or {}).get("error") or d.get("error") or d
                    emit({"error": (err.get("message") if isinstance(err, dict) else str(err))[:400]})
    except (BrokenPipeError, ConnectionResetError):
        return                                   # the browser went away; Hermes finishes the turn on its own
    except urllib.error.HTTPError as e:
        emit({"error": f"Hermes said {e.code}: {e.read().decode(errors='replace')[:300]}"})
    except Exception as e:
        emit({"error": "Couldn't reach Hermes: " + str(e)[:300]})
