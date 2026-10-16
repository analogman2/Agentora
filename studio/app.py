#!/usr/bin/env python3
"""Agentora Music Studio: a web UI and job queue for YuE2 (originally on an Intel XPU).

GPU policy: when a song is queued and the LLM service holds the GPUs, the
studio pauses the LLM and starts a GPU child process that loads YuE2 and stays
loaded, so later songs start immediately. The LLM is not restarted
automatically: someone presses "Give GPUs back" (POST /api/gpu/release),
which ends the child process (the only way to fully release its GPU memory),
waits until the cards are free, then starts the LLM. STUDIO_RETURN_MINUTES > 0
re-enables an automatic return after that many idle minutes (default 0 = never).

Library layout: DATA/<job id>/job.json, plus DATA/<job id>/song/ (YuE2 artifacts)
and song.mp3. Stdlib HTTP server; run with the YuE2 venv's python.
"""
import dataclasses, json, multiprocessing as mp, os, queue, urllib.request, random, re, shutil, subprocess, sys, threading, time, traceback, uuid
import logging
import logsetup                     # logging for every component: journal + logs/<component>.log
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("STUDIO_DATA", "~/yue2/studio/library")).expanduser()
DEVICES = [d.strip() for d in os.environ.get("STUDIO_DEVICES", os.environ.get("STUDIO_DEVICE", "xpu:0,xpu:1")).split(",") if d.strip()]
DEVICE = DEVICES[0]
RETURN_MINUTES = float(os.environ.get("STUDIO_RETURN_MINUTES", "0"))  # 0 = never auto-return
LLM_UNIT = os.environ.get("STUDIO_LLM_UNIT", "vllm-qwen38.service")
PORT = int(os.environ.get("STUDIO_PORT", "7860"))
SKILL = Path(os.environ.get("STUDIO_INSTRUMENTAL", "~/yue2/YuE/skills/yue2-music/instrumental/scripts")).expanduser()
MAX_QUEUED = 24
# Lyrics / songwriter LLMs (OpenAI-compatible), tried in order: "base_url|model|label,...". The local LLM is
# paused while the studio holds the GPUs, so add a second server on another machine as the fallback.
LYRICS_LLMS = [tuple(x.split("|")) for x in os.environ.get(
    "STUDIO_LYRICS_LLMS", "http://localhost:8000/v1|qwen38|local LLM").split(",") if x.strip()]
# extra JSON fields for every songwriter/lyrics request, e.g. '{"reasoning_effort": "none"}' to stop Ollama models thinking
LLM_EXTRA = json.loads(os.environ.get("STUDIO_LLM_EXTRA") or "{}")
# seconds to wait for one song spec; a CPU-only songwriter answers requests one at a time
LLM_TIMEOUT = int(os.environ.get("STUDIO_LLM_TIMEOUT", "180"))
TOKENS_PER_AUDIO_SECOND = 25          # YuE2 semantic tokens per second of audio
GPU_STATE = DATA.parent / "gpu.json"  # remembers whether we paused the LLM (survives restarts)
FILES = {"audio.flac": ("song/audio.flac", "audio/flac"), "song.mp3": ("song.mp3", "audio/mpeg"), "cover.jpg": ("cover.jpg", "image/jpeg"),
         "score.abc": ("song/score.abc", "text/plain; charset=utf-8"),
         "source.abc": ("source.abc", "text/plain; charset=utf-8")}
SAMPLING_KEYS = {"temperature": (float, 0.0, 5.0), "top_p": (float, 0.01, 1.0), "top_k": (int, 1, 1000),
                 "repetition_penalty": (float, 0.5, 3.0)}

lock = threading.RLock()
jobs: dict[str, dict] = {}
gpu = {"owner": "llm", "detail": "", "idle_since": None, "release_requested": False, "release_on_idle": False, "workers": []}


def log(*a):
    """Log a line as the calling module (radio_spool, art, ...) at a level read from the text (see logsetup.level_of)."""
    msg = " ".join(str(x) for x in a)
    name = sys._getframe(1).f_globals.get("__name__", "studio")
    logging.getLogger("studio" if name in ("__main__", "__mp_main__", "app") else name).log(logsetup.level_of(msg), msg)


def sh(*cmd, timeout=180):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception as e:
        return f"error: {e}"


# ---- GPU priority, shared with the Image & Video Studio: which AI tool gets the graphics card first
PRIORITY_FILE = Path.home() / ".config" / "ai-gpu-priority.json"
PRIORITIES = {"shared": "Shared — take turns", "studio": "Image & Video Studio", "radio": "Radio", "assistant": "Assistant (Hermes)",
              "clipsmith": "Clipsmith (Designer Studio)"}
_prio = {"t": 0, "v": "shared"}


def gpu_priority():
    if time.time() - _prio["t"] > 2:
        try:
            v = json.loads(PRIORITY_FILE.read_text()).get("priority")
            _prio["v"] = v if v in PRIORITIES else "shared"
        except (OSError, ValueError):
            _prio["v"] = "shared"
        _prio["t"] = time.time()
    return _prio["v"]


def set_gpu_priority(v):
    if v not in PRIORITIES:
        raise BadRequest("priority must be one of: " + ", ".join(PRIORITIES))
    PRIORITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRIORITY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"priority": v, "set": time.time()}))
    tmp.replace(PRIORITY_FILE)
    _prio.update(t=time.time(), v=v)
    return {"priority": v, "choices": PRIORITIES}


# ---- disk usage for the side panel (same folders the Image & Video Studio reports)
DISK_PARTS = [("ComfyUI models", Path.home() / "ComfyUI" / "models"), ("Chat models (Ollama)", Path("/usr/share/ollama/.ollama/models")),
              ("Creations", Path.home() / "ComfyUI" / "output"), ("Radio songs", DATA)]
_disk = {"t": 0, "parts": None}
_disk_lock = threading.Lock()


def _tree_bytes(root):
    total, stack = 0, [str(root)]
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


def disk_view():
    du = shutil.disk_usage(DATA if DATA.exists() else Path.home())
    if (_disk["parts"] is None or time.time() - _disk["t"] > 300) and _disk_lock.acquire(blocking=False):
        try:
            _disk.update(t=time.time(), parts=[{"label": l, "bytes": _tree_bytes(d)} for l, d in DISK_PARTS if d.exists()])
        finally:
            _disk_lock.release()
    return {"total": du.total, "used": du.used, "free": du.free, "parts": _disk["parts"] or []}


def _get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# Models the radio shares with the Hermes assistant (AGI = the GPU lyricist, Lemming = the GPU fallback writer): they count
# as the radio's only while the radio has used them recently (Ollama keeps a model ~5 min after its last request).
SHARED_WITH_ASSISTANT = {"qwen3.6-agent", "qwen3.5-agent-fast"}
_llm_used = {}


def llm_used(model):
    """The radio (songwriter, lyricist, art scenes, station helper) is about to call this Ollama model."""
    _llm_used[model] = time.time()


def radio_writer_models():
    """The radio's own songwriter models (settings keep LYRICS_LLMS current); these never count as the assistant."""
    import radio
    # only writers on the 4090's Ollama: the Radeon's (Hermes's 35B, shared by the songwriter) is never the radio's to unload
    names = {m for u, m, _ in LYRICS_LLMS if m and ":11434" in u} | {radio.LYRICIST[1]}   # the GPU lyricist is the radio's too
    names = {m for m in names if m not in SHARED_WITH_ASSISTANT or time.time() - _llm_used.get(m, 0) < 420
             or (m == radio.LYRICIST[1] and radio.bank_writing.is_set())}
    return names | {m + ":latest" for m in names if ":" not in m}       # Ollama reports untagged names as :latest


def assistant_on_gpu():
    """Ollama models on the graphics card that aren't the radio's own songwriters (e.g. the Hermes assistant's)."""
    try:
        own = radio_writer_models()
        return [m["name"] for m in _get_json("http://127.0.0.1:11434/api/ps").get("models", []) if m.get("size_vram") and m["name"] not in own]
    except Exception:
        return []


def comfy_busy():
    try:
        q = _get_json("http://127.0.0.1:8188/queue")
        return bool(q.get("queue_running") or q.get("queue_pending"))
    except Exception:
        return False


BROKER = os.environ.get("GPU_BROKER", "http://127.0.0.1:7890")   # gpu_broker.py: who gets the card (leases)
_broker = {"t": 0.0, "v": {}}


def broker_yield(client="radio"):
    """The GPU broker's answer for a background client: a reason to wait (someone is waiting for or using the card
    ahead of us), or None. If the broker isn't running, the radio decides on its own as before."""
    if time.time() - _broker["t"] > 1 or client not in _broker["v"]:
        try:
            with urllib.request.urlopen(f"{BROKER}/yield?client={client}", timeout=2) as r:
                d = json.loads(r.read())
            _broker["v"][client] = d["reason"] if d.get("yield") else None
        except Exception:
            _broker["v"][client] = None
        _broker["t"] = time.time()
    return _broker["v"][client]


def broker_holding(holding, mem_mb=12000, device=0, client="radio"):
    """Tell the broker the radio has (or no longer has) YuE2 on a card."""
    try:
        urllib.request.urlopen(urllib.request.Request(f"{BROKER}/holding", json.dumps(
            {"client": client, "holding": holding, "mem_mb": mem_mb, "device": device}).encode(),
            {"Content-Type": "application/json"}), timeout=2).read()
    except Exception:
        pass


def gpu_yield_reason():
    """Why the radio should not take the GPU right now under the chosen priority, or None."""
    why = broker_yield("radio")
    if why:
        return why
    p = gpu_priority()
    if p == "studio" and comfy_busy():
        return "Waiting: the Image & Video Studio has GPU priority and is making something"
    if p == "assistant" and assistant_on_gpu():
        return "Waiting: the assistant has GPU priority and its model is loaded"
    return None


def free_comfy_if_idle():
    """Radio priority: ComfyUI keeps models on the card after a job; ask it to let go when it isn't working."""
    if comfy_busy():
        return
    try:
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8188/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
                                                      {"Content-Type": "application/json"}), timeout=10).read()
        log("asked ComfyUI to unload its models (radio has GPU priority)")
    except Exception as e:
        log("couldn't ask ComfyUI to free memory:", e)


def unload_ollama_gpu_models():
    """Ollama models sitting on the graphics card (e.g. the Hermes assistant's) would leave YuE2 short of memory:
    unload them first. CPU-only models such as the songwriter are left alone. Not when the assistant has priority."""
    if gpu_priority() == "assistant":
        return
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=5) as r:
            loaded = json.loads(r.read()).get("models", [])
        for m in loaded:
            if m.get("size_vram"):
                log("unloading", m["name"], "from the GPU for YuE2")
                urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                       json.dumps({"model": m["name"], "keep_alive": 0}).encode(),
                                       {"Content-Type": "application/json"}), timeout=30).read()
    except Exception as e:
        log("couldn't check Ollama's GPU models:", e)


def unload_radio_writers():
    """Unload the radio's own songwriter/lyricist models from the GPU (they are only guests on the card)."""
    try:
        own = radio_writer_models()
        for m in _get_json("http://127.0.0.1:11434/api/ps", timeout=5).get("models", []):
            if m.get("size_vram") and m["name"] in own:
                log("unloading the radio's", m["name"], "from the GPU for YuE2")
                urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                       json.dumps({"model": m["name"], "keep_alive": 0}).encode(),
                                       {"Content-Type": "application/json"}), timeout=30).read()
    except Exception as e:
        log("couldn't unload the radio's writer models:", e)


def llm_active():
    if not LLM_UNIT:                     # STUDIO_LLM_UNIT="" -> no LLM service to pause (it shares the GPU)
        return False
    return sh("systemctl", "is-active", LLM_UNIT) in ("active", "activating")


def save_job(job):
    d = DATA / job["id"]; d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps({k: v for k, v in job.items() if not k.startswith("_")}, indent=1), encoding="utf-8")
    tmp.replace(d / "job.json")


def load_library():
    DATA.mkdir(parents=True, exist_ok=True)
    for f in DATA.glob("*/job.json"):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if job.get("status") in ("queued", "running"):
            job.update(status="failed", error="Interrupted: the studio restarted before this finished", stage="")
            save_job(job)
        jobs[job["id"]] = job


def read_gpu_state():
    try:
        return json.loads(GPU_STATE.read_text())
    except Exception:
        return {"paused_llm": False}


def write_gpu_state(paused):
    GPU_STATE.parent.mkdir(parents=True, exist_ok=True)
    GPU_STATE.write_text(json.dumps({"paused_llm": paused, "time": time.time()}))


# ----------------------------------------------------------------------------- validation

class BadRequest(ValueError):
    pass


def clean_text(v, name, limit, required=False):
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise BadRequest(f"{name} must be text")
    v = v.replace("\r\n", "\n").strip()
    if required and not v:
        raise BadRequest(f"{name} is required")
    if len(v) > limit:
        raise BadRequest(f"{name} is too long (max {limit} characters)")
    return v


def clean_sampling(raw, name):
    out = {}
    for k, v in (raw or {}).items():
        if v in (None, ""):
            continue
        if k not in SAMPLING_KEYS:
            raise BadRequest(f"unknown {name} setting {k}")
        typ, lo, hi = SAMPLING_KEYS[k]
        try:
            v = typ(v)
        except Exception:
            raise BadRequest(f"{name} {k} must be a number")
        if not lo <= v <= hi:
            raise BadRequest(f"{name} {k} must be between {lo} and {hi}")
        out[k] = v
    return out


def validate(body):
    mode = body.get("mode", "song")
    if mode not in ("song", "instrumental"):
        raise BadRequest("mode must be song or instrumental")
    job = {"mode": mode,
           "title": clean_text(body.get("title"), "Title", 120),
           "author": clean_text(body.get("author"), "Name", 60),
           "style": clean_text(body.get("style"), "Style", 1500, required=True),
           "lyrics": clean_text(body.get("lyrics"), "Lyrics", 20000, required=(mode == "song")),
           "abc": clean_text(body.get("abc"), "Score", 65536) or None}
    cot = body.get("cot", "full")
    if cot not in ("full", "melody", "off"):
        raise BadRequest("Planning mode must be full, melody or off")
    if job["abc"] and cot == "off":
        raise BadRequest("A custom score needs planning mode full or melody")
    if mode == "instrumental" and cot == "off":
        raise BadRequest("Instrumental mode needs planning mode full or melody")
    job["cot"] = cot
    seed = body.get("seed")
    if seed in (None, "", "random"):
        seed = random.randrange(1, 2**31)
    try:
        seed = int(seed)
    except Exception:
        raise BadRequest("Seed must be a whole number")
    if not 0 <= seed < 2**63:
        raise BadRequest("Seed out of range")
    job["seed"] = seed
    cfg = body.get("cfg_scale")
    if cfg not in (None, ""):
        try:
            cfg = float(cfg)
        except Exception:
            raise BadRequest("Style adherence must be a number")
        if not 1.0 <= cfg <= 5.0:
            raise BadRequest("Style adherence must be between 1.0 and 5.0")
        job["cfg_scale"] = None if cfg == 1.0 else cfg
    else:
        job["cfg_scale"] = None
    try:
        job["max_seconds"] = int(body.get("max_seconds") or 360)
        job["ode_steps"] = int(body.get("ode_steps") or 32)
        takes = int(body.get("takes") or 1)
    except Exception:
        raise BadRequest("Length, quality steps and takes must be whole numbers")
    if not 20 <= job["max_seconds"] <= 360:
        raise BadRequest("Max length must be between 20 and 360 seconds")
    if not 8 <= job["ode_steps"] <= 64:
        raise BadRequest("Audio quality steps must be between 8 and 64")
    if not 1 <= takes <= 4:
        raise BadRequest("Takes must be 1 to 4")
    job["abc_sampling"] = clean_sampling(body.get("abc_sampling"), "Score sampling")
    job["semantic_sampling"] = clean_sampling(body.get("semantic_sampling"), "Song sampling")
    return job, takes


def new_jobs(body, extra=None):
    base, takes = validate(body)
    extra = dict(extra or {})
    if body.get("rewrite_of") in jobs:                 # ✏️ a rewrite of another song: remember which one
        extra["rewrite_of"] = body["rewrite_of"]
    with lock:
        queued = sum(1 for j in jobs.values() if j["status"] in ("queued", "running") and j.get("source") != "radio")
        if not extra.get("source") and queued + takes > MAX_QUEUED:
            raise BadRequest(f"The queue is full ({queued} waiting); try again later")
        batch = uuid.uuid4().hex[:8]
        created = []
        for i in range(takes):
            jid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            job = dict(base, id=jid, batch=batch, take=i + 1, takes=takes, seed=base["seed"] + i,
                       created=time.time(), status="queued", stage="Waiting in queue", progress={},
                       source="studio", priority=0)
            job.update(extra)
            if not job["title"]:
                first = next((l for l in job["lyrics"].splitlines() if l.strip() and not l.strip().startswith("[")), "")
                job["title"] = (first[:60] or job["style"].split(",")[0][:60] or "Untitled").strip()
            jobs[jid] = job
            save_job(job)
            created.append(jid)
    log("queued", created)
    return created


# ----------------------------------------------------------------------------- GPU worker

def gpu_free_mib(index):
    """Used VRAM in MiB on one card, via xpu-smi or nvidia-smi (the parent process never initialises the GPU)."""
    if DEVICE.startswith("cuda"):
        try:
            return int(sh("nvidia-smi", "-i", str(index), "--query-gpu=memory.used", "--format=csv,noheader,nounits", timeout=20))
        except Exception:
            return None
    try:
        j = json.loads(sh("xpu-smi", "stats", "-d", str(index), "-j", timeout=20))
        return j["memory"]["used_mib"]["tile_0"]["current"]
    except Exception:
        return None


VRAM_START_MIB = 16 * 1024      # YuE2 only starts when the card's used memory is below this (24 GB card: ~8 GB free)
OOM_RETRIES = 2                 # a take that runs out of GPU memory goes back in the queue this many times


def gpu_holders(index):
    """Who is using GPU memory on one card: [(pid, short name, MiB)], biggest first. NVIDIA only; [] elsewhere."""
    if not DEVICE.startswith("cuda"):
        return []
    out = []
    try:
        uuid_ = sh("nvidia-smi", "-i", str(index), "--query-gpu=uuid", "--format=csv,noheader", timeout=20)
        rows = sh("nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits", timeout=20)
        for line in rows.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 3 or parts[0] != uuid_:
                continue
            pid, mib = int(parts[1]), int(parts[2])
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                args = [c.decode(errors="replace") for c in cmd if c]
            except OSError:
                args = []
            text = " ".join(args)
            if "ollama" in text:
                name = "Ollama"
            elif "ComfyUI" in text or "comfy" in text.lower():
                name = "ComfyUI"
            elif "yue2" in text.lower() or "multiprocessing" in text:
                name = "YuE2"
            else:
                name = Path(args[0]).name if args else f"pid {pid}"
            out.append((pid, name, mib))
    except Exception as e:
        log("couldn't list GPU memory users:", e)
    return sorted(out, key=lambda x: -x[2])


def holders_text(index):
    h = gpu_holders(index)
    return ", ".join(f"{name} {mib / 1024:.1f} GB" for _, name, mib in h[:3]) if h else "unknown"


def soften_ending(path, fade=6.0):
    """A song that stops at full volume (usually the length cap cut it off) gets a radio-style fade-out.
    Runs on the lossless render before the MP3 is made. Returns True if the file was changed."""
    import numpy as np, soundfile as sf
    path = Path(path)
    y, sr = sf.read(path, dtype="float32", always_2d=True)
    if len(y) < sr * 12:
        return False
    rms = lambda a: float(np.sqrt(np.mean(a ** 2)) + 1e-9)
    if 20 * np.log10(rms(y[-sr // 2:]) / rms(y[:-5 * sr])) < -12:      # already ends quietly on its own
        return False
    n = int(min(fade, len(y) / sr / 4) * sr)
    y[-n:] *= (0.5 * (1 + np.cos(np.linspace(0, np.pi, n, dtype=np.float32))))[:, None]
    tmp = path.with_name(".fade.flac")
    sf.write(tmp, y, sr, subtype=sf.info(str(path)).subtype)
    os.replace(tmp, path)                    # atomic: a broadcast already reading the old file keeps its copy
    return True


def run_job(pipe, base, job, update, cancelled):
    """Runs in the GPU child process. update(**fields) reports progress/results to the parent."""
    from yue2.protocol import SongRequest
    d = DATA / job["id"]
    t0 = time.time()
    counts, phase_start = {}, {}

    def on_token(phase, token):
        counts[phase] = counts.get(phase, 0) + 1
        now = time.time(); phase_start.setdefault(phase, now)
        n = counts[phase]
        if n % 4:                       # throttle IPC
            return
        tps = n / max(now - phase_start[phase], 1e-3)
        if phase == "abc":
            update(stage="Composing the score", progress={"phase": phase, "tokens": n, "tps": round(tps, 1)})
        else:
            update(stage="Generating the song", progress={"phase": phase, "tokens": n, "tps": round(tps, 1),
                   "music_seconds": round(n / TOKENS_PER_AUDIO_SECOND, 1), "max_music_seconds": job["max_seconds"]})

    abc_s = dataclasses.replace(base.abc, **job["abc_sampling"])
    sem_max = max(base.semantic.min_tokens + 1, min(base.semantic.max_tokens, job["max_seconds"] * TOKENS_PER_AUDIO_SECOND))
    sem_s = dataclasses.replace(base.semantic, max_tokens=int(sem_max), **job["semantic_sampling"])
    pipe.generation_config = dataclasses.replace(base, ode_steps=job["ode_steps"])
    orig_synth, orig_decode = pipe.synthesize, pipe.decode

    def synth(*a, **k):
        update(stage="Rendering audio", progress={"phase": "audio"}); return orig_synth(*a, **k)

    def decode(*a, **k):
        update(stage="Decoding audio", progress={"phase": "decode"}); return orig_decode(*a, **k)

    pipe.synthesize, pipe.decode = synth, decode
    update(status="running", started=t0, stage="Starting", progress={})
    log("start", job["id"], job["mode"], job["cot"], "seed", job["seed"])
    try:
        common = dict(style=job["style"], seed=job["seed"], cfg_scale=job["cfg_scale"],
                      abc_sampling=abc_s, semantic_sampling=sem_s, cancelled=cancelled, on_token=on_token)
        if job["mode"] == "instrumental":
            if str(SKILL) not in sys.path:
                sys.path.insert(0, str(SKILL))
            from instrumentalize import convert_score
            from instrumental import lyric_tags
            source = job["abc"]
            if not source:
                structure = job["lyrics"] or "[Intro]\n\n[Verse]\n\n[Chorus]\n\n[Verse]\n\n[Chorus]\n\n[Outro]"
                request = SongRequest(style=job["style"], lyrics=structure, cot=job["cot"], seed=job["seed"])
                plan = pipe.plan(request=request, abc_sampling=abc_s, cancelled=cancelled, on_token=on_token)
                if plan.truncated or not plan.abc:
                    raise RuntimeError("The model's score was empty or cut off; try another seed")
                source = plan.abc
            d.mkdir(parents=True, exist_ok=True)
            (d / "source.abc").write_text(source, encoding="utf-8")
            update(stage="Moving the melody to instruments")
            converted, check = convert_score(source, overlap="vocal", keep_chords=job["cot"] == "full")
            (d / "instrumental-check.json").write_text(json.dumps(check, indent=1), encoding="utf-8")
            counts.clear(); phase_start.clear()
            song = pipe(lyrics=lyric_tags(converted), cot=job["cot"], abc=converted, **common)
        else:
            song = pipe(lyrics=job["lyrics"], cot=job["cot"], abc=job["abc"], **common)
        out = d / "song"
        if out.exists():
            shutil.rmtree(out)
        song.save_artifacts(out)
        faded = False
        try:
            faded = soften_ending(out / "audio.flac")
        except Exception:
            log("fade-out failed", job["id"], traceback.format_exc())
        if faded:
            update(faded=True); log("faded out an abrupt ending", job["id"])
        update(stage="Encoding MP3")
        # the library keeps one 320 kbps MP3 per song; the 24-bit FLAC render is only an intermediate
        sh("ffmpeg", "-y", "-loglevel", "error", "-i", str(out / "audio.flac"), "-codec:a", "libmp3lame", "-b:a", "320k",
           "-metadata", f"title={job['title']}", "-metadata", "artist=Agentora", str(d / "song.mp3"))
        if (d / "song.mp3").stat().st_size > 10_000:
            (out / "audio.flac").unlink()
        result = json.loads((out / "result.json").read_text())
        gen = round(time.time() - t0, 1)
        update(status="done", stage="", finished=time.time(), seconds=result.get("audio_seconds"),
               truncated=result.get("truncated"), timing={k: v for k, v in result.get("timing", {}).items() if k != "load"},
               gen_seconds=gen, progress={})
        log("done", job["id"], f"{result.get('audio_seconds', 0):.1f}s audio in {gen}s")
    except InterruptedError:
        update(status="cancelled", stage="", finished=time.time(), progress={})
        log("cancelled", job["id"])
    except Exception as e:
        update(status="failed", stage="", finished=time.time(), error=f"{type(e).__name__}: {e}"[:500], progress={})
        log("failed", job["id"], traceback.format_exc())
    finally:
        pipe.synthesize, pipe.decode = orig_synth, orig_decode
        pipe.generation_config = base


def gpu_main(device, task_q, event_q, cancel_ev):
    """GPU child process for one card: loads YuE2 once, renders jobs until told to stop, then exits (freeing all VRAM)."""
    logsetup.setup("yue2-worker")
    try:
        from yue2 import YuE2Pipeline
        pipe = YuE2Pipeline.from_pretrained("m-a-p/YuE2-3B", device=device, progress=False)
        base = pipe.generation_config
        event_q.put(("ready", None, {}))
    except Exception as e:
        event_q.put(("fatal", None, {"error": f"{type(e).__name__}: {e}"}))
        return
    while True:
        job = task_q.get()
        if job is None:
            break
        cancel_ev.clear()
        run_job(pipe, base, job, lambda **kw: event_q.put(("update", job["id"], kw)), cancel_ev.is_set)
        event_q.put(("finished", job["id"], {}))
    try:
        pipe.close()
    except Exception:
        pass


class Slot:
    def __init__(self, device):
        self.device, self.index = device, int(device.split(":")[1]) if ":" in device else 0
        self.proc = self.job = None
        self.ready = False
        self.fails = 0              # consecutive failed jobs
        self.restarts = []          # restart timestamps (rate limit)
        self.disabled = False

    def alive(self):
        return self.proc is not None and self.proc.is_alive()

    def start(self):
        ctx = mp.get_context("spawn")
        self.task_q, self.event_q, self.cancel_ev = ctx.Queue(), ctx.Queue(), ctx.Event()
        self.proc = ctx.Process(target=gpu_main, args=(self.device, self.task_q, self.event_q, self.cancel_ev),
                                daemon=True, name=f"yue2-{self.device}")
        self.proc.start(); self.ready = False; self.job = None

    def stop(self):
        if self.proc is None:
            return
        try:
            self.task_q.put(None); self.proc.join(60)
        except Exception:
            pass
        if self.proc.is_alive():
            self.proc.kill(); self.proc.join(10)
        self.proc, self.ready, self.job = None, False, None


class Worker(threading.Thread):
    """Parent-side scheduler for one YuE2 process per GPU. Never touches the GPU itself.
    Studio jobs (priority 0) always go before radio jobs (priority 1)."""
    def __init__(self):
        super().__init__(daemon=True)
        self.slots = [Slot(d) for d in DEVICES]
        self.paused_llm = read_gpu_state().get("paused_llm", False)
        self.last_activity = time.time()

    def set_gpu(self, owner, detail=""):
        with lock:
            gpu["owner"], gpu["detail"] = owner, detail
            gpu["workers"] = [{"device": sl.device, "ready": sl.ready, "job": sl.job["title"] if sl.job else None,
                               "disabled": sl.disabled} for sl in self.slots]

    def holding(self):
        return any(sl.alive() for sl in self.slots)

    def pending(self):
        import radio
        with lock:
            for j in jobs.values():           # radio work never starts (or re-takes the GPUs) while the radio is off
                if j["status"] == "queued" and j.get("source") == "radio" and not radio.generating() and not j.get("requested"):
                    j.update(status="cancelled", stage="", finished=time.time(), radio_status="cancelled"); save_job(j)
            return sorted((j for j in jobs.values() if j["status"] == "queued"), key=lambda j: (j.get("priority", 0), j["created"]))

    def run(self):
        import radio
        if self.paused_llm and not llm_active() and radio.generating():
            log("radio resumed after a quick restart: leaving the LLM paused")
        elif self.paused_llm and not llm_active():
            log("recovering: restarting the LLM that a previous studio run paused")
            sh("sudo", "systemctl", "start", LLM_UNIT)
            self.paused_llm = False; write_gpu_state(False)
        while True:
            try:
                self.step()
            except Exception:
                log("worker error:", traceback.format_exc())
                time.sleep(3)

    def step(self):
        pend = self.pending()
        busy = any(sl.job for sl in self.slots)
        wait = gpu_yield_reason() if pend or self.holding() else None   # another tool has GPU priority right now
        if wait:
            if self.holding() and not busy:
                log(wait.lower() + ": giving the GPU back")
                self.release()
            if not self.holding():
                self.set_gpu("waiting", wait)
                time.sleep(2)
                return
            self.pump()                           # finish the song in progress, start nothing new
            return
        import art                                # cover art is drawn between songs, never while YuE2 records
        if art.step(busy, bool(pend)):
            self.pump()
            time.sleep(0.5)
            return
        if pend and not self.holding():
            if not self.acquire():                # not enough free GPU memory yet: wait, never start YuE2 into an OOM
                time.sleep(10)
                return
        if pend and self.holding():
            for sl in self.slots:                 # bring back crashed workers while a session is active
                if not sl.alive() and not sl.disabled and (not sl.restarts or time.time() - sl.restarts[-1] > 20) and \
                        (gpu_free_mib(sl.index) or 0) < VRAM_START_MIB:      # only onto a card with room (no OOM on start)
                    self.heal(sl, "worker process is not running")
        # hand queued jobs to idle, loaded slots
        for sl in self.slots:
            if sl.alive() and sl.ready and sl.job is None and pend:
                job = pend.pop(0)
                with lock:
                    job.update(status="running", stage="Starting", device=sl.device)
                sl.job = job
                sl.task_q.put({k: v for k, v in job.items() if not k.startswith("_")})
                self.set_gpu("studio", f"{sum(1 for x in self.slots if x.job)} of {len(self.slots)} GPUs rendering")
        self.pump()
        busy = any(sl.job for sl in self.slots)
        with lock:
            if busy or pend:
                gpu["idle_since"] = None
            elif self.holding() and gpu["idle_since"] is None:
                gpu["idle_since"] = time.time()
            want_release = self.holding() and not busy and not pend and (
                gpu["release_requested"] or gpu["release_on_idle"] or
                (RETURN_MINUTES > 0 and gpu["idle_since"] and time.time() - gpu["idle_since"] > RETURN_MINUTES * 60))
        if want_release:
            self.release()
        elif not self.holding():
            self.set_gpu("llm" if llm_active() else "free")
            with lock:
                gpu["release_requested"] = gpu["release_on_idle"] = False
            time.sleep(1)
        elif not busy:
            self.set_gpu("studio", "YuE2 loaded — idle")
            time.sleep(0.5)

    def pump(self):
        """Drain progress events from every slot (and notice crashed slots)."""
        deadline = time.time() + 0.5
        while time.time() < deadline:
            got = False
            for sl in self.slots:
                if sl.proc is None:
                    continue
                if sl.job and sl.job.get("_cancel"):
                    sl.cancel_ev.set()
                try:
                    kind, jid, data = sl.event_q.get_nowait()
                except queue.Empty:
                    if not sl.alive() and sl.proc is not None:
                        if sl.job:
                            with lock:
                                sl.job.update(status="failed", stage="", finished=time.time(), progress={},
                                              error="The GPU process crashed while rendering this song")
                            save_job(sl.job); self.finished(sl.job)
                        log("GPU process on", sl.device, "exited unexpectedly"); sl.proc, sl.ready, sl.job = None, False, None
                    continue
                got = True
                if kind == "ready":
                    sl.ready = True; log("YuE2 ready on", sl.device)
                elif kind == "fatal":
                    log("YuE2 failed to load on", sl.device, data.get("error")); sl.stop()
                elif sl.job and jid == sl.job["id"]:
                    if kind == "update":
                        with lock:
                            sl.job.update(data)
                        if "status" in data:
                            save_job(sl.job)
                    elif kind == "finished":
                        done = sl.job; sl.job = None
                        self.last_activity = time.time()
                        err = done.get("error") or ""
                        if done["status"] == "failed" and "out of memory" in err.lower() and done.get("oom_retries", 0) < OOM_RETRIES:
                            # another program grabbed the card: requeue the take and restart YuE2 once memory is free
                            log(f"{done['id']} ran out of GPU memory on {sl.device} (held by: {holders_text(sl.index)}); "
                                f"retry {done.get('oom_retries', 0) + 1}/{OOM_RETRIES}")
                            with lock:
                                done.update(status="queued", stage="Waiting for GPU memory (retrying)", error=None, started=None,
                                            finished=None, progress={}, device=None, oom_retries=done.get("oom_retries", 0) + 1)
                            save_job(done)
                            sl.stop()                     # a fresh process, started only after the memory check in acquire()
                            continue
                        save_job(done); self.finished(done)
                        sl.fails = sl.fails + 1 if done["status"] == "failed" else 0
                        if "DEVICE_LOST" in err or "level_zero backend failed" in err or sl.fails >= 3:
                            self.heal(sl, err or f"{sl.fails} failures in a row")
            if not got:
                time.sleep(0.05)

    def heal(self, sl, why):
        """A GPU context that hit a device fault stays dead; replace the worker process (a fresh one works after the
        driver's engine reset). Too many restarts -> retire that card for this session."""
        now = time.time()
        sl.restarts = [t for t in sl.restarts if now - t < 600] + [now]
        log(f"GPU worker {sl.device} unhealthy ({why[:120]}); restart #{len(sl.restarts)} in 10 min")
        sl.stop(); sl.fails = 0
        if len(sl.restarts) > 3:
            sl.disabled = True
            log(f"GPU worker {sl.device} retired for this session after repeated faults")
        else:
            sl.start()

    def finished(self, job):
        if job.get("source") == "radio":
            import radio
            threading.Thread(target=radio.judge, args=(job,), daemon=True).start()

    def wait_vram(self, index, below_mib, timeout, what):
        for _ in range(timeout):
            used = gpu_free_mib(index)
            if used is None or used < below_mib:      # None: this card can't be measured -> don't block on it
                return True
            time.sleep(1)
        log(f"timeout waiting for {what} on xpu:{index}; VRAM used {gpu_free_mib(index)} MiB")
        return False

    def acquire(self):
        import radio
        t0 = time.time()
        while radio.bank_writing.is_set() and time.time() - t0 < 300:      # let the GPU lyricist finish its song first
            self.set_gpu("switching", "Waiting for the lyricist to finish a song…")
            time.sleep(2)
        if llm_active():
            self.set_gpu("switching", "Pausing the LLM to free the GPUs…")
            log("stopping", LLM_UNIT)
            sh("sudo", "systemctl", "stop", LLM_UNIT)
            self.paused_llm = True; write_gpu_state(True)
        unload_ollama_gpu_models()
        if gpu_priority() == "radio":
            free_comfy_if_idle()
        self.set_gpu("switching", "Waiting for GPU memory…")
        short = [sl for sl in self.slots if not sl.disabled and not self.wait_vram(sl.index, VRAM_START_MIB, 60, "GPU memory to free up")]
        if short:
            free_comfy_if_idle()                  # an idle ComfyUI keeps its models on the card: ask it to let go, whatever the priority
            unload_ollama_gpu_models()
            unload_radio_writers()                # the radio's own GPU lyricist never outranks the radio
            short = [sl for sl in short if not self.wait_vram(sl.index, VRAM_START_MIB, 30, "GPU memory to free up")]
        if short and len(short) == len([sl for sl in self.slots if not sl.disabled]):
            who = holders_text(short[0].index)
            log(f"not starting YuE2: GPU memory still in use ({who}); waiting")
            self.set_gpu("waiting", f"Waiting for GPU memory — in use by {who}")
            return False
        for sl in short:                          # multi-GPU: start on the cards that have room, retry the others later
            log(f"skipping {sl.device} for now: memory in use ({holders_text(sl.index)})")
        self.set_gpu("switching", "Loading YuE2…")
        if all(sl.disabled for sl in self.slots):
            for sl in self.slots:
                sl.disabled = False; sl.restarts = []
        for sl in self.slots:
            if not sl.alive() and not sl.disabled and sl not in short:
                sl.start()
        t0 = time.time()
        while not any(sl.ready for sl in self.slots) and time.time() - t0 < 300:
            self.pump()
            if not self.holding():
                raise RuntimeError("Could not start YuE2 on any GPU")
        self.set_gpu("studio", "YuE2 loaded")
        broker_holding(True, device=next((sl.index for sl in self.slots if sl.ready), 0))
        return True

    def release(self):
        self.set_gpu("switching", "Returning the GPUs to the LLM…")
        log("releasing GPUs: stopping the YuE2 processes")
        for sl in self.slots:
            sl.stop(); sl.disabled = False; sl.restarts = []; sl.fails = 0
        for sl in self.slots:
            # on a shared CUDA card the songwriter LLM stays resident, so "freed" means below ~12 GB (desktop + songwriter)
            self.wait_vram(sl.index, 12 * 1024 if DEVICE.startswith("cuda") else 1024, 60, "the YuE2 process to free GPU memory")
        broker_holding(False)
        with lock:
            gpu["release_requested"] = gpu["release_on_idle"] = False; gpu["idle_since"] = None
        if LLM_UNIT and not llm_active():
            log("starting", LLM_UNIT)
            sh("sudo", "systemctl", "start", LLM_UNIT)
        self.paused_llm = False; write_gpu_state(False)
        self.set_gpu("llm" if llm_active() else "free")


# ----------------------------------------------------------------------------- lyrics (via an LLM)

LYRICS_SYSTEM = """You are a professional songwriter writing lyrics that a singing AI model will perform.
Output format (strict):
- First line: Title: <short song title>
- Then a blank line, then the lyrics.
- Every section starts with a tag on its own line, chosen only from: [Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Outro]. Do not number tags.
- One sung phrase per line, 5-12 syllables (or a similar length in other languages), consistent rhythm within a section. A blank line between sections.
- When the chorus returns, repeat it word for word.
- Only words to be sung. No markdown, no bold, no bullet points, no parenthetical notes, no stage directions, no chords, no explanations before or after.
- Write in the requested language (if none is given, the language named in the style; default English). Match the genre, mood and era of the style."""
LENGTHS = {"short": "about 1 minute: one verse and one chorus", "medium": "about 2 minutes",
           "long": "about 3 to 4 minutes, with fuller verses"}
RHYMES = {"rhyming": "Use clear end rhymes.", "loose": "Use loose, natural rhymes and near-rhymes.", "free": "Free verse: no rhyme scheme needed."}
POVS = {"i": "Write in the first person (I).", "you": "Address the listener directly (you).", "we": "Write as 'we'.",
        "story": "Tell a story in the third person."}
SECTION_RE = re.compile(r"^\W*(intro|verse|pre[- ]?chorus|chorus|bridge|outro|hook|refrain)\s*\d*\W*$", re.I)
ZH_TAGS = {"前奏": "Intro", "主歌": "Verse", "预副歌": "Pre-Chorus", "导歌": "Pre-Chorus", "副歌": "Chorus",
           "桥段": "Bridge", "间奏": "Bridge", "尾奏": "Outro", "结尾": "Outro", "尾声": "Outro"}
TAG_NAMES = {"intro": "Intro", "verse": "Verse", "prechorus": "Pre-Chorus", "chorus": "Chorus", "bridge": "Bridge",
             "outro": "Outro", "hook": "Chorus", "refrain": "Chorus"}


def lyrics_prompt(b):
    action = b.get("action", "write")
    style = clean_text(b.get("style"), "Style", 1500)
    lang = clean_text(b.get("language"), "Language", 40)
    parts = [f"Style: {style or 'any'}"]
    if lang:
        parts.append(f"Language: {lang}")
    if action == "write":
        idea = clean_text(b.get("idea"), "Idea", 2000)
        title = clean_text(b.get("title"), "Title", 120)
        parts.append(f"What the song is about: {idea or 'choose a fitting theme for this style'}")
        if title:
            parts.append(f"Use this title: {title}")
        parts.append(f"Structure: {clean_text(b.get('structure'), 'Structure', 300) or 'Verse, Chorus, Verse, Chorus, Bridge, Chorus'}")
        parts.append(f"Length: {LENGTHS.get(b.get('length'), LENGTHS['medium'])}")
        parts.append(RHYMES.get(b.get("rhyme"), RHYMES["rhyming"]))
        if b.get("pov") in POVS:
            parts.append(POVS[b["pov"]])
        if b.get("extra"):
            parts.append("Also: " + clean_text(b.get("extra"), "Extra instructions", 1000))
        parts.append("Write the song.")
    else:
        current = clean_text(b.get("lyrics"), "Lyrics", 20000, required=True)
        if action == "rewrite":
            parts.append("Revise these lyrics according to this feedback: " + (clean_text(b.get("feedback"), "Feedback", 1000) or "make them stronger and more singable"))
            parts.append("Keep what already works; return the complete revised song.")
        elif action == "polish":
            parts.append("Format these user-written lyrics for singing. Keep the user's words and meaning as much as possible: "
                         "add or fix section tags, split overly long lines into singable phrases, make repeated choruses identical, "
                         "and only lightly smooth the rhythm. Keep the existing title if there is one.")
        else:
            raise BadRequest("action must be write, rewrite or polish")
        parts.append("Lyrics:\n" + current)
    return "\n".join(parts)


def normalize_lyrics(text):
    """Pull out 'Title:' and coerce the model's text into YuE2's tag/line format."""
    title, out = "", []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip().strip("`").replace("**", "").replace("__", "").strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        m = re.match(r"^(?:#+\s*)?(?:title|标题|歌名|曲名)\s*[:：]\s*(.+)$", line, re.I)
        if m and not title and not any(l for l in out if l):
            title = m.group(1).strip().strip('"“”')
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        bare = line.strip("[]()（）【】: ").strip()
        sm = SECTION_RE.match(bare)
        zh = ZH_TAGS.get(re.sub(r"[\d一二三四五六七八九十\s]+$", "", bare))
        if sm or zh:
            name = zh or TAG_NAMES[re.sub(r"[- ]", "", sm.group(1).lower())]
            if out and out[-1] != "":
                out.append("")
            out.append(f"[{name}]")
            continue
        if re.fullmatch(r"[\[【(（][^\]】)）]{1,60}[\]】)）]", line):   # a made-up tag ("[Guitar Solo / Synth Build]"): never sing it
            continue
        line = re.sub(r"^\s*[-*•]\s+", "", line)
        line = re.sub(r"\s*\((?:x\d+|repeat|×\d+)\)\s*$", "", line, flags=re.I)
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    lyrics = "\n".join(out)
    lyrics = re.sub(r"\]\n\n", "]\n", lyrics)
    return title, lyrics


def pick_llm(prefer_gpu=False):
    """The first reachable lyrics writer; prefer_gpu: the GPU ones first (quick, targeted edits)."""
    for base, model, label in sorted(LYRICS_LLMS, key=lambda c: "gpu" not in c[2].lower()) if prefer_gpu else LYRICS_LLMS:
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=2) as r:
                if r.status == 200:
                    return base.rstrip("/"), model, label
        except Exception:
            continue
    return None


def stream_lyrics(handler, body):
    prompt = lyrics_prompt(body)            # validates before we commit to a stream
    llm = pick_llm(prefer_gpu=bool(body.get("fast")))
    if llm is None:
        return handler.send(503, {"error": "No lyrics writer is reachable right now. the radio's LLM is paused while the studio "
                                           "holds the GPUs, and the backup server did not answer."})
    base, model, label = llm
    llm_used(model)
    try:
        temperature = min(1.5, max(0.2, float(body.get("temperature", 0.85))))
    except Exception:
        temperature = 0.85
    req = urllib.request.Request(base + "/chat/completions", json.dumps({
        "model": model, "stream": True, "temperature": temperature, "top_p": 0.95, "max_tokens": 1600,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": LYRICS_SYSTEM}, {"role": "user", "content": prompt}], **LLM_EXTRA}).encode(),
        {"Content-Type": "application/json"})
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream"); handler.send_header("Cache-Control", "no-store")
    handler.end_headers()

    def emit(obj):
        handler.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n"); handler.wfile.flush()

    t0, text = time.time(), []
    try:
        emit({"source": label})
        with urllib.request.urlopen(req, timeout=300) as r:
            for line in r:
                line = line.strip()
                if not line.startswith(b"data: ") or line == b"data: [DONE]":
                    continue
                d = json.loads(line[6:])
                piece = (d.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                if piece:
                    text.append(piece); emit({"delta": piece})
        title, lyrics = normalize_lyrics("".join(text))
        emit({"done": True, "title": title, "lyrics": lyrics, "source": label, "seconds": round(time.time() - t0, 1)})
        log("lyrics", body.get("action", "write"), f"via {label}", f"{time.time() - t0:.1f}s")
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as e:
        try:
            emit({"error": f"The lyrics writer failed: {type(e).__name__}: {e}"[:300]})
        except Exception:
            pass


# ----------------------------------------------------------------------------- ✏️ rewrite a song: AI suggestions
REWRITE_SYSTEM = """You are a record producer helping someone improve a song made by an AI music model (YuE2), which sings
the lyrics in the style given as comma-separated tags. Be specific to THIS song: name the lines, sections or sounds you mean.
Reply with JSON only, no other text:
{"suggestions": [{"title": "at most 6 words", "why": "one short sentence", "lyrics": "", "style": ""}]}
"lyrics": ONE short instruction (under 30 words) telling a lyricist what to change, e.g. "Make the chorus hook shorter and
repeat it; replace 'traffic lights' in the bridge with a concrete memory of the two friends". Never write the new lyrics here.
Leave it empty when the idea is about the sound.
"style": only for ideas about the sound (genre, voice, instruments, mood, tempo): the complete new style line in the same form
as the current one: language first, then genre, vocal, instruments, mood, tempo, e.g.
"English, warm piano pop, expressive female vocal, acoustic piano, light drums, 88 BPM". Leave it empty for lyric ideas."""


def rewrite_suggestions(jid, ask=""):
    """Ideas for rewriting one song (3-4, each changing the lyrics or the style), or with ask: that one request as edits."""
    import radio
    with lock:
        j = dict(jobs.get(jid) or {})
    if not j:
        raise BadRequest("No such song")
    ctx = [f"Title: {j.get('title')}", f"Style: {j.get('style')}",
           "Lyrics:\n" + ((j.get("lyrics") or "").strip()[:6000] or "(instrumental)")]
    fb = {"keep": "The listener gave it a thumbs up.", "dislike": "The listener gave it a thumbs down.",
          "skip": "The listener skipped it."}.get(j.get("radio_feedback"))
    if fb:
        ctx.append(fb)
    c = j.get("critic") or {}
    if c.get("score") is not None:
        ctx.append(f"Automatic quality score {c['score']:.2f}" + (f" (this station's bar is {c['bar']:.2f})." if c.get("bar") else "."))
    st = radio.stations.get(j.get("station"))
    if st:
        ctx.append(f"Made for the radio station \"{st['name']}\": {(st.get('description') or '')[:400]}")
        taste = st.get("taste")
        taste = taste.get("notes") if isinstance(taste, dict) else taste     # stations keep {"notes": ..., "updated": ...}
        if taste:
            ctx.append("What this listener likes (learned from their thumbs):\n" + str(taste)[:800])
    ask = clean_text(ask, "Request", 500)
    ctx.append(f"The listener asks for this change: {ask}\nReturn exactly ONE suggestion that does ALL of it. Whatever the request "
               "says about the sound (tempo, mood, voice, instruments, genre) goes in a new style line, e.g. slower = a lower BPM and "
               "a calmer feel; whatever it says about the words or story goes in the lyrics instruction. Fill both when it asks for both."
               if ask else "Suggest 3 or 4 different improvements a listener would notice; each changes the "
               "lyrics OR the style, not both.")
    for base, model, label in radio.gpu_first(radio.llm_candidates()):
        try:
            llm_used(model)
            body = {"model": model, "temperature": 0.6 if ask else 0.8, "max_tokens": 1000, "response_format": {"type": "json_object"},
                    "chat_template_kwargs": {"enable_thinking": False},
                    "messages": [{"role": "system", "content": REWRITE_SYSTEM}, {"role": "user", "content": "\n\n".join(ctx)}], **LLM_EXTRA}
            r = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(),
                           {"Content-Type": "application/json"}), timeout=180).read())
            text = r["choices"][0]["message"]["content"]
            data = json.loads(text[text.find("{"):text.rfind("}") + 1])
            out = []
            for x in data.get("suggestions") or []:
                if str(x.get("lyrics") or "").count("\n") > 1 or len(str(x.get("lyrics") or "")) > 400:
                    continue                           # wrote new lyrics instead of saying what to change
                x = {k: re.sub(r"\s+", " ", str(x.get(k) or "")).strip() for k in ("title", "why", "lyrics", "style")}
                if x["style"].lower() == (j.get("style") or "").strip().lower():
                    x["style"] = ""                    # "changed" the style to what it already is
                x = {"title": x["title"][:60], "why": x["why"][:240], "lyrics": x["lyrics"][:400], "style": x["style"][:400]}
                if x["title"] and (x["lyrics"] or x["style"]):
                    out.append(x)
            if out:
                log(f"rewrite ideas for {jid} via {label}: {len(out)}")
                return {"suggestions": out[:1] if ask else out[:4], "source": label}
        except Exception as e:
            log(f"rewrite ideas via {label} failed: {e}")
    raise BadRequest("The AI couldn't come up with suggestions right now — try again in a minute.")


# ----------------------------------------------------------------------------- HTTP

def summary(job):
    keys = ("id", "batch", "take", "takes", "title", "author", "mode", "style", "cot", "seed", "status", "stage",
            "progress", "created", "started", "finished", "seconds", "gen_seconds", "error", "cfg_scale", "max_seconds")
    s = {k: job.get(k) for k in keys}
    d = DATA / job["id"]
    s["files"] = [name for name, (rel, _) in FILES.items() if (d / rel).is_file()]
    return s


def state():
    with lock:
        allj = sorted((j for j in jobs.values() if j.get("source") != "radio"), key=lambda j: -j["created"])
        queue = sorted((j for j in allj if j["status"] in ("queued", "running")), key=lambda j: j["created"])
        library = [j for j in allj if j["status"] in ("done", "failed", "cancelled")]
        idle_left = None
        if RETURN_MINUTES > 0 and gpu["owner"] == "studio" and gpu["idle_since"] and not queue:
            idle_left = max(0, RETURN_MINUTES * 60 - (time.time() - gpu["idle_since"]))
        import radio
        return {"gpu": dict(owner=gpu["owner"], detail=gpu["detail"], llm_unit=LLM_UNIT, return_minutes=RETURN_MINUTES,
                            idle_seconds_left=idle_left, release_requested=gpu["release_requested"], workers=gpu["workers"],
                            radio_on=radio.state["on"], radio_generating=radio.generating()),
                "queue": [summary(j) for j in queue], "library": [summary(j) for j in library[:300]],
                "limits": {"max_queued": MAX_QUEUED}, "lyrics_writers": [label for _, _, label in LYRICS_LLMS]}


class Handler(BaseHTTPRequestHandler):
    server_version = "agentora-music-studio"

    def handle_one_request(self):
        self._noted = False; self._json = None
        super().handle_one_request()
        if not self._noted and getattr(self, "command", "GET") not in ("GET", "HEAD", None):   # streamed answers (chat, lyrics)
            try:
                import useractivity
                useractivity.note(self, 200)
            except Exception:
                pass

    def send(self, code, body, ctype="application/json", extra=None):
        try:
            import useractivity
            useractivity.note(self, code)               # who did what (logs/activity-*.jsonl)
        except Exception:
            logging.getLogger("activity").warning("couldn't note the activity", exc_info=True)
        b = (json.dumps(body) if ctype == "application/json" else body)
        b = b.encode() if isinstance(b, str) else b
        extra = dict(extra or {})
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass                                         # the browser went away before the reply (e.g. left the page)

    @staticmethod
    def etag(f):
        st = f.stat()
        return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'

    def not_modified(self, tag, cache):
        """Answer 304 when the browser already has this exact version."""
        if tag not in (self.headers.get("If-None-Match") or ""):
            return False
        self.send_response(304)
        self.send_header("ETag", tag); self.send_header("Cache-Control", cache)
        self.end_headers()
        return True

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 200_000:
            raise BadRequest("Request too large")
        self._json = json.loads(self.rfile.read(n) or b"{}")
        return self._json

    def job_or_404(self, jid):
        with lock:
            job = jobs.get(jid)
        if not job:
            self.send(404, {"error": "No such song"})
        return job

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/radio", "/radio.html", "/radio/classic"):
            f = HERE / ("radio-classic.html" if path == "/radio/classic" else "radio.html" if path.startswith("/radio") else "index.html")
            tag = self.etag(f)                      # revalidate every time: unchanged pages cost a tiny 304
            if self.not_modified(tag, "no-cache"):
                return
            return self.send(200, f.read_bytes(), "text/html; charset=utf-8", {"ETag": tag, "Cache-Control": "no-cache"})
        if path in ("/agentora/agentora.css", "/agentora/agentora.js"):             # the look shared with the other apps (kept with the Studio)
            f = Path(os.environ.get("AGENTORA_SHARED") or os.environ.get("B70_SHARED", str(Path.home() / "genstudio" / "shared"))) / path.rsplit("/", 1)[1]
            if not f.is_file():
                return self.send(404, {"error": "shared file missing"})
            return self.send(200, f.read_bytes(), "text/css; charset=utf-8" if f.suffix == ".css" else "text/javascript; charset=utf-8",
                             {"Cache-Control": "no-cache"})
        if path.startswith("/web/"):                      # the radio page's scripts (studio/web/)
            name = path.rsplit("/", 1)[1]
            f = HERE / "web" / name
            if not re.fullmatch(r"[\w-]+\.(js|css)", name) or not f.is_file():
                return self.send(404, {"error": "No such file"})
            return self.send(200, f.read_bytes(), "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8",
                             {"Cache-Control": "no-cache"})
        if path == "/favicon.ico" or path.startswith("/icons/"):
            name = "studio-32.png" if path == "/favicon.ico" else path.rsplit("/", 1)[1]
            f = HERE / "icons" / name
            if not re.fullmatch(r"[\w-]+\.(png|svg)", name) or not f.is_file():
                return self.send(404, {"error": "No such icon"})
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml" if name.endswith(".svg") else "image/png")
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers(); self.wfile.write(body)
            return
        if path in ("/radio/stream.mp3", "/radio/stream", "/stream.mp3"):
            import radio
            return radio.stream(self)
        if path.startswith("/api/radio/"):
            import radio
            if radio.handle(self, "GET", path):
                return
        if path.startswith("/api/assistant/"):
            import assistant
            if assistant.handle(self, "GET", path):
                return
        if path == "/api/disk":
            return self.send(200, disk_view())
        if path == "/api/gpu/priority":
            return self.send(200, {"priority": gpu_priority(), "choices": PRIORITIES, "waiting": gpu["detail"] if gpu["owner"] == "waiting" else None})
        if path == "/api/state":
            return self.send(200, state())
        if path == "/api/health":
            st = state()
            return self.send(200, {"ok": True, "gpu_owner": st["gpu"]["owner"], "queued": len(st["queue"]),
                                   "running": next((j["title"] for j in st["queue"] if j["status"] == "running"), None),
                                   "songs": sum(1 for j in st["library"] if j["status"] == "done"), "radio_on": st["gpu"]["radio_on"]})
        m = re.fullmatch(r"/api/jobs/([\w-]+)", path)
        if m:
            job = self.job_or_404(m.group(1))
            if job:
                full = {k: v for k, v in job.items() if not k.startswith("_")}
                full["files"] = summary(job)["files"]
                for name in ("score.abc", "source.abc"):
                    f = DATA / job["id"] / FILES[name][0]
                    full[name.replace(".", "_")] = f.read_text(encoding="utf-8") if f.is_file() else None
                self.send(200, full)
            return
        m = re.fullmatch(r"/files/([\w-]+)/([\w.]+)", path)
        if m and m.group(2) in FILES:
            return self.file(m.group(1), m.group(2))
        self.send(404, {"error": "not found"})

    def file(self, jid, name):
        job = self.job_or_404(jid)
        if not job:
            return
        rel, ctype = FILES[name]
        root = DATA.resolve()
        f = (DATA / jid / rel).resolve()
        if not str(f).startswith(str(root) + os.sep) or not f.is_file():
            return self.send(404, {"error": "file not found"})
        size = f.stat().st_size
        tag, cache = self.etag(f), "private, max-age=604800"      # a finished song never changes
        if self.not_modified(tag, cache):
            return
        safe = re.sub(r"[^\w.-]+", "-", job.get("title") or jid).strip("-")[:60] or jid
        ext = name.rsplit(".", 1)[1]
        extra = {"Accept-Ranges": "bytes", "ETag": tag, "Cache-Control": cache}
        if "download=1" in self.path:
            extra["Content-Disposition"] = f'attachment; filename="{safe}-take{job.get("take", 1)}.{ext}"'
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        with open(f, "rb") as fh:
            if rng and (rng.group(1) or rng.group(2)):
                a = int(rng.group(1)) if rng.group(1) else max(0, size - int(rng.group(2)))
                b = min(int(rng.group(2)), size - 1) if rng.group(1) and rng.group(2) else size - 1
                fh.seek(a); data = fh.read(b - a + 1)
                extra["Content-Range"] = f"bytes {a}-{b}/{size}"
                return self.send(206, data, ctype, extra)
            return self.send(200, fh.read(), ctype, extra)

    def do_PUT(self):
        path = self.path.split("?")[0]
        import radio
        if not (path.startswith("/api/radio/") and radio.handle(self, "PUT", path)):
            self.send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/assistant/"):
            import assistant
            try:
                if assistant.handle(self, "POST", path):
                    return
            except json.JSONDecodeError:
                return self.send(400, {"error": "Invalid JSON"})
        if path.startswith("/api/radio/"):
            import radio
            if radio.handle(self, "POST", path):
                return
        try:
            if path == "/api/jobs":
                return self.send(200, {"ids": new_jobs(self.body())})
            if path == "/api/lyrics":
                return stream_lyrics(self, self.body())
            if path == "/api/rewrite/suggest":
                b = self.body()
                return self.send(200, rewrite_suggestions(str(b.get("id") or ""), b.get("ask") or ""))
            m = re.fullmatch(r"/api/jobs/([\w-]+)/cancel", path)
            if m:
                job = self.job_or_404(m.group(1))
                if job:
                    with lock:
                        if job["status"] == "queued":
                            job.update(status="cancelled", stage="", finished=time.time()); save_job(job)
                        elif job["status"] == "running":
                            job["_cancel"] = True; job["stage"] = "Cancelling…"
                    self.send(200, {"ok": True})
                return
            if path == "/api/gpu/priority":
                return self.send(200, set_gpu_priority(self.body().get("priority")))
            if path == "/api/gpu/release":
                import radio
                if radio.generating():
                    radio.set_power(False)          # otherwise the radio would take the GPUs straight back (reruns can stay on)
                with lock:
                    busy = any(j["status"] in ("queued", "running") and j.get("source") != "radio" for j in jobs.values())
                    if not busy:
                        gpu["release_requested"] = True
                return self.send(200, {"ok": not busy, "message": "Songs are still queued" if busy else "Returning the GPUs"})
        except BadRequest as e:
            return self.send(400, {"error": str(e)})
        except json.JSONDecodeError:
            return self.send(400, {"error": "Invalid JSON"})
        self.send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/radio/"):
            import radio
            if radio.handle(self, "DELETE", self.path.split("?")[0]):
                return
        m = re.fullmatch(r"/api/jobs/([\w-]+)", self.path.split("?")[0])
        if not m:
            return self.send(404, {"error": "not found"})
        job = self.job_or_404(m.group(1))
        if not job:
            return
        if job["status"] in ("queued", "running"):
            return self.send(400, {"error": "Cancel it first"})
        with lock:
            jobs.pop(job["id"], None)
        shutil.rmtree(DATA / job["id"], ignore_errors=True)
        log("deleted", job["id"])
        self.send(200, {"ok": True})

    def log_message(self, fmt, *a):
        logging.getLogger("http").debug("%s %s", self.address_string(), fmt % a)   # every request, at LOG_LEVEL=DEBUG


class Server(logsetup.QuietBrokenPipes, ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    logsetup.setup("radio")
    load_library()
    sys.modules.setdefault("app", sys.modules["__main__"])   # radio.py imports the studio as `app`
    import radio
    radio.init(sys.modules["__main__"])
    Worker().start()
    log(f"Music Studio on :{PORT}, data {DATA}, devices {DEVICES}, returns GPUs after {RETURN_MINUTES} min idle")
    Server((os.environ.get("STUDIO_HOST", "127.0.0.1"), PORT), Handler).serve_forever()
