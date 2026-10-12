"""Import ComfyUI workflows from pictures, videos or Civitai links; find what they need; install it; run them.

Reading:   PNG text chunks (prompt / workflow), WebP/JPEG EXIF ("Prompt:" / "Workflow:"), MP4/WebM/MOV tags
           (prompt / workflow / comment), Automatic1111 "parameters" (shown, not runnable), Civitai image pages.
Checking:  node types against ComfyUI's /object_info; model files against each loader's list of files.
Sources:   links embedded in the workflow, ComfyUI-Manager's model list, Civitai resources of the picture,
           Hugging Face search, Civitai search. Node packs only from ComfyUI-Manager's registry.
Installing (admins): downloads with progress, git clone + requirements for node packs, ComfyUI restart.
"""
import asyncio, copy, io, ipaddress, json, os, re, shutil, socket, subprocess, sys, tempfile, time, uuid
from pathlib import Path
from urllib.parse import urlparse, quote

import aiohttp
from PIL import Image
from yarl import URL

COMFY_DIR = Path(os.environ.get("COMFY_DIR", "~/ComfyUI")).expanduser()
MODELS, CUSTOM_NODES = COMFY_DIR / "models", COMFY_DIR / "custom_nodes"
COMFY_PY = COMFY_DIR / ".venv" / "bin" / "python"
UV = shutil.which("uv") or str(Path("~/.local/bin/uv").expanduser())
MANAGER = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/"
MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx", ".pkl")
VIDEO_SAVERS = {"SaveVideo", "VHS_VideoCombine", "SaveAnimatedWEBP", "SaveAnimatedPNG", "SaveWEBM", "SaveGIF"}
IMAGE_SAVERS = {"SaveImage", "PreviewImage", "Image Save", "SaveImageExtended"}
FREE_MARGIN = 10 * 2**30                     # keep this much disk free after any download
# where a loader's file goes, by node class (any input) or (class, input)
LOADER_FOLDERS = {
    "CheckpointLoaderSimple": "checkpoints", "CheckpointLoader": "checkpoints", "ImageOnlyCheckpointLoader": "checkpoints",
    "UNETLoader": "diffusion_models", "UnetLoaderGGUF": "unet", "UnetLoaderGGUFAdvanced": "unet",
    "CLIPLoader": "text_encoders", "DualCLIPLoader": "text_encoders", "TripleCLIPLoader": "text_encoders",
    "QuadrupleCLIPLoader": "text_encoders", "CLIPLoaderGGUF": "text_encoders", "DualCLIPLoaderGGUF": "text_encoders",
    "VAELoader": "vae", "LoraLoader": "loras", "LoraLoaderModelOnly": "loras", "ControlNetLoader": "controlnet",
    "DiffControlNetLoader": "controlnet", "CLIPVisionLoader": "clip_vision", "UpscaleModelLoader": "upscale_models",
    "StyleModelLoader": "style_models", "GLIGENLoader": "gligen", "HypernetworkLoader": "hypernetworks",
    "PhotoMakerLoader": "photomaker", "ModelPatchLoader": "model_patches", "AudioEncoderLoader": "audio_encoders",
}
KEYWORD_FOLDERS = [("lora", "loras"), ("vae", "vae"), ("clip_vision", "clip_vision"), ("clip", "text_encoders"),
                   ("text_encoder", "text_encoders"), ("t5", "text_encoders"), ("unet", "diffusion_models"),
                   ("diffusion", "diffusion_models"), ("controlnet", "controlnet"), ("control_net", "controlnet"),
                   ("upscale", "upscale_models"), ("ckpt", "checkpoints"), ("checkpoint", "checkpoints")]

http: aiohttp.ClientSession = None
settings = {}                                # hf_token / civitai_token (set by the server)
_registry, _object_info = {}, {"t": 0, "data": {}}
tasks = {}                                   # install task id -> progress
log = print


class WorkflowError(Exception):
    pass


# ---------------------------------------------------------------- reading metadata

def _json(v):
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def read_metadata(data, filename=""):
    """-> {"prompt": api-json|None, "workflow": ui-json|None, "a1111": text|None}"""
    out = {"prompt": None, "workflow": None, "a1111": None}
    try:
        im = Image.open(io.BytesIO(data))
        info = dict(im.info)
        out["prompt"], out["workflow"] = _json(info.get("prompt")), _json(info.get("workflow"))
        if isinstance(info.get("parameters"), str):
            out["a1111"] = info["parameters"]
        try:
            exif = im.getexif()
            for tag in list(exif) + list(exif.get_ifd(0x8769)):
                v = exif.get(tag) or exif.get_ifd(0x8769).get(tag)
                if isinstance(v, bytes):
                    v = v.decode("utf-8", "ignore").lstrip("UNICODE\0ASCII\0").replace("\0", "")
                if not isinstance(v, str):
                    continue
                low = v[:12].lower()
                if low.startswith("prompt:"):
                    out["prompt"] = out["prompt"] or _json(v.split(":", 1)[1])
                elif low.startswith("workflow:"):
                    out["workflow"] = out["workflow"] or _json(v.split(":", 1)[1])
                elif "Steps:" in v and "Sampler:" in v:
                    out["a1111"] = out["a1111"] or v
        except Exception:
            pass
        return out
    except Exception:
        pass
    # not a picture: try a video container's tags
    with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix or ".mp4") as f:
        f.write(data); f.flush()
        try:
            tags = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", f.name],
                                             capture_output=True, text=True, timeout=30).stdout or "{}").get("format", {}).get("tags", {})
        except Exception:
            tags = {}
    tags = {k.lower(): v for k, v in tags.items()}
    out["prompt"], out["workflow"] = _json(tags.get("prompt")), _json(tags.get("workflow"))
    c = _json(tags.get("comment"))
    if isinstance(c, dict):
        out["prompt"] = out["prompt"] or _json(c.get("prompt"))
        out["workflow"] = out["workflow"] or _json(c.get("workflow"))
    return out


def _public_host(host):
    """Refuse links to this machine or the home network (the server must not be used to reach them)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise WorkflowError("That address can't be found")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise WorkflowError("Only public internet links can be imported")


async def fetch_url(url, limit=80 * 2**20):
    """Download a picture/video from a public link (redirects re-checked), or a Civitai image page via its API."""
    m = re.match(r"^https?://(?:www\.)?civitai\.(?:com|green|red)/images/(\d+)", url)
    if m:
        return await civitai_image(m.group(1))
    for _ in range(5):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise WorkflowError("Use an http(s) link")
        await asyncio.get_running_loop().run_in_executor(None, _public_host, u.hostname)
        async with http.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=120)) as r:
            if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = str(r.url.join(URL(r.headers["Location"])))
                continue
            if r.status != 200:
                raise WorkflowError(f"The link answered {r.status}")
            data = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                data += chunk
                if len(data) > limit:
                    raise WorkflowError("That file is too big to import")
            return {"data": bytes(data), "name": Path(u.path).name or "download", "source": url}
    raise WorkflowError("Too many redirects")


async def civitai_image(image_id):
    async with http.get(f"https://civitai.com/api/v1/images?imageId={image_id}&withMeta=true", timeout=aiohttp.ClientTimeout(total=30)) as r:
        items = (await r.json()).get("items", [])
    if not items:
        raise WorkflowError("Civitai doesn't share that image (it may be private or removed)")
    it = items[0]; meta = it.get("meta") or {}
    comfy = _json(meta.get("comfy")) or {}
    picture = await fetch_url(it["url"]) if it.get("url") else {"data": b""}
    found = read_metadata(picture["data"], "civitai.png") if picture["data"] else {}
    return {"data": picture["data"], "name": f"civitai-{image_id}.png", "source": f"https://civitai.com/images/{image_id}",
            "meta": {"prompt": _json(comfy.get("prompt")) or found.get("prompt"), "workflow": _json(comfy.get("workflow")) or found.get("workflow"),
                     "a1111": None if comfy else _a1111_from_civitai(meta), "civitai": meta.get("civitaiResources") or []}}


def _a1111_from_civitai(meta):
    if not meta.get("prompt"):
        return None
    return f"{meta.get('prompt')}\nNegative prompt: {meta.get('negativePrompt', '')}\nSteps: {meta.get('steps')}, Sampler: {meta.get('sampler')}, " \
           f"CFG scale: {meta.get('cfgScale')}, Seed: {meta.get('seed')}, Model: {meta.get('Model', '')}"


# ---------------------------------------------------------------- ComfyUI knowledge

async def object_info(comfy, force=False):
    if force or time.time() - _object_info["t"] > 60:
        async with http.get(comfy + "/object_info", timeout=aiohttp.ClientTimeout(total=60)) as r:
            _object_info.update(t=time.time(), data=await r.json())
    return _object_info["data"]


async def registry(name):
    """ComfyUI-Manager's lists (node map, custom node list, model list), cached a day."""
    cache = Path(tempfile.gettempdir()) / f"genstudio-{name}.json"
    if name in _registry:
        return _registry[name]
    if not cache.exists() or time.time() - cache.stat().st_mtime > 86400:
        async with http.get(MANAGER + name + ".json", timeout=aiohttp.ClientTimeout(total=60)) as r:
            cache.write_bytes(await r.read())
    _registry[name] = json.loads(cache.read_text())
    return _registry[name]


# ---------------------------------------------------------------- UI workflow -> API prompt

def ui_to_api(wf, info):
    """Best-effort conversion of an editor workflow (what ComfyUI saves for the canvas) into a runnable prompt.
    Needs the node definitions, so nodes that aren't installed yet are kept with what can be inferred."""
    nodes = {str(n["id"]): n for n in wf.get("nodes", [])}
    links = {l[0]: l for l in wf.get("links", []) if isinstance(l, list)}
    links.update({l["id"]: [l["id"], l["origin_id"], l["origin_slot"], l["target_id"], l["target_slot"], l.get("type")]
                  for l in wf.get("links", []) if isinstance(l, dict)})
    if any(re.match(r"^[0-9a-f-]{36}$", str(n.get("type", ""))) for n in nodes.values()):
        raise WorkflowError("This workflow uses subgraphs, which can't be converted here. Export it with API format from ComfyUI.")
    SKIP = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute"}

    def source(link_id, depth=0):
        """Follow a link back through reroutes / bypassed nodes; returns [node_id, slot] or ("value", v)."""
        l = links.get(link_id)
        if not l or depth > 20:
            return None
        src = nodes.get(str(l[1]))
        if not src:
            return None
        if src["type"] == "Reroute":
            inp = (src.get("inputs") or [{}])[0]
            return source(inp.get("link"), depth + 1)
        if src["type"] == "PrimitiveNode":
            return ("value", (src.get("widgets_values") or [None])[0])
        if src.get("mode") == 4:                      # bypassed: pass the matching input straight through
            want = l[5]
            for inp in src.get("inputs") or []:
                if inp.get("link") is not None and (inp.get("type") == want or want in (None, "*")):
                    return source(inp["link"], depth + 1)
            return None
        return [str(src["id"]), l[2]]

    prompt = {}
    for nid, n in nodes.items():
        if n["type"] in SKIP or n.get("mode") in (2, 4):
            continue
        spec = info.get(n["type"])
        inputs, linked = {}, {}
        for inp in n.get("inputs") or []:
            if inp.get("link") is not None:
                s = source(inp["link"])
                if s is not None:
                    linked[inp["name"]] = s[1] if isinstance(s, tuple) else s
        wv = n.get("widgets_values")
        if isinstance(wv, dict):
            inputs.update({k: v for k, v in wv.items() if k not in linked})
        elif spec and isinstance(wv, list):
            order = spec.get("input_order") or {"required": list(spec["input"].get("required", {})), "optional": list(spec["input"].get("optional", {}))}
            defs = {**spec["input"].get("required", {}), **spec["input"].get("optional", {})}
            vals, i = list(wv), 0
            for name in order.get("required", []) + order.get("optional", []):
                d = defs.get(name)
                if d is None:
                    continue
                typ = d[0]
                force_input = len(d) > 1 and isinstance(d[1], dict) and (d[1].get("forceInput") or d[1].get("rawLink"))
                widget = (isinstance(typ, list) or typ in ("INT", "FLOAT", "STRING", "BOOLEAN", "COMBO")) and not force_input
                if not widget:
                    continue
                if i >= len(vals):
                    break
                v = vals[i]; i += 1
                if name not in linked:
                    inputs[name] = v
                if typ == "INT" and i < len(vals) and vals[i] in ("fixed", "increment", "decrement", "randomize"):
                    i += 1                                # the "control after generate" widget
        elif wv:
            inputs["_widgets"] = wv                       # node not installed yet: keep raw values for analysis
        inputs.update(linked)
        prompt[nid] = {"class_type": n["type"], "inputs": inputs, "_meta": {"title": n.get("title") or n["type"]}}
    return prompt


# ---------------------------------------------------------------- analysis

def _walk_strings(value, path=()):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, path + (k,))
    elif isinstance(value, list) and not (len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int)):
        for i, v in enumerate(value):
            yield from _walk_strings(v, path + (i,))


def _guess_folder(cls, key):
    if cls in LOADER_FOLDERS:
        return LOADER_FOLDERS[cls]
    s = f"{cls} {key}".lower()
    return next((f for kw, f in KEYWORD_FOLDERS if kw in s), "checkpoints")


def _options(info, cls, key):
    d = info.get(cls, {}).get("input", {})
    spec = {**d.get("required", {}), **d.get("optional", {})}.get(key)
    if spec and isinstance(spec[0], list):
        return spec[0]
    if spec and spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options", [])
    return None


def _norm(p):
    return str(p).replace("\\", "/")


def hints_from_workflow(wf):
    """Download links that ComfyUI templates embed in nodes (properties.models) or at the top level."""
    out = {}
    for m in (wf or {}).get("models", []) + [m for n in (wf or {}).get("nodes", []) for m in (n.get("properties") or {}).get("models", [])]:
        if isinstance(m, dict) and m.get("name") and m.get("url"):
            out[Path(_norm(m["name"])).name] = {"url": m["url"], "folder": m.get("directory")}
    return out


def find_installed(name, folder=None):
    """Is a model file already on disk (any model folder)? Returns its path relative to its folder."""
    base = Path(_norm(name)).name
    roots = [MODELS / folder] if folder and (MODELS / folder).exists() else []
    roots += [p for p in MODELS.iterdir() if p.is_dir() and p not in roots]
    for root in roots:
        for f in root.rglob(base):
            if f.is_file() and not f.name.endswith(".part"):
                return root.name, str(f.relative_to(root))
    return None


_PRECISION = re.compile(r"^(?:(?:fp|bf)\d+|(?:nv|mx)fp\d+|int\d+|e\dm\d(?:fn)?|q\d+|iq\d+|scaled|awq|gptq|convrot|mixed|quant(?:ized)?|pruned|safetensors|gguf)$")


def model_core(name):
    """A model's identity without precision / packaging tags: 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors' and
    'qwen3vl_32b_minimax_h3_int8_convrot.safetensors' both -> ('32b','h3','minimax','qwen3vl')."""
    toks = [t for t in re.split(r"[_\-. ]+", Path(name).stem.lower()) if t]
    quantized = any(re.fullmatch(r"i?q\d+", t) for t in toks)
    core = [t for t in toks if not _PRECISION.match(t) and not (quantized and t in ("k", "m", "s", "l", "xs", "xl", "0", "1"))]
    return tuple(sorted(core))


def similar_option(name, opts):
    """An installed file the same loader can open that is the same model in another precision, or None."""
    core = model_core(name)
    if len(core) < 2 and len("".join(core)) < 6:            # too little left to be sure it's the same model
        return None
    hits = [o for o in opts if model_core(o) == core and Path(_norm(o)).name != Path(_norm(name)).name]
    # prefer formats this RTX 4090 runs natively: NVFP4 / MXFP formats are made for RTX 50-series cards
    blackwell = lambda o: any(re.fullmatch(r"(?:nv|mx)fp\d+", t) for t in re.split(r"[_\-. ]+", Path(o).stem.lower()))
    return sorted(hits, key=lambda o: (blackwell(o), len(o), o))[0] if hits else None


async def analyze(recipe, comfy):
    """What this workflow needs and what's missing; also the fields a person can change."""
    info = await object_info(comfy)
    prompt = recipe.get("prompt")
    notes = []
    if not prompt and recipe.get("workflow"):
        prompt = ui_to_api(recipe["workflow"], info)
        notes.append("Converted from the editor workflow (no API copy was embedded).")
    if not prompt:
        raise WorkflowError("No ComfyUI workflow in this file")
    classes = sorted({n.get("class_type") for n in prompt.values() if isinstance(n, dict) and n.get("class_type")})
    missing_nodes = [c for c in classes if c not in info]
    hints = hints_from_workflow(recipe.get("workflow"))
    models, seen = [], set()
    for nid, node in prompt.items():
        cls = node.get("class_type")
        for path, value in _walk_strings(node.get("inputs", {})):
            if not value.lower().endswith(MODEL_EXT) or len(value) > 300:
                continue
            parent = node.get("inputs", {})
            for k in path[:-1]:
                parent = parent[k]
            if isinstance(parent, dict) and (parent.get("on") is False or parent.get("enabled") is False):
                continue                                  # a LoRA slot that's switched off: the workflow doesn't load it
            key = path[0] if path else ""
            opts = _options(info, cls, key) if cls in info else None
            norm = _norm(value)
            installed = None
            if opts is not None:
                if value in opts or norm in [_norm(o) for o in opts]:
                    installed = next(o for o in opts if _norm(o) == norm)
                else:
                    installed = next((o for o in opts if Path(_norm(o)).name == Path(norm).name), None)
            folder = (hints.get(Path(norm).name) or {}).get("folder") or _guess_folder(cls, key)
            on_disk = None if installed else find_installed(norm, folder)
            similar = similar_option(norm, opts) if opts and not installed and not on_disk else None
            substitute = similar if similar and Path(norm).name not in (recipe.get("exact_models") or []) else None
            if substitute:                                # same model in another precision is already here: use it
                installed = substitute
            if (Path(norm).name, folder) in seen:
                continue
            seen.add((Path(norm).name, folder))
            # on disk but not where this loader looks -> it gets linked into the right folder when installing
            elsewhere = on_disk if on_disk and opts is not None else None
            models.append({"name": Path(norm).name, "value": value, "node": nid, "path": list(path), "class": cls, "folder": folder,
                           "installed": bool(installed) if opts is not None else bool(on_disk), "use": installed,
                           "elsewhere": elsewhere, "lora": folder == "loras", "substitute": substitute, "similar": similar})
    for nid, node in prompt.items():                            # textual inversions: "embedding:name"
        for _, text in _walk_strings(node.get("inputs", {})):
            for e in re.findall(r"embedding:([\w\-. ]+)", text):
                name = e.strip()
                if ("emb", name) in seen:
                    continue
                seen.add(("emb", name))
                have = any((MODELS / "embeddings").glob(name + "*")) or any((MODELS / "embeddings").glob(Path(name).stem + ".*"))
                models.append({"name": name if Path(name).suffix else name + ".safetensors", "value": name, "node": nid, "path": [],
                               "class": "embedding", "folder": "embeddings", "installed": have, "use": None, "elsewhere": None, "lora": False})
    kind = "video" if any(c in VIDEO_SAVERS for c in classes) else "image"
    return {"prompt": prompt, "classes": classes, "missing_nodes": missing_nodes, "models": models, "kind": kind,
            "fields": editable_fields(prompt, info), "notes": notes,
            "ready": not missing_nodes and all(m["installed"] for m in models)}


def _src(node, key):
    v = node.get("inputs", {}).get(key)
    return v[0] if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) else None


def editable_fields(prompt, info):
    """Prompts, seeds, steps, CFG, sizes, LoRA strengths and input pictures, labelled for people."""
    fields, roles = [], {}
    for nid, n in prompt.items():                       # which text nodes feed positive / negative
        for role in ("positive", "negative"):
            start = _src(n, role)
            stack, hops = [start] if start else [], 0
            while stack and hops < 30:
                cur = stack.pop(); hops += 1
                node = prompt.get(cur)
                if not node:
                    continue
                if any(isinstance(v, str) and len(v) > 0 for k, v in node.get("inputs", {}).items() if k in ("text", "text_g", "text_l", "prompt")):
                    roles.setdefault(cur, role)
                for k, v in node.get("inputs", {}).items():
                    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and k not in ("clip", "model", "vae"):
                        stack.append(v[0])
    for nid, n in sorted(prompt.items(), key=lambda kv: (roles.get(kv[0]) != "positive", roles.get(kv[0]) != "negative", kv[0])):
        cls, title = n.get("class_type", ""), (n.get("_meta") or {}).get("title") or n.get("class_type", "")
        ins = n.get("inputs", {})
        for key in ("text", "text_g", "prompt", "string", "value"):
            if isinstance(ins.get(key), str) and (key != "value" or len(ins[key]) > 20) and (nid in roles or "Encode" in cls or "Prompt" in cls or "String" in cls):
                role = roles.get(nid)
                fields.append({"id": f"{nid}.{key}", "node": nid, "key": key, "type": "text", "value": ins[key],
                               "label": "Prompt" if role == "positive" else "Negative prompt" if role == "negative" else title})
                break
        for key in ("seed", "noise_seed"):
            if isinstance(ins.get(key), int):
                fields.append({"id": f"{nid}.{key}", "node": nid, "key": key, "type": "seed", "value": ins[key], "label": f"Seed · {title}", "random": True})
        for key, label, typ in (("steps", "Steps", "int"), ("cfg", "CFG", "float"), ("denoise", "Denoise", "float"),
                                ("width", "Width", "int"), ("height", "Height", "int"), ("length", "Length (frames)", "int"),
                                ("batch_size", "Pictures per run", "int"), ("frame_rate", "Frame rate", "float"), ("fps", "FPS", "float"),
                                ("strength_model", "LoRA strength", "float"), ("strength", "Strength", "float")):
            v = ins.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                extra = f" · {Path(str(ins.get('lora_name', ''))).stem}" if "lora_name" in ins else ""
                fields.append({"id": f"{nid}.{key}", "node": nid, "key": key, "type": typ, "value": v, "label": f"{label}{extra}",
                               "group": "size" if key in ("width", "height", "length", "batch_size") else "sampling"})
        if cls in ("LoadImage", "LoadImageMask") or (cls.startswith("LoadImage") and isinstance(ins.get("image"), str)):
            fields.append({"id": f"{nid}.image", "node": nid, "key": "image", "type": "image", "value": ins.get("image"), "label": f"Input picture · {title}"})
        if cls in ("VHS_LoadVideo", "LoadVideo") and isinstance(ins.get("video") or ins.get("file"), str):
            k = "video" if "video" in ins else "file"
            fields.append({"id": f"{nid}.{k}", "node": nid, "key": k, "type": "video", "value": ins.get(k), "label": f"Input video · {title}"})
    return fields


# ---------------------------------------------------------------- where missing things come from

async def _json_get(url, headers=None, timeout=30):
    async with http.get(url, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        if r.status != 200:
            return None
        return await r.json(content_type=None)


def _hf_headers():
    return {"Authorization": f"Bearer {settings['hf_token']}"} if settings.get("hf_token") else {}


async def _size(url):
    try:
        h = _hf_headers() if "huggingface.co" in url else {}
        async with http.head(_with_civitai_token(url), headers=h, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=20)) as r:
            n = int(r.headers.get("Content-Length") or r.headers.get("x-linked-size") or 0)
            return n or None, r.status
    except Exception:
        return None, None


def _is_civitai(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ("civitai.com", "civitai.green", "civitai.red"))


def _with_civitai_token(url):
    if settings.get("civitai_token") and _is_civitai(url):
        return url + ("&" if "?" in url else "?") + "token=" + quote(settings["civitai_token"])
    return url


def _queries(name):
    """Search terms from a filename, longest first: 'krea2_turbo_mxfp8' -> 'krea2 turbo mxfp8', 'krea2 turbo', 'krea2'."""
    parts = [x for x in re.split(r"[_\-. ]+", Path(name).stem) if x]
    out = [" ".join(parts), " ".join(parts[:2]), parts[0] if parts else ""]
    out += [w for w in re.findall(r"[A-Za-z]{4,}", Path(name).stem)[:2]]
    seen, res = set(), []
    for q in out:
        q = q.strip()[:60]
        if len(q) >= 3 and q.lower() not in seen:
            seen.add(q.lower()); res.append(q)
    return res


_trees = {}


async def _hf_tree(repo):
    if repo not in _trees:
        _trees[repo] = await _json_get(f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true", _hf_headers(), timeout=40) or []
    return _trees[repo]


async def _hf_find(query, name, max_repos=10, refused=None):
    repos = await _json_get(f"https://huggingface.co/api/models?search={quote(query)}&limit=20&sort=downloads", _hf_headers()) or []
    repos = sorted(repos, key=lambda r: (not r["id"].startswith("Comfy-Org/"), -r.get("downloads", 0)))[:max_repos]
    for r in repos:
        for f in await _hf_tree(r["id"]):
            if isinstance(f, dict) and f.get("type") == "file" and Path(f["path"]).name == name:
                src = {"url": f"https://huggingface.co/{r['id']}/resolve/main/{quote(f['path'])}", "source": f"Hugging Face · {r['id']}",
                       "size": f.get("size") or (f.get("lfs") or {}).get("size"), "page": f"https://huggingface.co/{r['id']}"}
                if await _usable(src, refused):
                    return src
    return None


async def _usable(src, refused=None):
    """A source we can actually download: gated or restricted repos (401/403) and dead links (404) are skipped,
    and noted in `refused` so the page can say why that copy wasn't used."""
    size, status = await _size(src["url"])
    if status in (401, 403, 404):
        if refused is not None:
            refused.append(f"{src['source']} (HTTP {status})")
        return False
    if size and not src.get("size"):
        src["size"] = size
    return True


def _is_hf(url):
    return (urlparse(url).hostname or "").lower() in ("huggingface.co", "www.huggingface.co", "hf.co")


async def resolve_link(url, name, folder):
    """A link an admin pasted for a missing model -> a direct file download we have checked we can fetch.
    Accepts Civitai model/version pages and Hugging Face file, blob and repo pages as well as direct file links."""
    url = (url or "").strip()
    if not re.match(r"^https://", url):
        raise WorkflowError(f"Use an https link for {name}")
    u = urlparse(url)
    src = {"url": url, "source": "link you provided", "folder": folder, "page": None}
    if _is_civitai(url):
        q = dict(x.split("=", 1) for x in u.query.split("&") if "=" in x)
        vid = re.search(r"/api/download/models/(\d+)", u.path) or re.search(r"/model-versions/(\d+)", u.path)
        vid = vid.group(1) if vid else q.get("modelVersionId")
        mid = re.search(r"/models/(\d+)", u.path)
        if not re.search(r"/api/download/models/\d+", u.path):
            versions = []
            if vid:
                v = await _json_get(f"https://civitai.com/api/v1/model-versions/{vid}", _civitai_headers())
                versions = [v] if v else []
            elif mid:
                versions = ((await _json_get(f"https://civitai.com/api/v1/models/{mid.group(1)}", _civitai_headers())) or {}).get("modelVersions", [])
            if not versions:
                raise WorkflowError(f"Couldn't read that Civitai page for {name}: check the link, or add a Civitai API key in Settings")
            pick = next(((v, f) for v in versions for f in v.get("files", []) if f.get("name") == name), None)
            if not pick:                                   # no file by that exact name: the chosen version's main file
                v = versions[0]
                f = next((f for f in v.get("files", []) if f.get("primary")), (v.get("files") or [None])[0])
                if not f:
                    raise WorkflowError(f"That Civitai version has no files to download for {name}")
                pick = (v, f)
            v, f = pick
            src.update(url=f["downloadUrl"], size=int((f.get("sizeKB") or 0) * 1024) or None, source="Civitai link you provided",
                       page=f"https://civitai.com/models/{v.get('modelId') or (mid and mid.group(1))}?modelVersionId={v.get('id')}")
    elif _is_hf(url):
        m = re.match(r"^/([\w.-]+/[\w.-]+)(?:/(blob|resolve|tree)/([^/]+)(?:/(.*))?)?/?$", u.path)
        if not m:
            raise WorkflowError(f"That doesn't look like a Hugging Face model link for {name}")
        repo, kind, rev, path = m.groups()
        if kind in ("blob", "resolve") and path:
            src["url"] = f"https://huggingface.co/{repo}/resolve/{rev}/{path}"
        else:                                              # a repo or folder page: find the file by its name
            hit = next((f for f in await _hf_tree(repo) if isinstance(f, dict) and f.get("type") == "file" and Path(f["path"]).name == name), None)
            if not hit:
                raise WorkflowError(f"{repo} on Hugging Face has no file named {name}. Link the file itself instead.")
            src.update(url=f"https://huggingface.co/{repo}/resolve/main/{quote(hit['path'])}", size=hit.get("size") or (hit.get("lfs") or {}).get("size"))
        src.update(source="Hugging Face link you provided", page=f"https://huggingface.co/{repo}")
    await asyncio.get_running_loop().run_in_executor(None, _public_host, urlparse(src["url"]).hostname)
    await _check_download(src, name)
    return src


def _civitai_headers():
    return {"Authorization": f"Bearer {settings['civitai_token']}"} if settings.get("civitai_token") else {}


async def _check_download(src, name):
    """Fetch the first byte: catches web pages, refused links and dead links before an install starts."""
    url = src["url"]
    headers = {"Range": "bytes=0-0", **(_hf_headers() if _is_hf(url) else {})}
    host = urlparse(url).hostname
    try:
        async with http.get(_with_civitai_token(url), headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=30)) as r:
            ctype = r.headers.get("Content-Type", "")
            if r.status in (401, 403):
                hint = ("Hugging Face says this repo is gated or restricted: open its page, request or accept access, and make sure "
                        "your Hugging Face token is in Settings" if _is_hf(url) else
                        "Civitai may need your API key (Settings) or the model may be early-access" if _is_civitai(url) else "it may need a login")
                raise WorkflowError(f"{host} refused the download for {name} (HTTP {r.status}): {hint}")
            if r.status == 404:
                raise WorkflowError(f"{host} has no file at that link for {name} (HTTP 404)")
            if r.status not in (200, 206):
                raise WorkflowError(f"{host} answered HTTP {r.status} for {name}")
            if "text/html" in ctype:
                raise WorkflowError(f"The link for {name} opens a web page, not a file. Use the file's download link "
                                    "(on Civitai, a model page link also works; on Hugging Face, the file's page)")
            total = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
            if total and not src.get("size"):
                src["size"] = int(total.group(1))
    except aiohttp.ClientError as e:
        raise WorkflowError(f"Couldn't reach {host} for {name}: {e}")


async def resolve_model(m, recipe):
    """Find a download for one missing model. Exact filename matches only, and only copies we can actually download:
    restricted ones are skipped and listed in m["refused"]."""
    name, refused = m["name"], m.setdefault("refused", [])
    h = hints_from_workflow(recipe.get("workflow")).get(name)
    if h:
        src = {"url": h["url"], "source": "link in the workflow", "folder": h.get("folder") or m["folder"]}
        if await _usable(src, refused):
            return src
    for e in (await registry("model-list")).get("models", []):
        if e.get("filename") == name and e.get("url"):
            folder = e.get("save_path") if e.get("save_path") not in (None, "default") else m["folder"]
            src = {"url": e["url"], "source": "ComfyUI-Manager model list", "folder": folder.split("/")[0], "page": e.get("reference")}
            if await _usable(src, refused):
                return src
    for res in recipe.get("civitai") or []:              # resources Civitai lists for this very picture
        vid = res.get("modelVersionId")
        if not vid:
            continue
        v = await _json_get(f"https://civitai.com/api/v1/model-versions/{vid}")
        for f in (v or {}).get("files", []):
            if f.get("name") == name:
                src = {"url": f["downloadUrl"], "source": "Civitai (from the picture)", "folder": m["folder"], "size": int((f.get("sizeKB") or 0) * 1024) or None,
                       "page": f"https://civitai.com/models/{v.get('modelId')}?modelVersionId={vid}"}
                if await _usable(src, refused):
                    return src
    for q in _queries(name):                           # Hugging Face: shorter and shorter names, exact file only
        found = await _hf_find(q, name, refused=refused)
        if found:
            return found | {"folder": m["folder"]}
    for q in _queries(name):                           # Civitai: same idea
        found = await _json_get(f"https://civitai.com/api/v1/models?query={quote(q)}&limit=10&nsfw=true") or {}   # rated models too: exact file only
        for model in found.get("items", []):
            for ver in model.get("modelVersions", []):
                for f in ver.get("files", []):
                    if f.get("name") == name:
                        src = {"url": f["downloadUrl"], "source": f"Civitai · {model.get('name')}", "folder": m["folder"],
                               "size": int((f.get("sizeKB") or 0) * 1024) or None, "page": f"https://civitai.com/models/{model['id']}?modelVersionId={ver['id']}"}
                        if await _usable(src, refused):
                            return src
    return None


async def suggestions(m):
    """Looser Civitai search for a model that has no exact match: shown to the admin to check, never auto-downloaded."""
    words = re.findall(r"[A-Za-z]{3,}", Path(m["name"]).stem)
    if not words:
        return []
    want = {"loras": "LORA", "vae": "VAE", "embeddings": "TextualInversion", "controlnet": "Controlnet", "upscale_models": "Upscaler"}.get(m["folder"])
    found = await _json_get(f"https://civitai.com/api/v1/models?query={quote(' '.join(words[:3]))}&limit=8&nsfw=true" + (f"&types={want}" if want else "")) or {}
    out = []
    for model in found.get("items", [])[:6]:
        ver = (model.get("modelVersions") or [{}])[0]
        f = next((f for f in ver.get("files", []) if f.get("primary")), (ver.get("files") or [{}])[0])
        if f.get("downloadUrl"):
            out.append({"title": f"{model.get('name')} · {ver.get('name')}", "file": f.get("name"), "url": f["downloadUrl"],
                        "size": int((f.get("sizeKB") or 0) * 1024) or None, "page": f"https://civitai.com/models/{model['id']}?modelVersionId={ver.get('id')}"})
        if len(out) == 3:
            break
    return out


def recorded_packs(wf):
    """Node pack ids that ComfyUI's editor records on each node (properties.cnr_id / aux_id), by node type."""
    out = {}
    nodes = list((wf or {}).get("nodes", []))
    for sg in ((wf or {}).get("definitions") or {}).get("subgraphs", []):
        nodes += sg.get("nodes", [])
    for n in nodes:
        props = n.get("properties") or {}
        if n.get("type") and (props.get("cnr_id") or props.get("aux_id")):
            out[n["type"]] = {"cnr_id": props.get("cnr_id"), "aux_id": props.get("aux_id")}
    return out


_registry_ids, _code_hits = {}, {}


async def registry_pack(cnr_id):
    """Comfy Registry id -> {"repo", "title", "description", "author"} (cached)."""
    if cnr_id not in _registry_ids:
        d = await _json_get(f"https://api.comfy.org/nodes/{quote(cnr_id)}") or {}
        _registry_ids[cnr_id] = {"repo": (d.get("repository") or "").rstrip("/"), "title": d.get("name") or cnr_id,
                                 "description": (d.get("description") or "")[:300], "author": (d.get("publisher") or {}).get("name")} if d.get("repository") else None
    return _registry_ids[cnr_id]


async def code_search(cls, also=(), limit=15):
    """Likely packs from a Comfy Registry search whose Python code defines this exact node type.
    Also reports which of `also` (other missing types) that pack's code defines."""
    hit = _code_hits.get(cls)
    if hit and time.time() - hit[0] < 86400 and (not hit[1] or set(also) <= set(hit[1].get("checked", ()))):
        return hit[1]
    words = re.findall(r"[A-Z]+[a-z]*\d*|\d+", cls)
    queries = list(dict.fromkeys([" ".join(words[:3]), " ".join(words[:2]), " ".join(words[-2:]), cls]))
    cands = {}
    for q in queries:
        d = await _json_get("https://api.comfy.org/nodes/search?search=" + quote(q) + "&limit=10") or {}
        for n in d.get("nodes", []):
            if n.get("repository"):
                cands.setdefault(n["repository"].rstrip("/"), n)
    found = None
    defines = lambda name, src: re.search(r"[\"']" + re.escape(name) + r"[\"']|class\s+" + re.escape(name) + r"\b", src)
    for repo, n in list(cands.items())[:limit]:
        m = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+)", repo)
        if not m:
            continue
        info = await _json_get(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}")
        branch = (info or {}).get("default_branch") or "main"
        tree = await _json_get(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/git/trees/{branch}?recursive=1") or {}
        code = ""
        for t in [t for t in tree.get("tree", []) if t.get("path", "").endswith(".py")][:40]:
            try:
                async with http.get(f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{branch}/{t['path']}", timeout=aiohttp.ClientTimeout(total=20)) as r:
                    code += (await r.text() if r.status == 200 else "") + "\n"
            except Exception:
                pass
        if defines(cls, code):
            found = {"repo": repo, "title": n.get("name") or repo.rsplit("/", 1)[-1], "description": (n.get("description") or "")[:300],
                     "author": (n.get("publisher") or {}).get("name"), "defines": sorted({cls} | {o for o in also if defines(o, code)}),
                     "checked": sorted(set(also))}
        if found:
            break
    _code_hits[cls] = (time.time(), found)
    return found


_mgr = {"t": 0, "db": None}


async def manager_db(comfy):
    """ComfyUI-Manager's own data, the same its "Install Missing Custom Nodes" uses: node type -> pack mappings and
    the installable pack list. Returns (mappings, packs by repo url, packs by id), or None when Manager isn't there."""
    if _mgr["db"] and time.time() - _mgr["t"] < 600:
        return _mgr["db"]
    try:
        mappings = await _json_get(f"{comfy}/customnode/getmappings?mode=cache", timeout=60)
        listing = await _json_get(f"{comfy}/customnode/getlist?mode=cache&skip_update=true", timeout=120)
    except Exception:
        return None
    if not mappings or not listing or not listing.get("node_packs"):
        return None
    by_id = listing["node_packs"]
    by_repo = {}
    for pid, p in by_id.items():
        for u in [p.get("repository"), p.get("reference")] + list(p.get("files") or []):
            if u:
                by_repo.setdefault(u.rstrip("/").removesuffix(".git"), {**p, "id": pid})
    _mgr.update(t=time.time(), db=(mappings, by_repo, by_id))
    return _mgr["db"]


def _manager_item(db, repo=None, pid=None):
    if not db:
        return None
    if repo and not repo.startswith("http"):             # Manager's mappings name registry packs by their id
        pid, repo = pid or repo, None
    if pid and pid in db[2]:
        return {**db[2][pid], "id": pid}
    return db[1].get((repo or "").rstrip("/").removesuffix(".git"))


def _from_manager(item, provides, found_by):
    return {"repo": (item.get("repository") or item.get("reference") or (item.get("files") or [""])[0]).rstrip("/"),
            "title": item.get("title") or item["id"], "author": item.get("author"), "description": (item.get("description") or "")[:300],
            "install_type": item.get("install_type", "git-clone"), "provides": list(provides), "listed": True, "found_by": found_by,
            "manager": {k: item.get(k) for k in ("id", "version", "cnr_latest", "repository", "files", "install_type", "title", "state", "reference")}}


async def resolve_nodes(missing, workflow=None, core_version=None, comfy=None):
    """Map missing node types to node packs, the way ComfyUI-Manager's "Install Missing Custom Nodes" does: pack ids
    recorded in the workflow first, then Manager's node -> pack mappings (with its name patterns), then a code search
    of likely Comfy Registry packs. Packs Manager knows install through Manager. Types marked comfy-core need a newer
    ComfyUI instead."""
    if not missing:
        return [], [], []
    db = await manager_db(comfy) if comfy else None
    packs, left, core = [], set(missing), []
    by_repo = {}
    for cls, rec in recorded_packs(workflow).items():
        if cls not in left:
            continue
        if rec.get("cnr_id") == "comfy-core":
            core.append(cls); left.discard(cls); continue
        item = _manager_item(db, pid=rec.get("cnr_id"))
        if item:
            pk = by_repo.setdefault(item["id"], _from_manager(item, [], "recorded in the workflow · ComfyUI-Manager"))
            pk["provides"].append(cls); left.discard(cls); continue
        hit = None
        if rec.get("cnr_id"):
            hit = await registry_pack(rec["cnr_id"])
        if not hit and rec.get("aux_id") and re.fullmatch(r"[\w.-]+/[\w.-]+", rec["aux_id"]):
            hit = {"repo": f"https://github.com/{rec['aux_id']}", "title": rec["aux_id"].split("/")[-1], "description": "", "author": rec["aux_id"].split("/")[0]}
        if hit:
            item = _manager_item(db, repo=hit["repo"])
            pk = by_repo.setdefault(hit["repo"], _from_manager(item, [], "recorded in the workflow · ComfyUI-Manager") if item else
                                    {**hit, "install_type": "git-clone", "provides": [], "listed": True, "found_by": "recorded in the workflow"})
            pk["provides"].append(cls); left.discard(cls)
    packs += list(by_repo.values())
    if db:
        nodemap, catalog = db[0], {}
    else:                                                 # Manager not running: the same lists, fetched from its GitHub
        nodemap = await registry("extension-node-map")
        catalog = {c.get("reference"): c for c in (await registry("custom-node-list")).get("custom_nodes", [])}
        for c in list(catalog.values()):
            for f in c.get("files", []):
                catalog.setdefault(f, c)
    providers = {}
    for repo, val in nodemap.items():
        names = set(val[0]) if val else set()
        pat = val[1].get("nodename_pattern") if len(val) > 1 and isinstance(val[1], dict) else None
        for cls in left:
            if cls in names or (pat and re.search(pat, cls)):
                providers.setdefault(repo, set()).add(cls)
    while left:
        best = max(providers, key=lambda r: (len(providers[r] & left), r in catalog), default=None)
        if not best or not providers[best] & left:
            break
        item = _manager_item(db, repo=best)
        if item:
            packs.append(_from_manager(item, sorted(providers[best] & left), "ComfyUI-Manager list"))
        else:
            c = catalog.get(best, {})
            meta = nodemap[best][1] if len(nodemap[best]) > 1 and isinstance(nodemap[best][1], dict) else {}
            packs.append({"repo": best, "title": c.get("title") or meta.get("title_aux") or best.rsplit("/", 1)[-1],
                          "author": c.get("author"), "description": (c.get("description") or "")[:300], "install_type": c.get("install_type", "git-clone"),
                          "provides": sorted(providers[best] & left), "listed": best in catalog or bool(db), "found_by": "ComfyUI-Manager list"})
        left -= providers[best]
    list_matched = {c for p in packs if p.get("found_by") == "ComfyUI-Manager list" for c in p["provides"]}
    for cls in sorted(left)[:3]:                          # code search: a few per workflow (GitHub limits anonymous lookups)
        if cls not in left:
            continue
        hit = await code_search(cls, also=sorted((left | list_matched) - {cls}))
        if not hit:
            continue
        got = set(hit.get("defines") or [cls])
        for p in packs:                                   # code that defines a node beats a list entry that names it
            if p.get("found_by") == "ComfyUI-Manager list":
                p["provides"] = [c for c in p["provides"] if c not in got]
        packs = [p for p in packs if p["provides"]]
        pk = next((p for p in packs if p["repo"] == hit["repo"]), None)
        clean = {k: v for k, v in hit.items() if k not in ("defines", "checked")}
        if pk:
            pk["provides"] = sorted(set(pk["provides"]) | got)
        else:
            item = _manager_item(db, repo=hit["repo"])
            packs.append(_from_manager(item, sorted(got), "found in the pack's code · ComfyUI-Manager") if item else
                         {**clean, "install_type": "git-clone", "provides": sorted(got), "listed": True, "found_by": "found in the pack's code"})
        left -= got
    return packs, sorted(left), sorted(core)


async def manual_pack(ref, provides, comfy=None):
    """A pack an admin named: a GitHub link or a Comfy Registry id. Installs through ComfyUI-Manager when it knows it."""
    ref = (ref or "").strip().rstrip("/")
    db = await manager_db(comfy) if comfy else None
    item = _manager_item(db, pid=ref) or _manager_item(db, repo=ref)
    if item:
        return _from_manager(item, provides, "added by an admin · ComfyUI-Manager")
    if re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+(\.git)?", ref):
        repo = ref[:-4] if ref.endswith(".git") else ref
        if not await _json_get(f"https://api.github.com/repos/{repo.split('github.com/')[1]}"):
            raise WorkflowError(f"{repo} doesn't exist or isn't public")
        return {"repo": repo, "title": repo.rsplit("/", 1)[-1], "author": repo.split("/")[-2], "description": "", "install_type": "git-clone",
                "provides": provides, "listed": False, "found_by": "added by an admin"}
    if re.fullmatch(r"[\w.-]{2,100}", ref):
        hit = await registry_pack(ref)
        if hit and re.match(r"^https://github\.com/", hit["repo"]):
            return {**hit, "install_type": "git-clone", "provides": provides, "listed": True, "found_by": "Comfy Registry id given by an admin"}
    raise WorkflowError("Use a GitHub repository link (https://github.com/owner/repo) or a Comfy Registry id")

async def plan(recipe, comfy):
    """Analysis plus a source for everything missing, with sizes."""
    a = await analyze(recipe, comfy)
    gate = asyncio.Semaphore(6)

    async def one(m):
        if m.get("elsewhere"):
            m["source"] = {"link": m["elsewhere"], "source": f"already downloaded (in {m['elsewhere'][0]})", "folder": m["folder"], "size": 0}
            return
        async with gate:
            try:
                src = await resolve_model(m, recipe)
                if src and not src.get("size"):
                    src["size"], _ = await _size(src["url"])
                if src and _is_civitai(src.get("url", "")) and not settings.get("civitai_token"):
                    src["note"] = "Civitai may require an API key (Settings) for this download"
                m["source"] = src
                if not src:
                    m["suggestions"] = await suggestions(m)
            except Exception as e:
                m["source"], m["lookup_error"] = None, str(e)[:200]
    packs = asyncio.create_task(resolve_nodes(a["missing_nodes"], recipe.get("workflow"), comfy=comfy))
    await asyncio.gather(*(one(m) for m in a["models"] if not m["installed"]))
    a["packs"], a["unknown_nodes"], a["core_nodes"] = await packs
    a["download_bytes"] = sum((m.get("source") or {}).get("size") or 0 for m in a["models"] if not m["installed"])
    a["free_bytes"] = shutil.disk_usage(MODELS).free
    return a


# ---------------------------------------------------------------- installing (admins)

async def _download(step, url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    headers = _hf_headers() if "huggingface.co" in url else {}
    have = part.stat().st_size if part.exists() else 0
    if have:
        headers["Range"] = f"bytes={have}-"
    async with http.get(_with_civitai_token(url), headers=headers, timeout=aiohttp.ClientTimeout(total=None, sock_read=120)) as r:
        if r.status in (401, 403):
            raise WorkflowError(f"{urlparse(url).hostname} refused the download (HTTP {r.status}): "
                                + ("the repo is gated or restricted — request access on its page and check your Hugging Face token in Settings"
                                   if "huggingface.co" in url else "it may need an API key in Settings, or the model is early-access"))
        if r.status not in (200, 206):
            raise WorkflowError(f"Download failed: HTTP {r.status}")
        if "text/html" in r.headers.get("Content-Type", ""):      # a web page, not a model: never save it under the model's name
            raise WorkflowError(f"{urlparse(url).hostname} sent a web page instead of the file — the link is a page, or the site wants a login")
        if r.status == 200:
            have = 0
        total = have + int(r.headers.get("Content-Length") or 0)
        if total and shutil.disk_usage(MODELS).free - (total - have) < FREE_MARGIN:
            raise WorkflowError("Not enough free disk space for this model")
        step.update(total=total or None, done=have)
        with part.open("ab" if have else "wb") as f:
            async for chunk in r.content.iter_chunked(1 << 20):
                f.write(chunk); step["done"] += len(chunk)
                if step.get("cancel"):
                    raise WorkflowError("Cancelled")
    if step.get("total") and part.stat().st_size < step["total"]:
        raise WorkflowError("Download ended early — try again to resume")
    part.replace(dest)


def _run(cmd, cwd=None, env=None, timeout=1800):
    r = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise WorkflowError((r.stderr or r.stdout).strip()[-600:] or f"{cmd[0]} failed")
    return r.stdout


def install_pack(step, pack):
    """git clone a registry-listed node pack and install its Python requirements into ComfyUI's environment."""
    repo = pack["repo"]
    if not re.match(r"^https://github\.com/[\w.-]+/[\w.-]+/?$", repo):
        raise WorkflowError("Only GitHub node packs from the ComfyUI-Manager registry can be installed automatically")
    if pack.get("install_type") not in (None, "git-clone"):
        raise WorkflowError(f"This pack installs by {pack['install_type']}, which isn't supported automatically")
    target = CUSTOM_NODES / repo.rstrip("/").rsplit("/", 1)[-1]
    step["detail"] = "downloading code"
    if not target.exists():
        _run(["git", "clone", "--depth", "1", repo, str(target)], timeout=600)
    req = target / "requirements.txt"
    if req.exists():
        step["detail"] = "installing Python packages"
        _run([UV, "pip", "install", "-r", str(req)], env={"VIRTUAL_ENV": str(COMFY_DIR / ".venv")}, timeout=1800)
    if (target / "install.py").exists():
        step["detail"] = "running the pack's installer"
        _run([str(COMFY_PY), "install.py"], cwd=target, timeout=1800)
    step["detail"] = "installed"


async def manager_install(step, pack, comfy):
    """Install a node pack through ComfyUI-Manager's queue (what its Install button does: registry version, its own
    dependency handling and security checks). Returns False when Manager declines, so the caller can fall back."""
    item = dict(pack["manager"])
    if item.get("state") == "enabled":
        step["detail"] = "already installed"
        return True
    item.update(ui_id=item["id"], skip_post_install=False, version=item.get("version") or "unknown",
                selected_version="latest" if item.get("cnr_latest") else ("nightly" if item.get("version") == "nightly" else "unknown"))
    step["detail"] = "queued in ComfyUI-Manager"
    async with http.post(f"{comfy}/manager/queue/install", json=item, timeout=aiohttp.ClientTimeout(total=60)) as r:
        if r.status != 200:
            step["manager_said"] = f"HTTP {r.status} {(await r.text())[:200]}"
            return False
    async with http.post(f"{comfy}/manager/queue/start", json={}, timeout=aiohttp.ClientTimeout(total=60)) as r:
        if r.status not in (200, 201):
            step["manager_said"] = f"start: HTTP {r.status}"
            return False
    step["detail"] = "installing with ComfyUI-Manager"
    end = time.time() + 3600
    while time.time() < end:
        await asyncio.sleep(2)
        q = await _json_get(f"{comfy}/manager/queue/status", timeout=20) or {}
        if not q.get("is_processing") and q.get("done_count", 0) >= q.get("total_count", 0):
            break
    _mgr["t"] = 0                                          # re-read pack states
    db = await manager_db(comfy)
    now = _manager_item(db, pid=item["id"]) or {}
    if now.get("state") in (None, "not-installed"):
        raise WorkflowError("ComfyUI-Manager couldn't install it — ComfyUI's log (journalctl --user -u comfyui) has the reason")
    step["detail"] = "installed with ComfyUI-Manager" + (" (needs a fix: import failed)" if now.get("state") == "import-fail" else "")
    return True


async def _restart_comfy(step, comfy):
    step["detail"] = "restarting ComfyUI"
    await asyncio.create_subprocess_exec("systemctl", "--user", "restart", "comfyui.service")
    await asyncio.sleep(5)
    for _ in range(120):
        try:
            await object_info(comfy, force=True)
            return
        except Exception:
            await asyncio.sleep(2)
    raise WorkflowError("ComfyUI didn't come back after the restart")


_downloading = {}                            # destination file -> future, while some install is downloading it
_install_gate = None                         # one install task at a time; later ones wait their turn ("queued")


def merge_plans(plans_):
    """[(workflow name, plan)] -> one plan: each model file and node pack once, remembering which workflows want it."""
    models, packs = {}, {}
    for name, p in plans_:
        for m in p["models"]:
            if m["installed"] or not m.get("source"):
                continue
            key = (m["source"].get("folder") or m["folder"], m["name"])
            models.setdefault(key, {**m, "for": []})["for"].append(name)
        for k in p["packs"]:
            packs.setdefault(k["repo"], {**k, "for": []})["for"].append(name)
    return {"models": list(models.values()), "packs": list(packs.values()),
            "download_bytes": sum((m["source"].get("size") or 0) for m in models.values() if not m["source"].get("link"))}


async def install(plan_, comfy, label):
    """Install everything missing in a plan (or several merged ones). Progress lives in tasks[id].
    Model downloads run three at a time; node packs then install one by one and ComfyUI restarts once at the end."""
    global _install_gate
    _install_gate = _install_gate or asyncio.Lock()
    tid = uuid.uuid4().hex[:10]
    steps = [{"kind": "link" if m["source"].get("link") else "model", "label": m["name"], "folder": m["source"].get("folder") or m["folder"],
              "url": m["source"].get("url"), "link": m["source"].get("link"), "status": "waiting", "done": 0, "total": m["source"].get("size"),
              "for": m.get("for")}
             for m in plan_["models"] if not m["installed"] and m.get("source")]
    steps += [{"kind": "pack", "label": p["title"], "pack": p, "status": "waiting", "for": p.get("for")} for p in plan_["packs"]]
    if any(s["kind"] == "pack" for s in steps):
        steps.append({"kind": "restart", "label": "Restart ComfyUI to load the new nodes", "status": "waiting"})
    busy = any(t["status"] in ("queued", "running") for t in tasks.values())
    tasks[tid] = {"id": tid, "label": label, "steps": steps, "status": "queued" if busy else "running", "started": time.time()}

    async def step(s):
        if tasks[tid].get("cancel"):
            s["status"] = "cancelled"; return
        s["status"] = "running"
        try:
            if s["kind"] == "model":
                dest = MODELS / s["folder"] / s["label"]
                while dest in _downloading:                # the same file is already coming in another install: wait for it
                    s["detail"] = "waiting for the same file in another install"
                    await asyncio.shield(_downloading[dest])
                if dest.exists():                          # another workflow (or task) already fetched it
                    s["detail"] = "already downloaded"
                else:
                    _downloading[dest] = asyncio.get_running_loop().create_future()
                    try:
                        await _download(s, s["url"], dest)
                    finally:
                        _downloading.pop(dest).set_result(None)
            elif s["kind"] == "link":                      # same file, second folder: a link costs no disk space
                dest = MODELS / s["folder"] / s["label"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    dest.symlink_to(MODELS / s["link"][0] / s["link"][1])
            elif s["kind"] == "pack":
                if not (s["pack"].get("manager") and await manager_install(s, s["pack"], comfy)):
                    await asyncio.get_running_loop().run_in_executor(None, install_pack, s, s["pack"])
            else:
                await _restart_comfy(s, comfy)
            s["status"] = "done"
        except Exception as e:
            s["status"], s["error"] = "error", str(e)[:500]
            log(f"workflow install: {s['label']} failed: {e}")

    async def go():
        async with _install_gate:
            tasks[tid]["status"] = "running"
            gate = asyncio.Semaphore(3)

            async def limited(s):
                async with gate:
                    await step(s)
            await asyncio.gather(*(limited(s) for s in steps if s["kind"] in ("model", "link")))
            for s in steps:
                if s["kind"] in ("pack", "restart"):
                    await step(s)
            _object_info["t"] = 0
            tasks[tid]["status"] = "error" if any(s["status"] == "error" for s in steps) else "cancelled" if tasks[tid].get("cancel") else "done"
            tasks[tid]["finished"] = time.time()
    asyncio.create_task(go())
    return tasks[tid]


def public_task(t):
    return {k: v for k, v in t.items() if k != "steps"} | {"steps": [{k: v for k, v in s.items() if k not in ("pack", "url", "link", "cancel")} for s in t["steps"]]}


# ---------------------------------------------------------------- running

def build_prompt(analysis, values, images, owner_prefix="genstudio/wf"):
    """A copy of the workflow with the person's changes, fresh seeds, installed model names and saving outputs."""
    import random
    p = copy.deepcopy(analysis["prompt"])
    for m in analysis["models"]:                      # use the installed spelling (folder prefix, slashes)
        if m.get("use") and m["path"]:
            tgt = p[m["node"]]["inputs"]
            for k in m["path"][:-1]:
                tgt = tgt[k]
            tgt[m["path"][-1]] = m["use"]
    for f in analysis["fields"]:
        given = values.get(f["id"]) or {}                 # {"value": ..., "random": bool} from the page
        v = given.get("value", f["value"])
        if f["type"] == "seed":
            v = random.randrange(2**48) if given.get("random", True) else int(v)
        elif f["type"] == "int":
            v = int(v)
        elif f["type"] == "float":
            v = float(v)
        elif f["type"] in ("image", "video"):
            v = images.get(f["id"]) or f["value"]
        p[f["node"]]["inputs"][f["key"]] = v
    for nid, n in p.items():
        n.get("inputs", {}).pop("_widgets", None)
        if n.get("class_type") == "PreviewImage":           # keep results: previews become saves
            n["class_type"] = "SaveImage"; n["inputs"]["filename_prefix"] = owner_prefix
        elif n.get("class_type") in ("SaveImage", "SaveVideo", "SaveAnimatedWEBP", "SaveAnimatedPNG", "SaveWEBM"):
            n["inputs"]["filename_prefix"] = owner_prefix
        elif n.get("class_type") == "VHS_VideoCombine":
            n["inputs"].update(filename_prefix=owner_prefix, save_output=True)
    return p


# ---------------------------------------------------------------- extra LoRAs (Create panel)

MODEL_LOADERS = ("UNETLoader", "CheckpointLoaderSimple", "UnetLoaderGGUF", "CheckpointLoader", "ImageOnlyCheckpointLoader",
                 "DiffusionModelLoaderKJ", "UNETLoaderNF4", "CheckpointLoaderNF4")


def inject_loras(prompt, loras):
    """Chain LoraLoaderModelOnly nodes right after the graph's diffusion-model loader and point everything that used
    the model at the end of the chain. Only for graphs with one model loader (two-model graphs like Wan 2.2 14B would
    need the LoRA matched to each model)."""
    if not loras:
        return prompt
    loaders = [nid for nid, n in prompt.items() if n.get("class_type") in MODEL_LOADERS]
    if len(loaders) != 1:
        raise WorkflowError("This workflow loads " + (f"{len(loaders)} models" if loaders else "its model in a way the studio doesn't recognise")
                            + ", so extra LoRAs can't be added to it. Use its own LoRA strength settings instead.")
    src = [loaders[0], 0]
    users = [(n, k) for nid, n in prompt.items() for k, v in n.get("inputs", {}).items() if v == src]
    prev = src
    for i, l in enumerate(loras):
        nid = f"gs_lora_{i}"
        prompt[nid] = {"class_type": "LoraLoaderModelOnly", "_meta": {"title": "LoRA (Create panel)"},
                       "inputs": {"model": prev, "lora_name": l["name"], "strength_model": l["strength"]}}
        prev = [nid, 0]
    for n, k in users:
        n["inputs"][k] = prev
    return prompt
