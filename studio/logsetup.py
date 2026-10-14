"""Logging for every Agentora component: one format, levels, the systemd journal and a rotating file per component.

    import logsetup
    log = logsetup.setup("radio")              # once, at startup (each process: servers, GPU workers, the relay)
    logging.getLogger(__name__).warning(...)   # anywhere

Lines look like `WARNING radio_spool: the critic failed ...` in the journal (journald adds the time) and
`2026-10-13 01:20:03 WARNING radio_spool: ...` in logs/<component>.log (5 MB x 5 files, next to the code).
LOG_LEVEL=DEBUG|INFO|WARNING sets the level (default INFO; DEBUG adds every web request). LOG_DIR moves the files.
Uncaught exceptions, in the main thread or any other, are logged as ERROR with their traceback.
"""
import logging, logging.handlers, os, re, sys, threading
from pathlib import Path

FMT = "%(levelname)s %(name)s: %(message)s"
_WARN = re.compile(r"\b(error|errors|failed|fails|couldn'?t|could not|cannot|can'?t|not reachable|gave up|timed out|"
                   r"timeout|refused|missing|invalid|skipped)\b", re.I)


def level_of(text):
    """A level for a plain log line: tracebacks are errors, trouble words are warnings, the rest is info."""
    if "Traceback (most recent call last)" in text:
        return logging.ERROR
    return logging.WARNING if _WARN.search(text) else logging.INFO


def setup(component, log_dir=None):
    root = logging.getLogger()
    if getattr(root, "_agentora", None) == component:
        return logging.getLogger(component)
    for h in list(root.handlers):
        root.removeHandler(h)
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root.setLevel(level)
    out = logging.StreamHandler(sys.stdout)                 # -> the systemd journal (it adds the timestamp)
    out.setFormatter(logging.Formatter(FMT))
    root.addHandler(out)
    d = Path(log_dir or os.environ.get("LOG_DIR") or Path(__file__).resolve().parent.parent / "logs")
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = logging.handlers.RotatingFileHandler(d / f"{component}.log", maxBytes=5 * 2**20, backupCount=5, encoding="utf-8")
        f.setFormatter(logging.Formatter("%(asctime)s " + FMT, "%Y-%m-%d %H:%M:%S"))
        root.addHandler(f)
    except OSError as e:
        root.warning("no log file in %s (%s); logging to the journal only", d, e)
    for noisy in ("urllib3", "asyncio", "PIL", "matplotlib", "numba", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    def excepthook(t, v, tb):
        if issubclass(t, KeyboardInterrupt):
            return sys.__excepthook__(t, v, tb)
        logging.getLogger(component).error("uncaught %s", t.__name__, exc_info=(t, v, tb))

    def thread_hook(a):
        if a.exc_type in (SystemExit,) or issubclass(a.exc_type, (BrokenPipeError, ConnectionResetError)):
            return
        logging.getLogger(a.thread.name if a.thread else component).error("uncaught %s in a thread", a.exc_type.__name__,
                                                                           exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    root._agentora = component
    return logging.getLogger(component)


class QuietBrokenPipes:
    """Mix into a socketserver/http.server server: a client that went away isn't an error; anything else is logged."""

    def handle_error(self, request, client_address):
        t, v = sys.exc_info()[:2]
        if t and issubclass(t, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        logging.getLogger("http").error("request from %s failed", client_address[0], exc_info=True)


# ---------------------------------------------------------------- user activity
# One log of who did what, shared by every app: logs/activity-YYYY-MM.jsonl (one JSON object per line, appended by
# each process with a single write, so the apps can share it; a new file each month instead of rotation). Each line:
#   {"t": "2026-10-13 20:31:05", "app": "radio", "user": "jt", "device": "Pixel 10 Pro", "ip": "100.85.161.32",
#    "event": "skip", ...details}
# tools/activity.py reads it (who's here now, a timeline). Passwords, prompts and lyrics are never logged.
import json, subprocess, time

ACTIVITY_DIR = Path(os.environ.get("ACTIVITY_DIR") or Path(__file__).resolve().parent.parent / "logs")
PEERS = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "agentora-peers"     # written by tools/tailnet-relay.py
_alog = logging.getLogger("activity")
_seen, _names, _alock = {}, {"t": 0, "map": {}}, threading.Lock()


def real_ip(ip, port):
    """The device's own address. Connections through the tailnet relay arrive from 127.0.0.1; the relay notes each one's
    real client under the local port it connects from."""
    if ip in ("127.0.0.1", "::1") and port:
        try:
            return (PEERS / str(port)).read_text().strip() or ip
        except OSError:
            pass
    return ip


def device_names():
    """Tailscale address -> device name ("Pixel 10 Pro"), and for devices shared from other tailnets, whose they are."""
    with _alock:
        if time.time() - _names["t"] < 300:
            return _names["map"]
        _names["t"] = time.time()
    m = {}
    try:
        d = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5).stdout)
        users = {str(k): (v.get("DisplayName") or v.get("LoginName") or "") for k, v in (d.get("User") or {}).items()}
        for p in [d.get("Self") or {}] + list((d.get("Peer") or {}).values()):
            name = p.get("HostName") or ""
            if name == "device-of-shared-to-user":           # someone else's device, shared into this tailnet
                name = "shared device of " + (users.get(str(p.get("UserID"))) or "another tailnet")
            for ip in p.get("TailscaleIPs") or []:
                m[ip] = name
    except Exception:
        pass
    with _alock:
        _names["map"] = m or _names["map"]
        return _names["map"]


def browser(ua):
    ua = ua or ""
    if not ua or ua.startswith(("curl", "python", "Python", "Wget")) or "HeadlessChrome" in ua:
        return "script"
    os_ = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
                                ("Macintosh", "Mac"), ("CrOS", "ChromeOS"), ("Linux", "Linux")) if k in ua), "")
    br = next((n for k, n in (("Edg/", "Edge"), ("Firefox", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"),
                               ("Safari", "Safari")) if k in ua), "")
    return " ".join(x for x in (os_, br) if x) or ua[:40]


def device(ip, ua):
    """A readable device: its Tailscale name when it has one, else the browser and OS."""
    return device_names().get(ip) or browser(ua)


def activity(app, event, user=None, ip=None, ua=None, every=0, key=None, **details):
    """Record one thing a person did. every=seconds logs it at most that often per (app, user, device, key) - for
    "still here" heartbeats and page visits."""
    if browser(ua) == "script" and event in ("open", "active"):
        return
    dev = device(ip, ua) if ip else browser(ua)
    if every:
        k = (app, user, dev, key or event)
        now = time.time()
        with _alock:
            if now - _seen.get(k, 0) < every:
                return
            _seen[k] = now
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "app": app, "user": user, "device": dev, "ip": ip, "event": event,
           **{k: v for k, v in details.items() if v not in (None, "")}}
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(ACTIVITY_DIR / f"activity-{rec['t'][:7]}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError as e:
        _alog.warning("couldn't write the activity log: %s", e)
    if event != "active":
        _alog.info("%s %s on %s (%s)%s", user or "someone", event, app, dev,
                   "".join(f" {k}={v}" for k, v in details.items() if v not in (None, "")))
