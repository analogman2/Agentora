#!/usr/bin/env python3
"""Relay local web apps (127.0.0.1:<port>) onto this machine's Tailscale IP (and with --lan its home-network IP), same ports.
Without --lan it listens only on the tailnet address, so the apps are not reachable from the LAN.

HTTPS on the same ports: a connection that starts with a TLS handshake is decrypted with the Tailscale certificate in
~/.config/tailnet-relay/ (cert.pem, key.pem; renewed by tailnet-cert.timer), so https://<machine>.<tailnet>.ts.net:<port>/
works next to plain http://<ip>:<port>/. Without the certificate files, TLS connections are simply closed.
No root needed. Usage: tailnet-relay.py [--lan] [--listen IP] PORT [PORT ...]   (e.g. --lan 7860 7870)"""
import asyncio, logging, logging.handlers, os, socket, ssl, subprocess, sys, time
from pathlib import Path

CERT_DIR = Path.home() / ".config" / "tailnet-relay"
# The apps see every relayed connection as coming from 127.0.0.1. For each one the relay writes the real client address
# to PEERS/<the local port it connects from>, so the apps' activity log can name the device (logsetup.real_ip).
PEERS = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "agentora-peers"
LOG = logging.getLogger("tailnet-relay")


def setup_logging():
    """Same format as the apps: the journal, plus ~/.local/state/tailnet-relay/relay.log (5 MB x 5). LOG_LEVEL=DEBUG logs
    every connection."""
    root = logging.getLogger(); root.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
    fmt = "%(levelname)s %(name)s: %(message)s"
    out = logging.StreamHandler(sys.stdout); out.setFormatter(logging.Formatter(fmt)); root.addHandler(out)
    d = Path.home() / ".local" / "state" / "tailnet-relay"; d.mkdir(parents=True, exist_ok=True)
    f = logging.handlers.RotatingFileHandler(d / "relay.log", maxBytes=5 * 2**20, backupCount=5)
    f.setFormatter(logging.Formatter("%(asctime)s " + fmt, "%Y-%m-%d %H:%M:%S")); root.addHandler(f)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
_tls = {"ctx": None, "mtime": None, "checked": 0}


def tls_context():
    """The current certificate as an SSL context, reloaded when the files change (renewals need no restart)."""
    if time.time() - _tls["checked"] < 60:
        return _tls["ctx"]
    _tls["checked"] = time.time()
    cert, key = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    try:
        mtime = (cert.stat().st_mtime, key.stat().st_mtime)
    except OSError:
        _tls.update(ctx=None, mtime=None)
        return None
    if mtime != _tls["mtime"]:
        try:
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(cert, key)
            _tls.update(ctx=ctx, mtime=mtime)
            LOG.info(f"https: loaded certificate ({cert})")
        except (OSError, ssl.SSLError) as e:
            LOG.warning(f"https: couldn't load the certificate: {e}")
            _tls.update(ctx=None, mtime=mtime)
    return _tls["ctx"]


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, ssl.SSLError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(conn, port):
    loop = asyncio.get_running_loop()
    try:
        fut = loop.create_future()                       # peek at the first byte without consuming it

        def ready():
            if not fut.done():
                fut.set_result(None)
        loop.add_reader(conn.fileno(), ready)
        try:
            await asyncio.wait_for(fut, 30)
        finally:
            loop.remove_reader(conn.fileno())
        first = conn.recv(1, socket.MSG_PEEK)
    except (asyncio.TimeoutError, OSError):
        conn.close()
        return
    if not first:
        conn.close()
        return
    ctx = None
    if first == b"\x16":                                 # a TLS ClientHello
        ctx = tls_context()
        if not ctx:
            conn.close()
            return
    try:
        client_ip = conn.getpeername()[0]
    except OSError:
        client_ip = ""
    reader = asyncio.StreamReader(limit=2 ** 20)
    proto = asyncio.StreamReaderProtocol(reader)
    try:
        transport, _ = await loop.connect_accepted_socket(lambda: proto, conn, ssl=ctx, ssl_handshake_timeout=15 if ctx else None)
    except (OSError, ssl.SSLError, asyncio.TimeoutError):
        conn.close()
        return
    writer = asyncio.StreamWriter(transport, proto, reader, loop)
    try:
        server_r, server_w = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        writer.close()
        return
    note = None
    try:
        note = PEERS / str(server_w.get_extra_info("sockname")[1])
        note.write_text(client_ip)
    except (OSError, TypeError, IndexError):
        note = None
    try:
        await asyncio.gather(pipe(reader, server_w), pipe(server_r, writer))
    finally:
        if note:
            note.unlink(missing_ok=True)


# asyncio keeps only weak references to tasks: an unreferenced connection task can be garbage-collected mid-stream
# ("Task was destroyed but it is pending!"), which cut long radio streams after a few seconds. Hold every one here.
CONNS = set()


async def serve(ip, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((ip, port)); srv.listen(128); srv.setblocking(False)
    loop = asyncio.get_running_loop()
    LOG.info(f"relaying http(s)://{ip}:{port}/ -> 127.0.0.1:{port}")
    while True:
        conn, _ = await loop.sock_accept(srv)
        conn.setblocking(False)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)   # notice phones that vanish (sleep, lost signal)
        t = asyncio.create_task(handle(conn, port))
        CONNS.add(t); t.add_done_callback(CONNS.discard)


def lan_ip():
    """This machine's address on the home network (source address of the default route)."""
    out = subprocess.check_output(["ip", "-4", "route", "get", "1.1.1.1"], text=True).split()
    return out[out.index("src") + 1]


async def main(ports, lan, listen):
    ips = listen or [subprocess.check_output(["tailscale", "ip", "-4"], text=True).split()[0]] + ([lan_ip()] if lan else [])
    tls_context()
    PEERS.mkdir(mode=0o700, exist_ok=True)
    for f in PEERS.iterdir():                            # left over from before a restart
        f.unlink(missing_ok=True)
    await asyncio.gather(*(serve(ip, p) for ip in ips for p in ports))


args = sys.argv[1:]
lan = "--lan" in args          # also listen on the home-network address, not just the tailnet
listen = [args[i + 1] for i, a in enumerate(args) if a == "--listen"]
rest = [a for i, a in enumerate(args) if a not in ("--lan", "--listen") and (i == 0 or args[i - 1] != "--listen")]
setup_logging()
asyncio.run(main([int(p) for p in rest] or [7860], lan, listen))
