#!/usr/bin/env python3
"""Agentora GPU broker: one place that decides which card runs what, and who waits, so a chat or a picture doesn't end
up half on the CPU because the radio got there first.

Two cards, two jobs:
  - the RTX 4090 (CUDA) runs everything that only runs on NVIDIA: YuE2 recordings, ComfyUI, the voice, whisper, and the
    Ollama models that live there (the radio's 27B lyricist, the 9B). It's shared, so it's handed out by *leases*.
  - the RX 7900 XTX (ROCm, its own Ollama on :11436) belongs to the models whose home it is (HOMES: Hermes's 35B, which
    the songwriter and ✨ Improve share). Nothing else uses it, so it needs no leases; the broker keeps them loaded there.
If a card's Ollama is down, its models fall back to the 4090 and the lease rules.

Leases (the 4090). Every GPU user says who it is, roughly how much memory it needs, and whether a person is waiting
("interactive": the assistant, the Image & Video Studio, the voice) or not ("background": the radio's recordings,
cover art, the lyricist). To grant a lease the broker makes room with real measurements (nvidia-smi): background
clients step aside (they ask GET /yield before starting new work), an idle ComfyUI is asked to unload, and idle
Ollama models are unloaded. Order follows "Who goes first" (~/.config/ai-gpu-priority.json): shared = a person
waiting beats background work; radio / studio / assistant / clipsmith ("designer-studio*") = that one first, and its
jobs may unload lower-priority chat models. A lease that can't get room in time is granted anyway with roomy=false.

The Ollama proxy (127.0.0.1:11435), for tools that can't ask for a lease (Hermes, Designer Studio): it sends each
model to its home card, takes a lease for 4090 models (for the client named in X-Agentora-Client, else "assistant"),
reloads a model squeezed partly onto the CPU once there's room, and maps the virtual model "agentora-auto" to the
strongest model that can run right now. Clients share one loaded copy: a smaller num_ctx than the model's own is
dropped (Ollama would reload it), and an unload (keep_alive 0) of a model someone else holds is answered without
unloading.

API (127.0.0.1:7890, JSON):
  POST /lease {client, kind: interactive|background, mem_mb, wait_s, ttl_s, device?} -> {granted, id, device, roomy, waited_s}
  POST /lease/<id>/renew {ttl_s?}     DELETE /lease/<id>
  GET  /yield?client=radio            -> {yield, reason}   (background clients: wait before starting new work)
  POST /holding {client, device, mem_mb, holding}       (a background client says it has its model on a card)
  GET  /status                        -> cards, leases (with queue positions), where each model runs
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
STATE_FILE = Path.home() / ".config" / "agentora-gpu-broker.json"     # learned model sizes and live leases
STATE_VERSION = 2                                 # sizes learned before the 8-bit KV cache (v1) are too big: start over
INTERACTIVE = {"assistant", "studio", "speech"}
MARGIN_MB = 600                                   # keep this much free beyond the estimate (CUDA context, fragmentation)
ASSISTANT_IDLE_S = int(os.environ.get("BROKER_ASSISTANT_IDLE", "180"))   # a chat keeps the card this long after its last call
AUTO = {"agentora-auto": ("qwen3.6-agent-moe", "qwen3.5-agent-fast")}   # strongest first, then the one that always fits
HOMES = {"qwen3.6-agent-moe": "amd"}              # models with a card of their own (21 GB at 64k: one fits the 7900)
# Mixture-of-experts models run well partly on the CPU (only ~3B of the 35B work per word): on the 4090 they need this
# much of it to be fast, take whatever more Ollama gives them, and are never reloaded just to get "whole".
SPLIT_OK = {"qwen3.6-agent-moe": 12000}
DEFAULT_MB = {"qwen3.6-agent": 20000, "qwen3.5-agent-fast": 9000, **SPLIT_OK}
AUTO_WAIT_S = 3.0                                 # how long "auto" waits for room for the strong model
STRONG_WAIT_S = 45.0                              # a model asked for by name waits this long for room, then loads anyway
BACKGROUND_MB = {"radio": 12000, "radio-art": 9000, "radio-llm": 18000}   # what each background client needs to work
KEEP_EVERY_S = 10
PIN_KEEP_ALIVE = "10m"                            # Hermes's 4090 model under Assistant priority, refreshed while it lasts
WHO = {"assistant": "the assistant", "studio": "the Image & Video Studio", "speech": "the voice"}

lock = threading.RLock()
leases = {}                                       # id -> {client, kind, mem_mb, device, granted, since, expires, model, roomy}
holders = {}                                      # client -> {device, mem_mb, since}   (background models on a card)
sizes = {}                                        # model -> MiB it really holds on the 4090 when whole (learned)
try:
    _saved = json.loads(STATE_FILE.read_text())
    if _saved.get("v") == STATE_VERSION:
        sizes.update(_saved.get("sizes", {}))
    leases.update({k: v for k, v in _saved.get("leases", {}).items() if v.get("expires", 0) > time.time() and v.get("granted")})
except (OSError, ValueError):
    pass


def save_state():
    """Learned sizes and the live leases survive a restart of the broker (else the radio could grab the card mid-chat)."""
    try:
        with lock:
            data = json.dumps({"v": STATE_VERSION, "sizes": sizes, "leases": {k: v for k, v in leases.items() if v.get("granted")}})
        tmp = STATE_FILE.with_suffix(".tmp"); tmp.write_text(data); tmp.replace(STATE_FILE)
    except OSError:
        pass


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


def who(client):
    return WHO.get(client, "Designer Studio" if client.startswith("designer-studio") else client)


# ---------------------------------------------------------------- Ollama, one per card
class Ollama:
    def __init__(self, url, name):
        self.url, self.name, self._up = url, name, {"t": 0.0, "v": False}

    def json(self, path, body=None, timeout=10):
        r = urllib.request.Request(self.url + path, json.dumps(body).encode() if body is not None else None,
                                   {"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")

    def up(self):
        """Answering? (cached for 5 s; changes are logged)"""
        if time.time() - self._up["t"] > 5:
            try:
                self.json("/api/version", timeout=2); v = True
            except Exception:
                v = False
            if v != self._up["v"] and self is not OLLAMA["nvidia"]:
                log.info("%s Ollama (%s) is %s", self.name, self.url, "up" if v else "down: its models fall back to the 4090")
            self._up.update(t=time.time(), v=v)
        return self._up["v"]

    def loaded(self):
        try:
            return {m["name"].removesuffix(":latest"): m for m in self.json("/api/ps").get("models", [])}
        except Exception:
            return {}

    def unload(self, model, timeout=30):
        self.json("/api/generate", {"model": model, "keep_alive": 0}, timeout=timeout)


OLLAMA = {"nvidia": Ollama(A.ollama, "RTX 4090"), "amd": Ollama(A.ollama_amd, "Radeon")}
NV = OLLAMA["nvidia"]


def where(model):
    """Which card's Ollama runs this model right now: its home if that's up, else the 4090."""
    home = HOMES.get(model)
    return home if home and OLLAMA[home].up() else "nvidia"


# ---------------------------------------------------------------- the cards
_nv = {"t": 0.0, "v": []}


def nvidia_cards():
    """[{index, name, total_mb, used_mb, free_mb}] from nvidia-smi (cached for a second). Leases are on these."""
    if time.time() - _nv["t"] > 1:
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10).stdout
            v = []
            for line in out.strip().splitlines():
                i, name, total, used = [x.strip() for x in line.split(",")]
                v.append({"index": int(i), "vendor": "nvidia", "name": name, "total_mb": int(total), "used_mb": int(used),
                          "free_mb": int(total) - int(used)})
            _nv.update(t=time.time(), v=v)
        except Exception as e:
            log.warning("nvidia-smi failed: %s", e)
    return _nv["v"]


def amd_card():
    """The Radeon from sysfs (no extra tools needed), with what its Ollama has loaded."""
    for d in sorted(Path("/sys/bus/pci/devices").glob("*")):
        try:
            if (d / "vendor").read_text().strip() != "0x1002" or not (d / "mem_info_vram_total").exists():
                continue
            total, used = (int((d / f).read_text()) >> 20 for f in ("mem_info_vram_total", "mem_info_vram_used"))
            up = OLLAMA["amd"].up()
            return {"index": "amd0", "vendor": "amd", "name": "AMD Radeon RX 7900 XTX", "pci": d.name, "total_mb": total,
                    "used_mb": used, "free_mb": total - used, "ollama": A.ollama_amd, "up": up,
                    "models": sorted(OLLAMA["amd"].loaded()) if up else [], "home_of": sorted(m for m, h in HOMES.items() if h == "amd")}
        except (OSError, ValueError):
            continue
    return None


def gpu_mb_of(name):
    """4090 memory held by processes whose command line contains name (bytes)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        total = 0
        for line in out.strip().splitlines():
            pid, mb = [x.strip() for x in line.split(",")]
            try:
                if name in Path(f"/proc/{pid}/cmdline").read_bytes():
                    total += int(mb)
            except OSError:
                continue
        return total
    except Exception:
        return 0


def ollama_gpu_mb():
    """What the 4090's Ollama runners really hold (weights + KV cache + compute buffers; Ollama's own size_vram leaves
    the buffers out)."""
    return gpu_mb_of(b"ollama")


def pick_device(want=None):
    """(index, free MiB) of the 4090-side card with the most room, or the one asked for."""
    ds = nvidia_cards()
    if not ds:
        return None, 0
    if want is not None:
        ds = [d for d in ds if d["index"] == want] or ds
    d = max(ds, key=lambda d: d["free_mb"])
    return d["index"], d["free_mb"]


def total_mb():
    return max((d["total_mb"] for d in nvidia_cards()), default=24000)


# ---------------------------------------------------------------- model facts (4090 side)
_show = {}                                        # model -> its Modelfile parameters, cached


def params(model):
    if model not in _show:
        try:
            _show[model] = NV.json("/api/show", {"model": model}).get("parameters", "")
        except Exception:
            return ""
    return _show[model]


def model_ctx(model):
    m = re.search(r"^num_ctx\s+(\d+)", params(model), re.M)
    return int(m.group(1)) if m else 0


def cpu_only(model):
    """Models created with num_gpu 0 (the radio's CPU songwriter) never need a card."""
    return bool(re.search(r"^num_gpu\s+0\s*$", params(model), re.M))


def model_mb(model):
    return SPLIT_OK.get(model) or sizes.get(model) or DEFAULT_MB.get(model) or 10000


def on_gpu_enough(model, m):
    """Loaded well enough: all of it, or for a mixture-of-experts model, at least its share of the card."""
    if model in SPLIT_OK:
        return (m["size_vram"] >> 20) >= SPLIT_OK[model] * 0.9
    return m["size_vram"] >= m["size"] * 0.97


def learn_size(model):
    """What a model really holds on the 4090, from what Ollama reports once it's loaded (only while it's the only one)."""
    if model in SPLIT_OK or where(model) != "nvidia":
        return
    loaded = NV.loaded()
    m = loaded.get(model)
    if not m or not m.get("size") or not m["size_vram"] or len(loaded) != 1:
        return
    if m["size_vram"] < m["size"] * 0.97:
        # split: what the card holds for the part on it, scaled up, is a fair estimate of the whole (never below the guess)
        est = int(ollama_gpu_mb() * m["size"] / m["size_vram"])
        if est > sizes.get(model, 0) + 200:
            sizes[model] = est; save_state()
            log.info("%s needs about %d MiB on the 4090 to load whole (learned from a partial load)", model, est)
        return
    mb = max(ollama_gpu_mb(), m["size"] >> 20)       # Ollama's size leaves the compute buffers out
    if abs(sizes.get(model, 0) - mb) > 200:
        sizes[model] = mb; save_state()


# ---------------------------------------------------------------- making room on the 4090
def comfy_idle():
    try:
        with urllib.request.urlopen(A.comfy + "/queue", timeout=3) as r:
            q = json.loads(r.read())
        return not q.get("queue_running") and not q.get("queue_pending")
    except Exception:
        return False


def kind_of(client, kind=None):
    if kind in ("interactive", "background"):
        return kind
    if kind == "batch":                              # Designer Studio's dubbing worker: nobody watches it
        return "background"
    return "interactive" if client in INTERACTIVE or client.startswith("designer-studio") else "background"


def make_room(for_client):
    """Free what isn't needed right now: an idle ComfyUI's models, and 4090 Ollama models nobody ahead of or level with
    for_client holds. Background clients (the radio) see the waiting lease through /yield and step aside on their own."""
    freed = []
    with lock:
        studio_busy = any(l["client"] == "studio" and l["granted"] for l in leases.values())
        me = rank(for_client, kind_of(for_client))
        keep = {l.get("model") for l in leases.values() if l.get("model") and (not l["granted"] or rank(l["client"], l["kind"]) <= me)}
    if not studio_busy and comfy_idle() and gpu_mb_of(b"ComfyUI") > 1500:
        try:
            urllib.request.urlopen(urllib.request.Request(A.comfy + "/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
                                                          {"Content-Type": "application/json"}), timeout=10).read()
            freed.append("ComfyUI")
        except Exception:
            pass
    for name, m in NV.loaded().items():
        if m.get("size_vram") and name not in keep:
            try:
                NV.unload(name); freed.append(name)
            except Exception:
                pass
    if freed:
        log.info("made room for %s: unloaded %s", for_client, ", ".join(freed))


# ---------------------------------------------------------------- leases
def ahead_of(lease):
    """Waiting leases that go before this one (better rank, or same rank and older)."""
    r = rank(lease["client"], lease["kind"])
    return [l for l in leases.values() if not l["granted"] and l is not lease and
            (rank(l["client"], l["kind"]), l["since"]) < (r, lease["since"])]


def acquire(client, kind, mem_mb, wait_s, ttl_s, device=None, model=None, extra_mb=0):
    """Wait (up to wait_s) until the 4090 has room for mem_mb, making room meanwhile. Granted either way when time runs
    out - the caller then knows it may run partly on the CPU (`roomy` is False). extra_mb: memory the client already
    holds there (counts as room)."""
    lid = uuid.uuid4().hex[:10]
    lease = {"id": lid, "client": client, "kind": kind, "mem_mb": mem_mb, "device": None, "granted": False,
             "since": time.time(), "expires": time.time() + ttl_s, "model": model, "roomy": False}
    with lock:
        leases[lid] = lease
    t0, made = time.time(), 0.0
    while True:
        with lock:
            first = not ahead_of(lease)
        dev, free = pick_device(device)
        roomy = first and free - MARGIN_MB >= mem_mb - extra_mb
        if roomy or time.time() - t0 >= wait_s:
            lease.update(granted=True, device=dev, roomy=roomy, expires=time.time() + ttl_s)
            break
        if first and time.time() - made > 4:
            make_room(client); made = time.time()
        time.sleep(0.5)
    waited = time.time() - t0
    (log.info if lease["roomy"] else log.warning)("%s lease %s: %s MiB on GPU %s after %.1f s%s", client, lid, mem_mb, lease["device"],
                                                   waited, "" if lease["roomy"] else " (not enough room: it may run partly on the CPU)")
    return lease | {"waited_s": round(waited, 1)}


def standing_lease(client, model, ttl_s):
    """A lease granted at once (the broker itself holds the room, e.g. Hermes's model under Assistant priority)."""
    lid = uuid.uuid4().hex[:10]
    lease = {"id": lid, "client": client, "kind": "interactive", "mem_mb": model_mb(model), "device": pick_device()[0],
             "granted": True, "since": time.time(), "expires": time.time() + ttl_s, "model": model, "roomy": True}
    with lock:
        leases[lid] = lease
    return lease


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


def should_yield(client):
    """For background clients: step aside (finish the current piece, start nothing new, give the card back)? Yes while
    someone ahead of it is waiting for the card, or holds so much of it that both wouldn't fit (a small model like the
    voice shares the card with the radio)."""
    with lock:
        live = [l for l in leases.values() if l["client"] != client]
    r, mine = rank(client, "background"), BACKGROUND_MB.get(client, 12000)
    for l in live:
        if rank(l["client"], l["kind"]) >= r:
            continue
        if l["granted"] and l["mem_mb"] + mine + MARGIN_MB <= total_mb():
            continue                                     # both fit: carry on
        return True, f"Waiting: {who(l['client'])} {'is using' if l['granted'] else 'is waiting for'} the GPU"
    return False, None


def reaper():
    """Expire leases; a Studio lease lives on while ComfyUI is busy (its jobs can take minutes). Saves the state."""
    while True:
        time.sleep(2)
        save_state()
        now = time.time()
        with lock:
            for lid, l in list(leases.items()):
                if l["client"] == "studio" and l["granted"] and not comfy_idle():
                    l["expires"] = max(l["expires"], now + 10)
                if now > l["expires"]:
                    leases.pop(lid, None)
                    log.info("%s lease %s expired", l["client"], lid)


# ---------------------------------------------------------------- chat models (the proxy's side of leases)
holds = {}                                        # client -> {"lease", "model"}: each chat client's model on the 4090
for _l in sorted(leases.values(), key=lambda l: l["since"]):          # restored after a restart of the broker
    if _l.get("model"):
        holds[_l["client"]] = {"lease": _l, "model": _l["model"]}
alock = threading.Lock()
reloads = {}                                      # model -> the room it had at its last reload


def held_by_other(model, client):
    with lock:
        return next((l["client"] for l in leases.values() if l["granted"] and l.get("model") == model and l["client"] != client), None)


def reload_if_split(model):
    """A model squeezed partly onto the CPU stays that way until it's reloaded: once the 4090 has room for all of it,
    unload it so the next request loads it fully (a reload from the page cache takes seconds; a split model is slow
    for the whole conversation)."""
    m = NV.loaded().get(model)
    if not m or on_gpu_enough(model, m):
        return False
    full = model_mb(model)
    room = pick_device()[1] + ollama_gpu_mb()        # unloading gives back everything Ollama holds on the card
    if room - MARGIN_MB < full:
        return False
    last = reloads.get(model)
    if last and room <= last["room"] + 1024:         # it was reloaded with this much room and still didn't fit: that's
        return False                                 # the most this card can take - don't reload on every message
    log.info("%s is %d%% on the GPU and there is room now: reloading it fully", model, 100 * m["size_vram"] // m["size"])
    reloads[model] = {"room": room, "t": time.time()}
    try:
        NV.unload(model)
        for _ in range(40):                          # wait until it's really gone, so the next load sees the room
            if model not in NV.loaded():
                break
            time.sleep(0.25)
    except Exception:
        pass
    return True


def model_lease(client, model, wait_s):
    """Hold the 4090 for a client's chat model; reload it if it had been squeezed partly onto the CPU. A model another
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
        loaded = NV.loaded().get(model)
        shared = loaded and held_by_other(model, client) and on_gpu_enough(model, loaded)
        need = 0 if shared else model_mb(model)
        extra = ollama_gpu_mb() if loaded and not shared else 0
        lease = acquire(client, "interactive", need, wait_s, ASSISTANT_IDLE_S, model=model, extra_mb=extra)
        if lease["roomy"] and not shared:
            reload_if_split(model)
        h.update(lease=leases.get(lease["id"], lease), model=model)
        return lease


def choose_model(model, client="assistant"):
    """-> (the model to run, the Ollama to send it to, its 4090 lease or None). 'agentora-auto' is the strong model when
    it has its own card or the 4090 can be had for it within a few seconds, else the one that fits."""
    if model in AUTO:
        strong, light = AUTO[model]
        if where(strong) != "nvidia":
            return strong, OLLAMA[where(strong)], None
        h = holds.get(client) or {}
        cur = h.get("lease")
        if cur and cur["id"] in leases and h.get("model") in (strong, light) and cur.get("roomy"):
            renew(cur["id"], ASSISTANT_IDLE_S)           # stay on the model the conversation is already using
            reload_if_split(h["model"])
            return h["model"], NV, cur
        lease = model_lease(client, strong, AUTO_WAIT_S)
        if lease["roomy"]:
            return strong, NV, lease
        log.info("auto: no room for %s right now, answering %s with %s", strong, client, light)
        return light, NV, model_lease(client, light, 10)
    if where(model) != "nvidia":
        return model, OLLAMA[where(model)], None     # its own card: no lease, nobody steps aside
    if cpu_only(model):
        return model, NV, None
    return model, NV, model_lease(client, model, STRONG_WAIT_S)


def is_unload(j):
    """{"model": m, "keep_alive": 0} with nothing to answer: a request to take the model off the card."""
    return str(j.get("keep_alive", "")).strip() in ("0", "0s", "0m") and not j.get("prompt") and not j.get("messages")


# ---------------------------------------------------------------- the keeper: models stay where they belong
keep = {"note": None, "touched": 0.0, "lease": None, "loading": set()}


def keep_homes():
    """Models with a card of their own stay loaded there (that Ollama also has OLLAMA_KEEP_ALIVE=-1); after a boot or
    a crash of it, load them again."""
    for model, home in HOMES.items():
        o = OLLAMA[home]
        if not o.up() or model in o.loaded() or model in keep["loading"]:
            continue
        keep["loading"].add(model)
        try:
            log.info("loading %s on the %s", model, o.name)
            o.json("/api/generate", {"model": model, "keep_alive": -1}, timeout=300)
        except Exception as e:
            log.warning("couldn't load %s on the %s: %s", model, o.name, e)
        finally:
            keep["loading"].discard(model)


def keep_assistant_model():
    """While "Who goes first" is Assistant and Hermes's model runs on the 4090 (its own card is down, or Hermes picked
    a 4090 model), it stays loaded and entirely on the card: a standing assistant lease (so the radio steps aside and
    nothing else takes its room), and it's loaded only when all of it fits - after making room and waiting for running
    jobs, never by squeezing it partly onto the CPU."""
    h = holds.setdefault("assistant", {"lease": None, "model": None})
    model = h.get("model") or AUTO["agentora-auto"][0]
    if priority() != "assistant" or where(model) != "nvidia":
        if keep["lease"]:
            drop(keep["lease"]); keep.update(lease=None, note=None)
            if h.get("lease") and h["lease"]["id"] not in leases:
                h["lease"] = None
        return
    cur = h.get("lease")
    if cur and cur["id"] in leases and h.get("model") == model:
        renew(cur["id"], 3600)
    else:
        lease = standing_lease("assistant", model, 3600)
        h.update(lease=lease, model=model); keep["lease"] = lease["id"]
        log.info("assistant priority: keeping %s loaded for Hermes", model)

    def note(level, text):
        if text[:60] != (keep["note"] or "")[:60]:
            log.log(level, "assistant priority: %s", text); keep["note"] = text

    m = NV.loaded().get(model)
    if m and on_gpu_enough(model, m):
        if time.time() - keep["touched"] > 240:      # keep it from timing out while nobody chats
            try:
                NV.json("/api/generate", {"model": model, "keep_alive": PIN_KEEP_ALIVE}, timeout=30); keep["touched"] = time.time()
            except Exception:
                pass
        if keep["note"]:
            log.info("assistant priority: %s is loaded %s", model, "with its share on the GPU" if model in SPLIT_OK else "entirely on the GPU")
            keep["note"] = None
        return
    learn_size(model)
    need = model_mb(model)
    baseline = sum(gpu_mb_of(n) for n in (b"speech_server", b"ComfyUI", b"claude-desktop", b"gnome-shell", b"Xwayland"))
    if m and model not in SPLIT_OK and need > total_mb() - baseline - MARGIN_MB:
        return note(logging.WARNING, f"{model} can't load whole on the 4090: it needs about {need} MiB, and {total_mb()} MiB minus "
                    f"{baseline} MiB held by other programs leaves {total_mb() - baseline} MiB. It runs "
                    f"{100 * m['size_vram'] // m['size']}% on the GPU.")
    with lock:
        busy = [l["client"] for l in leases.values() if l["granted"] and l["client"] != "assistant" and l["mem_mb"] > 1000]
    if m:                                           # squeezed partly onto the CPU: reload whole once there's room
        if not reload_if_split(model):
            note(logging.INFO, f"{model} is only {100 * m['size_vram'] // m['size']}% on the GPU; reloading it whole once "
                 f"{', '.join(busy) or 'the card'} makes room")
            make_room("assistant")
        return
    free = pick_device()[1]
    if free - MARGIN_MB >= need:
        try:
            log.info("assistant priority: loading %s (%d MiB) entirely on the GPU", model, need)
            NV.json("/api/generate", {"model": model, "keep_alive": PIN_KEEP_ALIVE}, timeout=300); keep["touched"] = time.time()
            threading.Thread(target=learn_size, args=(model,), daemon=True).start()
        except Exception as e:
            log.warning("assistant priority: couldn't load %s: %s", model, e)
        return
    make_room("assistant")
    note(logging.INFO, f"waiting for room to load {model} whole ({need} MiB, {free} MiB free; in use: {', '.join(busy) or 'nothing leased'})")


def keeper():
    while True:
        for f in (keep_homes, keep_assistant_model):
            try:
                f()
            except Exception:
                log.warning("%s failed", f.__name__, exc_info=True)
        time.sleep(KEEP_EVERY_S)


# ---------------------------------------------------------------- status
def status():
    now = time.time()
    with lock:
        ls = []
        for l in leases.values():
            v = dict(l, age_s=round(now - l["since"]), expires_in_s=round(l["expires"] - now))
            if not l["granted"]:
                v["queue_position"] = len(ahead_of(l)) + 1
            ls.append(v)
    amd = amd_card()
    models = sorted(set(NV.loaded()) | set(HOMES) | set(DEFAULT_MB))
    return {"priority": priority(), "devices": nvidia_cards() + ([amd] if amd else []), "leases": ls, "holders": holders,
            "sizes": sizes, "models": {c: h["model"] for c, h in holds.items() if h.get("model")},
            "placement": {m: where(m) for m in models}, "loaded": {k: sorted(o.loaded()) for k, o in OLLAMA.items() if o.up()},
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
            client = str(b.get("client", "unknown"))[:40]
            return self.reply(200, acquire(client, kind_of(client, b.get("kind")), int(b.get("mem_mb") or 4000),
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


# ---------------------------------------------------------------- the Ollama proxy (for Hermes and Designer Studio)
GPU_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/responses", "/api/chat", "/api/generate")


class OllamaProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"                 # stream replies until the connection closes (SSE and NDJSON alike)

    def log_message(self, fmt, *a):
        log.debug("ollama proxy %s", fmt % a)

    def route(self, client, body):
        """-> (body to send, the Ollama to send it to, the 4090 model this request holds or None), or a dict to answer
        straight away."""
        try:
            j = json.loads(body)
            model = j.get("model", "").removesuffix(":latest")
        except (ValueError, TypeError, AttributeError):
            return body, NV, None
        if not model:
            return body, NV, None
        if is_unload(j):
            h = holds.get(client)
            if h and h.get("model") == model and h.get("lease"):
                drop(h["lease"]["id"]); h.update(lease=None, model=None)
            if where(model) != "nvidia":                 # a model with its own card stays loaded there
                return {"model": model, "done": True, "done_reason": "unload", "response": ""}
            other = held_by_other(model, client)
            if other:                                    # someone else is using it: done for this client, model stays
                log.info("%s asked to unload %s, which %s is using: kept loaded", client, model, other)
                return {"model": model, "done": True, "done_reason": "unload", "response": ""}
            return body, NV, None
        real, ollama, lease = choose_model(model, client)
        changed = real != model
        opts = j.get("options") or {}
        own = model_ctx(real)
        if own and opts.get("num_ctx") and int(opts["num_ctx"]) <= own:
            opts.pop("num_ctx"); changed = True          # its own (larger) context: one loaded copy for everyone
        if changed:
            j["model"] = real; body = json.dumps(j).encode()
        return body, ollama, real if ollama is NV else None

    def forward(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        client = re.sub(r"[^\w.-]", "", self.headers.get("X-Agentora-Client") or "")[:40] or "assistant"
        ollama, model = NV, None
        if method == "POST" and self.path.split("?")[0] in GPU_PATHS and body:
            r = self.route(client, body)
            if isinstance(r, dict):
                return self.answer(r)
            body, ollama, model = r
        conn = http.client.HTTPConnection(ollama.url.split("://", 1)[1], timeout=900)
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
            log.warning("ollama proxy %s %s -> %s failed: %s", method, self.path, ollama.name, e)
            try:
                self.send_error(502, "Ollama didn't answer")
            except Exception:
                pass
        finally:
            conn.close()
            if model:
                cur = (holds.get(client) or {}).get("lease")
                if cur and cur["id"] in leases:
                    renew(cur["id"], ASSISTANT_IDLE_S)   # the conversation keeps the card for a while after each reply
                threading.Thread(target=learn_size, args=(model,), daemon=True).start()

    def answer(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.send_header("Connection", "close"); self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.split("?")[0] in ("/v1/models", "/api/tags"):        # list the virtual "auto" model too
            try:
                d = NV.json(self.path)
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
    threading.Thread(target=keeper, daemon=True, name="keeper").start()
    threading.Thread(target=Server(("127.0.0.1", A.ollama_port), OllamaProxy).serve_forever, daemon=True, name="ollama-proxy").start()
    amd = amd_card()
    log.info("leases on 127.0.0.1:%d, Ollama proxy on 127.0.0.1:%d; cards: %s; homes: %s", A.port, A.ollama_port,
             ", ".join([f"{d['index']}: {d['name']} {d['total_mb'] // 1024} GB (leases, {A.ollama})" for d in nvidia_cards()] +
                       ([f"{amd['name']} {amd['total_mb'] // 1024} GB ({A.ollama_amd}, {'up' if amd['up'] else 'down'})"] if amd else [])) or "none found",
             ", ".join(f"{m} -> {h}" for m, h in HOMES.items()))
    Server(("127.0.0.1", A.port), Api).serve_forever()
