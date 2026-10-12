#!/usr/bin/env python3
"""Image & Video Studio: a small web front end over the local ComfyUI.

Pictures: Z-Image Turbo. Video: Wan 2.2 TI2V 5B (text-to-video, or animate a picture).
Runs with ComfyUI's own venv (aiohttp + Pillow). Listens on 127.0.0.1:7870 by default.
"""
import asyncio, base64, copy, hmac, io, json, os, random, re, shutil, time, uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp
from aiohttp import web
from PIL import Image

import auth
import ratings as R
import workflows as W
import models as M

HOST = os.environ.get("GEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("GEN_PORT", "7870"))
COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
COMFY_DIR = Path(os.environ.get("COMFY_DIR", "~/ComfyUI")).expanduser()
OUTPUT, INPUT = COMFY_DIR / "output", COMFY_DIR / "input"
RADIO = os.environ.get("RADIO_URL", "http://127.0.0.1:7860")
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
HERE = Path(__file__).resolve().parent
DATA = HERE / "jobs.json"
CLIENT_ID = "genstudio-" + uuid.uuid4().hex[:8]

IMAGE_SIZES = {"1:1": (1024, 1024), "3:2": (1216, 832), "2:3": (832, 1216), "16:9": (1344, 768), "9:16": (768, 1344)}
VIDEO_SIZES = {"720p": {"16:9": (1280, 704), "9:16": (704, 1280), "1:1": (960, 960)},
               "480p": {"16:9": (832, 480), "9:16": (480, 832), "1:1": (640, 640)}}
VIDEO_NEGATIVE = ("blurry, low quality, static, distorted, deformed, watermark, text, subtitles, "
                  "jpeg artifacts, flickering, extra limbs, bad anatomy")
STAGES = {"image": {"8": "Drawing", "9": "Finishing"},
          "video": {"8": "Animating", "9": "Decoding frames", "10": "Encoding video", "11": "Saving"}}

jobs = {}                      # job id -> job dict (persisted to jobs.json)
live = {"prompt_id": None, "node": None, "value": 0, "max": 0}
http: aiohttp.ClientSession = None
refresh_lock = asyncio.Lock()
last_refresh = 0.0


# ---------- settings (⚙ dialog; saved to settings.json, applied to the next job) ----------

SAMPLERS = ["res_multistep", "euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "uni_pc", "lcm"]
SCHEDULERS = ["simple", "normal", "karras", "beta", "sgm_uniform"]
# key, group, label, type, options-or-min, max, default, help
SCHEMA = [
    ("image_shape", "Pictures", "Default shape", "choice", list(IMAGE_SIZES), None, "1:1", "Shape picked when the page opens."),
    ("image_count", "Pictures", "Default number of pictures", "choice", ["1", "2", "4"], None, "1", ""),
    ("image_megapixels", "Pictures", "Picture size (megapixels)", "choice", ["1", "1.5", "2"], None, "1",
     "1 MP = 1024x1024. Bigger shows more detail and takes longer; Z-Image Turbo is happiest up to 2 MP."),
    ("image_steps", "Pictures", "Steps", "int", 4, 20, 8, "Z-Image Turbo is tuned for 8. More rarely helps; fewer is faster but rougher."),
    ("image_shift", "Pictures", "Shift", "float", 1, 8, 3, "Higher favours overall composition, lower favours fine detail."),
    ("image_sampler", "Pictures", "Sampler", "choice", SAMPLERS, None, "res_multistep", "The algorithm that draws the picture."),
    ("image_scheduler", "Pictures", "Scheduler", "choice", SCHEDULERS, None, "simple", "How the steps are spaced."),
    ("video_shape", "Video", "Default shape", "choice", ["16:9", "9:16", "1:1"], None, "16:9", "Used for text-to-video; animated pictures keep their own shape."),
    ("video_resolution", "Video", "Resolution", "choice", ["720p", "480p"], None, "720p", "480p renders roughly twice as fast; Wan 2.2 5B is trained for 720p."),
    ("video_default_seconds", "Video", "Default length (seconds)", "int", 1, 10, 5, ""),
    ("video_max_seconds", "Video", "Longest video (seconds)", "int", 2, 10, 5,
     "Wan 2.2 5B is made for up to 5 s; longer clips take proportionally longer and tend to drift or loop."),
    ("video_fps", "Video", "Frames per second", "choice", ["16", "24"], None, "24", "24 is the model's native rate; 16 makes each second faster to render."),
    ("video_steps", "Video", "Steps", "int", 10, 50, 30, "More = cleaner motion and detail, slower."),
    ("video_cfg", "Video", "Prompt strength (CFG)", "float", 1, 10, 5, "How strictly the video follows the prompt. Too high looks overcooked."),
    ("video_shift", "Video", "Shift", "float", 1, 12, 8, "Higher favours big, coherent motion; lower favours fine detail."),
    ("video_sampler", "Video", "Sampler", "choice", SAMPLERS, None, "uni_pc", ""),
    ("video_negative", "Video", "Avoid (negative prompt)", "text", None, None, VIDEO_NEGATIVE, "Things every video should avoid."),
    ("hf_token", "Downloads", "Hugging Face token", "secret", None, None, "",
     "Needed only for gated models (e.g. FLUX.1 dev). Create a read token at huggingface.co → Settings → Access Tokens."),
    ("civitai_token", "Downloads", "Civitai API key", "secret", None, None, "",
     "Many Civitai models only download with a key. Create one at civitai.com → Account settings → API Keys."),
    ("auto_free_gpu", "GPU", "Free the GPU automatically", "bool", None, None, False,
     "Before each job, switch the music radio off and unload chat models if they are using the GPU."),
]
DEFAULTS = {row[0]: row[6] for row in SCHEMA}
S = dict(DEFAULTS)
SETTINGS_FILE = HERE / "settings.json"


def check_setting(key, raw):
    _, _, label, kind, lo, hi, _, _ = next(r for r in SCHEMA if r[0] == key)
    try:
        if kind == "int":
            v = int(raw)
        elif kind == "float":
            v = float(raw)
        elif kind == "bool":
            v = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "on", "yes")
        elif kind == "choice":
            v = str(raw)
            if v not in lo:
                raise ValueError
        elif kind == "secret":
            v = str(raw).strip()[:400]
        else:
            v = str(raw).strip()[:1000]
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"{label}: invalid value")
    if kind in ("int", "float") and not lo <= v <= hi:
        raise web.HTTPBadRequest(text=f"{label}: must be {lo}–{hi}")
    return v


def load_settings():
    if SETTINGS_FILE.exists():
        for k, raw in json.loads(SETTINGS_FILE.read_text()).items():
            if k in DEFAULTS:
                try:
                    S[k] = check_setting(k, raw)
                except web.HTTPBadRequest:
                    pass


def settings_view():
    secrets_ = [r[0] for r in SCHEMA if r[3] == "secret"]
    return {"values": {k: ("" if k in secrets_ else v) for k, v in S.items()}, "defaults": DEFAULTS,
            "secret_set": {k: bool(S.get(k)) for k in secrets_},
            "schema": [{"key": k, "group": g, "label": lab, "type": t, "help": h,
                        **({"options": lo} if t == "choice" else {"min": lo, "max": hi})} for k, g, lab, t, lo, hi, _, h in SCHEMA]}


def image_size(aspect):
    w, h = IMAGE_SIZES[aspect]
    f = float(S["image_megapixels"]) ** 0.5
    return int(round(w * f / 64)) * 64, int(round(h * f / 64)) * 64


# ---------- persistence ----------

def load_jobs():
    if DATA.exists():
        for j in json.loads(DATA.read_text()):
            jobs[j["id"]] = j


def save_jobs():
    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(jobs.values(), key=lambda j: j["created"]), indent=1))
    tmp.replace(DATA)


# ---------- ComfyUI workflows (API format) ----------

def image_workflow(prompt, w, h, seed):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": S["image_shift"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
              "latent_image": ["7", 0], "seed": seed, "steps": S["image_steps"], "cfg": 1, "sampler_name": S["image_sampler"],
              "scheduler": S["image_scheduler"], "denoise": 1}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "genstudio/img"}},
    }


def video_workflow(prompt, w, h, frames, seed, start_image=None):
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_ti2v_5B_fp16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan2.2_vae.safetensors"}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": S["video_shift"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": S["video_negative"]}},
        "7": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"vae": ["3", 0], "width": w, "height": h,
              "length": frames, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
              "latent_image": ["7", 0], "seed": seed, "steps": S["video_steps"], "cfg": S["video_cfg"], "sampler_name": S["video_sampler"],
              "scheduler": "simple", "denoise": 1}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": int(S["video_fps"])}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "genstudio/vid",
               "format": "auto", "codec": "auto"}},
    }
    if start_image:
        wf["12"] = {"class_type": "LoadImage", "inputs": {"image": start_image}}
        wf["7"]["inputs"]["start_image"] = ["12", 0]
    return wf


# ---------- job bookkeeping ----------

def collect_outputs(entry):
    outs = []
    for node in entry.get("outputs", {}).values():
        for items in node.values():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and it.get("filename") and it.get("type", "output") == "output":
                    rel = f"{it['subfolder']}/{it['filename']}" if it.get("subfolder") else it["filename"]
                    if Path(rel).suffix.lower() in (".json", ".txt", ".latent"):
                        continue
                    outs.append({"path": rel, "url": "/media/" + quote(rel)})
    return outs


def history_error(entry):
    for kind, data in entry.get("status", {}).get("messages", []):
        if kind == "execution_interrupted":
            return "cancelled", "Cancelled"
        if kind == "execution_error":
            return "error", (data.get("exception_message") or "Generation failed").strip()[:400]
    return "error", "Generation failed"


async def comfy_get(path):
    async with http.get(COMFY + path, timeout=aiohttp.ClientTimeout(total=10)) as r:
        return await r.json()


async def refresh(force=False):
    """Bring every unfinished job up to date with ComfyUI's queue and history."""
    global last_refresh
    pending = [j for j in jobs.values() if j["status"] in ("queued", "running")]
    if not pending or (not force and time.time() - last_refresh < 0.8):
        return
    async with refresh_lock:
        last_refresh = time.time()
        try:
            q = await comfy_get("/queue")
        except Exception:
            return                                  # ComfyUI restarting: try again on the next poll
        running = {item[1] for item in q.get("queue_running", [])}
        waiting = [item[1] for item in sorted(q.get("queue_pending", []), key=lambda i: i[0])]
        changed = False
        for j in pending:
            pid = j["prompt_id"]
            if pid in running:
                if j["status"] != "running":
                    j.update(status="running", started=j.get("started") or time.time()); changed = True
                continue
            if pid in waiting:
                j["position"] = waiting.index(pid) + 1
                continue
            try:
                hist = (await comfy_get(f"/history/{pid}")).get(pid)
            except Exception:
                continue
            if not hist:
                j.update(status="error", error="Lost: ComfyUI restarted before this finished", finished=time.time())
            elif hist.get("status", {}).get("status_str") == "success":
                j.update(status="done", outputs=collect_outputs(hist), finished=time.time())
            else:
                status, msg = history_error(hist)
                j.update(status=status, error=msg, finished=time.time())
            j.pop("position", None)
            changed = True
        if changed:
            save_jobs()


async def watch_comfy(app):
    """Follow ComfyUI's websocket for live progress of our prompts."""
    while True:
        try:
            async with http.ws_connect(f"{COMFY.replace('http', 'ws', 1)}/ws?clientId={CLIENT_ID}", heartbeat=30) as ws:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    m = json.loads(msg.data)
                    t, d = m.get("type"), m.get("data", {})
                    if t == "execution_start":
                        live.update(prompt_id=d.get("prompt_id"), node=None, value=0, max=0)
                        await refresh(force=True)
                    elif t == "executing":
                        live.update(prompt_id=d.get("prompt_id"), node=d.get("node"), value=0, max=0)
                        if d.get("node") is None:
                            await refresh(force=True)
                    elif t == "progress":
                        live.update(prompt_id=d.get("prompt_id"), node=str(d.get("node")), value=d.get("value", 0), max=d.get("max", 0))
                    elif t in ("execution_success", "execution_error", "execution_interrupted"):
                        live.update(prompt_id=None, node=None, value=0, max=0)
                        await refresh(force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(3)


# ---------- GPU sharing ----------

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


# ---------- HTTP handlers ----------

def versioned(rel):
    """URL for an output file that changes whenever the file does, so browsers can cache it for good."""
    try:
        v = f"{(OUTPUT / rel).stat().st_mtime_ns:x}"
    except OSError:
        v = "0"
    return f"/media/{quote(rel)}?v={v}"


def public(j):
    out = {k: v for k, v in j.items() if k != "prompt_id"}
    out["outputs"] = [dict(o, url=versioned(o["path"])) for o in j.get("outputs", [])]
    if j["status"] == "running" and live["prompt_id"] == j["prompt_id"]:
        out["stage"] = STAGES[j["kind"]].get(str(live["node"]), "Loading models")
        out["progress"] = [live["value"], live["max"]] if live["max"] else None
    return out


async def index(request):
    return web.FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


async def state(request):
    await refresh()
    me = request["user"]["name"]
    ordered = sorted((j for j in jobs.values() if j.get("owner") == me), key=lambda j: j["created"], reverse=True)
    return web.json_response({"jobs": [public(j) for j in ordered], "gpu": await gpu_state()})


def source_for_video(src, me):
    """Copy the chosen start picture into ComfyUI's input folder. Returns (input name, aspect)."""
    if src.get("job"):
        job = jobs.get(src["job"])
        if not job or job.get("owner") != me or job["kind"] != "image" or not job.get("outputs"):
            raise web.HTTPBadRequest(text="That picture is not available")
        path = OUTPUT / job["outputs"][int(src.get("index", 0))]["path"]
    elif src.get("upload"):
        if uploads.get(Path(src["upload"]).name) != me:
            raise web.HTTPBadRequest(text="That picture is not available")
        path = INPUT / Path(src["upload"]).name
    else:
        raise web.HTTPBadRequest(text="No start picture")
    if not path.is_file():
        raise web.HTTPBadRequest(text="Start picture not found")
    with Image.open(path) as im:
        ratio = im.width / im.height
    aspect = "16:9" if ratio > 1.2 else "9:16" if ratio < 0.83 else "1:1"
    if path.parent != INPUT:
        name = f"genstudio_{uuid.uuid4().hex[:10]}{path.suffix}"
        shutil.copyfile(path, INPUT / name)
    else:
        name = path.name
    return name, aspect


def lora_files():
    root = W.MODELS / "loras"
    return {str(f.relative_to(root)) for f in root.rglob("*") if f.is_file() and f.suffix.lower() in (".safetensors", ".pt", ".ckpt", ".bin")} if root.exists() else set()


def check_loras(raw, admin=True):
    """[{"name", "strength"}] from the Create panel: installed files only, at most 6, strengths -4..4."""
    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > 6:
        raise web.HTTPBadRequest(text="Up to 6 LoRAs")
    have, out = lora_files(), []
    for l in raw:
        name = str((l or {}).get("name") or "")
        if name not in have:
            raise web.HTTPBadRequest(text=f"LoRA not installed: {name or '(none chosen)'}")
        if not admin and R.rating_of("loras", name) != "pg13":
            raise web.HTTPForbidden(text=f"{name} isn't available on this account")
        try:
            strength = float(l.get("strength", 1))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text=f"Strength for {name} must be a number")
        if not -4 <= strength <= 4:
            raise web.HTTPBadRequest(text=f"Strength for {name} must be between -4 and 4")
        out.append({"name": name, "strength": round(strength, 3)})
    return out


async def list_loras(request):
    """Installed LoRAs with what the Models page knows about them (base model, trigger words)."""
    items = await asyncio.get_running_loop().run_in_executor(None, M.installed)
    by = {m["file"]: m for m in items if m["folder"] == "loras"}
    admin = is_admin(request)
    return web.json_response({"loras": [{"name": n, "title": ((by.get(n) or {}).get("meta") or {}).get("model"),
                                         "base": ((by.get(n) or {}).get("meta") or {}).get("base"),
                                         "words": (((by.get(n) or {}).get("meta") or {}).get("trained_words") or [])[:8],
                                         **({"rating": R.rating_of("loras", n)} if admin else {})}
                                        for n in sorted(lora_files(), key=str.lower) if admin or R.rating_of("loras", n) == "pg13"]})


async def wf_exact(request):
    """Admins: for one model, download the exact file instead of using a similar one that's installed (or go back)."""
    require_admin(request)
    r = my_recipe(request)
    b = await request.json()
    name = str(b.get("name") or "")[:300]
    exact = set(r.get("exact_models") or [])
    exact.add(name) if b.get("exact") else exact.discard(name)
    r["exact_models"] = sorted(exact)
    save_recipe(r); plans.pop(r["id"], None)
    return web.json_response({"ok": True})


async def wf_save_values(request):
    """Remember the settings someone typed into a workflow (prompt, seeds, strengths...) for next time."""
    r = my_recipe(request)
    vals = (await request.json()).get("values") or {}
    if not isinstance(vals, dict) or len(json.dumps(vals)) > 200_000:
        raise web.HTTPBadRequest(text="Too much to save")
    clean = {k: {"value": v.get("value"), **({"random": bool(v["random"])} if "random" in v else {})}
             for k, v in vals.items() if re.fullmatch(r"[\w.:-]{1,80}", str(k)) and isinstance(v, dict)}
    r["saved_values"] = {**(r.get("saved_values") or {}), **clean}
    save_recipe(r)
    return web.json_response({"ok": True})


async def generate(request):
    body = await request.json()
    kind = body.get("kind")
    prompt = (body.get("prompt") or "").strip()
    if kind not in ("image", "video"):
        raise web.HTTPBadRequest(text="kind must be image or video")
    if not prompt:
        raise web.HTTPBadRequest(text="Write a prompt first")
    count = max(1, min(int(body.get("count") or 1), 8 if kind == "image" else 4))
    loras = check_loras(body.get("loras"), is_admin(request))
    await prepare_gpu()
    fixed_seed = body.get("seed")
    created = []
    for n in range(count):
        seed = int(fixed_seed) + n if str(fixed_seed or "").strip().lstrip("-").isdigit() else random.randrange(2**48)
        params = {"seed": seed}
        if kind == "image":
            aspect = body.get("aspect") if body.get("aspect") in IMAGE_SIZES else S["image_shape"]
            w, h = image_size(aspect)
            wf = image_workflow(prompt, w, h, seed)
        else:
            seconds = max(1, min(int(body.get("seconds") or S["video_default_seconds"]), S["video_max_seconds"]))
            sizes = VIDEO_SIZES[S["video_resolution"]]
            start, aspect = (None, body.get("aspect") if body.get("aspect") in sizes else S["video_shape"])
            if body.get("source"):
                start, aspect = source_for_video(body["source"], request["user"]["name"])
                params["source"] = body["source"]
            w, h = sizes[aspect]
            wf = video_workflow(prompt, w, h, seconds * int(S["video_fps"]) + 1, seed, start)
            params["seconds"] = seconds
        params.update(aspect=aspect, width=w, height=h)
        if loras:
            W.inject_loras(wf, loras); params["loras"] = loras
        keys = (("image_steps", "image_shift", "image_sampler", "image_scheduler", "image_megapixels") if kind == "image" else
                ("video_steps", "video_cfg", "video_shift", "video_sampler", "video_fps", "video_resolution", "video_negative"))
        params["settings"] = {k: S[k] for k in keys}        # so "Load settings" can show what it was made with
        async with http.post(COMFY + "/prompt", json={"prompt": wf, "client_id": CLIENT_ID}) as r:
            res = await r.json()
            if r.status != 200 or "prompt_id" not in res:
                msg = res.get("error", {}).get("message") if isinstance(res.get("error"), dict) else res.get("error")
                raise web.HTTPBadGateway(text=f"ComfyUI refused the job: {msg or res}")
        job = {"id": uuid.uuid4().hex[:12], "owner": request["user"]["name"], "kind": kind, "prompt": prompt, "params": params,
               "prompt_id": res["prompt_id"], "status": "queued", "created": time.time(), "outputs": []}
        jobs[job["id"]] = job
        created.append(job["id"])
    save_jobs()
    await refresh(force=True)
    return web.json_response({"ok": True, "jobs": created})


async def upload(request):
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name != "file":
        raise web.HTTPBadRequest(text="Expected a file")
    data = await field.read(decode=False)
    if len(data) > 40 * 2**20:
        raise web.HTTPRequestEntityTooLarge(max_size=40 * 2**20, actual_size=len(data))
    tmp = INPUT / f".upload-{uuid.uuid4().hex}"
    tmp.write_bytes(data)
    try:
        with Image.open(tmp) as im:
            im = im.convert("RGB")
            name = f"genstudio_up_{uuid.uuid4().hex[:10]}.png"
            im.save(INPUT / name)
            uploads[name] = request["user"]["name"]; save_uploads()
    except Exception:
        raise web.HTTPBadRequest(text="That file is not a picture")
    finally:
        tmp.unlink(missing_ok=True)
    return web.json_response({"upload": name, "url": "/input/" + name})


def safe_file(base, rel):
    path = (base / rel).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise web.HTTPNotFound()
    return path


def owns_output(me, rel):
    rel = str(Path(rel))
    return any(j.get("owner") == me and any(str(Path(o["path"])) == rel for o in j.get("outputs", [])) for j in jobs.values())


async def media(request):
    if not owns_output(request["user"]["name"], request.match_info["path"]):
        raise web.HTTPNotFound()
    path = safe_file(OUTPUT, request.match_info["path"])
    # versioned URLs never change content (ComfyUI may reuse a deleted file's name, but that gets a new ?v=)
    cache = "private, max-age=31536000, immutable" if request.query.get("v") else "no-cache"
    resp = web.FileResponse(path, headers={"Cache-Control": cache})
    if request.query.get("download"):
        resp.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return resp


async def input_file(request):
    if uploads.get(request.match_info["name"]) != request["user"]["name"]:
        raise web.HTTPNotFound()
    return web.FileResponse(safe_file(INPUT, request.match_info["name"]),          # upload names are unique
                            headers={"Cache-Control": "private, max-age=31536000, immutable"})


async def cancel(request):
    job = jobs.get(request.match_info["id"])
    if not job or job.get("owner") != request["user"]["name"]:
        raise web.HTTPNotFound()
    if job["status"] == "queued":
        await http.post(COMFY + "/queue", json={"delete": [job["prompt_id"]]})
        job.update(status="cancelled", error="Cancelled", finished=time.time())
        save_jobs()
    elif job["status"] == "running":
        await http.post(COMFY + "/interrupt", json={"prompt_id": job["prompt_id"]})
    return web.json_response({"ok": True})


async def delete(request):
    job = jobs.get(request.match_info["id"])
    if not job or job.get("owner") != request["user"]["name"]:
        raise web.HTTPNotFound()
    jobs.pop(job["id"])
    if job["status"] in ("queued", "running"):
        jobs[job["id"]] = job
        raise web.HTTPConflict(text="Cancel it first")
    for o in job.get("outputs", []):
        try:
            safe_file(OUTPUT, o["path"]).unlink()
        except web.HTTPNotFound:
            pass
    save_jobs()
    return web.json_response({"ok": True})


async def _free(release_radio=True, unload_llm=True):
    done = []
    if release_radio:
        try:
            async with http.post(RADIO + "/api/gpu/release", timeout=aiohttp.ClientTimeout(total=5)) as r:
                d = await r.json()
                done.append("Radio: " + (d.get("message") or "released"))
        except Exception:
            pass
    if unload_llm:
        try:
            async with http.get(OLLAMA + "/api/ps", timeout=aiohttp.ClientTimeout(total=2)) as r:
                models = [m["name"] for m in (await r.json()).get("models", []) if m.get("size_vram")]
            for m in models:
                await http.post(OLLAMA + "/api/generate", json={"model": m, "keep_alive": 0})
                done.append(f"Unloaded {m}")
        except Exception:
            pass
    return done


async def free_gpu(request):
    """The Free the GPU button: ask the music radio to give the GPU back and unload Ollama chat models (they reload on next use)."""
    return web.json_response({"ok": True, "done": await _free()})


# GPU priority, shared with the radio (it reads the same file): which AI tool gets the graphics card first
PRIORITY_FILE = Path.home() / ".config" / "ai-gpu-priority.json"
PRIORITIES = {"shared": "Shared — take turns", "studio": "Image & Video Studio", "radio": "Radio", "assistant": "Assistant (Hermes)"}


def gpu_priority():
    try:
        v = json.loads(PRIORITY_FILE.read_text()).get("priority")
    except (OSError, ValueError):
        v = None
    return v if v in PRIORITIES else "shared"


async def prepare_gpu():
    """Before a job: make room according to the GPU priority (and the older "free automatically" setting)."""
    p, auto = gpu_priority(), S["auto_free_gpu"]
    await _free(release_radio=p == "studio" or (auto and p not in ("radio",)),
                unload_llm=p in ("studio", "radio") or (auto and p != "assistant"))


# ---------- disk usage for the side panel
DISK_PARTS = [("ComfyUI models", Path.home() / "ComfyUI" / "models"), ("Chat models (Ollama)", Path("/usr/share/ollama/.ollama/models")),
              ("Creations", Path.home() / "ComfyUI" / "output"), ("Radio songs", Path.home() / "yue2-xpu-radio" / "library")]
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
    du = shutil.disk_usage(W.MODELS)
    if (_disk["parts"] is None or time.time() - _disk["t"] > 300) and not _disk["busy"]:
        _disk["busy"] = True
        try:
            loop = asyncio.get_running_loop()
            sizes = await asyncio.gather(*(loop.run_in_executor(None, _tree_bytes, d) for _, d in DISK_PARTS))
            _disk.update(t=time.time(), parts=[{"label": l, "bytes": b} for (l, d), b in zip(DISK_PARTS, sizes) if d.exists()])
        finally:
            _disk["busy"] = False
    partial = sum(f.stat().st_size for f in W.MODELS.rglob("*.part")) if W.MODELS.exists() else 0
    return web.json_response({"total": du.total, "used": du.used, "free": du.free, "parts": _disk["parts"] or [],
                              "partial_downloads": partial, "measured": _disk["t"]})


# ---------- ✨ Improve prompt (the fast GPU model through Ollama; the big CPU model if that's unavailable)
PROMPT_MODELS = ["qwen3:4b-instruct-2507-q4_K_M", "qwen3.6-songwriter"]
PROMPT_GUIDE = {
    "image": "a picture model (Z-Image) that reads natural sentences: say who/what the subject is, what they're doing, the setting, "
             "composition and framing, lighting, colour palette, and the medium or style (photo with camera and lens, oil painting, "
             "3D render...). 50-110 words.",
    "video": "a video model (Wan 2.2) that animates a few seconds: describe the subject, the ACTION as it unfolds over time, the camera "
             "movement (slow dolly in, handheld tracking, static wide...), the setting, lighting and mood. 40-90 words.",
}


SHARED = HERE / "shared"


async def shared_file(request):
    """The look and script shared by every app (switcher, now-playing bar); the radio serves the same files."""
    name = request.match_info["name"]
    if name not in ("b70.css", "b70.js"):
        raise web.HTTPNotFound()
    return web.FileResponse(SHARED / name, headers={"Cache-Control": "no-cache"})


async def nowplaying(request):
    """What's on the radio, for the now-playing bar on the Studio's pages."""
    try:
        async with http.get(RADIO + "/api/radio/state", timeout=aiohttp.ClientTimeout(total=3)) as r:
            d = await r.json()
    except Exception:
        raise web.HTTPServiceUnavailable(text="The radio isn't answering")
    sv = d.get("stream") or {}
    aired = [a for a in sv.get("aired", []) if a.get("start_pos") is not None and a["start_pos"] <= (sv.get("pos") or 0)]
    a = aired[-1] if aired and aired[-1].get("id") else None
    st = next((x for x in d.get("stations", []) if x["id"] == d.get("station")), None)
    return web.json_response({"on": d.get("on"), "now": a and {"id": a["id"], "title": a.get("title"), "cover": a.get("cover"), "feedback": a.get("feedback")},
                              "station": st and {"id": st["id"], "name": st["name"], "art": st.get("art")}})


async def nowplaying_action(request):
    """👍 / skip from the now-playing bar, passed on to the radio (which has no sign-in of its own)."""
    action = request.match_info["action"]
    if action not in ("feedback", "skip"):
        raise web.HTTPNotFound()
    b = await request.json()
    body = {"track": str(b.get("track") or "")[:64], "kind": b.get("kind") if b.get("kind") in ("keep", "clear", "skip") else "skip"}
    async with http.post(f"{RADIO}/api/radio/{action}", json=body, timeout=aiohttp.ClientTimeout(total=5)) as r:
        return web.json_response(await r.json(content_type=None), status=r.status)


# ---------- the assistant's internet access (Hermes's web search + browser tools on the chat connection)
HERMES_BIN = str(Path.home() / ".local" / "bin" / "hermes")
NET_TOOLSETS = ("web", "browser")
OFFLINE_ALSO = ("terminal", "code_execution")          # shell commands and scripts could reach the internet too: off with it


async def _hermes_toolsets():
    async with http.get(HERMES + "/v1/toolsets", headers={"Authorization": "Bearer " + _hermes_key()},
                        timeout=aiohttp.ClientTimeout(total=10)) as r:
        d = await r.json()
    items = d.get("toolsets", d.get("data", [])) if isinstance(d, dict) else d
    return {t.get("name"): bool(t.get("enabled")) for t in items}


async def assistant_internet(request):
    """Admins: is the assistant allowed on the internet (web search, reading pages, its own browser)? POST {on} to change it.
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


async def home_page(request):
    return web.FileResponse(HERE / "home.html", headers={"Cache-Control": "no-cache"})


async def home_summary(request):
    """Everything the home page shows. Open to anyone who can reach the site: counts and what's on the radio only;
    a signed-in person also gets their own latest creations."""
    out = {"radio": None, "studio": {}, "system": {}, "user": None}
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
    active = [j for j in jobs.values() if j["status"] in ("queued", "running")]
    out["studio"] = {"queued": sum(j["status"] == "queued" for j in active), "running": sum(j["status"] == "running" for j in active)}
    g = await gpu_state()
    du = shutil.disk_usage(W.MODELS)
    out["system"] = {"vram_used": g["used_mb"], "vram_total": g["total_mb"], "disk_free": du.free, "disk_total": du.total, "priority": gpu_priority()}
    me = request.get("user")
    if me:
        mine = sorted((j for j in jobs.values() if j.get("owner") == me["name"] and j["status"] == "done" and j.get("outputs")),
                      key=lambda j: -j["created"])[:6]
        out["user"] = {"name": me["name"], "admin": me["role"] == "admin",
                       "recent": [{"id": j["id"], "kind": j["kind"], "url": public(j)["outputs"][0]["url"]} for j in mine]}
    return web.json_response(out)


async def improve_prompt(request):
    """Edit the person's prompt into a fuller one for the chosen model; keeps their idea, adds what's missing."""
    b = await request.json()
    text = str(b.get("prompt") or "").strip()[:2000]
    kind = b.get("kind") if b.get("kind") in PROMPT_GUIDE else "image"
    if not text:
        raise web.HTTPBadRequest(text="Write a few words first")
    system = ("You improve prompts for " + PROMPT_GUIDE[kind] + " EDIT the person's prompt; do not replace it. Keep every subject, "
              "detail, name, style and wording choice they gave, in their language. Only add what's missing and make it vivid and "
              "specific. Reply with the improved prompt only: no preamble, no quotes, no lists.")
    if not is_admin(request):
        system += " Keep it PG-13: no nudity, sexual content, graphic violence or gore; if the prompt asks for that, tone it down."
    last = None
    for model in PROMPT_MODELS:
        try:
            async with http.post(OLLAMA + "/api/chat", json={"model": model, "stream": False, "think": False,
                                 "options": {"temperature": 0.4, "num_predict": 300},
                                 "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]},
                                 timeout=aiohttp.ClientTimeout(total=180)) as r:
                if r.status != 200:
                    last = f"{model}: HTTP {r.status}"; continue
                out = ((await r.json()).get("message") or {}).get("content", "").strip().strip('"')
            if len(out) >= max(10, len(text) // 2):
                return web.json_response({"prompt": out[:2500], "model": model})
            last = f"{model}: an empty reply"
        except Exception as e:
            last = f"{model}: {e}"
    raise web.HTTPServiceUnavailable(text=f"Couldn't improve it right now ({last})")


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


async def on_startup(app):
    global http
    http = aiohttp.ClientSession()
    W.http = http
    W.log = lambda *a: print(*a, flush=True)
    W.settings.update(hf_token=S["hf_token"], civitai_token=S["civitai_token"])
    app["watcher"] = asyncio.create_task(watch_comfy(app))
    R.load()
    W.after_download = M.after_workflow_download
    app["ratings"] = asyncio.create_task(rate_models_forever())


async def rate_models_forever():
    """Keep content-model ratings current (new downloads get rated within minutes)."""
    await asyncio.sleep(20)
    while True:
        try:
            await R.refresh()
        except Exception as e:
            print(f"ratings: {e}", flush=True)
        await asyncio.sleep(600)


def is_admin(request):
    return (request.get("user") or {}).get("role") == "admin"


async def on_cleanup(app):
    app["watcher"].cancel()
    await http.close()


async def get_settings(request):
    return web.json_response(settings_view())


async def put_settings(request):
    require_admin(request)
    body = await request.json()
    new = dict(DEFAULTS) if body.get("reset") else dict(S)
    for k, raw in body.items():
        if k == "reset":
            continue
        if k.endswith("_clear") and k[:-6] in DEFAULTS:
            new[k[:-6]] = ""; continue
        if k not in DEFAULTS:
            raise web.HTTPBadRequest(text=f"Unknown setting: {k}")
        if next(r for r in SCHEMA if r[0] == k)[3] == "secret" and not str(raw).strip():
            continue                                  # blank secret field = keep the saved one
        new[k] = check_setting(k, raw)
    if new["video_default_seconds"] > new["video_max_seconds"]:
        raise web.HTTPBadRequest(text="Default video length can't be longer than the longest video")
    S.update(new)
    SETTINGS_FILE.write_text(json.dumps({k: v for k, v in S.items() if v != DEFAULTS[k]}, indent=1))
    SETTINGS_FILE.chmod(0o600)
    W.settings.update(hf_token=S["hf_token"], civitai_token=S["civitai_token"])
    return web.json_response(settings_view())


# ---------- accounts: everyone sees only their own pictures and videos ----------

UPLOADS_FILE = HERE / "uploads.json"
uploads = {}                       # uploaded start-picture name -> owner
PUBLIC_PATHS = {"/", "/home", "/api/home", "/api/session", "/api/setup", "/api/login"}


def load_uploads():
    if UPLOADS_FILE.exists():
        uploads.update(json.loads(UPLOADS_FILE.read_text()))


def save_uploads():
    UPLOADS_FILE.write_text(json.dumps(uploads))


AGENT_TOKEN = HERE / "agent.token"              # {"token", "user"}: lets the local Hermes assistant act as that account

HERMES = "http://127.0.0.1:8642"
INSTRUCTIONS = ("You are being used from the Image & Video Studio's Assistant panel in a web browser. Keep replies short and "
                "conversational, use plain text or light markdown, and prefer the radio_* and studio_* tools for the radio "
                "and the Image & Video Studio. When you make pictures or videos, mention they appear in the Studio.")


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

    model = body.get("model") if body.get("model") in ("lemming", "agi") else "agi"    # Hermes model routes (config.yaml)
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


def agent_user(request):
    """The local assistant's tools send "Authorization: Bearer <token>" from agent.token (readable only by this account)."""
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer ") or not AGENT_TOKEN.exists():
        return None
    try:
        cfg = json.loads(AGENT_TOKEN.read_text())
    except ValueError:
        return None
    if cfg.get("user") in auth.users and hmac.compare_digest(h[7:].encode(), str(cfg.get("token", "")).encode()):
        return auth.user_for_name(cfg["user"]) | {"via": "assistant"}
    return None


@web.middleware
async def guard(request, handler):
    """Every request: who is it (session cookie), are they signed in, and is a change coming from this site."""
    request["user"] = auth.user_for(request.cookies.get(auth.COOKIE)) or agent_user(request)
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:      # another website trying to act as the user
            raise web.HTTPForbidden(text="Cross-site request refused")
    if request.path not in PUBLIC_PATHS and not request.path.startswith(("/b70/", "/api/nowplaying")) and not request["user"]:
        raise web.HTTPUnauthorized(text="Please sign in")
    try:
        return await handler(request)
    except auth.AuthError as e:
        raise web.HTTPBadRequest(text=str(e))


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
    """First run only: create the admin account. Everything made before accounts existed becomes theirs."""
    if not auth.needs_setup():
        raise web.HTTPForbidden(text="Setup is already done")
    body = await request.json()
    name = auth.create_user(body.get("username"), body.get("password"), "admin")
    for j in jobs.values():
        j.setdefault("owner", name)
    save_jobs()
    for f in INPUT.glob("genstudio_*"):
        uploads.setdefault(f.name, name)
    save_uploads()
    return session_response({"user": auth.user_for_name(name)}, token=auth.new_session(name))


async def login(request):
    body = await request.json()
    try:
        name = auth.verify(body.get("username"), body.get("password"))
    except auth.AuthError:
        await asyncio.sleep(0.6)                       # slows down password guessing
        raise
    return session_response({"user": auth.user_for_name(name)}, token=auth.new_session(name))


async def logout(request):
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
    counts = {}
    for j in jobs.values():
        counts[j.get("owner")] = counts.get(j.get("owner"), 0) + 1
    return web.json_response({"users": [u | {"items": counts.get(u["name"], 0)} for u in auth.listing()]})


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
    """Delete an account together with its pictures, videos and uploads."""
    require_admin(request)
    name = request.match_info["name"]
    if name not in auth.users:
        raise web.HTTPNotFound()
    if name == request["user"]["name"]:
        raise web.HTTPBadRequest(text="You can't delete your own account while signed in to it")
    auth.delete_user(name)
    for j in [j for j in jobs.values() if j.get("owner") == name]:
        if j["status"] in ("queued", "running"):
            await http.post(COMFY + "/queue", json={"delete": [j["prompt_id"]]})
        for o in j.get("outputs", []):
            try:
                safe_file(OUTPUT, o["path"]).unlink()
            except web.HTTPNotFound:
                pass
        jobs.pop(j["id"])
    save_jobs()
    for f, owner in list(uploads.items()):
        if owner == name:
            (INPUT / f).unlink(missing_ok=True); uploads.pop(f)
    save_uploads()
    return web.json_response({"ok": True})


# ---------- workflows imported from pictures / videos / Civitai (each person's own library) ----------

RECIPES = HERE / "workflows"
recipes = {}                   # id -> recipe (prompt, editor workflow, source...) ; one JSON file each
plans = {}                     # id -> (time, plan) : model/node lookups are slow, keep them a few minutes


def load_recipes():
    RECIPES.mkdir(exist_ok=True)
    for f in RECIPES.glob("*.json"):
        r = json.loads(f.read_text()); recipes[r["id"]] = r


def save_recipe(r):
    (RECIPES / f"{r['id']}.json").write_text(json.dumps(r))


def my_recipe(request):
    r = recipes.get(request.match_info["id"])
    if not r or r["owner"] != request["user"]["name"]:
        raise web.HTTPNotFound()
    return r


def blocked_models(plan):
    """Installed content models in a workflow that aren't rated PG-13 (non-admins can't run it)."""
    return [m["name"] for m in plan.get("models", []) if m.get("installed") and not R.name_allowed(m.get("use") or m["name"])]


def recipe_card(r, plan=None, admin=True):
    out = {k: r.get(k) for k in ("id", "name", "kind", "thumb", "created", "source")}
    out["saved"] = r.get("saved_values") or {}
    if plan:
        out["plan"] = {k: plan.get(k) for k in ("kind", "missing_nodes", "models", "packs", "unknown_nodes", "core_nodes", "fields", "notes", "ready",
                                                "download_bytes", "free_bytes")}
        if not admin:                                     # PG-13 accounts: no prompts carried over from the source picture
            out["plan"]["fields"] = [{**f, "value": ""} if f["type"] == "text" else f for f in plan.get("fields", [])]
            out["plan"]["blocked"] = blocked_models(plan)
    return out


planning = {}                  # id -> running lookup task (so a second request waits for the same one)


async def fresh_plan(r, force=False):
    hit = plans.get(r["id"])
    if hit and not force and time.time() - hit[0] < 300:
        return hit[1]
    task = planning.get(r["id"])
    if not task or (force and task.done()):
        task = planning[r["id"]] = asyncio.create_task(W.plan(r, COMFY))
    try:
        plan = await asyncio.shield(task)
    except W.WorkflowError as e:
        raise web.HTTPBadRequest(text=str(e))
    finally:
        if task.done():
            planning.pop(r["id"], None)
    plans[r["id"]] = (time.time(), plan)
    r["kind"] = plan["kind"]
    return plan


def thumbnail(data):
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.thumbnail((320, 320)); im = im.convert("RGB")
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=80)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


async def wf_import(request):
    """A picture/video file (multipart) or {"url": ...}: read its ComfyUI workflow and add it to the person's library."""
    if request.content_type.startswith("multipart/"):
        field = await (await request.multipart()).next()
        if not field or field.name != "file":
            raise web.HTTPBadRequest(text="Expected a file")
        data, name, source = bytearray(), field.filename or "upload", field.filename or "upload"
        while chunk := await field.read_chunk(1 << 20):
            data += chunk
            if len(data) > 120 * 2**20:
                raise web.HTTPRequestEntityTooLarge(max_size=120 * 2**20, actual_size=len(data))
        data, meta = bytes(data), None
    else:
        body = await request.json()
        try:
            got = await W.fetch_url(str(body.get("url", "")).strip())
        except W.WorkflowError as e:
            raise web.HTTPBadRequest(text=str(e))
        data, name, source, meta = got["data"], got["name"], got["source"], got.get("meta")
    meta = meta or await asyncio.get_running_loop().run_in_executor(None, W.read_metadata, data, name)
    if not meta.get("prompt") and not meta.get("workflow"):
        if meta.get("a1111"):
            return web.json_response({"a1111": meta["a1111"]}, status=422)
        raise web.HTTPBadRequest(text="No ComfyUI workflow found in that file. Pictures copied from a web page or sent "
                                      "through chat apps usually lose it — use the original file, or a Civitai image link.")
    first_text = next((v for n in (meta.get("prompt") or {}).values() if isinstance(n, dict)
                       for k, v in n.get("inputs", {}).items() if k in ("text", "prompt") and isinstance(v, str) and v.strip()), "")
    r = {"id": uuid.uuid4().hex[:12], "owner": request["user"]["name"], "created": time.time(), "source": source,
         "name": ((first_text.strip().split("\n")[0][:60] if is_admin(request) else "") or Path(name).stem[:60]), "thumb": thumbnail(data),
         "prompt": meta.get("prompt"), "workflow": meta.get("workflow"), "civitai": meta.get("civitai") or [], "kind": "image"}
    try:
        quick = await W.analyze(r, COMFY)                 # instant: reads the workflow, checks what's installed
    except W.WorkflowError as e:
        raise web.HTTPBadRequest(text=str(e))
    r["kind"] = quick["kind"]
    recipes[r["id"]] = r; save_recipe(r)
    planning[r["id"]] = asyncio.create_task(W.plan(r, COMFY))   # download lookups continue in the background
    card = recipe_card(r)
    card["summary"] = {"ready": quick["ready"], "missing": sum(1 for m in quick["models"] if not m["installed"]) + len(quick["missing_nodes"]),
                       "kind": quick["kind"]}
    return web.json_response(card)


async def wf_list(request):
    me = request["user"]["name"]
    out = []
    for r in sorted(recipes.values(), key=lambda r: -r["created"]):
        if r["owner"] == me:
            card, hit = recipe_card(r), plans.get(r["id"])
            if hit:
                card["ready"] = hit[1]["ready"]
                if not is_admin(request):
                    card["blocked"] = bool(blocked_models(hit[1]))
            out.append(card)
    return web.json_response({"workflows": out})


async def wf_get(request):
    r = my_recipe(request)
    return web.json_response(recipe_card(r, await fresh_plan(r, force=request.query.get("refresh") == "1"), is_admin(request)))


async def wf_delete(request):
    r = my_recipe(request)
    recipes.pop(r["id"]); plans.pop(r["id"], None)
    (RECIPES / f"{r['id']}.json").unlink(missing_ok=True)
    return web.json_response({"ok": True})


async def install_plan(r, body, force=True):
    """A workflow's plan with the admin's pasted model links and named node packs folded in."""
    plan = copy.deepcopy(await fresh_plan(r, force=force))
    for m in plan["models"]:                              # links the admin pasted or picked: these replace any found source
        url = str((body.get("links") or {}).get(m["name"]) or "").strip()
        if url and not m["installed"]:
            try:                                          # page links become file links; checked before anything starts
                m["source"] = await W.resolve_link(url, m["name"], m["folder"])
            except W.WorkflowError as e:
                raise web.HTTPBadRequest(text=str(e))
    skip = set(body.get("skip_packs") or [])
    plan["packs"] = [p for p in plan["packs"] if p["repo"] not in skip]
    for ref, nodes in (body.get("extra_packs") or {}).items():   # packs an admin named for nodes nobody could match
        try:
            pk = await W.manual_pack(ref, [n for n in (nodes or []) if isinstance(n, str)][:50], COMFY)
        except W.WorkflowError as e:
            raise web.HTTPBadRequest(text=str(e))
        if pk["repo"] not in {p["repo"] for p in plan["packs"]}:
            plan["packs"].append(pk)
    return plan


async def wf_install(request):
    """Admins: download the missing models and install the missing node packs for one workflow."""
    require_admin(request)
    r = my_recipe(request)
    body = await request.json() if request.can_read_body else {}
    plan = await install_plan(r, body)
    task = await W.install(plan, COMFY, r["name"])
    plans.pop(r["id"], None)
    return web.json_response(W.public_task(task))


async def wf_install_many(request):
    """Admins: everything several workflows need, as one task. {"ids": [...], "per": {id: {links, extra_packs}}, "dry_run": bool}
    A dry run returns what would be installed (for the confirmation), plus what still can't be found."""
    require_admin(request)
    body = await request.json()
    me = request["user"]["name"]
    picked = [recipes[i] for i in dict.fromkeys(body.get("ids") or []) if i in recipes and recipes[i]["owner"] == me][:100]
    if not picked:
        raise web.HTTPBadRequest(text="Pick at least one workflow")
    per = body.get("per") or {}
    gate = asyncio.Semaphore(4)

    async def one(r):
        async with gate:
            try:
                return r, await install_plan(r, per.get(r["id"]) or {}, force=not body.get("dry_run")), None
            except web.HTTPException as e:
                return r, None, e.text
    got = await asyncio.gather(*(one(r) for r in picked))
    sources = {m["name"]: m["source"] for _, p, _ in got if p for m in p["models"] if not m["installed"] and m.get("source")}
    for _, p, _ in got:                                    # same file named in several workflows: one source serves all
        for m in (p or {}).get("models", []):
            if not m["installed"] and not m.get("source") and m["name"] in sources:
                m["source"] = sources[m["name"]]
    merged = W.merge_plans([(r["name"], p) for r, p, _ in got if p])
    if body.get("models_only"):                            # the assistant: node packs wait for a person in the Studio
        merged["held_packs"], merged["packs"] = merged["packs"], []
    covered = {m["name"] for m in merged["models"]}            # a link pasted on one workflow also serves the others
    unresolved = [{"id": r["id"], "name": r["name"], "error": err,
                   "models": [m["name"] for m in (p or {}).get("models", []) if not m["installed"] and not m.get("source") and m["name"] not in covered],
                   "nodes": (p or {}).get("unknown_nodes") or []} for r, p, err in got]
    unresolved = [u for u in unresolved if u["error"] or u["models"] or u["nodes"]]
    summary = {"workflows": len(picked), "models": len(merged["models"]), "download_bytes": merged["download_bytes"],
               "free_bytes": shutil.disk_usage(W.MODELS).free, "unresolved": unresolved,
               "packs": [{"title": k["title"], "repo": k["repo"], "listed": k.get("listed", True), "for": k["for"]} for k in merged["packs"]]}
    if body.get("dry_run"):
        return web.json_response(summary)
    if not merged["models"] and not merged["packs"]:
        raise web.HTTPBadRequest(text="Nothing to install — those workflows already have everything that could be found")
    task = await W.install(merged, COMFY, f"{len(picked)} workflows" if len(picked) > 1 else picked[0]["name"])
    for r in picked:
        plans.pop(r["id"], None)
    return web.json_response(W.public_task(task) | {"summary": summary})


async def install_list(request):
    """Admins: install tasks that are queued or running, newest first (so the page can pick them back up)."""
    require_admin(request)
    live = [W.public_task(t) for t in W.tasks.values() if t["status"] in ("queued", "running")]
    return web.json_response({"tasks": sorted(live, key=lambda t: -t["started"])})


async def install_status(request):
    require_admin(request)
    t = W.tasks.get(request.match_info["id"])
    if not t:
        raise web.HTTPNotFound()
    return web.json_response(W.public_task(t))


async def wf_run(request):
    """Run an imported workflow with the person's prompt/seed/size changes and their own input pictures."""
    r = my_recipe(request)
    body = await request.json()
    me = request["user"]["name"]
    try:
        a = await W.analyze(r, COMFY)
    except W.WorkflowError as e:
        raise web.HTTPBadRequest(text=str(e))
    if not is_admin(request):
        blocked = blocked_models(a)
        if blocked:
            raise web.HTTPForbidden(text="This workflow uses models that aren't available on this account: " + ", ".join(blocked[:5]))
    if not a["ready"]:
        missing = [m["name"] for m in a["models"] if not m["installed"]] + a["missing_nodes"]
        raise web.HTTPConflict(text="Still missing: " + ", ".join(missing[:8]) + (" …" if len(missing) > 8 else "") + ". An admin can install them.")
    images = {}
    for fid, name in (body.get("images") or {}).items():
        if isinstance(name, dict):                         # one of the person's own creations: copy it into ComfyUI's inputs
            name, _ = source_for_video(name, me)
            uploads[name] = me; save_uploads()             # so "Load settings" can reuse it later
        if uploads.get(Path(str(name)).name) != me:
            raise web.HTTPBadRequest(text="That input picture isn't yours")
        images[fid] = Path(str(name)).name
    for f in a["fields"]:
        if f["type"] in ("image", "video") and f["id"] not in images:
            raise web.HTTPBadRequest(text=f"Choose a picture for “{f['label']}” first")
    loras = check_loras(body.get("loras"), is_admin(request))
    values = dict(body.get("values") or {})
    if not is_admin(request):                             # prompt boxes left empty stay empty (not the source picture's text)
        for f in a["fields"]:
            if f["type"] == "text" and f["id"] not in values:
                values[f["id"]] = {"value": ""}
    if loras:                                             # fail before freeing the GPU if they can't be added
        try:
            W.inject_loras(W.build_prompt(a, values, images), loras)
        except W.WorkflowError as e:
            raise web.HTTPBadRequest(text=str(e))
    await prepare_gpu()
    count = max(1, min(int(body.get("count") or 1), 8))
    kinds = {f["id"]: f["type"] for f in a["fields"]}     # what they typed is remembered for next time
    keep = {k: {"value": v.get("value"), **({"random": bool(v["random"])} if "random" in v else {})}
            for k, v in values.items() if kinds.get(k) in ("text", "int", "float", "seed") and isinstance(v, dict)}
    if keep:
        r["saved_values"] = {**(r.get("saved_values") or {}), **keep}
        save_recipe(r)
    val = lambda key: next((((values.get(f["id"]) or {}).get("value", f["value"])) for f in a["fields"] if f["key"] == key), None)
    created = []
    for _ in range(count):
        wf = W.inject_loras(W.build_prompt(a, values, images), loras)
        async with http.post(COMFY + "/prompt", json={"prompt": wf, "client_id": CLIENT_ID}) as resp:
            res = await resp.json()
        if resp.status != 200 or "prompt_id" not in res:
            errs = [f"{(e.get('class_type') or nid)}: {x.get('message')} {x.get('details', '')}".strip()
                    for nid, e in (res.get("node_errors") or {}).items() for x in e.get("errors", [])]
            msg = (res.get("error") or {}).get("message") if isinstance(res.get("error"), dict) else res.get("error")
            raise web.HTTPBadGateway(text="ComfyUI refused the workflow: " + ("; ".join(errs[:4]) or str(msg)))
        seed = next((n["inputs"].get(k) for n in wf.values() for k in ("seed", "noise_seed") if isinstance(n.get("inputs", {}).get(k), int)), None)
        w, h = val("width"), val("height")
        job = {"id": uuid.uuid4().hex[:12], "owner": me, "kind": a["kind"], "prompt": str(val("text") or r["name"]),
               "params": {"seed": seed, "width": int(w or 1024), "height": int(h or 1024), "aspect": "", "workflow": r["name"], "recipe": r["id"],
                          # the values actually used (seeds resolved), so "Load settings" can put them back
                          "values": {f["id"]: wf[f["node"]]["inputs"].get(f["key"]) for f in a["fields"] if f["type"] in ("text", "int", "float", "seed")},
                          "images": images, **({"loras": loras} if loras else {})},
               "prompt_id": res["prompt_id"], "status": "queued", "created": time.time(), "outputs": []}
        if a["kind"] == "video":
            job["params"]["seconds"] = ""
        jobs[job["id"]] = job; created.append(job["id"])
    save_jobs()
    await refresh(force=True)
    return web.json_response({"ok": True, "jobs": created})


# ---------- model library: browse Civitai / Hugging Face, download with pictures + information, see what's installed ----------

def wf_error(fn):
    async def wrapped(request):
        try:
            return await fn(request)
        except W.WorkflowError as e:
            raise web.HTTPBadRequest(text=str(e))
    return wrapped


@wf_error
async def models_folders(request):
    return web.json_response({"folders": M.comfy_folders()})


@wf_error
async def models_civitai_search(request):
    q = request.query
    admin = is_admin(request)
    d = await M.civitai_search(q.get("query", "")[:100], q.get("types", ""), q.get("sort", "Most Downloaded"),
                               q.get("base", ""), admin and q.get("nsfw") == "1", q.get("cursor", ""))
    if not admin:                                         # PG-13 accounts: only models Civitai rates PG / PG-13
        d["items"] = [m for m in d["items"] if m.get("level") and not (m["level"] & ~R.PG13_BITS) and not m.get("nsfw")]
    return web.json_response(d)


@wf_error
async def models_civitai_model(request):
    admin = is_admin(request)
    d = await M.civitai_model(request.match_info["id"], admin and request.query.get("nsfw") == "1")
    if not admin and (not d.get("level") or d["level"] & ~R.PG13_BITS or d.get("nsfw")):
        raise web.HTTPForbidden(text="That model isn't available on this account")
    return web.json_response(d)


@wf_error
async def models_hf_search(request):
    return web.json_response(await M.hf_search(request.query.get("query", "")[:100]))


@wf_error
async def models_hf_repo(request):
    return web.json_response(await M.hf_repo(request.match_info["repo"]))


@wf_error
async def models_download(request):
    require_admin(request)
    b = await request.json()
    if b.get("source") == "civitai":
        task = await M.start_civitai(b.get("model_id"), b.get("version_id"), b.get("file_id"), b.get("folder"))
    elif b.get("source") == "huggingface":
        task = await M.start_hf(str(b.get("repo", "")), str(b.get("path", "")), b.get("folder"))
    else:
        raise web.HTTPBadRequest(text="Unknown source")
    return web.json_response(W.public_task(task))


async def models_tasks(request):
    return web.json_response({"tasks": [W.public_task(W.tasks[t]) for t in reversed(M.recent) if t in W.tasks]})


async def models_installed(request):
    items = await asyncio.get_running_loop().run_in_executor(None, M.installed)
    for m in items:
        m["rating"] = R.rating_of(m["folder"], m["file"])
        m["content"] = m["folder"] in R.CONTENT
        if m["rating"] != "pg13" and m["content"]:
            e = R.ratings.get(f"{m['folder']}/{m['file']}") or {}
            m["rated_by"], m["level"] = e.get("by"), e.get("level")
    if not is_admin(request):
        items = [m for m in items if m["rating"] == "pg13"]
    return web.json_response({"models": items, "free": shutil.disk_usage(W.MODELS).free,
                              "rating": {"running": R.state["running"], "last": R.state["last"]}})


async def models_fetch_info(request):
    """Admins: look up information + pictures for every installed model that has none, or for one model."""
    require_admin(request)
    b = await request.json() if request.can_read_body else {}
    only = {(str(b["folder"]), str(b["file"]))} if b.get("file") else None
    return web.json_response(W.public_task(M.start_fetch_all(force=bool(b.get("force")), only=only)))


@wf_error
async def models_rate(request):
    """Admins: rate a content model PG-13 or adults-only, or hand it back to automatic rating."""
    require_admin(request)
    b = await request.json()
    folder, file = str(b.get("folder", "")), str(b.get("file", ""))
    if folder not in R.CONTENT or not (W.MODELS / folder / file).resolve().is_relative_to((W.MODELS / folder).resolve()):
        raise web.HTTPBadRequest(text="Not a content model")
    R.set_rating(folder, file, b.get("rating"))
    if b.get("rating") == "auto":
        asyncio.create_task(R.refresh())
    return web.json_response({"ok": True, "rating": R.rating_of(folder, file)})


@wf_error
async def models_remove(request):
    require_admin(request)
    M.remove(request.query.get("folder", ""), request.query.get("file", ""))
    return web.json_response({"ok": True})


async def model_media(request):
    """Saved example pictures of models (shared library: any signed-in person)."""
    rel = request.match_info["path"]
    root = W.MODELS.resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file() or not (".previews" in path.parts or ".preview." in path.name):
        raise web.HTTPNotFound()
    if not is_admin(request):                             # previews of models that aren't PG-13 stay admin-only
        if ".previews" in path.parts:                     # <dir>/.previews/<stem>/N.ext
            stem, home = path.parent.name, path.parent.parent.parent
        else:                                             # <dir>/<stem>.preview.ext
            stem, home = path.name.split(".preview.")[0], path.parent
        model = next((home / (stem + ext) for ext in (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft") if (home / (stem + ext)).is_file()), None)
        rel = model.relative_to(root).parts if model else ()
        if not model or R.rating_of(rel[0], str(Path(*rel[1:]))) != "pg13":
            raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


def main():
    auth.load(HERE)
    load_uploads()
    load_jobs()
    load_recipes()
    load_settings()
    app = web.Application(client_max_size=48 * 2**20, middlewares=[guard])
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/generate", generate)
    app.router.add_post("/api/upload", upload)
    app.router.add_post("/api/jobs/{id}/cancel", cancel)
    app.router.add_delete("/api/jobs/{id}", delete)
    app.router.add_post("/api/free-gpu", free_gpu)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_put("/api/settings", put_settings)
    app.router.add_get("/api/session", session_info)
    app.router.add_post("/api/setup", setup)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_post("/api/password", change_password)
    app.router.add_get("/api/users", list_users)
    app.router.add_post("/api/users", add_user)
    app.router.add_put("/api/users/{name}", update_user)
    app.router.add_delete("/api/users/{name}", remove_user)
    app.router.add_get("/api/workflows", wf_list)
    app.router.add_post("/api/workflows/import", wf_import)
    app.router.add_get("/api/workflows/{id}", wf_get)
    app.router.add_delete("/api/workflows/{id}", wf_delete)
    app.router.add_post("/api/workflows/{id}/install", wf_install)
    app.router.add_post("/api/workflows/{id}/run", wf_run)
    app.router.add_put("/api/workflows/{id}/values", wf_save_values)
    app.router.add_put("/api/workflows/{id}/exact", wf_exact)
    app.router.add_get("/api/loras", list_loras)
    app.router.add_get("/api/gpu-priority", get_priority)
    app.router.add_post("/api/prompt/improve", improve_prompt)
    app.router.add_get("/home", home_page)
    app.router.add_get("/b70/{name}", shared_file)
    app.router.add_get("/api/assistant/internet", assistant_internet)
    app.router.add_post("/api/assistant/internet", assistant_internet)
    app.router.add_get("/api/nowplaying", nowplaying)
    app.router.add_post("/api/nowplaying/{action}", nowplaying_action)
    app.router.add_get("/api/home", home_summary)
    app.router.add_get("/api/disk", disk_usage)
    app.router.add_post("/api/models/rating", models_rate)
    app.router.add_post("/api/models/fetch-info", models_fetch_info)
    app.router.add_post("/api/gpu-priority", set_priority)
    app.router.add_post("/api/workflows/install-many", wf_install_many)
    app.router.add_get("/api/installs", install_list)
    app.router.add_get("/api/installs/{id}", install_status)
    app.router.add_get("/api/models/folders", models_folders)
    app.router.add_get("/api/models/civitai", models_civitai_search)
    app.router.add_get("/api/models/civitai/{id:\\d+}", models_civitai_model)
    app.router.add_get("/api/models/hf", models_hf_search)
    app.router.add_get("/api/models/hf/{repo:.+}", models_hf_repo)
    app.router.add_post("/api/models/download", models_download)
    app.router.add_get("/api/models/tasks", models_tasks)
    app.router.add_get("/api/models/installed", models_installed)
    app.router.add_delete("/api/models/installed", models_remove)
    app.router.add_post("/api/assistant/chat", assistant_chat)
    app.router.add_get("/model-media/{path:.+}", model_media)
    app.router.add_get("/media/{path:.+}", media)
    app.router.add_get("/input/{name}", input_file)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    print(f"Image & Video Studio on http://{HOST}:{PORT}/ (ComfyUI at {COMFY})", flush=True)
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
