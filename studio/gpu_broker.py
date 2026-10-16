#!/usr/bin/env python3
"""Agentora GPU broker: one place that decides who uses the graphics card(s), so a chat or a picture doesn't end up
half on the CPU because the radio got there first.

Every GPU user takes a *lease*: who it is, roughly how much memory it needs, and whether a person is waiting
("interactive": the assistant, the Image & Video Studio) or not ("background": the radio's recordings, cover art,
the lyricist). To grant a lease the broker makes room with real measurements (nvidia-smi): it tells background
clients to step aside (they ask GET /yield before starting new work), asks an idle ComfyUI to unload, and unloads
idle Ollama models. The order follows the "Who goes first" setting (~/.config/ai-gpu-priority.json): shared = a
person waiting beats background work; radio / studio / assistant / clipsmith (Designer Studio's leases,
"designer-studio*") = that one first, and its jobs may unload lower-priority chat models to make room.

Tools that can't ask for a lease themselves get a proxy: the Ollama proxy (127.0.0.1:11435) takes a lease for every
chat request, for the client named in the X-Agentora-Client header (Hermes sends none: "assistant"; Designer Studio
sends "designer-studio"), reloads a model that was squeezed half onto the CPU once there is room, and maps the virtual
model "agentora-auto" to the strongest model that can have the card right now. Clients share one loaded copy of a
model: a smaller num_ctx than the model's own is dropped (Ollama would reload it), and an unload request
(keep_alive 0) for a model another client holds is answered without unloading it.

API (127.0.0.1:7890, JSON):
  POST /lease {client, kind: interactive|background, mem_mb, wait_s, ttl_s, device?} -> {granted, id, device, ...}
  POST /lease/<id>/renew {ttl_s?}     DELETE /lease/<id>
  GET  /yield?client=radio            -> {yield, reason}   (background clients: wait before starting new work)
  POST /holding {client, device, mem_mb, holding}       (a background client says it has its model on a card)
  GET  /status
Multi-GPU: devices come from nvidia-smi and leases are per device (the one with the most room unless asked).
The Radeon (RX 7900 XTX) has its own Ollama (127.0.0.1:11436, ROCm) and belongs to Hermes's model alone: the proxy sends
AMD_MODELS there without a lease and keeps it loaded, so chat never waits for the 4090 and the radio never steps aside
for chat. If that Ollama is down, those models fall back to the 4090 and the rules above.
Stdlib only. Usage: gpu_broker.py [--port 7890] [--ollama-port 11435]
"""
import argparse, http.client, json, logging, os, re, subprocess, threading, time, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import logsetup

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=7890)
ap.add_argument("--ollama-port", type=int, default=11435)
ap.add_argument("--ollama", default="http://127.0.0.1:11434")
ap.add_argument("--ollama-amd", default="http://127.0.0.1:11436")
ap.add_argument("--comfy", default="http://127.0.0.1:8188")
A = ap.parse_args()
log = logging.getLogger("gpu-broker")

PRIORITY_FILE = Path.home() / ".config" / "ai-gpu-priority.json"
STATE_FILE = Path.home() / ".config" / "agentora-gpu-broker.json"     # learned model sizes
INTERACTIVE = {"assistant", "studio"}
MARGIN_MB = 600                                   # keep this much free beyond the estimate (CUDA context, fragmentation)
ASSISTANT_IDLE_S = int(os.environ.get("BROKER_ASSISTANT_IDLE", "180"))   # a chat keeps the card this long after its last call
AUTO = {"agentora-auto": ("qwen3.6-agent-moe", "qwen3.5-agent-fast")}   # strongest first, then the one that always fits
# Mixture-of-experts models run well partly on the CPU (only ~3B of the 35B work per word): they need this much of the
# card to be fast, take whatever more Ollama gives them, and are never reloaded just to get "whole".
SPLIT_OK = {"qwen3.6-agent-moe": 12000}
AUTO_WAIT_S = 3.0                                 # how long "auto" waits for room for the strong model
STRONG_WAIT_S = 45.0                              # a model asked for by name waits this long for room, then loads anyway
DEFAULT_MB = {"qwen3.6-agent": 18000, "qwen3.5-agent-fast": 9000, **SPLIT_OK}
AMD_MODELS = {"qwen3.6-agent-moe"}                # live on the 7900 (21 GB at 64k with the 8-bit KV cache: one fits)
AMD_SYSFS = Path("/sys/bus/pci/devices")

lock = threading.RLock()
leases = {}                                       # id -> {client, kind, mem_mb, device, granted, since, expires, note}
holders = {}                                      # client -> {device, mem_mb, since}   (background models on a card)
sizes = {}                                        # model -> full size in MiB (learned from Ollama)
try:
    _saved = json.loads(STATE_FILE.read_text())
    sizes.update(_saved.get("sizes", {}))
    leases.update({k: v for k, v in _saved.get("leases", {}).items() if v.get("expires", 0) > time.time() and v.get("granted")})
except (OSError, ValueError):
    pass


# ---------------------------------------------------------------- facts
_dev = {"t": 0, "v": []}


def devices():
    """[{index, name, total_mb, used_mb, free_mb}] from nvidia-smi (cached for a second)."""
    if time.time() - _dev["t"] > 1:
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10).stdout
            v = []
            for line in out.strip().splitlines():
                i, name, total, used = [x.strip() for x in line.split(",")]
                v.append({"index": int(i), "name": name, "total_mb": int(total), "used_mb": int(used), "free_mb": int(total) - int(used)})
            _dev.update(t=time.time(), v=v)
        except Exception as e:
            log.warning("nvidia-smi failed: %s", e)
    return _dev["v"]


def priority():
    try:
        return json.loads(PRIORITY_FILE.read_text()).get("priority", "shared")
    except (OSError, ValueError):
        return "shared"


def rank(client, kind):
    """Lower goes first."""
    p = priority()
    if client == p or (p == "radio" and client.startswith("radio")) or (p == "clipsmith" and client.startswith("designer-studio")):
        return 0
    return 1 if kind == "interactive" else 2


def ollama_json(path, body=None, timeout=10, base=None):
    r = urllib.request.Request((base or A.ollama) + path, json.dumps(body).encode() if body is not None else None,
                               {"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def ollama_loaded():
    try:
        return {m["name"].removesuffix(":latest"): m for m in ollama_json("/api/ps").get("models", [])}
    except Exception:
        return {}


_amd = {"t": 0, "up": False}


def amd_up():
    """Is the 7900's Ollama answering? (cached for 5 s)"""
    if time.time() - _amd["t"] > 5:
        try:
            ollama_json("/api/version", base=A.ollama_amd, timeout=2); up = True
        except Exception:
            up = False
        if up != _amd["up"]:
            log.info("Radeon Ollama (%s) is %s", A.ollama_amd, "up: Hermes's model runs there" if up else "down: its models fall back to the 4090")
        _amd.update(t=time.time(), up=up)
    return _amd["up"]


def on_amd(model):
    return model in AMD_MODELS and amd_up()


def amd_card():
    """The Radeon's memory from sysfs (no extra tools needed), plus what its Ollama has loaded."""
    for d in AMD_SYSFS.glob("*"):
        try:
            if (d / "vendor").read_text().strip() == "0x1002" and (d / "mem_info_vram_total").exists():
                total, used = (int((d / f).read_text()) >> 20 for f in ("mem_info_vram_total", "mem_info_vram_used"))
                try:
                    models = [m["name"].removesuffix(":latest") for m in ollama_json("/api/ps", base=A.ollama_amd, timeout=2).get("models", [])]
                except Exception:
                    models = None
                return {"name": "AMD Radeon RX 7900 XTX", "pci": d.name, "total_mb": total, "used_mb": used,
                        "free_mb": total - used, "ollama": A.ollama_amd, "up": models is not None, "models": models or []}
        except (OSError, ValueError):
            continue
    return None


def save_sizes():
    """Learned sizes and the live leases survive a restart of the broker (else the radio could grab the card mid-chat)."""
    try:
        with lock:
            data = json.dumps({"sizes": sizes, "leases": {k: v for k, v in leases.items() if v.get("granted")}})
        tmp = STATE_FILE.with_suffix(".tmp"); tmp.write_text(data); tmp.replace(STATE_FILE)
    except OSError:
        pass


def model_mb(model):
    if model in SPLIT_OK:
        return SPLIT_OK[model]
    return sizes.get(model) or DEFAULT_MB.get(model) or 10000


def on_gpu_enough(model, m):
    """Loaded well enough: all of it, or for a mixture-of-experts model, at least its share of the card."""
    if model in SPLIT_OK:
        return (m["size_vram"] >> 20) >= SPLIT_OK[model] * 0.9
    return m["size_vram"] >= m["size"] * 0.97


def ollama_gpu_mb():
    """GPU memory Ollama's runners really hold (nvidia-smi per process: weights + KV cache + compute buffers; Ollama's
    own size_vram leaves the buffers out)."""
    return gpu_mb_of(b"ollama")


def gpu_mb_of(name):
    """GPU memory held by processes whose command line contains name (bytes)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        total = 0
        for line in out.strip().splitlines():
            pid, mb = [x.strip() for x in line.split(",")]
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            if name in cmd:
                total += int(mb)
        return total
    except Exception:
        return 0


# ---------------------------------------------------------------- making room
def comfy_idle():
    try:
        with urllib.request.urlopen(A.comfy + "/queue", timeout=3) as r:
            q = json.loads(r.read())
        return not q.get("queue_running") and not q.get("queue_pending")
    except Exception:
        return False


def make_room(for_client):
    """Free what isn't needed right now: idle ComfyUI models, Ollama models nobody holds a lease for. Background
    clients (the radio) see the waiting lease through /yield and step aside on their own."""
    freed = []
    if not any(l["client"] == "studio" and l["granted"] for l in leases.values()) and comfy_idle() and gpu_mb_of(b"ComfyUI") > 1500:
        try:
            urllib.request.urlopen(urllib.request.Request(A.comfy + "/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
                                                          {"Content-Type": "application/json"}), timeout=10).read()
            freed.append("ComfyUI")
        except Exception:
            pass
    me = rank(for_client, "interactive" if for_client in INTERACTIVE or for_client.startswith("designer-studio") else "background")
    # someone's chat model stays, unless the one asking has priority over them (Image Studio / Clipsmith priority)
    leased = {l.get("model") for l in leases.values() if l["granted"] and l.get("model") and rank(l["client"], l["kind"]) <= me}
    for name, m in ollama_loaded().items():
        if m.get("size_vram") and name not in leased and name != leases_model_wanted():
            try:
                ollama_json("/api/generate", {"model": name, "keep_alive": 0}, timeout=30)
                freed.append(name)
            except Exception:
                pass
    if freed:
        log.info("made room for %s: unloaded %s", for_client, ", ".join(freed))


def leases_model_wanted():
    with lock:
        return next((l.get("model") for l in leases.values() if l.get("model") and not l["granted"]), None)


def pick_device(mem_mb, want=None):
    ds = devices()
    if not ds:
        return None, 0
    if want is not None:
        ds = [d for d in ds if d["index"] == want] or ds
    d = max(ds, key=lambda d: d["free_mb"])
    return d["index"], d["free_mb"]


def ahead_of(lease):
    """Waiting leases that go before this one (better rank, or same rank and older)."""
    r = rank(lease["client"], lease["kind"])
    return [l for l in leases.values() if not l["granted"] and l is not lease and
            (rank(l["client"], l["kind"]), l["since"]) < (r, lease["since"])]


def acquire(client, kind, mem_mb, wait_s, ttl_s, device=None, model=None, extra_mb=0):
    """Wait (up to wait_s) until the card has room for mem_mb, making room meanwhile. Granted either way when
    time runs out - the caller then knows it may run partly on the CPU (`roomy` is False)."""
    lid = uuid.uuid4().hex[:10]
    lease = {"id": lid, "client": client, "kind": kind, "mem_mb": mem_mb, "device": None, "granted": False,
             "since": time.time(), "expires": time.time() + ttl_s, "model": model, "roomy": False}
    with lock:
        leases[lid] = lease
    t0, made = time.time(), 0.0
    while True:
        with lock:
            first = not ahead_of(lease)
        dev, free = pick_device(mem_mb, device)
        if first and free - MARGIN_MB >= mem_mb - extra_mb:
            lease.update(granted=True, device=dev, roomy=True, expires=time.time() + ttl_s)
            break
        if time.time() - t0 >= wait_s:
            lease.update(granted=True, device=dev, roomy=False, expires=time.time() + ttl_s)
            break
        if first and time.time() - made > 4:
            make_room(client); made = time.time()
        time.sleep(0.5)
    waited = time.time() - t0
    (log.info if lease["roomy"] else log.warning)("%s lease %s: %s MiB on GPU %s after %.1f s%s", client, lid, mem_mb, lease["device"],
                                                   waited, "" if lease["roomy"] else " (not enough room: it may run partly on the CPU)")
    return lease | {"waited_s": round(waited, 1)}


def renew(lid, ttl_s):
    with lock:
        if lid in leases:
            leases[lid]["expires"] = time.time() + ttl_s
            return True
    return False


def drop(lid):
    with lock:
        l = leases.pop(lid, None)
    if l:
        log.info("%s lease %s released", l["client"], lid)


BACKGROUND_MB = {"radio": 12000, "radio-art": 9000, "radio-llm": 18000}   # what each background client needs to work


def should_yield(client):
    """For background clients: step aside (finish the current piece, start nothing new, give the card back)? Yes while
    someone ahead of it is waiting for the card, or holds so much of it that both wouldn't fit (a small model like the
    speech service's voice shares the card with the radio)."""
    with lock:
        live = [l for l in leases.values() if l["client"] != client]
    r, mine = rank(client, "background"), BACKGROUND_MB.get(client, 12000)
    total = max((d["total_mb"] for d in devices()), default=24000)
    for l in live:
        if rank(l["client"], l["kind"]) >= r:
            continue
        if l["granted"] and l["mem_mb"] + mine + MARGIN_MB <= total:
            continue                                     # both fit: carry on
        who = {"assistant": "the assistant", "studio": "the Image & Video Studio", "speech": "the voice"}.get(l["client"],
              "Designer Studio" if l["client"].startswith("designer-studio") else l["client"])
        return True, f"Waiting: {who} {'is using' if l['granted'] else 'is waiting for'} the GPU"
    return False, None


def reaper():
    """Expire leases; a Studio lease lives on while ComfyUI is busy (its jobs can take minutes). Saves the state."""
    while True:
        time.sleep(2)
        save_sizes()
        now = time.time()
        with lock:
            for lid, l in list(leases.items()):
                if l["client"] == "studio" and l["granted"] and not comfy_idle():
                    l["expires"] = max(l["expires"], now + 10)
                if now > l["expires"]:
                    leases.pop(lid, None)
                    log.info("%s lease %s expired", l["client"], lid)


def status():
    with lock:
        ls = [dict(l, age_s=round(time.time() - l["since"]), expires_in_s=round(l["expires"] - time.time())) for l in leases.values()]
    return {"priority": priority(), "devices": devices(), "amd": amd_card(), "leases": ls, "holders": holders, "sizes": sizes,
            "models": {c: h["model"] for c, h in holds.items() if h.get("model")},
            "yield": {c: should_yield(c)[1] for c in ("radio", "radio-art", "radio-llm")}}


# ---------------------------------------------------------------- the lease API
class Api(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        log.debug("api %s", fmt % a)

    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        path, _, q = self.path.partition("?")
        args = dict(x.split("=", 1) for x in q.split("&") if "=" in x)
        if path == "/status":
            return self.reply(200, status())
        if path == "/yield":
            y, why = should_yield(args.get("client", "radio"))
            return self.reply(200, {"yield": y, "reason": why})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        b = self.body()
        if self.path == "/lease":
            kind = b.get("kind") if b.get("kind") in ("interactive", "background") else ("interactive" if b.get("client") in INTERACTIVE else "background")
            return self.reply(200, acquire(str(b.get("client", "unknown"))[:40], kind, int(b.get("mem_mb") or 4000),
                                           min(float(b.get("wait_s") or 0), 300), float(b.get("ttl_s") or 120), b.get("device")))
        m = re.fullmatch(r"/lease/(\w+)/renew", self.path)
        if m:
            return self.reply(200, {"ok": renew(m.group(1), float(b.get("ttl_s") or 120))})
        if self.path == "/holding":
            c = str(b.get("client", "unknown"))[:40]
            with lock:
                if b.get("holding"):
                    holders[c] = {"device": b.get("device"), "mem_mb": b.get("mem_mb"), "since": time.time()}
                else:
                    holders.pop(c, None)
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})

    def do_DELETE(self):
        m = re.fullmatch(r"/lease/(\w+)", self.path)
        if m:
            drop(m.group(1)); return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})


# ---------------------------------------------------------------- the Ollama proxy (for Hermes)
GPU_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/responses", "/api/chat", "/api/generate")
holds = {}                                        # client -> {"lease", "model"}: each chat client's model on the card
for _l in sorted(leases.values(), key=lambda l: l["since"]):          # restored after a restart of the broker
    if _l.get("model"):
        holds[_l["client"]] = {"lease": _l, "model": _l["model"]}
alock = threading.Lock()
_ctx = {}                                         # model -> its own num_ctx (Modelfile), cached


def model_ctx(model):
    if model not in _ctx:
        try:
            m = re.search(r"^num_ctx\s+(\d+)", ollama_json("/api/show", {"model": model}).get("parameters", ""), re.M)
            _ctx[model] = int(m.group(1)) if m else 0
        except Exception:
            return 0
    return _ctx[model]


def held_by_other(model, client):
    with lock:
        return next((l["client"] for l in leases.values() if l["granted"] and l.get("model") == model and l["client"] != client), None)


def cpu_only(model):
    """Models created with num_gpu 0 (the radio's CPU songwriter) never need the card."""
    try:
        return bool(re.search(r"^num_gpu\s+0\s*$", ollama_json("/api/show", {"model": model}).get("parameters", ""), re.M))
    except Exception:
        return False


reloads = {}                                      # model -> the room it had at its last reload


def reload_if_split(model):
    """A model squeezed partly onto the CPU stays that way until it's reloaded: once the card has room for all of it,
    unload it so the next request loads it fully (a reload from the page cache takes seconds; a split model is slow
    for the whole conversation)."""
    m = ollama_loaded().get(model)
    if not m or on_gpu_enough(model, m):
        return False
    full = model_mb(model)                           # its size when fully on the GPU (a split model reports more)
    _, free = pick_device(full)
    room = free + ollama_gpu_mb()                    # unloading gives back everything Ollama holds on the card
    if room - MARGIN_MB < full:
        return False
    last = reloads.get(model)
    if last and room <= last["room"] + 1024:         # it was reloaded with this much room and still didn't fit: that's
        return False                                 # the most this card can take - don't reload on every message
    on_gpu = 100 * m["size_vram"] // m["size"]
    log.info("%s is %d%% on the GPU and there is room now: reloading it fully", model, on_gpu)
    reloads[model] = {"room": room, "t": time.time()}
    try:
        ollama_json("/api/generate", {"model": model, "keep_alive": 0}, timeout=30)
        for _ in range(40):                              # wait until it's really gone, so the next load sees the room
            if model not in ollama_loaded():
                break
            time.sleep(0.25)
    except Exception:
        pass
    return True


def model_lease(client, model, wait_s):
    """Hold the card for a client's chat model; reload it if it had been squeezed partly onto the CPU. A model another
    client already has on the card is shared: no extra room needed."""
    with alock:
        h = holds.setdefault(client, {"lease": None, "model": None})
        cur = h["lease"]
        if cur and cur["id"] in leases and h["model"] == model and cur.get("roomy"):
            renew(cur["id"], ASSISTANT_IDLE_S)
            reload_if_split(model)
            return cur
        if cur:
            drop(cur["id"])
        loaded = ollama_loaded().get(model)
        shared = loaded and held_by_other(model, client) and on_gpu_enough(model, loaded)
        need = 0 if shared else model_mb(model)
        extra = ollama_gpu_mb() if loaded and not shared else 0      # what it already holds on the card counts as room
        lease = acquire(client, "interactive", need, wait_s, ASSISTANT_IDLE_S, model=model, extra_mb=extra)
        if lease["roomy"] and not shared:
            reload_if_split(model)
        h.update(lease=leases.get(lease["id"], lease), model=model)
        return lease


def choose_model(model, client="assistant"):
    """'agentora-auto': the strong model if the card can be had for it within a few seconds, else the one that fits."""
    if on_amd(model):
        return model, None                           # the 7900 is its own: no lease, nobody steps aside
    if model not in AUTO:
        if cpu_only(model):
            return model, None                       # runs on the CPU: no lease needed
        return model, model_lease(client, model, STRONG_WAIT_S)
    strong, light = AUTO[model]
    if on_amd(strong):
        return strong, None
    h = holds.get(client) or {}
    cur = h.get("lease")
    if cur and cur["id"] in leases and h.get("model") in (strong, light) and cur.get("roomy"):
        renew(cur["id"], ASSISTANT_IDLE_S)               # stay on the model the conversation is already using
        reload_if_split(h["model"])
        return h["model"], cur
    lease = model_lease(client, strong, AUTO_WAIT_S)
    if lease["roomy"]:
        return strong, lease
    log.info("auto: no room for %s right now, answering %s with %s", strong, client, light)
    return light, model_lease(client, light, 10)


def is_unload(j):
    """{"model": m, "keep_alive": 0} with nothing to answer: a request to take the model off the card."""
    return str(j.get("keep_alive", "")).strip() in ("0", "0s", "0m") and not j.get("prompt") and not j.get("messages")


def learn_size(model):
    if model in SPLIT_OK:
        return
    loaded = ollama_loaded()
    m = loaded.get(model)
    if m and m.get("size") and m["size_vram"] < m["size"] * 0.97 and len(loaded) == 1 and m["size_vram"]:
        # split: what the card holds for the part on it, scaled up, is a fair estimate of the whole (never below the guess)
        est = int(ollama_gpu_mb() * m["size"] / m["size_vram"])
        if est > sizes.get(model, 0) + 200:
            sizes[model] = est; save_sizes()
            log.info("%s needs about %d MiB on the GPU to load whole (learned from a partial load)", model, est)
        return
    if m and m.get("size") and m["size_vram"] >= m["size"] * 0.97:   # only a full load tells its real size
        # what the card really holds for it (weights + KV cache + compute buffers) when it's the only model loaded;
        # Ollama's own size leaves the buffers out (17.6 GB reported vs 20 GB held for the 27B at 64k)
        mb = max(ollama_gpu_mb(), m["size"] >> 20) if len(loaded) == 1 else m["size"] >> 20
        if abs(sizes.get(model, 0) - mb) > 200:
            sizes[model] = mb; save_sizes()


# ---------------------------------------------------------------- Assistant priority: Hermes's model stays whole
PIN_EVERY_S = 10
PIN_KEEP_ALIVE = "10m"                            # refreshed every few minutes while Assistant has priority
pin = {"note": None, "touched": 0.0, "lease": None}


def keep_assistant_model():
    """While "Who goes first" is Assistant, Hermes's model (the one it used last) stays loaded and entirely on the card:
    a standing assistant lease (so the radio steps aside and nothing else takes its room), and the model is loaded
    only when all of it fits - after making room (idle ComfyUI, lower-priority models) and waiting for running jobs to
    finish, never by squeezing it partly onto the CPU."""
    if priority() != "assistant":
        if pin["lease"]:
            drop(pin["lease"]); pin.update(lease=None, note=None)
        return
    h = holds.setdefault("assistant", {"lease": None, "model": None})
    model = h.get("model") or AUTO["agentora-auto"][0]
    if on_amd(model):                               # it has the 7900: hold nothing on the 4090, just keep it loaded there
        if pin["lease"]:
            drop(pin["lease"]); pin["lease"] = None
            if h.get("lease") and h["lease"]["id"] not in leases:
                h["lease"] = None
        keep_on_amd(model)
        return
    cur = h.get("lease")
    if cur and cur["id"] in leases and h.get("model") == model:
        renew(cur["id"], 3600)
    else:
        lid = uuid.uuid4().hex[:10]                  # a standing lease: no waiting (the broker itself holds it)
        lease = {"id": lid, "client": "assistant", "kind": "interactive", "mem_mb": model_mb(model), "device": pick_device(0)[0],
                 "granted": True, "since": time.time(), "expires": time.time() + 3600, "model": model, "roomy": True}
        with lock:
            leases[lid] = lease
        h.update(lease=lease, model=model); pin["lease"] = lid
        log.info("assistant priority: keeping %s loaded for Hermes", model)
    m = ollama_loaded().get(model)
    if m and on_gpu_enough(model, m):
        if time.time() - pin["touched"] > 240:       # keep it from timing out while nobody chats
            try:
                ollama_json("/api/generate", {"model": model, "keep_alive": PIN_KEEP_ALIVE}, timeout=30)
                pin["touched"] = time.time()
            except Exception:
                pass
        if pin["note"]:
            log.info("assistant priority: %s is loaded %s", model, "with its share on the GPU" if model in SPLIT_OK else "entirely on the GPU")
            pin["note"] = None
        return
    learn_size(model)
    need, total = model_mb(model), max((d["total_mb"] for d in devices()), default=0)
    baseline = sum(gpu_mb_of(n) for n in (b"speech_server", b"ComfyUI", b"claude-desktop", b"gnome-shell", b"Xwayland"))
    if m and total and model not in SPLIT_OK and need > total - baseline - MARGIN_MB:
        note = (f"{model} can't load whole on this card: it needs about {need} MiB, and {total} MiB minus {baseline} MiB held "
                f"by other programs leaves {total - baseline} MiB. It runs {100 * m['size_vram'] // m['size']}% on the GPU. "
                "An 8-bit KV cache (OLLAMA_KV_CACHE_TYPE=q8_0 + OLLAMA_FLASH_ATTENTION=1) or a second card fixes this.")
        if note[:60] != (pin["note"] or "")[:60]:
            log.warning("assistant priority: %s", note); pin["note"] = note
        return
    if m:                                           # squeezed partly onto the CPU: reload whole once there's room
        if not reload_if_split(model):
            with lock:
                busy = [l["client"] for l in leases.values() if l["granted"] and l["client"] != "assistant" and l["mem_mb"] > 1000]
            note = f"{model} is only {100 * m['size_vram'] // m['size']}% on the GPU; reloading it whole once {', '.join(busy) or 'the card'} makes room"
            if note != pin["note"]:
                log.info("assistant priority: %s", note); pin["note"] = note
            make_room("assistant")
        return
    need = model_mb(model)
    _, free = pick_device(need)
    if free - MARGIN_MB >= need:
        try:
            log.info("assistant priority: loading %s (%d MiB) entirely on the GPU", model, need)
            ollama_json("/api/generate", {"model": model, "keep_alive": PIN_KEEP_ALIVE}, timeout=300)
            pin["touched"] = time.time()
            threading.Thread(target=learn_size, args=(model,), daemon=True).start()
        except Exception as e:
            log.warning("assistant priority: couldn't load %s: %s", model, e)
        return
    make_room("assistant")
    with lock:
        busy = [l["client"] for l in leases.values() if l["granted"] and l["client"] != "assistant" and l["mem_mb"] > 1000]
    note = f"waiting for room to load {model} whole ({need} MiB, {free} MiB free; in use: {', '.join(busy) or 'nothing leased'})"
    if note != pin["note"]:
        log.info("assistant priority: %s", note); pin["note"] = note


def keep_on_amd(model):
    """The 7900's Ollama keeps models forever (OLLAMA_KEEP_ALIVE=-1); this loads Hermes's model there after a boot."""
    try:
        if model in [m["name"].removesuffix(":latest") for m in ollama_json("/api/ps", base=A.ollama_amd, timeout=5).get("models", [])]:
            return
        log.info("loading %s on the Radeon", model)
        ollama_json("/api/generate", {"model": model, "keep_alive": -1}, timeout=300, base=A.ollama_amd)
    except Exception as e:
        log.warning("couldn't load %s on the Radeon: %s", model, e)


def pinner():
    while True:
        try:
            keep_assistant_model()
        except Exception:
            log.warning("assistant priority keeper failed", exc_info=True)
        time.sleep(PIN_EVERY_S)


class OllamaProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"                 # stream replies until the connection closes (SSE and NDJSON alike)

    def log_message(self, fmt, *a):
        log.debug("ollama proxy %s", fmt % a)

    def forward(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        model, base = None, A.ollama
        client = re.sub(r"[^\w.-]", "", self.headers.get("X-Agentora-Client") or "")[:40] or "assistant"
        if method == "POST" and self.path.split("?")[0] in GPU_PATHS and body:
            try:
                j = json.loads(body)
                model = j.get("model", "").removesuffix(":latest")
                if model and is_unload(j):
                    h = holds.get(client)
                    if h and h.get("model") == model and h.get("lease"):
                        drop(h["lease"]["id"]); h.update(lease=None, model=None)
                    other = held_by_other(model, client)
                    if on_amd(model):                    # Hermes's model stays on its own card
                        return self.answer({"model": model, "done": True, "done_reason": "unload", "response": ""})
                    if other:                            # someone else is using it: done for this client, model stays
                        log.info("%s asked to unload %s, which %s is using: kept loaded", client, model, other)
                        return self.answer({"model": model, "done": True, "done_reason": "unload", "response": ""})
                    model = None
                elif model:
                    real, lease = choose_model(model, client)
                    changed = real != model
                    opts = j.get("options") or {}
                    own = model_ctx(real)
                    if own and opts.get("num_ctx") and int(opts["num_ctx"]) <= own:
                        opts.pop("num_ctx"); changed = True  # its own (larger) context: one loaded copy for everyone
                    if changed:
                        j["model"] = real; body = json.dumps(j).encode()
                    model = real
                    if lease is None and on_amd(real):
                        base, model = A.ollama_amd, None       # no lease to renew, nothing to learn about the 4090
            except (ValueError, TypeError):
                pass
        u = base.split("://", 1)[1]
        conn = http.client.HTTPConnection(u, timeout=900)
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "connection", "x-agentora-client")}
        if body is not None:
            hdrs["Content-Length"] = str(len(body))
        try:
            conn.request(method, self.path, body=body, headers=hdrs)
            r = conn.getresponse()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = r.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.warning("ollama proxy %s %s failed: %s", method, self.path, e)
            try:
                self.send_error(502, "Ollama didn't answer")
            except Exception:
                pass
        finally:
            conn.close()
            if model:
                cur = (holds.get(client) or {}).get("lease")
                if cur and cur["id"] in leases:
                    renew(cur["id"], ASSISTANT_IDLE_S)         # the conversation keeps the card for a while after each reply
                threading.Thread(target=learn_size, args=(model,), daemon=True).start()

    def answer(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.send_header("Connection", "close"); self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.split("?")[0] in ("/v1/models", "/api/tags"):        # list the virtual "auto" model too
            try:
                d = ollama_json(self.path)
                if "data" in d:
                    d["data"] += [{"id": k, "object": "model", "owned_by": "agentora"} for k in AUTO]
                b = json.dumps(d).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            except Exception:
                pass
        self.forward("GET")

    def do_POST(self):
        self.forward("POST")

    def do_DELETE(self):
        self.forward("DELETE")


class Server(logsetup.QuietBrokenPipes, ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    logsetup.setup("gpu-broker")
    threading.Thread(target=reaper, daemon=True, name="reaper").start()
    threading.Thread(target=pinner, daemon=True, name="assistant-priority").start()
    threading.Thread(target=Server(("127.0.0.1", A.ollama_port), OllamaProxy).serve_forever, daemon=True, name="ollama-proxy").start()
    log.info("leases on 127.0.0.1:%d, Ollama proxy on 127.0.0.1:%d -> %s (%s -> %s); devices: %s", A.port, A.ollama_port, A.ollama,
             ", ".join(sorted(AMD_MODELS)), A.ollama_amd,
             ", ".join(f"{d['index']}: {d['name']} {d['total_mb'] // 1024} GB" for d in devices()) or "none found")
    Server(("127.0.0.1", A.port), Api).serve_forever()
