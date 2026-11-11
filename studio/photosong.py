"""📷 Song from photos (Music Studio): photos -> what's in them (Qwen3.8 vision) -> a story -> lyrics -> a private song.

Spec: docs/photo-song-spec.md. Phase 1: ingest (EXIF date, offline place names), per-photo cards, moments and story,
lyrics (action "photos" in app.lyrics_prompt), the song (private to its maker, kept off the radio), a photo as cover.

A draft lives in PHOTOSONGS/<id>/:  draft.json, originals/<n>.<ext>, view/<n>.jpg (1024 px, no metadata), thumb/<n>.jpg.
Privacy: photos, captions, places and lyrics never go to the activity log or an outside service. GPS is turned into a
city name on this machine and the coordinates are not kept.
"""
import base64, hashlib, io, json, os, re, shutil, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app

ROOT = Path(os.environ.get("PHOTOSONG_DATA") or app.DATA.parent / "photosongs").expanduser()
VISION_URL = os.environ.get("PHOTOSONG_VISION", "http://127.0.0.1:11435/api/chat")   # the broker's Ollama proxy (leases)
VISION_MODEL = os.environ.get("PHOTOSONG_VISION_MODEL", "qwen3.8-agent")              # Qwen3.8 27B: vision on the 4090
MAX_PHOTOS, MAX_BYTES, MAX_DRAFT = 30, 25 << 20, 300 << 20
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic", "image/heif": "heic"}
lock = threading.Lock()

OCCASIONS = {
    "anniversary": "an anniversary", "valentines": "Valentine's Day", "proposal": "a proposal", "wedding": "a wedding / first dance",
    "birthday": "their birthday", "just": "just because", "other": "not a love song (a trip, family, friends, a memorial)"}
FEELS = {"tender": "tender and warm", "playful": "playful and light", "epic": "big and sweeping", "bittersweet": "bittersweet",
         "funny": "funny, teasing, affectionate"}
PARTS = ("verse1", "verse2", "chorus", "bridge", "outro")
LOVE_ROLES = {"verse1": "how it started (the earliest moments)", "verse2": "the life built together: trips, home, the small days",
              "chorus": "what they are to the singer: the feeling, carried by one image from the photos (the hook)",
              "bridge": "the turn: a hard time got through, or the promise", "outro": "a last image to end on"}
OTHER_ROLES = {"verse1": "the beginning", "verse2": "what happened next", "chorus": "what it all meant: one image as the hook",
               "bridge": "the turn or the high point", "outro": "a last image to end on"}
COUNTRIES = {"US": "USA", "GB": "UK", "PT": "Portugal", "BR": "Brazil", "ES": "Spain", "FR": "France", "IT": "Italy",
             "DE": "Germany", "MX": "Mexico", "CA": "Canada", "JP": "Japan", "IE": "Ireland", "NL": "Netherlands", "GR": "Greece",
             "AU": "Australia", "NZ": "New Zealand", "CH": "Switzerland", "AT": "Austria", "BE": "Belgium", "CR": "Costa Rica",
             "TH": "Thailand", "IS": "Iceland", "HR": "Croatia", "MA": "Morocco", "AR": "Argentina", "CO": "Colombia", "PE": "Peru",
             "CL": "Chile", "CU": "Cuba", "VN": "Vietnam", "KR": "South Korea", "CN": "China", "IN": "India", "TR": "Turkey",
             "NO": "Norway", "SE": "Sweden", "DK": "Denmark", "FI": "Finland", "PL": "Poland", "CZ": "Czechia", "HU": "Hungary"}


class Denied(app.BadRequest):
    """Not signed in, or not this account's draft (answered as 401 / 404 here, 400 from app.py's routes)."""


# ----------------------------------------------------------------------------- drafts
def _dir(did):
    if not re.fullmatch(r"[0-9a-f]{12}", did or ""):
        raise Denied("No such photo song")
    return ROOT / did


def _load(did):
    f = _dir(did) / "draft.json"
    if not f.is_file():
        raise Denied("No such photo song")
    return json.loads(f.read_text(encoding="utf-8"))


def _save(d):
    d["updated"] = time.time()
    p = _dir(d["id"]); p.mkdir(parents=True, exist_ok=True)
    tmp = p / "draft.json.tmp"
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p / "draft.json")


def _update(did, fn):
    """Load, change and save a draft under the lock (analysis threads and page edits interleave)."""
    with lock:
        d = _load(did)
        out = fn(d)
        _save(d)
        return out if out is not None else d


def owned(did, user):
    d = _load(did)
    if not user or d.get("owner") != user:
        raise Denied("No such photo song")
    return d


def _clean(v, n):
    return re.sub(r"[ \t]+", " ", str(v or "")).strip()[:n]


def clean_answers(a):
    a = a or {}
    out = {"for": _clean(a.get("for"), 60), "from": _clean(a.get("from"), 60), "since": _clean(a.get("since"), 80),
           "include": _clean(a.get("include"), 1500), "leave_out": _clean(a.get("leave_out"), 800), "language": _clean(a.get("language"), 40)}
    out["occasion"] = a.get("occasion") if a.get("occasion") in OCCASIONS else "anniversary"
    out["feel"] = a.get("feel") if a.get("feel") in FEELS else "tender"
    return out


def new_draft(user, answers):
    ROOT.mkdir(parents=True, exist_ok=True)
    d = {"id": uuid.uuid4().hex[:12], "owner": user, "created": time.time(), "answers": clean_answers(answers),
         "photos": [], "moments": [], "story": None, "lyrics": "", "title": "", "style": "", "jobs": []}
    with lock:
        _save(d)
    return d


def view(d):
    """What the page gets: everything but the owner and the original file names."""
    v = {k: d.get(k) for k in ("id", "created", "updated", "answers", "moments", "story", "lyrics", "title", "style", "jobs")}
    v["qa"] = d.get("qa") or []
    v["photos"] = [{k: p.get(k) for k in ("n", "date", "date_src", "place", "kind", "caption", "detail", "star", "skip", "vision_error", "qa")}
                   | {"read": bool(p.get("vision"))} for p in d["photos"]]
    v["occasions"], v["feels"] = OCCASIONS, FEELS
    v["album"] = album_view(d)
    return v


def drafts_of(user):
    out = []
    if not user or not ROOT.is_dir():
        return out
    for f in ROOT.glob("*/draft.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("owner") == user:
            out.append({"id": d["id"], "title": d.get("title") or (d.get("story") or {}).get("title") or "", "photos": len(d["photos"]),
                        "created": d["created"], "updated": d.get("updated"), "jobs": d.get("jobs", []),
                        "cover": next((p["n"] for p in d["photos"] if p.get("star") and not p.get("skip")),
                                      next((p["n"] for p in d["photos"] if not p.get("skip")), None))})
    return sorted(out, key=lambda x: -(x["updated"] or x["created"]))


def delete_draft(d, keep_song):
    """Delete the photos (and, unless keep_song, the songs made from them)."""
    if not keep_song:
        with app.lock:
            mine = [app.jobs[j] for j in d.get("jobs", []) if j in app.jobs]
            if any(j["status"] == "running" for j in mine):
                raise app.BadRequest("A song from these photos is still recording: wait for it or cancel it first")
            for j in mine:
                app.jobs.pop(j["id"], None)
        for j in mine:
            shutil.rmtree(app.DATA / j["id"], ignore_errors=True)
    shutil.rmtree(_dir(d["id"]), ignore_errors=True)
    app.log("photo song deleted", d["id"], "(kept the songs)" if keep_song else "")


# ----------------------------------------------------------------------------- ingest
_rg = None


def _pil():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise app.BadRequest("Pillow isn't installed in the Studio's Python (uv pip install pillow pillow-heif reverse_geocoder)")
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    return Image, ImageOps


def place_name(lat, lon):
    """A city-level name for GPS coordinates, worked out on this machine (GeoNames cities via reverse_geocoder)."""
    global _rg
    try:
        if _rg is None:
            import reverse_geocoder
            _rg = reverse_geocoder
        r = _rg.search([(lat, lon)], mode=1, verbose=False)[0]
    except Exception as e:
        app.log("photo song: place lookup failed", type(e).__name__)
        return None
    city, region, cc = r.get("name"), r.get("admin1"), r.get("cc")
    tail = region if cc == "US" else COUNTRIES.get(cc, cc)
    return ", ".join(x for x in (city, tail) if x) or None


def _gps(exif):
    try:
        g = exif.get_ifd(0x8825)
        if not g or 2 not in g or 4 not in g:
            return None
        deg = lambda v: float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
        lat, lon = deg(g[2]), deg(g[4])
        if str(g.get(1, "N")).upper().startswith("S"):
            lat = -lat
        if str(g.get(3, "E")).upper().startswith("W"):
            lon = -lon
        return (lat, lon) if (lat or lon) and abs(lat) <= 90 and abs(lon) <= 180 else None
    except Exception:
        return None


FNAME_DATE = re.compile(r"(20\d\d|19\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?:[-_ T.]?([01]\d|2[0-3])[-_.]?([0-5]\d)[-_.]?([0-5]\d))?")


def photo_date(exif, filename, file_ms, known=None):
    """(ISO date-time, where it came from): the camera's clock, then Google Photos' date (known), then the file name
    (IMG_20240214_…), then the file's date."""
    try:
        sub = exif.get_ifd(0x8769)
        raw = sub.get(36867) or sub.get(36868) or exif.get(306)
        if raw:
            return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S").isoformat(timespec="minutes"), "camera"
    except Exception:
        pass
    if known:
        return known, "Google Photos"
    m = FNAME_DATE.search(filename or "")
    if m:
        try:
            y, mo, da, h, mi, s = (int(x) if x else 0 for x in m.groups())
            return datetime(y, mo, da, h, mi, s).isoformat(timespec="minutes"), "file name"
        except ValueError:
            pass
    try:
        if file_ms and 0 < float(file_ms) < (time.time() - 600) * 1000:     # a file "made" just now (a download) says nothing
            return datetime.fromtimestamp(float(file_ms) / 1000).isoformat(timespec="minutes"), "file date"
    except (ValueError, OverflowError, OSError):
        pass
    return None, None


def add_photo(did, raw, filename="", ctype="", file_ms=None, known_date=None, source=None):
    Image, ImageOps = _pil()
    if not raw:
        raise app.BadRequest("Empty photo")
    if len(raw) > MAX_BYTES:
        raise app.BadRequest(f"That photo is over {MAX_BYTES >> 20} MB")
    h = hashlib.sha1(raw).hexdigest()
    with lock:
        d = _load(did)
        same = next((p for p in d["photos"] if p["hash"] == h), None)
        if same:
            return same, False
        if len(d["photos"]) >= MAX_PHOTOS:
            raise app.BadRequest(f"Up to {MAX_PHOTOS} photos in one song")
        used = sum(f.stat().st_size for f in _dir(did).rglob("*") if f.is_file())
        if used + len(raw) > MAX_DRAFT:
            raise app.BadRequest("These photos are too big together: leave a few out")
        n = max((p["n"] for p in d["photos"]), default=0) + 1
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        raise app.BadRequest("That file isn't a photo this can read (JPEG, PNG, WebP or HEIC)")
    exif = im.getexif()
    date, src = photo_date(exif, filename, file_ms, known_date)
    gps = _gps(exif)
    camera = _clean(" ".join(str(exif.get(t) or "") for t in (271, 272)), 60)
    im = ImageOps.exif_transpose(im).convert("RGB")
    ext = TYPES.get((ctype or "").split(";")[0].lower()) or (im.format or "jpg").lower().replace("jpeg", "jpg")
    p = _dir(did)
    for sub in ("originals", "view", "thumb"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    (p / "originals" / f"{n}.{re.sub(r'[^a-z0-9]', '', ext)[:5] or 'jpg'}").write_bytes(raw)
    for sub, size, q in (("view", 1024, 86), ("thumb", 320, 80)):
        c = im.copy(); c.thumbnail((size, size))
        c.save(p / sub / f"{n}.jpg", "JPEG", quality=q, optimize=True)     # no exif= : the copies carry no metadata
    photo = {"n": n, "hash": h, "date": date, "date_src": src, "place": place_name(*gps) if gps else None,
             "camera": bool(camera), "size": [im.width, im.height], "kind": "photo", "caption": "", "detail": "",
             "star": False, "skip": False, "vision": None}
    if source:
        photo["source"] = source                    # e.g. the Google Photos item key, so it isn't imported twice

    def put(d):
        if not any(x["hash"] == h for x in d["photos"]):
            d["photos"].append(photo)
            d["moments"] = []                       # the grouping is stale now
    _update(did, put)
    return photo, True


def remove_photo(did, n):
    def drop(d):
        d["photos"] = [p for p in d["photos"] if p["n"] != n]
        d["moments"] = []
    _update(did, drop)
    for f in _dir(did).glob(f"*/{n}.*"):
        f.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- 📁 a Google Photos shared album
# Google's Photos APIs no longer read a user's own albums (only the Picker, which needs an OAuth app), so this reads the
# album's share link the way a browser does: the share page embeds each item's address, size and capture time, and
# "<address>=d" downloads the original file with its metadata (location too, if the owner shares it with the link).
# Unofficial: if Google changes the page, list_album fails with a clear message and uploading still works.
ALBUM_HOSTS = ("photos.app.goo.gl", "photos.google.com", "goo.gl")
MEDIA_HOST = re.compile(r"^https://lh\d\.googleusercontent\.com/")
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


class _GoogleOnly(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to Google Photos itself (a share link can't send us anywhere else)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname or ""
        if urllib.parse.urlsplit(newurl).scheme != "https" or host not in ALBUM_HOSTS:
            raise app.BadRequest("That link doesn't lead to a Google Photos album")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _album_link(url):
    url = str(url or "").strip()
    u = urllib.parse.urlsplit(url)
    ok = u.scheme == "https" and (u.hostname == "photos.app.goo.gl" or (u.hostname == "goo.gl" and u.path.startswith("/photos/"))
                                  or (u.hostname == "photos.google.com" and re.match(r"^/(u/\d+/)?share/", u.path)))
    if not ok:
        raise app.BadRequest("Paste a Google Photos album's share link (photos.app.goo.gl/… or photos.google.com/share/…). "
                             "In Google Photos: open the album → Share → Create link")
    return url


def list_album(url):
    """The album's title and its photos: [{key, url, w, h, date, video}] (the first page: up to a few hundred)."""
    url = _album_link(url)
    opener = urllib.request.build_opener(_GoogleOnly)
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en"})
    try:
        with opener.open(req, timeout=30) as r:
            html = r.read(12 << 20).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise app.BadRequest("Google Photos says that album doesn't exist or isn't shared by link any more" if e.code in (403, 404)
                             else f"Google Photos didn't answer ({e.code}): try again in a minute")
    except urllib.error.URLError:
        raise app.BadRequest("Couldn't reach Google Photos")
    data = None
    for m in re.finditer(r"AF_initDataCallback\(\{key: 'ds:\d+'.*?data:(.*?), sideChannel: \{\}\}\);</script>", html, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, list) and len(d) > 3 and isinstance(d[1], list) and d[1] and isinstance(d[1][0], list):
            data = d
            break
    if data is None:
        if "accounts.google.com" in html[:20000] or "ServiceLogin" in html[:20000]:
            raise app.BadRequest("That album isn't shared by link: in Google Photos, open it → Share → Create link")
        raise app.BadRequest("Couldn't read that album's page (Google may have changed it): download the photos and add them instead")
    items = []
    for e in data[1]:
        try:
            key, det, taken = e[0], e[1], e[2]
            if not (isinstance(key, str) and isinstance(det, list) and isinstance(det[0], str) and MEDIA_HOST.match(det[0])):
                continue
            tz = e[4] if len(e) > 4 and isinstance(e[4], (int, float)) else 0
            date = (datetime.fromtimestamp((taken + tz) / 1000, timezone.utc).replace(tzinfo=None).isoformat(timespec="minutes")
                    if isinstance(taken, (int, float)) else None)        # the capture time, in the place it was taken
            extra = json.dumps(e[-1]) if isinstance(e[-1], dict) else ""
            video = "76647426" in extra or "76647427" in extra            # Google's markers for a video item
            items.append({"key": key[:80], "url": det[0], "w": det[1], "h": det[2], "date": date, "video": video})
        except (IndexError, TypeError, ValueError, OverflowError, OSError):
            continue
    title = ""
    try:
        title = _clean(data[3][1], 120)
    except (IndexError, TypeError):
        pass
    return {"title": title, "items": items}


def open_album(did, url):
    """Remember the album on the draft and return it for the page (thumbnails come straight from Google)."""
    album = list_album(url)
    album["link"] = _album_link(url)
    album["at"] = time.time()
    _update(did, lambda d: d.__setitem__("album", album))
    shutil.rmtree(_dir(did) / "album", ignore_errors=True)        # thumbnails of an album opened before
    return album_view(_load(did))


def album_view(d):
    a = d.get("album")
    if not a:
        return None
    have = {p.get("source") for p in d["photos"]}
    return {"title": a["title"], "count": len(a["items"]),
            "items": [{"key": i["key"], "thumb": f"/api/photosong/{d['id']}/album/thumb/{n}.jpg", "date": i["date"], "video": i["video"],
                       "added": i["key"] in have} for n, i in enumerate(a["items"])]}


def album_thumb(d, n):
    """A small square of album item n, fetched from Google once and kept with the draft (the page only talks to the Studio)."""
    items = (d.get("album") or {}).get("items") or []
    if not 0 <= n < len(items):
        raise Denied("No such photo")
    f = _dir(d["id"]) / "album" / f"{n}.jpg"
    if not f.is_file():
        url = items[n]["url"]
        if not MEDIA_HOST.match(url):
            raise Denied("No such photo")
        with urllib.request.urlopen(urllib.request.Request(url + "=w240-h240-c", headers={"User-Agent": BROWSER_UA}), timeout=30) as r:
            if not MEDIA_HOST.match(r.geturl()) or not r.headers.get("Content-Type", "").startswith("image/"):
                raise Denied("No such photo")
            raw = r.read(4 << 20)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(raw)
    return f.read_bytes()


PICK_GAP = timedelta(hours=1)          # ✨ Pick for me: never two photos taken within this of each other


def spread(items, n, taken=()):
    """✨ Pick for me, from the album's metadata (each photo's capture time) before anything is downloaded:
    - photos taken within an hour of each other count as one moment, and only its middle photo is a candidate;
    - no candidate within an hour of another, or of a photo already in the song (taken: their dates);
    - then up to n of them, spread evenly over the dates.
    Photos with no date are left out: there's no telling when they were taken."""
    when = lambda x: datetime.fromisoformat(x)
    dated = sorted((i for i in items if i.get("date")), key=lambda i: i["date"])
    bursts = []
    for i in dated:
        if bursts and when(i["date"]) - when(bursts[-1][0]["date"]) < PICK_GAP:
            bursts[-1].append(i)
        else:
            bursts.append([i])
    anchors = [when(x) for x in taken if x]
    kept = []
    for b in bursts:
        i = b[len(b) // 2]
        t = when(i["date"])
        if kept and t - when(kept[-1]["date"]) < PICK_GAP:
            continue
        if any(abs(t - a) < PICK_GAP for a in anchors):
            continue
        kept.append(i)
    if len(kept) <= n:
        return kept
    step = len(kept) / n
    return [kept[int(k * step + step / 2)] for k in range(n)]


def import_album(did, keys, emit):
    """Download the chosen originals ("=d": full size, with their metadata) and add them like uploads."""
    d = _load(did)
    a = d.get("album") or {}
    by_key = {i["key"]: i for i in a.get("items", [])}
    have = {p.get("source") for p in d["photos"]}
    if keys == "spread":
        room = MAX_PHOTOS - len(d["photos"])
        pool = [i for i in by_key.values() if not i["video"] and i["key"] not in have]
        chosen = spread(pool, max(0, min(room, 20)), [p.get("date") for p in d["photos"]])
        emit({"picked": len(chosen), "of": len(pool), "undated": sum(1 for i in pool if not i.get("date"))})
    else:
        chosen = [by_key[k] for k in keys if k in by_key and k not in have]
    emit({"todo": len(chosen)})
    added, skipped = 0, []
    for n, it in enumerate(chosen):
        emit({"importing": n + 1, "of": len(chosen)})
        if it["video"]:
            skipped.append("a video"); continue
        try:
            req = urllib.request.Request(it["url"] + "=d", headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                if not MEDIA_HOST.match(r.geturl()):
                    raise ValueError("left Google's photo server")
                ctype = r.headers.get("Content-Type", "")
                if not ctype.startswith("image/"):
                    skipped.append("a video" if ctype.startswith("video/") else "not a photo"); continue
                size = int(r.headers.get("Content-Length") or 0)
                if size > MAX_BYTES:
                    skipped.append(f"over {MAX_BYTES >> 20} MB"); continue
                raw = r.read(MAX_BYTES + 1)
                name = (re.search(r'filename="?([^";]+)', r.headers.get("Content-Disposition", "")) or [None, ""])[1]
            photo, new = add_photo(did, raw, name, ctype, None, known_date=it["date"], source=it["key"])
            added += new
            emit({"added": {k: photo.get(k) for k in ("n", "date", "date_src", "place")}})
        except app.BadRequest as e:
            skipped.append(str(e))
            if str(e).startswith("Up to"):
                break
        except Exception as e:
            skipped.append(f"couldn't download one ({type(e).__name__})")
            app.log("photo song: album download failed", type(e).__name__)
    app.log("photo song: imported", added, "from an album", f"({len(skipped)} skipped)" if skipped else "")
    emit({"done": True, "added": added, "skipped": skipped})


# ----------------------------------------------------------------------------- what's in each photo
VISION_PROMPT = """You are helping someone write a song from their own photos{occasion}. Look at this photo{when}{where}.
Describe what you see, for a songwriter. Strict rules: never guess who anyone is, their name, age, ethnicity or body; say
"two people", "a woman in a green coat", "a child" at most. Pick concrete details someone who was there would remember.
Reply with JSON only, no other text:
{{"scene": "one sentence, 10-25 words: what is happening, where",
 "setting": "a few words", "time_of_day": "morning|day|evening|night|unknown", "season_weather": "a few words or unknown",
 "people": 0, "child": false, "mood": ["one", "two"], "details": ["3 to 5 concrete visual details"],
 "song_detail": "one vivid, singable image from this photo, 6-14 words",
 "kind": "photo|screenshot|document|meme"}}"""


def _vision(d, p):
    a = d["answers"]
    when = ""
    if p.get("date"):
        dt = datetime.fromisoformat(p["date"])
        when = f", taken {dt:%B %Y}" + (f" in the {['night', 'morning', 'afternoon', 'evening'][dt.hour // 6]}" if p["date_src"] == "camera" else "")
    prompt = VISION_PROMPT.format(occasion=f" for {OCCASIONS[a['occasion']]}" if a["occasion"] != "other" else "",
                                  when=when, where=f", in {p['place']}" if p.get("place") else "")
    img = base64.b64encode((_dir(d["id"]) / "view" / f"{p['n']}.jpg").read_bytes()).decode()
    body = {"model": VISION_MODEL, "stream": False, "think": False, "keep_alive": "10m",
            "options": {"temperature": 0.3, "num_predict": 450},          # a hard cap: an open-ended reply can run for minutes
            "messages": [{"role": "user", "content": prompt, "images": [img]}]}
    req = urllib.request.Request(VISION_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:          # waits in the broker's queue while a song records
        text = json.loads(r.read())["message"]["content"]
    import radio_songwriter
    v = radio_songwriter.parse_json_reply(text)
    s = lambda k, n=200: _clean(v.get(k), n)
    out = {"scene": s("scene", 300), "setting": s("setting", 80), "time_of_day": s("time_of_day", 20),
           "season_weather": s("season_weather", 60), "song_detail": s("song_detail", 160),
           "mood": [_clean(m, 30) for m in (v.get("mood") or [])[:4] if m],
           "details": [_clean(m, 80) for m in (v.get("details") or [])[:6] if m],
           "kind": v.get("kind") if v.get("kind") in ("photo", "screenshot", "document", "meme") else "photo",
           "child": bool(v.get("child"))}
    try:
        out["people"] = max(0, min(50, int(v.get("people") or 0)))
    except (TypeError, ValueError):
        out["people"] = 0
    if not out["scene"]:
        raise ValueError("no description in the reply")
    return out


def analyze(did, emit):
    """Read every photo not read yet (one at a time), then group them into moments. emit(dict) streams progress."""
    d = _load(did)
    todo = [p for p in d["photos"] if not p.get("vision")]
    emit({"todo": len(todo), "total": len(d["photos"])})
    t0 = time.time()
    for i, p in enumerate(todo):
        emit({"reading": p["n"], "i": i + 1, "of": len(todo)})
        try:
            v, err = _vision(d, p), None
        except Exception as e:
            v, err = None, f"Couldn't read this photo ({type(e).__name__})"
            app.log("photo song: vision failed", type(e).__name__, str(e)[:120])

        def put(dd, n=p["n"], v=v, err=err):
            for q in dd["photos"]:
                if q["n"] == n:
                    q["vision"], q["vision_error"] = v, err
                    if v:
                        q["caption"] = q.get("caption") or v["scene"]
                        q["detail"] = q.get("detail") or v["song_detail"]
                        q["kind"] = v["kind"]
                        if v["kind"] != "photo":
                            q["skip"] = True                 # screenshots, documents, memes sit out unless put back
                    return q
        q = _update(did, put)
        if q:
            emit({"photo": {k: q.get(k) for k in ("n", "caption", "detail", "kind", "skip", "vision_error")} | {"read": bool(v),
                  "child": bool(v and v.get("child"))}})
    d = _update(did, lambda dd: dd.__setitem__("moments", group_moments(dd["photos"])))
    emit({"moments": d["moments"]})
    app.log("photo song: read", len(todo), "photos", f"{time.time() - t0:.0f}s")
    if todo and any(p.get("vision") for p in d["photos"]):
        emit({"asking": True})
        try:
            d = ask_questions(did)
            emit({"questions": True})
        except Exception as e:                         # the questions are a help, never a blocker
            app.log("photo song: questions failed", type(e).__name__, str(e)[:120])


# ----------------------------------------------------------------------------- 💬 questions about the photos
QUESTIONS_SYSTEM = """You help someone write a song from their own photos. You get the occasion, who the song is for, and
what each photo shows (numbered). Ask them short, warm questions whose answers would make the song personal:
- who the people are, described by how they look or what they're doing ("Who's the woman in the green coat?");
- what was happening, what was said, why that moment mattered;
- what the photo can't show: a song that was playing, a smell, an inside joke, what happened just before or after.
Never guess who anyone is or name anyone. Don't ask what they already told you. 1-2 questions for each photo with
people or a story in it (skip plain ones), each under 15 words. Then 2-4 questions about the whole set, for example
a person who seems to appear in several photos, how it all started, what they love most about the person.
Reply with JSON only: {"photos": [{"n": 1, "questions": ["..."]}], "overall": ["..."]}"""


def _qa_lines(qa, limit=400):
    return [f"{_clean(x['q'], 160)} → {_clean(x['a'], limit)}" for x in (qa or []) if x.get("a", "").strip()]


def ask_questions(did, more=False):
    """Questions for the person about the photos (who's in them, what was happening). Answers already given are kept;
    more=True asks new questions besides the ones there."""
    d = _load(did)
    a = d["answers"]
    use = [p for p in d["photos"] if p.get("vision") and not p.get("skip")]
    if not use:
        raise app.BadRequest("Read the photos first")
    lines = [f"Occasion: {OCCASIONS[a['occasion']]}"]
    if a["for"]:
        lines.append(f"The song is for: {a['for']}")
    if a["since"]:
        lines.append(f"Together / how long: {a['since']}")
    if a["include"]:
        lines.append(f"Already told us: {a['include']}")
    told = _qa_lines(d.get("qa"))
    for p in use:
        v = p["vision"]
        lines.append(f"[{p['n']}] {p.get('date') or 'no date'}" + (f", {p['place']}" if p.get("place") else "") +
                     f": {p.get('caption') or v.get('scene')} (people: {v.get('people', 0)}; detail: {p.get('detail') or v.get('song_detail')})")
        told += [f"photo {p['n']}: " + x for x in _qa_lines(p.get("qa"))]
    if told:
        lines.append("\nAlready answered (don't ask these again):\n" + "\n".join(told[:40]))
    if more:
        lines.append("\nAsk different questions from the ones already asked.")
    import radio_songwriter
    text, label = _chat(QUESTIONS_SYSTEM, "\n".join(lines), max_tokens=1400, temperature=0.6)
    r = radio_songwriter.parse_json_reply(text)
    per = {}
    for x in r.get("photos") or []:
        try:
            per[int(x.get("n"))] = [_clean(q, 160) for q in (x.get("questions") or [])[:2] if _clean(q, 160)]
        except (TypeError, ValueError, AttributeError):
            continue
    overall = [_clean(q, 160) for q in (r.get("overall") or [])[:4] if _clean(q, 160)]

    def merge(old, new):                       # old: what stays (answered ones; with more=True all of them)
        have = {x["q"] for x in old}
        return (old + [{"q": q, "a": ""} for q in new if q not in have])[:6]

    def put(dd):
        for q in dd["photos"]:
            if q["n"] in per:
                q["qa"] = merge(q.get("qa") if more else [x for x in (q.get("qa") or []) if x.get("a", "").strip()], per[q["n"]])
        dd["qa"] = merge(dd.get("qa") if more else [x for x in (dd.get("qa") or []) if x.get("a", "").strip()], overall)
    d = _update(did, put)
    app.log("photo song: questions", f"via {label}", sum(len(p.get("qa") or []) for p in d["photos"]) + len(d.get("qa") or []))
    return d


# ----------------------------------------------------------------------------- moments and the story
def group_moments(photos):
    """Photos (not left out) in date order, split where more than a day and a half passes or the place changes.
    Undated photos come last, in groups of four."""
    use = [p for p in photos if not p.get("skip")]
    dated = sorted((p for p in use if p.get("date")), key=lambda p: p["date"])
    out, cur = [], []
    for p in dated:
        if cur:
            last = cur[-1]
            gap = datetime.fromisoformat(p["date"]) - datetime.fromisoformat(last["date"])
            moved = p.get("place") and last.get("place") and p["place"] != last["place"]
            if gap > timedelta(hours=36) or (moved and gap > timedelta(hours=3)):
                out.append(cur); cur = []
        cur.append(p)
    if cur:
        out.append(cur)
    undated = [p for p in use if not p.get("date")]
    out += [undated[i:i + 4] for i in range(0, len(undated), 4)]
    moments = []
    for i, g in enumerate(out):
        ds = [p["date"] for p in g if p.get("date")]
        places = [p["place"] for p in g if p.get("place")]
        when = None
        if ds:
            a, b = datetime.fromisoformat(ds[0]), datetime.fromisoformat(ds[-1])
            when = f"{a:%B %Y}" if (a.year, a.month) == (b.year, b.month) else f"{a:%b %Y} – {b:%b %Y}"
        moments.append({"id": f"m{i + 1}", "photos": [p["n"] for p in g], "when": when,
                        "where": max(set(places), key=places.count) if places else None, "summary": "", "part": ""})
    return moments


STORY_SYSTEM = """You plan a song from someone's photos. You get the occasion, who it's for, what they want in or out, and the
photos grouped into moments (date, place, what each photo shows, and a detail worth singing). Turn them into a story a
listener could follow, with concrete images from the photos, not generic feelings.
Reply with JSON only:
{"title": "a short song title (2-5 words), ideally from the hook",
 "hook": "the chorus's central image or line, built from one real detail in the photos",
 "moments": [{"id": "m1", "summary": "one sentence: what this moment is, in the singer's words", "part": "verse1|verse2|chorus|bridge|outro"}],
 "lines": ["8 to 14 short, concrete, singable fragments taken from the photo details"],
 "style": "a style line for an AI singer: language, genre, voice, instruments, tempo (15-30 words) that suits the occasion and feel"}
Every moment gets a part; several moments may share one. What the person told us (their answers) matters most: build the
story around it, and prefer their words. Use only what the photos and the person say: never invent names, places or events;
names they gave may be used. Never use anything the person asked to leave out."""


def _story_input(d):
    a = d["answers"]
    roles = OTHER_ROLES if a["occasion"] == "other" else LOVE_ROLES
    by_n = {p["n"]: p for p in d["photos"]}
    lines = [f"Occasion: {OCCASIONS[a['occasion']]}", f"Feel: {FEELS[a['feel']]}"]
    if a["for"]:
        lines.append(f"The song is for: {a['for']}")
    if a["since"]:
        lines.append(f"Together / how long: {a['since']}")
    if a["include"]:
        lines.append(f"They want this in the song (things no photo shows): {a['include']}")
    if a["leave_out"]:
        lines.append(f"NEVER mention: {a['leave_out']}")
    told = _qa_lines(d.get("qa"))
    if told:
        lines.append("What they told us about it all:\n" + "\n".join("  " + x for x in told))
    lines.append("Song parts: " + "; ".join(f"{k} = {v}" for k, v in roles.items()))
    lines.append("\nMoments:")
    for m in d["moments"]:
        lines.append(f"[{m['id']}] {m.get('when') or 'date unknown'}" + (f", {m['where']}" if m.get("where") else ""))
        for n in m["photos"]:
            p = by_n.get(n)
            if p:
                star = " (a favourite)" if p.get("star") else ""
                lines.append(f"  - {p.get('caption') or '(no description)'}{star}" + (f" | detail: {p['detail']}" if p.get("detail") else ""))
                for x in _qa_lines(p.get("qa")):
                    lines.append(f"      they told us: {x}")
    return "\n".join(lines)


def _chat(system, user, max_tokens=1500, temperature=0.7):
    llm = app.pick_llm()
    if llm is None:
        raise app.BadRequest("No writer model is reachable right now: try again in a minute")
    base, model, label = llm
    app.llm_used(model)
    body = {"model": model, "temperature": temperature, "top_p": 0.95, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], **app.LLM_EXTRA}
    req = urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=app.LLM_TIMEOUT) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"], label


def write_story(did):
    d = _load(did)
    if not any(p.get("vision") for p in d["photos"]):
        raise app.BadRequest("Read the photos first")
    if not d["moments"]:
        d = _update(did, lambda dd: dd.__setitem__("moments", group_moments(dd["photos"])))
    if not d["moments"]:
        raise app.BadRequest("Every photo is left out: put at least one back")
    import radio_songwriter
    t0 = time.time()
    text, label = _chat(STORY_SYSTEM, _story_input(d))
    s = radio_songwriter.parse_json_reply(text)
    parts = {m.get("id"): m for m in (s.get("moments") or []) if isinstance(m, dict)}
    story = {"title": _clean(s.get("title"), 80), "hook": _clean(s.get("hook"), 200),
             "lines": [_clean(x, 120) for x in (s.get("lines") or [])[:16] if x], "style": _clean(s.get("style"), 400), "writer": label}

    def put(dd):
        for m in dd["moments"]:
            got = parts.get(m["id"]) or {}
            m["summary"] = _clean(got.get("summary"), 300) or m.get("summary") or ""
            m["part"] = got.get("part") if got.get("part") in PARTS else (m.get("part") or "verse2")
        dd["story"] = story
        dd["title"] = dd.get("title") or story["title"]
        dd["style"] = dd.get("style") or story["style"]
    d = _update(did, put)
    app.log("photo song: story", f"via {label}", f"{time.time() - t0:.1f}s")
    return d


# ----------------------------------------------------------------------------- lyrics (app.lyrics_prompt, action "photos")
def lyrics_prompt(b, user):
    d = owned(str(b.get("photosong") or ""), user)
    st = d.get("story")
    if not st:
        raise app.BadRequest("Write the story first")
    a = d["answers"]
    roles = OTHER_ROLES if a["occasion"] == "other" else LOVE_ROLES
    style = app.clean_text(b.get("style"), "Style", 1500) or d.get("style") or st.get("style")
    lang = app.clean_text(b.get("language"), "Language", 40) or a.get("language")
    parts = [f"Style: {style or 'any'}"]
    if lang:
        parts.append(f"Language: {lang}")
    love = a["occasion"] != "other"
    parts.append(f"A song made from the singer's own photos, for {OCCASIONS[a['occasion']]}. Feel: {FEELS[a['feel']]}.")
    if a["for"] and love:
        parts.append(f"Sing it to {a['for']} (address them as 'you'). Use the name {a['for']} in one or two lines at most "
                     "(a repeated chorus counts once), never forced into a rhyme.")
    elif a["for"]:
        parts.append(f"It's for {a['for']}. Use that name in one or two lines at most.")
    if a["since"]:
        parts.append(f"Background: {a['since']}.")
    title = app.clean_text(b.get("title"), "Title", 120) or d.get("title") or st.get("title")
    if title:
        parts.append(f"Use this title: {title}")
    parts.append(f"The chorus hook: {st.get('hook') or 'one image from the story'}. The chorus carries it and the title.")
    by_part = {}
    for m in d["moments"]:
        if m.get("summary"):
            by_part.setdefault(m.get("part") or "verse2", []).append(m["summary"] + (f" ({m['where']}, {m['when']})" if m.get("where") and m.get("when") else ""))
    parts.append("The story, part by part:")
    for k in PARTS:
        if by_part.get(k):
            parts.append(f"- {k} ({roles[k]}): " + " / ".join(by_part[k]))
    if st.get("lines"):
        parts.append("Real details from the photos to use (at least two in every verse): " + "; ".join(st["lines"]))
    if a["include"]:
        parts.append(f"Must be in the song: {a['include']}")
    told = _qa_lines(d.get("qa"), 300) + [x for p in d["photos"] if not p.get("skip") for x in _qa_lines(p.get("qa"), 300)]
    if told:
        parts.append("What the singer told us (use these real memories; they matter more than the photo descriptions):\n" +
                     "\n".join("- " + x for x in told[:24]))
        parts.append("Names of other people from these answers may appear, once each at most.")
    if a["leave_out"]:
        parts.append(f"Never mention: {a['leave_out']}")
    parts.append("Rules: only names, places and events from above; places may be sung, but no addresses or exact dates. "
                 "Concrete images over abstract words. Every line must make literal sense and follow from the one before: "
                 "no filler lines just to rhyme. Avoid stock phrases such as: " + ", ".join(_cliches()[:24]) + ".")
    structure = app.clean_text(b.get("structure"), "Structure", 300) or \
        ("Verse, Chorus, Verse, Chorus, Bridge, Chorus" if by_part.get("bridge") or love else "Verse, Chorus, Verse, Chorus, Chorus")
    parts.append(f"Structure: {structure}")
    parts.append(f"Length: {app.LENGTHS.get(b.get('length'), app.LENGTHS['medium'])}")
    parts.append(app.RHYMES.get(b.get("rhyme"), app.RHYMES["loose"]))
    if b.get("feedback") and b.get("lyrics"):
        parts.append("Revise these lyrics according to this feedback: " + app.clean_text(b.get("feedback"), "Feedback", 1000) +
                     "\nKeep what already works; return the complete revised song.\nLyrics:\n" + app.clean_text(b.get("lyrics"), "Lyrics", 20000))
    else:
        parts.append("Write the song.")
    return "\n".join(parts)


def _cliches():
    try:
        import radio_songwriter
        return list(radio_songwriter.CLICHES)
    except Exception:
        return []


# ----------------------------------------------------------------------------- the song
def job_extra(h, body):
    """Extra fields for a song from a photo draft (private to its maker), or a new take / rewrite of a private song."""
    user = user_of(h)
    if body.get("photosong"):
        d = owned(str(body["photosong"]), user)
        return {"private": True, "owner": user, "photosong": d["id"]}
    src = app.jobs.get(str(body.get("rewrite_of") or ""))
    if src and src.get("private"):
        if src.get("owner") != user:
            raise Denied("No such song")
        return {"private": True, "owner": user, "photosong": src.get("photosong")}
    return None


def after_jobs(ids, extra):
    """Cover from the draft's starred (or first) photo, square; remember the songs on the draft."""
    did = (extra or {}).get("photosong")
    if not did:
        return
    try:
        d = _load(did)
    except Denied:
        return
    use = [p for p in d["photos"] if not p.get("skip")] or d["photos"]
    pick = next((p for p in use if p.get("star")), use[0] if use else None)
    if pick:
        try:
            Image, _ = _pil()
            im = Image.open(_dir(did) / "view" / f"{pick['n']}.jpg").convert("RGB")
            s = min(im.size)
            im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2)).resize((768, 768))
            for jid in ids:
                im.save(app.DATA / jid / "cover.jpg", "JPEG", quality=88)
        except Exception as e:
            app.log("photo song: cover failed", type(e).__name__)
    _update(did, lambda dd: dd.__setitem__("jobs", (dd.get("jobs") or []) + [i for i in ids if i not in (dd.get("jobs") or [])]))


# ----------------------------------------------------------------------------- privacy helpers (used by app.py and the radio)
def user_of(h):
    import useractivity
    return useractivity.user_of(h)


def visible(job, user):
    return not job.get("private") or (user is not None and job.get("owner") == user)


# ----------------------------------------------------------------------------- HTTP
def _sse(h):
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream"); h.send_header("Cache-Control", "no-store")
    h.end_headers()

    def emit(obj):
        h.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"); h.wfile.flush()
    return emit


def _put(did, user, b):
    owned(did, user)

    def edit(d):
        if "answers" in b:
            d["answers"] = clean_answers(b["answers"])
        for e in b.get("photos") or []:
            p = next((p for p in d["photos"] if p["n"] == e.get("n")), None)
            if not p:
                continue
            if "caption" in e:
                p["caption"] = _clean(e["caption"], 300)
            if "detail" in e:
                p["detail"] = _clean(e["detail"], 160)
            if isinstance(e.get("replies"), list):                  # 💬 answers to the questions, by position
                for x, r in zip(p.get("qa") or [], e["replies"]):
                    x["a"] = _clean(r, 600)
            for k in ("star", "skip"):
                if k in e:
                    if k == "skip" and bool(e[k]) != p.get("skip"):
                        d["moments"] = []                    # left out / back in: group again
                    p[k] = bool(e[k])
        if isinstance(b.get("qa_replies"), list):
            for x, r in zip(d.get("qa") or [], b["qa_replies"]):
                x["a"] = _clean(r, 600)
        if b.get("moments") and d["moments"]:
            got = {m.get("id"): m for m in b["moments"] if isinstance(m, dict)}
            for m in d["moments"]:
                if m["id"] in got:
                    if "summary" in got[m["id"]]:
                        m["summary"] = _clean(got[m["id"]]["summary"], 300)
                    if got[m["id"]].get("part") in PARTS:
                        m["part"] = got[m["id"]]["part"]
        if isinstance(b.get("story"), dict) and d.get("story"):
            for k, n in (("title", 80), ("hook", 200), ("style", 400)):
                if k in b["story"]:
                    d["story"][k] = _clean(b["story"][k], n)
        for k, n in (("title", 120), ("style", 1500), ("lyrics", 20000)):
            if k in b:
                d[k] = str(b[k] or "")[:n]
        if not d["moments"] and any(p.get("vision") for p in d["photos"]):
            d["moments"] = group_moments(d["photos"])
    return view(_update(did, edit))


def handle(h, method, path):
    """/api/photosong/... Returns True when answered."""
    if not path.startswith("/api/photosong"):
        return False
    try:
        user = user_of(h)
        if not user:
            raise Denied("Sign in first: songs from photos are kept private to your account")
        parts = path.strip("/").split("/")[2:]                       # after api/photosong
        if not parts:
            if method == "GET":
                return h.send(200, {"drafts": drafts_of(user)}) or True
            if method == "POST":
                return h.send(200, view(new_draft(user, h.body().get("answers")))) or True
            return False
        did, rest = parts[0], parts[1:]
        if method == "GET" and not rest:
            return h.send(200, view(owned(did, user))) or True
        if method == "GET" and len(rest) == 2 and rest[0] in ("thumb", "view") and re.fullmatch(r"\d+\.jpg", rest[1]):
            owned(did, user)
            f = _dir(did) / rest[0] / rest[1]
            if not f.is_file():
                return h.send(404, {"error": "No such photo"}) or True
            return h.send(200, f.read_bytes(), "image/jpeg", {"Cache-Control": "private, max-age=86400"}) or True
        if method == "PUT" and not rest:
            return h.send(200, _put(did, user, h.body())) or True
        if method == "DELETE" and not rest:
            delete_draft(owned(did, user), "keep_song=1" in (h.path.partition("?")[2]))
            return h.send(200, {"ok": True}) or True
        if method == "POST" and rest == ["photos"]:
            owned(did, user)
            n = int(h.headers.get("Content-Length") or 0)
            if n > MAX_BYTES:
                raise app.BadRequest(f"That photo is over {MAX_BYTES >> 20} MB")
            raw = h.rfile.read(n)
            name = urllib.parse.unquote(h.headers.get("X-Filename") or "")[:200]
            photo, new = add_photo(did, raw, name, h.headers.get("Content-Type"), h.headers.get("X-Last-Modified"))
            return h.send(200, {"photo": {k: photo.get(k) for k in ("n", "date", "date_src", "place", "kind", "caption", "detail", "star", "skip")},
                                "new": new}) or True
        if method == "DELETE" and len(rest) == 2 and rest[0] == "photos" and rest[1].isdigit():
            owned(did, user)
            remove_photo(did, int(rest[1]))
            return h.send(200, {"ok": True}) or True
        if rest[:1] == ["album"]:                                   # 📁 a Google Photos shared album
            d = owned(did, user)
            if method == "GET" and rest == ["album"]:
                return h.send(200, {"album": album_view(d)}) or True
            if method == "GET" and len(rest) == 3 and rest[1] == "thumb" and re.fullmatch(r"\d{1,5}\.jpg", rest[2]):
                try:
                    img = album_thumb(d, int(rest[2][:-4]))
                except (urllib.error.URLError, OSError):
                    return h.send(502, {"error": "Google Photos didn't send that picture"}) or True
                return h.send(200, img, "image/jpeg", {"Cache-Control": "private, max-age=86400"}) or True
            if method == "POST" and rest == ["album"]:
                return h.send(200, {"album": open_album(did, h.body().get("url"))}) or True
            if method == "POST" and rest == ["album", "import"]:
                b = h.body()
                keys = "spread" if b.get("spread") else [str(k) for k in (b.get("keys") or [])][:MAX_PHOTOS]
                if not d.get("album"):
                    raise app.BadRequest("Open the album first")
                emit = _sse(h)
                try:
                    import_album(did, keys, emit)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    app.log("photo song: album import failed", type(e).__name__)
                    try:
                        emit({"error": f"Adding the album's photos failed: {type(e).__name__}"})
                    except Exception:
                        pass
                return True
        if method == "POST" and rest == ["analyze"]:
            owned(did, user)
            emit = _sse(h)
            try:
                analyze(did, emit)
                emit({"done": True})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                app.log("photo song: analyze failed", type(e).__name__)
                try:
                    emit({"error": f"Reading the photos failed: {type(e).__name__}"})
                except Exception:
                    pass
            return True
        if method == "POST" and rest == ["questions"]:
            owned(did, user)
            return h.send(200, view(ask_questions(did, more=bool(h.body().get("more"))))) or True
        if method == "POST" and rest == ["story"]:
            owned(did, user)
            return h.send(200, view(write_story(did))) or True
        return h.send(404, {"error": "not found"}) or True
    except Denied as e:
        return h.send(401 if "Sign in" in str(e) else 404, {"error": str(e)}) or True
    except app.BadRequest as e:
        return h.send(400, {"error": str(e)}) or True
    except json.JSONDecodeError:
        return h.send(400, {"error": "Invalid JSON"}) or True
    except Exception as e:
        app.log("photo song: error", type(e).__name__, str(e)[:160])
        return h.send(500, {"error": f"Something went wrong ({type(e).__name__})"}) or True
