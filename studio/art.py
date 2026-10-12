"""Cover art for stations and songs, drawn by ComfyUI (Z-Image Turbo, the Image & Video Studio's picture model).

Art is made in the radio worker's quiet moments: never while YuE2 is recording a song, and no new song starts while
a picture is being drawn. ComfyUI is asked to let go of its models afterwards so YuE2 gets the GPU back.
Station art first (a new or changed station gets it between any two songs); song art when the song queue is full.

Station art: <station dir>/art.jpg     Song art: <song dir>/cover.jpg
"""
import json, random, re, subprocess, tempfile, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

import app

COMFY = "http://127.0.0.1:8188"
MODE = "stations and songs"            # settings: "stations and songs" | "stations only" | "off"
SONG_BACKLOG = 40                      # older songs than the newest this many don't get art automatically
lock = threading.Lock()
inflight = None                        # {"kind", "id", "prompt_id", "started"}
failed = {}                            # "kind:id" -> when (tried again after an hour)
forced = []                            # [("station"|"song", id)] asked for from the page: first in line
comfy_down_until = 0                   # ComfyUI not answering (e.g. restarting): try again after this
radio = None                           # the radio module (set by init)


def init(radio_module):
    global radio
    radio = radio_module


def station_path(sid):
    return radio.sdir(sid) / "art.jpg"


def song_path(jid):
    return app.DATA / jid / "cover.jpg"


def _clean(text, n):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]


def station_prompt(s):
    theme = f" Its songs are about: {_clean(s.get('theme'), 300)}." if s.get("theme") else ""
    return (f"Square cover art for a music radio station. The music: {_clean(s.get('description') or s.get('name'), 700)}.{theme} "
            "An evocative illustrated scene that captures the genre and mood, rich colour, cinematic lighting, strong album-cover "
            "composition, tasteful and safe for work. Absolutely no text, letters, words, logos or watermarks anywhere in the image.")


def song_prompt(j):
    lines = [l.strip() for l in (j.get("lyrics") or "").splitlines() if l.strip() and not l.strip().startswith("[")][:8]
    words = f" Imagery inspired by these lyrics: {_clean(' / '.join(lines), 500)}." if lines else ""
    return (f"Square album cover art for a song. Music style: {_clean(j.get('style'), 400)}. The song's mood and story: "
            f"{_clean(j.get('title'), 80)}.{words} A striking, original illustrated or photographic scene that fits the music, "
            "rich colour, dramatic lighting, album-cover composition, tasteful and safe for work. "
            "Absolutely no text, letters, words, titles, logos or watermarks anywhere in the image.")


def _graph(prompt, seed, prefix):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 768, "height": 768, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0],
              "seed": seed, "steps": 8, "cfg": 1, "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
    }


def _req(path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.urlopen(urllib.request.Request(COMFY + path, data, {"Content-Type": "application/json"} if data else {}), timeout=timeout)
    raw = r.read()
    return json.loads(raw) if raw[:1] in (b"{", b"[") else raw


def _wanted(songs_ok):
    """The next picture to make: (kind, id, prompt) or None."""
    now = time.time()
    skip = lambda k: now - failed.get(k, 0) < 3600
    with lock:
        while forced:
            kind, oid = forced[0]
            if kind == "station" and oid in radio.stations:
                return kind, oid, station_prompt(radio.stations[oid])
            with app.lock:
                j = app.jobs.get(oid)
            if kind == "song" and j:
                return kind, oid, song_prompt(j)
            forced.pop(0)
    if MODE == "off":
        return None
    for sid, s in list(radio.stations.items()):
        if (s.get("description") or s.get("name")) and not station_path(sid).exists() and not skip("station:" + sid):
            return "station", sid, station_prompt(s)
    if MODE != "stations and songs" or not songs_ok:
        return None
    with app.lock:
        songs = [j for j in app.jobs.values() if j.get("source") == "radio" and j["status"] == "done"
                 and j.get("radio_status") in ("approved", "played")]
    songs.sort(key=lambda j: (j.get("radio_status") != "approved", -(j.get("approved_at") or j.get("finished") or 0)))
    for j in songs[:SONG_BACKLOG]:
        if not song_path(j["id"]).exists() and not skip("song:" + j["id"]):
            return "song", j["id"], song_prompt(j)
    return None


def _finish(item, entry):
    """Copy ComfyUI's picture next to the station or song as a small JPEG, then remove ComfyUI's copy."""
    imgs = [i for out in entry.get("outputs", {}).values() for i in out.get("images", []) if i.get("type") == "output"]
    if not imgs:
        raise RuntimeError("ComfyUI made no picture")
    i = imgs[0]
    data = _req("/view?" + urllib.parse.urlencode({"filename": i["filename"], "subfolder": i.get("subfolder", ""), "type": "output"}), timeout=30)
    dest = station_path(item["id"]) if item["kind"] == "station" else song_path(item["id"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.png"; src.write_bytes(data)
        tmp = Path(td) / "out.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-q:v", "3", str(tmp)], check=True, timeout=60)
        part = dest.with_name(dest.stem + ".part.jpg"); part.write_bytes(tmp.read_bytes()); part.replace(dest)
    out = Path.home() / "ComfyUI" / "output" / i.get("subfolder", "") / i["filename"]
    out.unlink(missing_ok=True)


def _free():
    try:
        _req("/free", {"unload_models": True, "free_memory": True})
    except Exception:
        pass


def step(busy, pend):
    """Called by the radio worker each turn. True while a picture is being drawn (the worker starts no song then)."""
    global inflight
    if inflight:
        try:
            h = _req(f"/history/{inflight['prompt_id']}")
        except Exception:
            h = {}
        entry = (h or {}).get(inflight["prompt_id"])
        status = ((entry or {}).get("status") or {}).get("status_str")
        if entry and status != "error":
            item = inflight
            try:
                _finish(item, entry)
                app.log(f"art: made {item['kind']} art for {item['id']}")
            except Exception as e:
                failed[f"{item['kind']}:{item['id']}"] = time.time(); app.log(f"art: {e}")
            with lock:
                if forced and forced[0] == (item["kind"], item["id"]):
                    forced.pop(0)
            inflight = None
            if pend or not _wanted(songs_ok=not pend):
                _free()                                    # give the GPU back before the next song
        elif status == "error" or time.time() - inflight["started"] > 300:
            failed[f"{inflight['kind']}:{inflight['id']}"] = time.time()
            app.log(f"art: gave up on {inflight['kind']} {inflight['id']}")
            with lock:
                if forced and forced[0] == (inflight["kind"], inflight["id"]):
                    forced.pop(0)
            inflight = None
            _free()
        return inflight is not None
    global comfy_down_until
    if busy or app.gpu_yield_reason() or time.time() < comfy_down_until:
        return False
    item = _wanted(songs_ok=not pend)
    if not item:
        return False
    kind, oid, prompt = item
    try:
        r = _req("/prompt", {"prompt": _graph(prompt, random.randrange(2**48), f"radio_art/{kind}_{oid}"), "client_id": "radio-art"})
        inflight = {"kind": kind, "id": oid, "prompt_id": r["prompt_id"], "started": time.time()}
        return True
    except urllib.error.URLError as e:
        if not isinstance(e, urllib.error.HTTPError):    # ComfyUI isn't up (restarting): not this picture's fault
            comfy_down_until = time.time() + 30
            return False
        failed[f"{kind}:{oid}"] = time.time()
        app.log(f"art: ComfyUI didn't take the job ({e})")
        return False
    except Exception as e:
        failed[f"{kind}:{oid}"] = time.time()
        app.log(f"art: ComfyUI didn't take the job ({e})")
        return False


def request(kind, oid):
    """🎨 New art from the page: drop the current picture and make a new one first."""
    if kind == "station":
        if oid not in radio.stations:
            raise app.BadRequest("No such station")
        station_path(oid).unlink(missing_ok=True)
    else:
        with app.lock:
            if oid not in app.jobs:
                raise app.BadRequest("No such song")
        song_path(oid).unlink(missing_ok=True)
    failed.pop(f"{kind}:{oid}", None)
    with lock:
        if (kind, oid) not in forced:
            forced.append((kind, oid))
    return {"queued": True}


def stamp(path):
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return None
