"""Model library: browse Civitai and Hugging Face, download into the right ComfyUI folder, and keep each model's
information and example pictures next to it.

For every downloaded model <folder>/<name>.<ext> this writes:
  <folder>/<stem>.preview.<jpeg|png|webp|mp4>   first example picture (the name other ComfyUI tools look for)
  <folder>/<stem>.genstudio.json                 source, name, version, type, base model, trigger words, description, hashes
  <folder>/.previews/<stem>/N.<ext>              up to MAX_PREVIEWS example pictures/videos
"""
import asyncio, hashlib, html, json, re, time, uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import aiohttp

import workflows as W

MAX_PREVIEWS, MAX_PREVIEW_BYTES = 8, 15 * 2**20
MODEL_EXT = W.MODEL_EXT
SKIP_FOLDERS = {"custom_nodes", "configs", "datasets"}
# Civitai model type -> ComfyUI folder
CIVITAI_FOLDERS = {"Checkpoint": "checkpoints", "LORA": "loras", "LoCon": "loras", "DoRA": "loras", "TextualInversion": "embeddings",
                   "VAE": "vae", "Controlnet": "controlnet", "Upscaler": "upscale_models", "Hypernetwork": "hypernetworks",
                   "MotionModule": "animatediff_models"}
NOT_MODELS = {"Poses", "Wildcards", "Workflows", "Other", "AestheticGradient", "Detection"}
# base models whose "checkpoints" on Civitai are usually just the diffusion model (no text encoder/VAE inside)
DIFFUSION_ONLY_BASES = re.compile(r"flux|krea|qwen|wan|hidream|chroma|hunyuan|ltx|z-?image|lumina|cosmos|mochi|kolors|aura|sd ?3|stable diffusion 3", re.I)
_sem = asyncio.Semaphore(2)                    # at most two big downloads at once
recent = []                                    # task ids, newest last


def comfy_folders():
    return sorted(p.name for p in W.MODELS.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name not in SKIP_FOLDERS)


def check_folder(folder):
    if folder not in comfy_folders() and not re.fullmatch(r"[a-z0-9_]{2,40}", folder or ""):
        raise W.WorkflowError("Unknown model folder")
    return folder


def plain_text(raw, limit=6000):
    """Civitai descriptions are HTML: keep paragraphs and list items as plain text."""
    t = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", raw or "")
    t = re.sub(r"(?i)<\s*li[^>]*>", "• ", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", t).strip()[:limit]


def civitai_folder(model_type, base, file_type="Model"):
    if file_type == "VAE":
        return "vae"
    folder = CIVITAI_FOLDERS.get(model_type, "checkpoints")
    if folder == "checkpoints" and base and DIFFUSION_ONLY_BASES.search(base):
        return "diffusion_models"
    return folder


def hf_folder(path):
    parts = [p.lower() for p in Path(path).parts[:-1]]
    for p in parts:
        if p in ("unet",):
            return "unet" if path.endswith(".gguf") else "diffusion_models"
        if p == "clip":
            return "text_encoders"
        if p in comfy_folders():
            return p
    name = Path(path).name.lower()
    if name.endswith(".gguf"):
        return "unet"
    return next((f for kw, f in W.KEYWORD_FOLDERS if kw in name), "checkpoints")


# ---------------------------------------------------------------- Civitai

def _thumb(url, width=450):
    return re.sub(r"/(original=true|width=\d+)(,[^/]*)?/", f"/width={width}/", url) if url else url


async def civitai_search(query="", types="", sort="Most Downloaded", base="", nsfw=False, cursor=""):
    params = [f"limit=24", f"sort={quote(sort)}", f"nsfw={'true' if nsfw else 'false'}"]
    if query:
        params.append(f"query={quote(query)}")
    if types:
        params.append(f"types={quote(types)}")
    if base:
        params.append(f"baseModels={quote(base)}")
    if cursor:
        params.append(f"cursor={quote(cursor)}")
    d = await W._json_get("https://civitai.com/api/v1/models?" + "&".join(params), _civitai_headers(), timeout=40) or {}
    out = []
    for m in d.get("items", []):
        v = (m.get("modelVersions") or [{}])[0]
        imgs = [i for i in v.get("images", []) if nsfw or (i.get("nsfwLevel") or 1) <= 2]
        out.append({"id": m["id"], "name": m.get("name"), "type": m.get("type"), "nsfw": m.get("nsfw"), "level": m.get("nsfwLevel"), "base": v.get("baseModel"),
                    "creator": (m.get("creator") or {}).get("username"), "downloads": (m.get("stats") or {}).get("downloadCount"),
                    "likes": (m.get("stats") or {}).get("thumbsUpCount"),
                    "image": _thumb(imgs[0]["url"]) if imgs else None, "image_type": imgs[0].get("type") if imgs else None})
    return {"items": out, "next": (d.get("metadata") or {}).get("nextCursor")}


def _civitai_headers():
    return {"Authorization": f"Bearer {W.settings['civitai_token']}"} if W.settings.get("civitai_token") else {}


async def civitai_model(model_id, nsfw=False):
    m = await W._json_get(f"https://civitai.com/api/v1/models/{int(model_id)}", _civitai_headers(), timeout=40)
    if not m:
        raise W.WorkflowError("Civitai didn't return that model (it may be private, removed, or need an API key)")
    versions = []
    for v in m.get("modelVersions", []):
        versions.append({
            "id": v["id"], "name": v.get("name"), "base": v.get("baseModel"), "trained_words": v.get("trainedWords") or [],
            "notes": plain_text(v.get("description"), 2000), "published": v.get("publishedAt"),
            "images": [{"url": _thumb(i["url"], 900), "full": i["url"], "type": i.get("type", "image"), "nsfw": (i.get("nsfwLevel") or 1) > 2,
                        "w": i.get("width"), "h": i.get("height")} for i in v.get("images", [])][:20],
            "files": [{"id": f.get("id"), "name": f.get("name"), "size": int((f.get("sizeKB") or 0) * 1024), "type": f.get("type"),
                       "format": (f.get("metadata") or {}).get("format"), "fp": (f.get("metadata") or {}).get("fp"),
                       "variant": (f.get("metadata") or {}).get("size"), "primary": f.get("primary"),
                       "sha256": (f.get("hashes") or {}).get("SHA256"), "url": f.get("downloadUrl"),
                       "folder": civitai_folder(m.get("type"), v.get("baseModel"), f.get("type")),
                       "installed": bool(W.find_installed(f.get("name") or "", civitai_folder(m.get("type"), v.get("baseModel"), f.get("type"))))}
                      for f in v.get("files", []) if f.get("type") != "Training Data"]})
    return {"id": m["id"], "name": m.get("name"), "type": m.get("type"), "nsfw": m.get("nsfw"), "level": m.get("nsfwLevel"), "tags": m.get("tags") or [],
            "creator": (m.get("creator") or {}).get("username"), "stats": m.get("stats") or {}, "downloadable": m.get("type") not in NOT_MODELS,
            "description": plain_text(m.get("description")), "page": f"https://civitai.com/models/{m['id']}", "versions": versions}


# ---------------------------------------------------------------- Hugging Face

async def hf_search(query):
    repos = await W._json_get(f"https://huggingface.co/api/models?search={quote(query)}&limit=30&sort=downloads", W._hf_headers()) or []
    return {"items": [{"id": r["id"], "downloads": r.get("downloads"), "likes": r.get("likes"), "updated": r.get("lastModified"),
                       "tags": [t for t in r.get("tags", []) if ":" not in t][:6], "pipeline": r.get("pipeline_tag")} for r in repos]}


async def hf_repo(repo):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise W.WorkflowError("Not a Hugging Face repository name")
    info = await W._json_get(f"https://huggingface.co/api/models/{repo}", W._hf_headers()) or {}
    tree = await W._json_get(f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true", W._hf_headers(), timeout=60) or []
    files = [{"path": f["path"], "size": f.get("size") or (f.get("lfs") or {}).get("size"), "folder": hf_folder(f["path"]),
              "installed": bool(W.find_installed(Path(f["path"]).name, hf_folder(f["path"])))}
             for f in tree if isinstance(f, dict) and f.get("type") == "file" and f["path"].lower().endswith(MODEL_EXT)]
    readme = ""
    try:
        async with W.http.get(f"https://huggingface.co/{repo}/raw/main/README.md", headers=W._hf_headers(), timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status == 200:
                readme = (await r.text())[:20000]
    except Exception:
        pass
    readme = re.sub(r"^---\n.*?\n---\n", "", readme, flags=re.S)          # YAML front matter
    images = []
    for src in re.findall(r"!\[[^\]]*\]\(([^)\s]+)", readme) + re.findall(r"<img[^>]+src=[\"']([^\"']+)", readme):
        url = src if src.startswith("http") else f"https://huggingface.co/{repo}/resolve/main/{src.lstrip('./')}"
        if url not in images:
            images.append(url)
    images += [f"https://huggingface.co/{repo}/resolve/main/{quote(f['path'])}" for f in tree if isinstance(f, dict)
               and f.get("type") == "file" and f["path"].lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))][:6]
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)|<img[^>]*>", "", readme)
    return {"id": repo, "gated": bool(info.get("gated")), "downloads": info.get("downloads"), "likes": info.get("likes"),
            "license": (info.get("cardData") or {}).get("license"), "tags": [t for t in info.get("tags", []) if ":" not in t][:12],
            "page": f"https://huggingface.co/{repo}", "readme": plain_text(text, 8000), "images": images[:12], "files": files}


# ---------------------------------------------------------------- downloading with information + pictures

async def _save_previews(images, folder, stem):
    """Download up to MAX_PREVIEWS example pictures/videos; the first also becomes <stem>.preview.<ext>."""
    target = W.MODELS / folder / ".previews" / stem
    target.mkdir(parents=True, exist_ok=True)
    saved = []
    for n, img in enumerate(images[: MAX_PREVIEWS * 2]):
        url = img["full"] if isinstance(img, dict) else img
        try:
            headers = W._hf_headers() if "huggingface.co" in url else {}
            async with W.http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status != 200:
                    continue
                kind = r.headers.get("Content-Type", "")
                ext = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "video/mp4": "mp4", "video/webm": "webm"}.get(kind.split(";")[0])
                if not ext:
                    continue
                data = bytearray()
                async for chunk in r.content.iter_chunked(1 << 16):
                    data += chunk
                    if len(data) > MAX_PREVIEW_BYTES:
                        break
                if len(data) > MAX_PREVIEW_BYTES:
                    continue
            path = target / f"{len(saved) + 1}.{ext}"
            path.write_bytes(data)
            saved.append({"file": str(path.relative_to(W.MODELS)), "nsfw": bool(isinstance(img, dict) and img.get("nsfw")), "type": "video" if ext in ("mp4", "webm") else "image"})
            if len(saved) == 1:
                (W.MODELS / folder / f"{stem}.preview.{ext}").write_bytes(data)
        except Exception:
            continue
        if len(saved) >= MAX_PREVIEWS:
            break
    return saved


async def download(info, label):
    """info: {"url", "folder", "name", "meta": {...}, "images": [...]} -> a task like the workflow installs use."""
    tid = uuid.uuid4().hex[:10]
    step = {"kind": "model", "label": info["name"], "folder": info["folder"], "status": "waiting", "done": 0, "total": info.get("size"),
            "detail": "waiting for a free download slot"}
    task = {"id": tid, "label": label, "steps": [step], "status": "running", "started": time.time(), "kind": "model",
            "folder": info["folder"], "name": info["name"]}
    W.tasks[tid] = task; recent.append(tid); del recent[:-30]

    async def go():
        async with _sem:
            step.update(status="running", detail="downloading")
            dest = W.MODELS / info["folder"] / info["name"]
            try:
                if dest.exists():
                    step["detail"] = "already on disk — refreshing its information"
                else:
                    await W._download(step, info["url"], dest)
                if info["folder"] in W.NOT_LORA_FOLDERS and W.file_kind(dest) == "lora":   # it's really a LoRA: file it as one
                    (W.MODELS / "loras").mkdir(exist_ok=True)
                    moved = W.MODELS / "loras" / dest.name
                    dest.replace(moved)
                    step["note"] = f"It's a LoRA, not a {info['folder'].rstrip('s').replace('_', ' ')}: saved in loras/ instead"
                    info["folder"], dest = "loras", moved
                step["detail"] = "saving pictures and information"
                stem = Path(info["name"]).stem
                previews = await _save_previews(info.get("images") or [], info["folder"], stem)
                meta = dict(info.get("meta") or {}, file=info["name"], folder=info["folder"], downloaded=time.time(), previews=previews)
                (W.MODELS / info["folder"] / f"{stem}.genstudio.json").write_text(json.dumps(meta, indent=1))
                step.update(status="done", detail="installed")
                task["status"] = "done"
            except Exception as e:
                step.update(status="error", error=str(e)[:400]); task["status"] = "error"
            task["finished"] = time.time()
    asyncio.create_task(go())
    return task


def installed_with_sha(sha):
    """An installed model with this SHA-256 (from the ratings' hashes and the saved model information), or None."""
    if not sha:
        return None
    sha = sha.lower()
    try:
        import ratings
        for key, r in ratings.ratings.items():
            if (r.get("sha256") or "").lower() == sha and (W.MODELS / key).exists():
                return key
    except Exception:
        pass
    for meta in W.MODELS.glob("*/*.genstudio.json"):
        try:
            m = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if (m.get("sha256") or "").lower() == sha and (meta.parent / (m.get("file") or "")).exists():
            return f"{meta.parent.name}/{m['file']}"
    return None


async def start_civitai(model_id, version_id, file_id, folder=None):
    m = await civitai_model(model_id, nsfw=True)
    v = next((v for v in m["versions"] if v["id"] == int(version_id)), None)
    f = next((f for f in (v or {}).get("files", []) if f["id"] == int(file_id)), None)
    if not v or not f:
        raise W.WorkflowError("That version or file isn't on Civitai any more")
    if not m["downloadable"]:
        raise W.WorkflowError(f"Civitai {m['type']} entries aren't model files")
    have = installed_with_sha(f.get("sha256"))
    if have and Path(have).name != f["name"]:          # the same file, already here under another name
        raise W.WorkflowError(f"You already have this file: {have}")
    folder = check_folder(folder or f["folder"])
    meta = {"source": "civitai", "page": f"{m['page']}?modelVersionId={v['id']}", "model": m["name"], "version": v["name"], "type": m["type"],
            "base": v["base"], "trained_words": v["trained_words"], "creator": m["creator"], "description": m["description"],
            "notes": v["notes"], "tags": m["tags"], "sha256": f["sha256"], "nsfw": m["nsfw"], "size": f["size"]}
    return await download({"url": f["url"], "folder": folder, "name": f["name"], "size": f["size"], "meta": meta, "images": v["images"]},
                          f"{m['name']} · {v['name']}")


async def start_hf(repo, path, folder=None):
    r = await hf_repo(repo)
    f = next((f for f in r["files"] if f["path"] == path), None)
    if not f:
        raise W.WorkflowError("That file isn't in the repository")
    folder = check_folder(folder or f["folder"])
    meta = {"source": "huggingface", "page": r["page"], "model": repo, "version": path, "type": "", "base": "", "trained_words": [],
            "creator": repo.split("/")[0], "description": r["readme"][:4000], "license": r["license"], "tags": r["tags"], "size": f["size"]}
    return await download({"url": f"https://huggingface.co/{repo}/resolve/main/{quote(path)}", "folder": folder, "name": Path(path).name,
                           "size": f["size"], "meta": meta, "images": r["images"]}, f"{repo} · {Path(path).name}")


async def start_link(url, folder=None, name=None):
    """A pasted link: a Civitai model or version page (or its download link), a Hugging Face file link, or any direct
    https link to a model file (then the folder must be chosen). Civitai and Hugging Face links bring their pictures,
    description and trigger words along, like downloads from the Civitai / Hugging Face tabs."""
    url = (url or "").strip()
    if not re.match(r"^https://\S+$", url):
        raise W.WorkflowError("Paste an https link")
    u = urlparse(url)
    if W._is_civitai(url):
        q = dict(x.split("=", 1) for x in u.query.split("&") if "=" in x)
        mid = re.search(r"/models/(\d+)", u.path)
        vid = re.search(r"/api/download/models/(\d+)", u.path) or re.search(r"/model-versions/(\d+)", u.path)
        vid = int(vid.group(1)) if vid else int(q["modelVersionId"]) if q.get("modelVersionId", "").isdigit() else None
        if not mid:
            if not vid:
                raise W.WorkflowError("That Civitai link has no model in it: open the model's page and copy that link")
            v = await W._json_get(f"https://civitai.com/api/v1/model-versions/{vid}", _civitai_headers())
            if not v or not v.get("modelId"):
                raise W.WorkflowError("Couldn't read that Civitai version: check the link, or add a Civitai API key in Settings")
            model_id = int(v["modelId"])
        else:
            model_id = int(mid.group(1))
        m = await civitai_model(model_id, nsfw=True)
        ver = next((v for v in m["versions"] if v["id"] == vid), None) if vid else (m["versions"] or [None])[0]
        if not ver:
            raise W.WorkflowError("That version isn't on Civitai any more")
        f = next((f for f in ver["files"] if f.get("primary")), (ver["files"] or [None])[0])
        if not f:
            raise W.WorkflowError("That Civitai version has no files to download")
        return await start_civitai(model_id, ver["id"], f["id"], folder or None)
    if W._is_hf(url):
        hm = re.match(r"^/([\w.-]+/[\w.-]+)/(?:blob|resolve)/[^/]+/(.+)$", u.path)
        if not hm:
            raise W.WorkflowError("Link the model file itself on Hugging Face (its page has /blob/ in the address), not the repository")
        return await start_hf(hm.group(1), unquote(hm.group(2)), folder or None)
    # any other site: a direct link to the file
    name = Path(unquote(name or u.path.rstrip("/").rsplit("/", 1)[-1])).name
    if not name.lower().endswith(W.MODEL_EXT):
        raise W.WorkflowError("That link doesn't end in a model file name (.safetensors, .gguf, .ckpt …): type the file name, or use a Civitai / Hugging Face link")
    if not folder:
        raise W.WorkflowError("Choose the folder for a link from another site")
    folder = check_folder(folder)
    await asyncio.get_running_loop().run_in_executor(None, W._public_host, u.hostname)
    src = {"url": url}
    await W._check_download(src, name)
    meta = {"source": "link", "page": url, "model": Path(name).stem, "version": "", "type": "", "base": "", "trained_words": [],
            "creator": u.hostname, "description": ""}
    return await download({"url": src["url"], "folder": folder, "name": name, "size": src.get("size"), "meta": meta, "images": []},
                          f"{name} (from {u.hostname})")


# ---------------------------------------------------------------- what's installed

def installed():
    out = []
    for folder in comfy_folders():
        root = W.MODELS / folder
        for f in sorted(root.rglob("*")):
            if not f.is_file() or not f.name.lower().endswith(MODEL_EXT) or ".previews" in f.parts:
                continue
            rel = f.relative_to(root)
            side = f.with_name(f.stem + ".genstudio.json")
            meta = json.loads(side.read_text()) if side.exists() else {}
            preview = next((p for p in f.parent.glob(f.stem + ".preview.*")), None)
            out.append({"folder": folder, "file": str(rel), "size": f.stat().st_size if not f.is_symlink() else 0, "link": f.is_symlink(),
                        "modified": f.stat().st_mtime, "preview": str(preview.relative_to(W.MODELS)) if preview else None,
                        "meta": {k: meta.get(k) for k in ("source", "page", "model", "version", "type", "base", "trained_words", "creator",
                                                          "description", "notes", "previews", "sha256", "license", "nsfw")} if meta else None})
    return out


def remove(folder, file):
    folder = check_folder(folder)
    root = (W.MODELS / folder).resolve()
    f = (root / file).resolve()
    if not f.is_relative_to(root) or not f.is_file() or not f.name.lower().endswith(MODEL_EXT):
        raise W.WorkflowError("No such model file")
    stem = f.stem
    f.unlink()
    for extra in list(f.parent.glob(stem + ".preview.*")) + [f.with_name(stem + ".genstudio.json")]:
        extra.unlink(missing_ok=True)
    prev = root / ".previews" / stem
    if prev.is_dir():
        for p in prev.iterdir():
            p.unlink()
        prev.rmdir()
    if (root / ".previews").is_dir() and not any((root / ".previews").iterdir()):
        (root / ".previews").rmdir()                  # last one gone: no empty folder left behind


# ---------------------------------------------------------------- information for models downloaded elsewhere

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def _known_sha(folder, rel):
    try:
        import ratings
        return (ratings.ratings.get(f"{folder}/{rel}") or {}).get("sha256")
    except Exception:
        return None


async def _civitai_meta(model_id, version_id, sha=None, name=None):
    m = await civitai_model(model_id, nsfw=True)
    v = next((v for v in m["versions"] if v["id"] == int(version_id)), None) or (m["versions"] or [None])[0]
    if not v:
        return None, []
    f = next((f for f in v["files"] if sha and (f.get("sha256") or "").lower() == sha.lower()), None) or \
        next((f for f in v["files"] if f["name"] == name), None) or (v["files"] or [{}])[0]
    meta = {"source": "civitai", "page": f"{m['page']}?modelVersionId={v['id']}", "model": m["name"], "version": v["name"], "type": m["type"],
            "base": v["base"], "trained_words": v["trained_words"], "creator": m["creator"], "description": m["description"],
            "notes": v["notes"], "tags": m["tags"], "sha256": sha or f.get("sha256"), "nsfw": m["nsfw"], "level": m.get("level")}
    return meta, v["images"]


async def fetch_info(folder, rel, hint=None, step=None):
    """Find information and example pictures for an installed model and save them like a Models-page download:
    Civitai by the file's SHA-256 (or the Civitai link it came from), else Hugging Face by exact name and size."""
    f = W.MODELS / folder / rel
    if not f.is_file():
        raise W.WorkflowError(f"{folder}/{rel} isn't installed")
    stem, size = Path(rel).stem, f.stat().st_size
    meta, images = None, []
    url = (hint or {}).get("url") or ""
    vid = re.search(r"civitai\.(?:com|green|red)/api/download/models/(\d+)", url)
    if vid:
        v = await W._json_get(f"https://civitai.com/api/v1/model-versions/{vid.group(1)}", {"User-Agent": "genstudio", **_civitai_headers()})
        if v and v.get("modelId"):
            meta, images = await _civitai_meta(v["modelId"], v["id"], name=Path(rel).name)
    if not meta:
        if step is not None:
            step["detail"] = f"fingerprinting {Path(rel).name}"
        sha = _known_sha(folder, rel) or await asyncio.get_running_loop().run_in_executor(None, _sha256, f)
        v = await W._json_get(f"https://civitai.com/api/v1/model-versions/by-hash/{sha}", {"User-Agent": "genstudio", **_civitai_headers()})
        if v and v.get("modelId"):
            meta, images = await _civitai_meta(v["modelId"], v["id"], sha=sha, name=Path(rel).name)
        else:
            repo = re.match(r"https://huggingface\.co/([\w.-]+/[\w.-]+)/", url)
            repo = repo.group(1) if repo else None
            if not repo:                                    # same file name and size in a Hugging Face repo
                for q in W._queries(Path(rel).name):
                    hit = await W._hf_find(q, Path(rel).name)
                    if hit and (not hit.get("size") or int(hit["size"]) == size):
                        repo = hit["source"].split("· ", 1)[-1]
                        break
            if repo:
                r = await hf_repo(repo)
                meta = {"source": "huggingface", "page": r["page"], "model": repo, "version": rel, "type": "", "base": "", "trained_words": [],
                        "creator": repo.split("/")[0], "description": r["readme"][:4000], "license": r["license"], "tags": r["tags"], "sha256": sha}
                images = r["images"]
            else:
                meta = {"source": "unknown", "sha256": sha, "not_found": True}
    if step is not None:
        step["detail"] = f"saving pictures for {Path(rel).name}"
    previews = await _save_previews(images, folder, stem) if images else []
    meta.update(file=Path(rel).name, folder=folder, size=size, downloaded=f.stat().st_mtime, fetched=time.time(), previews=previews)
    (f.parent / f"{stem}.genstudio.json").write_text(json.dumps(meta, indent=1))
    return meta


def _needs_info(m, force):
    meta = m.get("meta")
    return not m.get("link") and (force or not meta or (meta.get("not_found") and time.time() - (meta.get("fetched") or 0) > 7 * 86400))


def start_fetch_all(force=False, only=None):
    """A Models-page task: look up information for every installed model that has none (or `only` these)."""
    todo = [m for m in installed() if (only and (m["folder"], m["file"]) in only) or (not only and _needs_info(m, force))]
    tid = uuid.uuid4().hex[:10]
    step = {"kind": "info", "label": "Model information", "status": "waiting", "done": 0, "total": len(todo), "detail": f"{len(todo)} models to look up"}
    task = {"id": tid, "label": "Looking up model information", "steps": [step], "status": "running", "started": time.time(), "kind": "info"}
    W.tasks[tid] = task; recent.append(tid); del recent[:-30]

    async def go():
        step["status"] = "running"
        found = missing = 0
        for m in todo:
            try:
                meta = await fetch_info(m["folder"], m["file"], step=step)
                missing += bool(meta.get("not_found")); found += not meta.get("not_found")
            except Exception as e:
                W.log(f"model info: {m['folder']}/{m['file']}: {e}")
                missing += 1
            step["done"] += 1
        step.update(status="done", detail=f"found information for {found} · {missing} not found on Civitai or Hugging Face")
        task.update(status="done", finished=time.time())
    asyncio.create_task(go())
    return task


async def after_workflow_download(folder, name, src):
    """Hook for workflow installs: fetch the model's information right after its download finishes."""
    try:
        await fetch_info(folder, name, hint=src)
    except Exception as e:
        W.log(f"model info for {folder}/{name}: {e}")
