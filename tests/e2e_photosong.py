#!/usr/bin/env python3
"""📷 Song from photos (studio/photosong.py), in-process: no running Studio needed, its own scratch folders, the real
vision and writer models through the GPU broker (about half a minute). Run with the Studio's Python (it needs Pillow,
pillow-heif and reverse_geocoder):

    ~/venv-yue2-rocm/bin/python tests/e2e_photosong.py            # all of it
    ~/venv-yue2-rocm/bin/python tests/e2e_photosong.py --no-models  # privacy and plumbing only (no GPU)

Test photos: a few of the library's AI cover pictures (no real people), given made-up dates and GPS (Lisbon, Big Sur,
Berkeley), one saved as HEIC, one with the date only in its file name, one with no metadata (as WhatsApp sends them).
Accounts are stand-ins ("jt-test", "someone-else"): nothing touches Home's accounts or sessions.
"""
import glob, io, json, os, sys, tempfile, time
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
MODELS = "--no-models" not in sys.argv
S = tempfile.mkdtemp(prefix="photosong-test-")

def make_photos():
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational
    import pillow_heif; pillow_heif.register_heif_opener()
    covers = sorted(glob.glob(str(REPO / "library" / "*" / "cover.jpg")))[-7:]
    if len(covers) < 7:
        sys.exit("needs 7 songs with cover art in library/")
    plan = [("2024:02:14 20:10:00", (38.7139, -9.1394)), ("2024:02:15 11:30:00", (38.7139, -9.1394)),
            ("2024:08:03 17:45:00", (36.2704, -121.8081)), ("2024:08:04 09:05:00", (36.2704, -121.8081)),
            ("2024:12:24 21:00:00", (37.8716, -122.2727)), None, None]
    def dms(x):
        x = abs(x); d = int(x); m = int((x - d) * 60); s = round(((x - d) * 60 - m) * 60, 2)
        return (IFDRational(d, 1), IFDRational(m, 1), IFDRational(int(s * 100), 100))
    os.makedirs(f"{S}/photos")
    for i, (c, p) in enumerate(zip(covers, plan)):
        im = Image.open(c).convert("RGB")
        if p:
            ex = Image.Exif(); ex[271] = "Apple"; ex[272] = "iPhone 15"; ex.get_ifd(0x8769)[36867] = p[0]
            lat, lon = p[1]; g = ex.get_ifd(0x8825)
            g[1] = "N" if lat >= 0 else "S"; g[2] = dms(lat); g[3] = "E" if lon >= 0 else "W"; g[4] = dms(lon)
            im.save(f"{S}/photos/IMG_{i}.{'heic' if i == 2 else 'jpg'}", exif=ex.tobytes())
        elif i == 5:
            im.save(f"{S}/photos/PXL_20250301_143000123.png")
        else:
            im.save(f"{S}/photos/IMG-WA0007.jpg", quality=80)
os.environ["STUDIO_DATA"] = f"{S}/lib"; os.environ["PHOTOSONG_DATA"] = f"{S}/ps"
os.environ.setdefault("STUDIO_LYRICS_LLMS", "http://127.0.0.1:11435/v1|qwen3.8-agent|Qwen 3.8 27B (4090 shared with Hermes)")
os.environ.setdefault("STUDIO_LLM_EXTRA", '{"reasoning_effort": "none"}')
sys.path.insert(0, str(REPO / "studio"))
import app; sys.modules["app"] = app
import photosong
photosong.user_of = lambda h: h.user
import useractivity; useractivity.note = lambda h, code: None

class H(app.Handler):
    def __init__(self, user, method, path, body=b"", headers=None):
        self.user, self.command, self.path = user, method, path
        self.headers = {"Content-Length": str(len(body))} | (headers or {})
        self.rfile, self.wfile = io.BytesIO(body), io.BytesIO()
        self._noted, self._json, self.code, self.out = True, None, None, None
    def send(self, code, body, ctype="application/json", extra=None):
        self.code, self.out = code, body
    def send_response(self, code): self.code = code
    def send_header(self, *a): pass
    def end_headers(self): pass
    def events(self):
        return [json.loads(l[6:]) for l in self.wfile.getvalue().decode().split("\n\n") if l.startswith("data: ")]

def call(user, method, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    h = H(user, method, path, data, headers)
    getattr(h, "do_" + method)()
    return h

def ok(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if cond: ok.passed += 1
    else: ok.failed += 1
ok.failed = ok.passed = 0

make_photos()
A = {"for": "Ana", "occasion": "anniversary", "feel": "tender", "since": "met in Lisbon, Feb 2024",
     "include": "she calls me 'bear'; we missed the last tram", "leave_out": "work"}
# --- anonymous / wrong user
ok(call(None, "GET", "/api/photosong").code == 401, "not signed in -> 401")
h = call("jt-test", "POST", "/api/photosong", {"answers": A}); did = h.out["id"]
ok(h.code == 200 and h.out["answers"]["for"] == "Ana", "new draft")
ok(call("someone-else", "GET", f"/api/photosong/{did}").code == 404, "another account can't open it")
# --- uploads
for f in sorted(os.listdir(f"{S}/photos")):
    ct = {"jpg": "image/jpeg", "png": "image/png", "heic": "image/heic"}[f.rsplit(".", 1)[1]]
    h = call("jt-test", "POST", f"/api/photosong/{did}/photos", raw=open(f"{S}/photos/{f}", "rb").read(),
             headers={"Content-Type": ct, "X-Filename": f, "X-Last-Modified": str(int(time.time() * 1000))})
    p = h.out.get("photo", {})
    print(f"   {f}: {h.code} n={p.get('n')} date={p.get('date')} ({p.get('date_src')}) place={p.get('place')}")
h = call("jt-test", "POST", f"/api/photosong/{did}/photos", raw=open(f"{S}/photos/IMG_0.jpg", "rb").read(), headers={"Content-Type": "image/jpeg"})
ok(h.out.get("new") is False, "the same photo twice is added once")
ok(call("jt-test", "POST", f"/api/photosong/{did}/photos", raw=b"not an image", headers={"Content-Type": "image/jpeg"}).code == 400, "a non-image is refused")
from PIL import Image
v = Image.open(f"{S}/ps/{did}/view/1.jpg")
ok(not v.getexif() and v.size[0] <= 1024, "view copy has no metadata and is <=1024px")
d = json.load(open(f"{S}/ps/{did}/draft.json"))
ok("gps" not in json.dumps(d).lower() and "38.71" not in json.dumps(d), "no coordinates kept in the draft")
ok(call("someone-else", "GET", f"/api/photosong/{did}/thumb/1.jpg").code == 404, "another account can't see the thumbnails")
# --- read photos (real vision via the broker)
if not MODELS:
    from photosong import group_moments
    def _fake(d):
        for p in d["photos"]:
            p["vision"] = {"scene": "a test photo", "song_detail": "a test detail", "kind": "photo"}; p["caption"] = "a test photo"; p["detail"] = "a test detail"
        d["moments"] = group_moments(d["photos"])
        for m in d["moments"]: m["summary"], m["part"] = "a moment", "verse1"
        d["story"] = {"title": "Test", "hook": "a hook", "lines": [], "style": "acoustic", "writer": "none"}
    photosong._update(did, _fake)
if MODELS:
    t = time.time(); h = call("jt-test", "POST", f"/api/photosong/{did}/analyze", {})
    ev = h.events(); photos = [e["photo"] for e in ev if "photo" in e]; moments = next((e["moments"] for e in ev if "moments" in e), [])
    print(f"   read {len(photos)} photos in {time.time() - t:.0f}s; errors: {[e for e in ev if 'error' in e]}")
    for p in photos: print(f"   #{p['n']} [{p['kind']}{' child' if p.get('child') else ''}] {p['caption']} | {p['detail']}")
    for m in moments: print(f"   moment {m['id']}: {m['when']} {m['where']} photos {m['photos']}")
    ok(len(photos) == 7 and all(p["read"] for p in photos), "every photo read")
    ok(len(moments) >= 3, "photos grouped into moments")
    # --- story
    t = time.time(); h = call("jt-test", "POST", f"/api/photosong/{did}/story", {})
    st = h.out.get("story") or {}
    print(f"   story ({time.time() - t:.0f}s, {st.get('writer')}): title={st.get('title')!r} hook={st.get('hook')!r}\n   style={st.get('style')!r}")
    for m in h.out.get("moments", []): print(f"   {m['id']} -> {m['part']}: {m['summary']}")
    print("   lines:", st.get("lines"))
    ok(h.code == 200 and st.get("title") and all(m["part"] for m in h.out["moments"]), "story written, every moment has a part")
# --- edits
h = call("jt-test", "PUT", f"/api/photosong/{did}", {"photos": [{"n": 1, "star": True}]})
ok(h.code == 200 and h.out["photos"][0]["star"], "star a photo")
# --- lyrics
done = None
if MODELS:
    h = call("jt-test", "POST", "/api/lyrics", {"action": "photos", "photosong": did, "style": st.get("style") or "acoustic ballad"})
    done = next((e for e in h.events() if e.get("done")), None)
    ok(done and done["lyrics"].count("[") >= 4, "lyrics written")
    if done:
        print(f"   lyrics by {done['source']} in {done['seconds']}s, title {done['title']!r}\n" + "\n".join("     " + l for l in done["lyrics"].splitlines()))
        import radio_songwriter
        print("   cliché hits:", radio_songwriter.cliche_hits(done["lyrics"]), "| 'Ana' x", done["lyrics"].count("Ana"), "| 'work':", "work" in done["lyrics"].lower())
ok(call("someone-else", "POST", "/api/lyrics", {"action": "photos", "photosong": did}).code == 400, "another account can't write lyrics from it")
# --- the song (queued in this process only)
body = {"style": "acoustic ballad", "lyrics": (done or {}).get("lyrics") or "[Verse]\nla", "engine": "minimax", "takes": 2, "photosong": did}
ok(call("someone-else", "POST", "/api/jobs", body).code == 400, "another account can't make a song from it")
h = call("jt-test", "POST", "/api/jobs", body); ids = h.out.get("ids", [])
ok(h.code == 200 and len(ids) == 2, "2 takes queued")
j = app.jobs[ids[0]]
ok(j.get("private") and j.get("owner") == "jt-test" and j.get("photosong") == did, "song is private to its maker")
ok((app.DATA / ids[0] / "cover.jpg").is_file(), "cover from the starred photo")
ok(all(x["id"] not in ids for x in app.state(None)["queue"]) and all(x["id"] not in ids for x in app.state("someone-else")["queue"]), "hidden from others in /api/state")
ok(any(x["id"] == ids[0] and x.get("private") for x in app.state("jt-test")["queue"]), "shown to its maker, marked private")
ok(call("someone-else", "GET", f"/api/jobs/{ids[0]}").code == 404 and call(None, "GET", f"/files/{ids[0]}/cover.jpg").code == 404, "others get 404 for the song and its files")
ok(call("jt-test", "GET", f"/api/jobs/{ids[0]}").code == 200, "its maker can open it")
h = call("jt-test", "POST", "/api/jobs", {"style": "x", "lyrics": "[Verse]\nla", "rewrite_of": ids[0]})
ok(app.jobs[h.out["ids"][0]].get("private"), "a rewrite of a private song stays private")
import radio_library, radio_broadcast
j["status"] = "done"
try:
    radio_broadcast.play_next([ids[0]]); ok(False, "radio refuses to queue it")
except app.BadRequest: ok(True, "radio refuses to queue it")
try:
    radio_library.set_playlist(ids[0], True); ok(False, "radio playlist refuses it")
except app.BadRequest: ok(True, "radio playlist refuses it")
# --- delete
for i in ids: app.jobs[i]["status"] = "done"
ok(call("jt-test", "DELETE", f"/api/photosong/{did}?keep_song=1").code == 200 and not (app.DATA.parent / "ps" / did).exists() and ids[0] in app.jobs,
   "delete the photos, keep the songs")
import shutil; shutil.rmtree(S, ignore_errors=True)
print(f"{ok.passed} passed, {ok.failed} failed")
sys.exit(1 if ok.failed else 0)
