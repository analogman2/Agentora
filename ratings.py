"""PG-13 gate for non-admin users: a rating for every content model (LoRAs, checkpoints, diffusion models).

Ratings come from, in order: an admin's choice; Civitai's rating for that exact file (looked up by its SHA-256);
an obviously adult filename. Anything else is "unknown", and non-admins can't use it until an admin rates it.
Text encoders, VAEs, upscalers and the like don't decide what a picture shows, so they're always allowed.

Civitai's nsfwLevel is a set of flags: 1 PG, 2 PG-13, 4 R, 8 X, 16 XXX. PG-13 and below = no flag above 2.
ratings.json {"<folder>/<file>": {"rating": "pg13"|"adult"|"unknown", "by", "level", "size", "mtime", "sha256"}}
"""
import asyncio, hashlib, json, re, threading, time
from pathlib import Path

import workflows as W

CONTENT = ("loras", "checkpoints", "diffusion_models", "unet", "embeddings", "lycoris")
BUILTIN = {"z_image_turbo_bf16.safetensors", "wan2.2_ti2v_5B_fp16.safetensors"}   # the Create panel's own models
PG13_BITS = 1 | 2
ADULT_SUBSTR = ("porn", "hentai", "nsfw", "xxx", "nude", "naked", "nudity", "erotic", "lewd", "deepthroat", "ahegao", "uncensor", "nipple")
ADULT_TOKENS = {"ass", "sex", "sexy", "cum", "boobs", "boob", "tits", "butt", "futa", "bdsm", "nud3", "nud", "unlocked", "jiggle",
                "thighs", "breasts", "pussy", "penis", "lingerie", "fetish", "onlyfans", "bikini"}
FILE = Path(__file__).resolve().parent / "ratings.json"
lock = threading.Lock()
ratings, state = {}, {"running": False, "checked": 0, "last": 0}


def load():
    if FILE.exists():
        ratings.update(json.loads(FILE.read_text()))


def _save():
    with lock:
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(ratings, indent=1))
        tmp.replace(FILE)


def adult_name(name):
    low = Path(name).stem.lower()
    toks = set(re.split(r"[_\-. ()\[\]]+", low))
    return any(s in low for s in ADULT_SUBSTR) or bool(toks & ADULT_TOKENS)


def content_files():
    for folder in CONTENT:
        root = W.MODELS / folder
        if root.exists():
            for f in root.rglob("*"):
                if f.is_file() and f.name.lower().endswith(W.MODEL_EXT) and ".previews" not in f.parts:
                    yield folder, str(f.relative_to(root)), f


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def _sidecar_sha(f):
    side = f.with_name(f.stem + ".genstudio.json")
    try:
        return (json.loads(side.read_text()) or {}).get("sha256") if side.exists() else None
    except ValueError:
        return None


async def rate_file(folder, rel, f):
    key = f"{folder}/{rel}"
    st = f.stat()
    old = ratings.get(key) or {}
    if old.get("by") == "admin" or (old.get("size") == st.st_size and old.get("mtime") == int(st.st_mtime) and old.get("by") != "pending"):
        return old
    if Path(rel).name in BUILTIN:
        entry = {"rating": "pg13", "by": "built-in"}
    else:
        sha = old.get("sha256") if old.get("size") == st.st_size else None
        sha = sha or _sidecar_sha(f) or await asyncio.get_running_loop().run_in_executor(None, _sha256, f)
        entry = {"rating": "unknown", "by": "nobody yet", "sha256": sha}
        v = await W._json_get(f"https://civitai.com/api/v1/model-versions/by-hash/{sha}", {"User-Agent": "genstudio", **W._civitai_headers()})
        if v and v.get("nsfwLevel") is not None:
            level = int(v.get("nsfwLevel") or 0)
            ok = level and not (level & ~PG13_BITS) and not (v.get("model") or {}).get("nsfw")
            entry.update(rating="pg13" if ok else "adult", by="civitai", level=level,
                         civitai=f"https://civitai.com/models/{v.get('modelId')}?modelVersionId={v.get('id')}")
        elif adult_name(rel):
            entry.update(rating="adult", by="name")
        await asyncio.sleep(0.3)                          # be gentle with Civitai
    entry.update(size=st.st_size, mtime=int(st.st_mtime))
    with lock:
        ratings[key] = entry
    return entry


async def refresh():
    """Rate every content model that's new or changed (hashing big files takes a while the first time)."""
    if state["running"]:
        return
    state["running"] = True
    try:
        seen = set()
        for folder, rel, f in list(content_files()):
            seen.add(f"{folder}/{rel}")
            try:
                await rate_file(folder, rel, f)
            except Exception as e:
                W.log(f"ratings: {folder}/{rel}: {e}")
            state["checked"] += 1
            if state["checked"] % 10 == 0:
                _save()
        with lock:
            for k in [k for k, v in ratings.items() if k not in seen and v.get("by") != "admin"]:
                ratings.pop(k)
        _save()
        state["last"] = time.time()
    finally:
        state["running"] = False


def set_rating(folder, rel, rating):
    if rating not in ("pg13", "adult", "auto"):
        raise W.WorkflowError("rating must be pg13, adult or auto")
    key = f"{folder}/{rel}"
    with lock:
        if rating == "auto":
            ratings.pop(key, None)                         # re-checked on the next refresh
        else:
            ratings[key] = {**ratings.get(key, {}), "rating": rating, "by": "admin"}
    _save()


def rating_of(folder, rel):
    if folder not in CONTENT:
        return "pg13"
    if Path(rel).name in BUILTIN:
        return "pg13"
    return (ratings.get(f"{folder}/{rel}") or {}).get("rating", "unknown")


def name_allowed(name):
    """A model file named in a workflow (any content folder): allowed for non-admins only if every copy is PG-13.
    Files outside the content folders (text encoders, VAEs...) are always allowed."""
    base = Path(str(name).replace("\\", "/")).name
    if base in BUILTIN:
        return True
    hits = [v for k, v in ratings.items() if Path(k).name == base]
    if hits:
        return all(v.get("rating") == "pg13" for v in hits)
    in_content = any((W.MODELS / folder).exists() and any((W.MODELS / folder).rglob(base)) for folder in CONTENT)
    return not in_content                                  # a content model nobody has rated yet: not for non-admins
