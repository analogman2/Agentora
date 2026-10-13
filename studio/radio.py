"""Agentora: infinite AI radio stations for the Music Studio (inspired by CharlesMod/infinite-tapedeck).

Stations are text-defined ("late-night lo-fi") or learned from a folder of songs.
While the radio is on, a spool manager keeps a few approved songs ready per
station: an LLM writes each song's spec (title, style, lyrics) from the station
description, its learned profile and listener feedback; YuE2 renders it through
the studio's GPU pool; a CLAP critic scores the take against the station (its
songs, or its description) and drops the weakest. Keep / skip / dislike feedback
steers both the prompts and the critic.

Everything runs on the CPU here (CLAP, librosa); the GPUs stay with YuE2.
"""
import collections, json, os, queue, random, re, shutil, subprocess, sys, threading, time, traceback, urllib.request, uuid
from pathlib import Path

import numpy as np

import art                          # cover art for stations and songs (drawn by ComfyUI between songs)

app = None                          # the studio module, set by init()
RADIO = None
lock = threading.RLock()
stations: dict[str, dict] = {}
state = {"on": False, "station": None, "target_ahead": 4, "max_inflight": 2}
RERUNS = "__reruns__"               # pseudo-station: replays songs from every station, generates nothing
TAKES = int(os.environ.get("RADIO_TAKES", "2"))   # takes rendered per song; the critic airs the best one
# tunables below are defaults; the ⚙ Settings dialog (settings.py) changes them live
MAX_SECONDS, OPENER_SECONDS = 300, 150   # length caps (safety only: songs should end on their own)
INSTRUMENTAL_SHARE = 0.4                 # 'mix' stations: share of instrumentals
KEEP_TEXT, KEEP_LEARNED = 0.85, 0.7      # critic: share of takes kept once a station has WARMUP_SCORES scores
WARMUP_SCORES, WARMUP_FLOOR = 4, 0.75    # before that: drop takes under WARMUP_FLOOR x the station's median
MIN_SONG_SECONDS = 30                    # shorter takes are broken
SPEC_TEMPERATURE = 1.0                   # songwriter creativity
# broadcast sound (applied per song by the ffmpeg encoder; see audio_filters)
BITRATE = 192                            # kbit/s; a change takes effect at the next song (listeners reconnect)
LOUDNESS = None                          # target LUFS for loudness evening (EBU R128), None = off
BASS_DB = TREBLE_DB = 0.0                # tone controls
STEREO_WIDTH = 1.0                       # 0 = mono, 1 = as recorded, 2 = extra wide
COMPRESSION = "off"                      # off | light | medium | heavy
LIMITER = True                           # stop clipping
FADE_IN, FADE_OUT, GAP = 0.0, 0.0, 0.0   # seconds
# music model (YuE2) for radio songs; None = the model's own default
ODE_STEPS, CFG_SCALE = 32, None
SONG_SAMPLING, SCORE_SAMPLING = {}, {}   # temperature / top_p / top_k / repetition_penalty overrides


def generating():
    """True while the radio is on a real station (and so wants the GPUs); rerun mode leaves them to the LLM."""
    return state["on"] and state["station"] in stations
writing = {}                        # station id -> number of specs being written right now
requested = {}                      # station id -> specs being written because someone pressed "Create a song now"
backoff = {}                        # station id -> {"until": t, "delay": s, "reason": str}
AUDIO_EXT = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus"}
KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# CLAP tag vocabulary: turns a folder of songs into words for the style prompt.
TAGS = {
    "genre": ["pop", "rock", "indie rock", "alternative rock", "punk", "metal", "folk", "country", "blues", "jazz", "soul",
              "R&B", "funk", "disco", "hip hop", "rap", "trap", "lo-fi hip hop", "electronic", "house", "techno", "trance",
              "drum and bass", "dubstep", "synthwave", "ambient", "downtempo", "trip hop", "classical", "orchestral",
              "cinematic soundtrack", "piano ballad", "reggae", "latin", "bossa nova", "k-pop", "j-pop", "gospel", "chiptune"],
    "mood": ["happy", "uplifting", "energetic", "aggressive", "dark", "melancholic", "sad", "romantic", "calm", "relaxing",
             "dreamy", "nostalgic", "epic", "playful", "mysterious", "hopeful", "angry", "peaceful"],
    "instrument": ["acoustic guitar", "electric guitar", "distorted guitar", "piano", "electric piano", "synthesizer",
                   "synth pads", "strings", "violin", "cello", "brass", "saxophone", "trumpet", "flute", "bass guitar",
                   "808 bass", "drum machine", "live drums", "organ", "harp", "ukulele", "banjo"],
}
TEMPLATES = {"genre": ["This is a {} song.", "{} music", "a {} track"],
             "mood": ["{} music", "a {} song", "music that feels {}"],
             "instrument": ["music featuring {}", "a song with {}", "{} playing"]}
VOCAL = ["a song with a singer singing lyrics", "vocals singing a melody", "a pop song with lead vocals", "a person singing"]
INSTRUMENTAL = ["an instrumental track with no vocals", "instrumental music without singing", "background music with no voice", "an instrumental piece"]
FEMALE, MALE = ["a female singer", "a woman singing"], ["a male singer", "a man singing"]


def init(studio_module):
    global app, RADIO
    app = studio_module
    art.init(sys.modules[__name__])
    RADIO = app.DATA.parent / "radio"
    (RADIO / "stations").mkdir(parents=True, exist_ok=True)
    try:
        state.update(json.loads((RADIO / "state.json").read_text()))
    except Exception:
        pass
    if os.environ.get("RADIO_AHEAD"):                 # songs to keep ready or in the making (overrides the saved value)
        state["target_ahead"] = int(os.environ["RADIO_AHEAD"])
    import settings
    settings.init(app, sys.modules[__name__])        # saved ⚙ settings win over env vars and built-ins
    # resume only after a quick service restart (studio was alive < 2 min ago); after a reboot the radio starts off
    state["on"] = bool(state.get("on")) and time.time() - (state.get("heartbeat") or 0) < 120 and state.get("station") is not None
    if state["on"]:
        app.log("radio: resuming the broadcast after a quick restart")
    for f in (RADIO / "stations").glob("*/station.json"):
        try:
            s = json.loads(f.read_text())
            if s.get("analysis", {}).get("status") == "running":
                s["analysis"]["status"] = "interrupted"
            stations[s["id"]] = s
        except Exception:
            pass
    if not stations:
        for name, desc, vocals in [
                ("Late Night Lo-fi", "Mellow late-night lo-fi hip hop and chillhop: dusty piano, warm Rhodes, soft boom-bap drums, rain on the window, mostly instrumental with the occasional soft vocal.", "mix"),
                ("Indie Road Trip", "Sunny indie rock and indie folk for driving with the windows down: jangly guitars, big singalong choruses, male and female vocals, nostalgic and hopeful.", "vocals"),
                ("Neon Nights", "80s-inspired synthwave and synth-pop: analog synths, gated drums, arpeggiated bass, dreamy vocals, neon city at night.", "mix")]:
            create_station({"name": name, "kind": "text", "description": desc, "vocals": vocals})
    save_state()
    threading.Thread(target=spool_loop, daemon=True, name="radio-spool").start()
    threading.Thread(target=bank_loop, daemon=True, name="radio-lyricist").start()
    threading.Thread(target=playout_loop, daemon=True, name="radio-playout").start()


def save_state():
    state["heartbeat"] = time.time()
    (RADIO / "state.json").write_text(json.dumps({k: state.get(k) for k in ("station", "target_ahead", "on", "heartbeat", "upnext")}))


def sdir(sid):
    return RADIO / "stations" / sid


def save_station(s):
    d = sdir(s["id"]); d.mkdir(parents=True, exist_ok=True)
    tmp = d / "station.json.tmp"; tmp.write_text(json.dumps(s, indent=1)); tmp.replace(d / "station.json")


def create_station(body):
    name = app.clean_text(body.get("name"), "Station name", 60, required=True)
    kind = body.get("kind", "text")
    if kind not in ("text", "folder"):
        raise app.BadRequest("kind must be text or folder")
    desc = app.clean_text(body.get("description"), "Description", 2000, required=(kind == "text"))
    vocals = body.get("vocals", "mix")
    if vocals not in ("vocals", "instrumental", "mix"):
        raise app.BadRequest("vocals must be vocals, instrumental or mix")
    s = {"id": uuid.uuid4().hex[:10], "name": name, "kind": kind, "description": desc, "vocals": vocals,
         "language": app.clean_text(body.get("language"), "Language", 40),
         "theme": app.clean_text(body.get("theme"), "Lyrics theme", 1000), "created": time.time(),
         "stats": {"generated": 0, "approved": 0, "rejected": 0, "played": 0, "kept": 0, "skipped": 0, "disliked": 0},
         "feedback": [], "scores": [], "analysis": {"status": "none" if kind == "folder" else "n/a"}, "profile": None}
    with lock:
        stations[s["id"]] = s
        save_station(s)
    if kind == "folder":
        (sdir(s["id"]) / "music").mkdir(parents=True, exist_ok=True)
    return s


def delete_station(sid):
    with lock:
        s = stations.pop(sid, None)
        if state["station"] == sid:
            state["station"] = None; state["on"] = False
    if s:
        with app.lock:
            for j in list(app.jobs.values()):
                if j.get("station") == sid:
                    if j["status"] == "queued":
                        j.update(status="cancelled", finished=time.time())
                    elif j["status"] == "running":
                        j["_cancel"] = True
        shutil.rmtree(sdir(sid), ignore_errors=True)
    save_state()


# ----------------------------------------------------------------------------- CLAP + listener (CPU)

_clap = {"model": None, "proc": None, "lock": threading.Lock()}


def clap():
    with _clap["lock"]:
        if _clap["model"] is None:
            import torch
            from transformers import ClapModel, ClapProcessor
            torch.set_num_threads(8)
            _clap["model"] = ClapModel.from_pretrained("laion/clap-htsat-unfused").eval()
            _clap["proc"] = ClapProcessor.from_pretrained("laion/clap-htsat-unfused")
        return _clap["model"], _clap["proc"]


def _norm(x):
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-9)


def audio_embedding(path, max_windows=6):
    """Mean of CLAP embeddings over up to 6 x 10 s windows from the middle of the track; also returns the mono audio."""
    import librosa, torch
    y, sr = librosa.load(str(path), sr=48000, mono=True)
    n = 480000
    if len(y) < n:
        y = np.pad(y, (0, n - len(y)))
    starts = list(range(0, len(y) - n + 1, n))
    if len(starts) > max_windows:
        mid = len(starts) // 2; starts = starts[max(0, mid - max_windows // 2):][:max_windows]
    model, proc = clap()
    with _clap["lock"], torch.inference_mode():
        a = model.get_audio_features(**proc(audio=[y[s:s + n] for s in starts], sampling_rate=48000, return_tensors="pt"))
    return _norm(_norm(a.numpy()).mean(0)), y


_text_cache = {}


def text_embeddings(texts):
    import torch
    missing = [t for t in texts if t not in _text_cache]
    if missing:
        model, proc = clap()
        with _clap["lock"], torch.inference_mode():
            e = model.get_text_features(**proc(text=missing, return_tensors="pt", padding=True)).numpy()
        for t, v in zip(missing, _norm(e)):
            _text_cache[t] = v
    return np.stack([_text_cache[t] for t in texts])


def features(y):
    import librosa
    y22 = librosa.resample(y, orig_sr=48000, target_sr=22050)
    tempo, _ = librosa.beat.beat_track(y=y22, sr=22050)
    tempo = float(np.atleast_1d(tempo)[0])
    while tempo > 150: tempo /= 2          # librosa often reports double time
    while tempo and tempo < 60: tempo *= 2
    chroma = librosa.feature.chroma_cqt(y=y22, sr=22050).mean(1)
    rms = librosa.feature.rms(y=y22)[0]
    cent = librosa.feature.spectral_centroid(y=y22, sr=22050)[0]
    return {"bpm": round(tempo), "key": KEYS[int(chroma.argmax())], "energy": float(np.sqrt(np.mean(rms ** 2))),
            "brightness": float(np.median(cent)), "seconds": round(len(y) / 48000, 1)}


def tag_scores(emb):
    """Prompt-ensembled CLAP similarities, centred within each category (removes per-category text bias)."""
    out = {}
    for cat, words in TAGS.items():
        sims = np.mean([text_embeddings([t.format(w) for w in words]) @ emb for t in TEMPLATES[cat]], axis=0)
        out[cat] = dict(zip(words, (sims - sims.mean()).tolist()))
    vocal_margin = float((text_embeddings(VOCAL) @ emb).mean() - (text_embeddings(INSTRUMENTAL) @ emb).mean())
    out["p_vocal"] = float(1 / (1 + np.exp(-(vocal_margin + 0.04) * 60)))   # calibrated on known vocal/instrumental takes
    out["female_margin"] = float((text_embeddings(FEMALE) @ emb).mean() - (text_embeddings(MALE) @ emb).mean())
    return out


def analyze_station(sid):
    s = stations[sid]
    music = sdir(sid) / "music"
    files = sorted(p for p in music.rglob("*") if p.suffix.lower() in AUDIO_EXT)
    s["analysis"] = {"status": "running", "done": 0, "total": len(files), "started": time.time(), "errors": 0}
    save_station(s)
    tracks, embs = [], []
    for i, f in enumerate(files):
        try:
            emb, y = audio_embedding(f)
            t = dict(features(y), file=str(f.relative_to(music)), tags=tag_scores(emb))
            tracks.append(t); embs.append(emb)
        except Exception as e:
            s["analysis"]["errors"] += 1
            app.log("radio: analysis failed for", f, e)
        s["analysis"]["done"] = i + 1
    if not embs:
        s["analysis"].update(status="failed", error="No readable audio files"); save_station(s)
        return
    E = np.stack(embs)
    np.save(sdir(sid) / "corpus.npy", E)
    sims = E @ E.T; np.fill_diagonal(sims, np.nan)
    nn = np.nanmax(sims, axis=1) if len(E) > 1 else np.array([1.0])
    agg = {cat: {w: float(np.mean([t["tags"][cat][w] for t in tracks])) for w in TAGS[cat]} for cat in TAGS}
    top = {cat: [w for w, v in sorted(agg[cat].items(), key=lambda x: -x[1])[:5] if v > 0] for cat in TAGS}
    bpms = [t["bpm"] for t in tracks if t["bpm"]]
    vocal = float(np.mean([t["tags"]["p_vocal"] for t in tracks]))
    sung = [t["tags"]["female_margin"] for t in tracks if t["tags"]["p_vocal"] > 0.5]
    fem = float(np.mean([m > 0 for m in sung])) if sung else None
    top["voice"] = ([] if vocal < 0.3 else ["female vocals"] if fem is not None and fem > 0.65 else
                    ["male vocals"] if fem is not None and fem < 0.35 else ["female and male vocals"]) or ["instrumental"]
    keyc = {}
    for t in tracks:
        keyc[t["key"]] = keyc.get(t["key"], 0) + 1
    s["profile"] = {"tracks": len(tracks), "top": top, "weights": agg, "bpm_median": int(np.median(bpms)) if bpms else None,
                    "bpm_range": [int(np.percentile(bpms, 20)), int(np.percentile(bpms, 80))] if bpms else None,
                    "keys": sorted(keyc.items(), key=lambda x: -x[1])[:4], "vocal_share": round(vocal, 2),
                    "corpus_nn_p10": float(np.percentile(nn, 10)), "corpus_nn_median": float(np.median(nn)),
                    "track_list": [{k: t[k] for k in ("file", "bpm", "key", "seconds")} for t in tracks]}
    if s.get("vocals") == "mix" and vocal < 0.25:
        s["vocals"] = "instrumental"
    s["analysis"].update(status="done", finished=time.time())
    save_station(s)
    app.log("radio: analyzed", s["name"], len(tracks), "tracks", top)


def profile_text(s):
    p = s.get("profile")
    if not p:
        return ""
    t = p["top"]
    parts = [f"Genres heard in the station's songs: {', '.join(t['genre'])}.", f"Moods: {', '.join(t['mood'])}.",
             f"Instruments: {', '.join(t['instrument'])}."]
    parts.append("Vocals: mostly instrumental." if p["vocal_share"] < 0.3 else f"Vocals: {', '.join(t['voice'])} on about {round(p['vocal_share'] * 100)}% of songs.")
    if p.get("bpm_range"):
        parts.append(f"Tempo mostly {p['bpm_range'][0]}-{p['bpm_range'][1]} BPM.")
    return " ".join(parts)


# ----------------------------------------------------------------------------- song specs (LLM)

SPEC_SYSTEM = """You are the program director and songwriter of an AI radio station. Each time you are asked, invent ONE new song for the station.
Reply with a single JSON object and nothing else:
{"title": "...", "instrumental": true|false, "style": "...", "lyrics": "..."}
- style: one line for a music model: language, genre/subgenre, vocal type (or 'instrumental, no vocals'), 3-5 instruments, mood, and tempo as 'NN BPM'. Example: "English, dreamy synth-pop, airy female vocal, analog synth pads, arpeggiated bass, gated drums, nostalgic, 108 BPM"
- lyrics (when not instrumental): sections tagged [Verse] [Pre-Chorus] [Chorus] [Bridge] [Outro] on their own lines, one sung phrase per line, chorus repeated word for word. Only sung words. Size it for about 2.5-3 minutes: 24-34 sung lines in total (e.g. Verse 6, Chorus 4, Verse 6, Chorus 4, Bridge 4, Chorus 4), and always finish with a short [Outro] of 2-3 lines so the song comes to a real ending.
- lyrics (instrumental): only section tags, e.g. "[Intro]\\n\\n[Verse]\\n\\n[Chorus]\\n\\n[Verse]\\n\\n[Chorus]\\n\\n[Outro]".
- Stay inside the station's sound, but vary subgenre, tempo, key feel, instrumentation, topic and title from song to song so the station never sounds repetitive.
LYRIC CRAFT (this matters most - write like a real working songwriter, not a greeting card and not a poetry generator):
- First decide, privately, in one sentence: who is singing, to whom, what happened, and how they feel about it. Every line must
  serve that one story, and a listener must be able to retell it after one listen. Verse 1 sets the scene, verse 2 moves it on,
  the bridge turns it, the chorus says what it all means.
- Every line must make literal sense on its own and follow from the line before. Plain, conversational English, the way people
  really talk - clarity beats cleverness. No mixed metaphors, no surreal or impossible images ("a bowl of cold light", "cast iron
  butter"), no lists of random objects, no lines that only sound poetic.
- Show the story with a few well-chosen real details (2-3 per verse: a place, an object, a small action, something said out loud)
  rather than naming feelings ("I'm so lost" is weak; "your coat's still on the hook by the door" is strong). Every detail must
  belong to this story and be something that really happens in it.
- The chorus is a short, singable hook (2-4 lines) that says the emotional point plainly, contains the title and could be shouted
  along to. Keep one point of view and tense.
- Rhyme naturally: mix perfect and slant rhymes; never bend a line just to rhyme, never use filler words to fit the meter.
- Lines a singer can breathe: about 6-10 syllables, plain spoken words, no tongue-twisters, no stacked adjectives.
- Avoid stock imagery and clichés: shadows, darkness, fire/flames/ignite, rise/rising, break free, chains, scars, broken pieces,
  demons, ghosts, echoes, whispers, neon, hollow, the void, storms and thunder as metaphors, ashes, phoenix, wings, "stand tall",
  "hold on", "let go", "light the way", "heart of gold", "against the tide". If one slips in, replace it with something specific.
- Titles: a natural phrase from the chorus that a person would actually say ("Laundromat on 9th", "Two Coffees, One Spoon",
  "Leave the Porch Light On"), never an odd pairing of objects and never abstract ("Echoes of the Soul")."""


STORY_SEEDS = [
    "someone driving home at dawn after a night shift, noticing small things on the road", "two old friends meeting again at a wedding",
    "a kid's first summer job at a seaside snack bar", "packing up a parent's house after they move into care",
    "a late-night conversation in a 24-hour diner", "a band's van breaking down in a small town", "learning to cook a grandmother's recipe",
    "a lighthouse keeper's last week before automation", "a long-distance couple on a bad phone line", "a street musician and one regular listener",
    "moving into a first apartment with one chair and a mattress", "the last day before a best friend leaves for another country",
    "a farmer watching the sky before harvest", "a nurse's walk home after a hard shift", "fixing an old car with a brother",
    "a small-town Friday night football game", "a power cut that brings neighbours outside", "finding an old letter in a second-hand book",
    "a waitress who remembers every regular's order", "a road trip with a broken radio", "a dog waiting at the window every evening",
    "teaching a kid to ride a bike in an empty parking lot", "a stranger's kindness at a bus station", "the first warm day of spring in the city",
    "a boxer's early-morning training run", "two people falling for each other in a hardware store", "a retired dancer watching a rehearsal",
    "a fisherman's boat coming in late", "a teenage garage band's first real gig", "a wedding ring lost and found in the sand"]


VARIATIONS = [
    "pick a noticeably different tempo from the recent songs", "explore a different subgenre within the station's sound",
    "change the vocal approach (different voice type, delivery or harmonies)", "write about a completely different topic",
    "feature a different lead instrument", "make it more upbeat than the last few", "make it more laid-back than the last few",
    "use a different song structure (e.g. start with the chorus, or add a bridge)", "shift the decade or regional flavour slightly",
    "give it a storytelling lyric about a specific character or place"]


def parse_json_reply(text):
    """Pull a JSON object out of a chat reply (no JSON mode: xgrammar + MTP speculative decoding makes some vLLM builds return 500s)."""
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        candidates = [m.group(0)]
    elif "{" in text:                     # the model sometimes stops mid-lyrics without closing the string/object
        tail = text[text.index("{"):].rstrip()
        candidates = [tail + "}", tail + '"}', tail[:tail.rindex('"') + 1] + "}"]
    else:
        raise ValueError(f"no JSON object in reply: {text[:80]!r}")
    for raw in candidates:
        for fix in (raw, re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", raw)):        # escape stray backslashes
            try:
                return json.loads(fix, strict=False)              # strict=False tolerates raw newlines in strings
            except json.JSONDecodeError:
                pass
    raise ValueError(f"unparseable JSON in reply: {text[:80]!r}")


TITLE_STOP = {"the", "a", "an", "of", "and", "in", "on", "my", "your", "to", "for", "&", "it", "is", "me", "got", "made",
              "at", "with", "from", "by", "we", "you", "i", "i'm", "our", "all", "no", "so", "up", "down", "out"}


def title_words(t):
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9']+", t.lower()) if w not in TITLE_STOP and len(w) > 2}


def overused_words(titles, min_count=2):
    """Title words the songwriter keeps reaching for (e.g. 'Paper' six times in a row)."""
    counts = {}
    for t in titles:
        for w in title_words(t):
            counts[w] = counts.get(w, 0) + 1
    return sorted(w for w, n in counts.items() if n >= min_count)


def similar_title(title, recent_titles, last=()):
    """True when a title remixes a recent one: shares 2+ content words with any recent title (or all of a short one),
    or shares any content word with the last few titles, or uses a word that is already overused."""
    w = title_words(title)
    for t in recent_titles:
        o = title_words(t)
        if o and w and len(w & o) >= min(2, len(w), len(o)):
            return True
    if any(w & title_words(t) for t in last):
        return True
    return bool(w & set(overused_words(recent_titles)))


def reference_tracks(s, limit=15):
    """Song names from a learned station's files - the LLM usually knows these artists far better than CLAP's tags."""
    d = sdir(s["id"]) / "music"
    if s["kind"] != "folder" or not d.exists():
        return []
    out = []
    for f in sorted(d.rglob("*")):
        if f.suffix.lower() not in AUDIO_EXT:
            continue
        n = re.sub(r"^[\d\s.\-_]+", "", f.stem)                      # leading track numbers
        n = re.sub(r"[-_ ](fb|hq|official|audio|lyrics|320|128)$", "", n, flags=re.I)
        n = re.sub(r"\s+", " ", n.replace("_", " ")).strip(" -")
        if "-" in n and " - " not in n:
            n = n.replace("-", " - ", 1)
        if n and n.lower() not in (x.lower() for x in out):
            out.append(n.title() if n.islower() else n)
        if len(out) >= limit:
            break
    return out


def llm_candidates():
    """Reachable LLMs in order. While the radio is on, b70's own LLM is (or is about to be) paused for the
    GPUs, so the other servers go first."""
    cands = list(app.LYRICS_LLMS)
    if state["on"] or app.gpu["owner"] != "llm":
        cands.sort(key=lambda c: "localhost" in c[0] or "127.0.0.1" in c[0])
    out = []
    for base, model, label in cands:
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=2) as r:
                if r.status == 200:
                    out.append((base.rstrip("/"), model, label))
        except Exception:
            pass
    return out


def gpu_first(cands):
    """Songwriters on the GPU first (labelled "GPU" in settings, e.g. Qwen 3.5 9B): for things someone is waiting on.
    The big CPU songwriter stays as the fallback."""
    return sorted(cands, key=lambda c: "gpu" not in c[2].lower())


def write_spec(s, short=False, fast=False, writers=None, like=None):
    want_inst = s["vocals"] == "instrumental" or (s["vocals"] == "mix" and random.random() < INSTRUMENTAL_SHARE)
    recent = [j for j in app.jobs.values() if j.get("station") == s["id"] and j.get("source") == "radio"
              and j.get("radio_status") in ("approved", "played", "rendering", "judging")]
    recent = sorted(recent, key=lambda j: -j["created"])[:14]
    attempts = sorted((j for j in app.jobs.values() if j.get("station") == s["id"] and j.get("source") == "radio"
                       and j.get("spec_writer") != "fallback"), key=lambda j: -j["created"])[:20]
    attempt_titles = [j["title"] for j in attempts]
    likes = [f for f in s["feedback"] if f["kind"] == "keep"][-6:]
    dislikes = [f for f in s["feedback"] if f["kind"] == "dislike"][-6:]
    skips = [f for f in s["feedback"] if f["kind"] == "skip"][-4:]
    u = [f"Station: {s['name']}", f"Station sound: {s['description'] or '(learned from its songs; see below)'}"]
    if s.get("profile"):
        u.append("Learned from the station's own songs (automatic tags, can be wrong): " + profile_text(s))
    refs = reference_tracks(s)
    if refs:
        u.append("The station's own songs are: " + "; ".join(refs) + ". Match the sound of these artists and songs "
                 "(genre, vocal type and gender, instrumentation, era, lyrical themes); where your knowledge of them "
                 "disagrees with the automatic tags, trust your knowledge.")
    if s.get("language"):
        u.append(f"Language for vocals: {s['language']}")
    if s.get("theme") and not want_inst:
        u.append(f"LYRICS THEME for every song on this station (keep the station's musical sound; this is about the words, "
                 f"title and mood): {s['theme']}")
    u.append("This song must be INSTRUMENTAL." if want_inst else "This song has vocals.")
    story = None
    if like:                                              # "More like this" from a song page
        if not want_inst and like.get("lyrics"):
            u.append(f"The listener LOVED this song and asked for more like it. Same world, feel, groove and lyrical voice; a NEW "
                     f"story and a new title: {like['title']} [{like['style'][:160]}] — it opens: "
                     + " / ".join(l for l in like["lyrics"].splitlines() if l.strip() and not l.startswith("["))[:300])
        else:
            u.append(f"The listener LOVED this song and asked for more like it: {like['title']} [{like['style'][:160]}]. Same feel, new song.")
    elif not want_inst:                                   # a concrete starting point, so songs aren't all the same anthem
        story = random.choice(STORY_SEEDS)
        u.append("Optional story idea: " + story + ". Use it only if it fits naturally with this station's sound, era and lyrics "
                 "theme (the kind of story its listeners would expect). If it doesn't fit, ignore it and invent a story that does.")
    taste = (s.get("taste") or {}).get("notes")
    if taste:
        u.append("WHAT THIS LISTENER HAS TAUGHT THE STATION (follow it closely): " + taste)
    loved = [j for j in liked_songs(s) if j.get("lyrics")][:2]
    if loved and not want_inst:
        u.append("Opening lines of songs the listener loved, as the voice to aim for (never copy them): "
                 + "; ".join(f"{j['title']}: " + " / ".join(l for l in j["lyrics"].splitlines() if l.strip() and not l.startswith("["))[:160] for j in loved))
    if short:
        u.append("Make this one SHORT - it opens the broadcast: [Verse] of 4 lines, [Chorus] of 4 lines, [Outro] of 2 lines (10 sung lines total).")
    if recent:
        u.append("Recently played (do not repeat these titles or exact styles): " + "; ".join(f"{j['title']} [{j['style'][:70]}]" for j in recent))
    if likes:
        u.append("The listener liked these. Keep the general vibe that made them work, but this song must be clearly "
                 "DIFFERENT from them: new title (reuse none of their title words), new topic, different groove/tempo or "
                 "instrumentation: " + "; ".join(f"{f['title']} [{f['style'][:80]}]" for f in likes))
    u.append("For variety, this time: " + random.choice(VARIATIONS) + ".")
    banned = overused_words(attempt_titles + [f["title"] for f in likes])
    if attempt_titles:
        u.append("Titles of the latest songs (don't repeat their main words or ideas): " + "; ".join(attempt_titles[:10]))
    if banned:
        u.append("Overused lately, avoid in the title: " + ", ".join(banned) + ".")
    if dislikes:
        u.append("The listener DISLIKED these, steer away from them: " + "; ".join(f"{f['title']} [{f['style'][:80]}]" for f in dislikes))
    if skips:
        u.append("Skipped (mildly less of this): " + "; ".join(f"[{f['style'][:60]}]" for f in skips))
    cands = writers or (gpu_first(llm_candidates()) if fast else llm_candidates())
    for base, model, label in [c for c in cands for _ in range(3)]:     # each server gets two retries
        if not state["on"]:
            break
        try:
            body = {"model": model, "temperature": SPEC_TEMPERATURE, "top_p": 0.95, "max_tokens": 2200,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "messages": [{"role": "system", "content": SPEC_SYSTEM}, {"role": "user", "content": "\n".join(u)}],
                    **app.LLM_EXTRA}
            app.llm_used(model)
            r = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(),
                           {"Content-Type": "application/json"}), timeout=app.LLM_TIMEOUT).read())
            text = r["choices"][0]["message"]["content"]
            spec = parse_json_reply(text)
            style = str(spec.get("style", "")).strip()[:600]
            inst = bool(spec.get("instrumental", want_inst))
            ltitle, lyrics = app.normalize_lyrics(str(spec.get("lyrics", "")))
            if not style:
                raise ValueError("spec without a style")
            if not inst and sung_lines(lyrics) < (8 if short else 14):
                u_hint = "IMPORTANT: write the complete lyrics - every section needs its sung lines, not just the section tags."
                if u[-1] != u_hint:
                    u.append(u_hint)
                raise ValueError(f"only {sung_lines(lyrics)} sung lines in the lyrics")
            title = str(spec.get("title") or spec.get("name") or ltitle or "").strip()
            if not title:
                first = next((l for l in lyrics.splitlines() if l.strip() and not l.startswith("[")), "")
                genre = style.split(",")[1].strip().title()[:40] if "," in style else ""
                title = first[:40] or genre or "Untitled"
            recent_titles = attempt_titles + [j["title"] for j in recent[:8]] + [f["title"] for f in likes]
            if similar_title(title, recent_titles, last=attempt_titles[:5]) and not u[-1].startswith("IMPORTANT"):
                u.append(f"IMPORTANT: your title '{title}' repeats words from recent titles. Write a NEW song with a "
                         "completely different title and topic.")
                raise ValueError(f"title '{title}' repeats recent titles; asking again")
            if not inst and lyrics.strip() and "fallback" not in label.lower():   # editor pass (the small fallback model edits badly: skip)
                lyrics, title = edit_lyrics(s, title, style, lyrics, base, model, label)
            return {"title": title[:80], "style": style, "instrumental": inst, "lyrics": lyrics if not inst else "", "writer": label, "story": story}
        except Exception as e:
            app.log(f"radio: spec writer {label} failed ({e}); trying the next one")
    words = s["description"] or profile_text(s)
    return {"title": f"{s['name']} #{s['stats']['generated'] + 1}", "style": "Instrumental, " + words[:300] + ", no vocals",
            "instrumental": True, "lyrics": "", "writer": "fallback"}


CLICHES = ["shadow", "darkness", "the dark", "fire", "flame", "ignite", "we rise", "i rise", "rising up", "break free", "breaking free",
           "chains", "scars", "broken pieces", "demons", "ghost", "echo", "whisper", "neon", "hollow", "the void", "ashes", "phoenix",
           "wings", "stand tall", "hold on", "let go", "light the way", "heart of gold", "against the tide", "into the night",
           "tears like rain", "set me free", "burning bright", "lost in the", "thunder", "the storm"]


def sung_lines(lyrics):
    return sum(1 for l in (lyrics or "").splitlines() if l.strip() and not l.strip().startswith("["))


def cliche_hits(lyrics):
    low = lyrics.lower()
    return [c for c in CLICHES if re.search(r"\b" + re.escape(c), low)]


EDIT_SYSTEM = """You are a demanding lyric editor. You get a draft song. First check that it tells ONE clear story a listener could
retell, and that every line makes literal sense and follows from the one before. Then find its weakest lines: lines that don't make
sense, surreal or mixed metaphors, random objects that don't belong to the story, clichés and stock images, vague lines that name
feelings, forced or filler rhymes, lines too long to sing, a chorus hook that isn't memorable or doesn't contain the title,
verses that repeat instead of moving the story on. Then rewrite the lyrics fixing all of that in plain, natural language -
when in doubt, choose the clearer line over the more colourful one. Keep the same section tags and order, keep about the same number of lines,
keep the chorus identical every time it appears, keep the language, and keep everything that already works.
Reply with JSON only: {"notes": "one short line on what you changed", "title": "...", "lyrics": "..."}"""


def edit_lyrics(s, title, style, lyrics, base, model, label):
    """One editor pass (two if clichés survive): sharper images, natural rhymes, a real hook. The draft is kept on failure."""
    best = (lyrics, title)
    for round_ in range(2):
        hits = cliche_hits(best[0])
        if round_ and len(hits) < 3:
            break
        ask = (f"Station: {s['name']} — {s.get('description') or ''}\nStyle: {style}\nTitle: {best[1]}\n"
               + (f"These words are clichés here, replace every one of them with something specific: {', '.join(hits)}\n" if hits else "")
               + f"\nDRAFT LYRICS:\n{best[0]}")
        try:
            body = {"model": model, "temperature": 0.7, "top_p": 0.95, "max_tokens": 2200, "chat_template_kwargs": {"enable_thinking": False},
                    "messages": [{"role": "system", "content": EDIT_SYSTEM}, {"role": "user", "content": ask}], **app.LLM_EXTRA}
            app.llm_used(model)
            r = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(),
                           {"Content-Type": "application/json"}), timeout=app.LLM_TIMEOUT).read())
            out = parse_json_reply(r["choices"][0]["message"]["content"])
            _, new = app.normalize_lyrics(str(out.get("lyrics", "")))
            tags = lambda t: re.findall(r"^\[[^\]]+\]", t, re.M)
            if not new.strip() or tags(new) != tags(best[0]) or sung_lines(new) < 0.6 * sung_lines(best[0]):
                raise ValueError("the edit changed the song's shape")
            if len(cliche_hits(new)) > len(hits):
                raise ValueError("the edit added clichés")
            new_title = str(out.get("title") or best[1]).strip()[:80]
            app.log(f"radio: lyric edit ({label}) {best[1]!r}: {len(hits)} -> {len(cliche_hits(new))} clichés; {str(out.get('notes', ''))[:120]}")
            best = (new, new_title)
        except Exception as e:
            app.log(f"radio: lyric edit skipped ({e})")
            break
    return best


LYRICIST = ("http://localhost:11434/v1", "qwen3.6-agent", "Qwen 3.6 27B (GPU lyricist)")   # one loaded copy shared with Hermes's AGI
BANK_SIZE = 3
bank_writing = threading.Event()        # the worker waits for this before loading YuE2


def lyricist_may_run():
    """The 27B lyricist needs most of the card: only when nothing else is on it."""
    if app.gpu["owner"] in ("studio", "switching", "waiting") or art.inflight or app.comfy_busy() or app.assistant_on_gpu():
        return False
    used = app.gpu_free_mib(0)
    return used is not None and used < 6000


def bank_loop():
    """Fill the current station's lyric bank with the GPU lyricist whenever the card is free."""
    while True:
        time.sleep(20)
        try:
            sid = state["station"]
            if not state["on"] or sid not in stations:
                continue
            s = stations[sid]
            if len(s.get("bank") or []) >= BANK_SIZE or not lyricist_may_run():
                continue
            bank_writing.set()
            try:
                t0 = time.time()
                spec = write_spec(s, writers=[LYRICIST])
            finally:
                bank_writing.clear()
                try:                                      # give the card straight back
                    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                           json.dumps({"model": LYRICIST[1], "keep_alive": 0}).encode(), {"Content-Type": "application/json"}), timeout=30).read()
                except Exception:
                    pass
            if spec.get("writer") == LYRICIST[2]:
                with lock:
                    s.setdefault("bank", []).append(spec | {"banked": time.time()})
                    save_station(s)
                app.log(f"radio: lyricist banked {spec['title']!r} for {s['name']} in {time.time() - t0:.0f}s")
        except Exception:
            bank_writing.clear()
            app.log("radio bank error:", traceback.format_exc())


def liked_songs(s):
    """Songs on this station the listener thumbed up or saved to the playlist, newest first."""
    keep = {f["track"] for f in s.get("feedback", []) if f["kind"] == "keep"}
    with app.lock:
        songs = [j for j in app.jobs.values() if j.get("station") == s["id"] and j.get("source") == "radio"
                 and (j["id"] in keep or j.get("playlist_at"))]
    return sorted(songs, key=lambda j: -(j.get("playlist_at") or j.get("finished") or 0))


TASTE_SYSTEM = """You read what one radio listener liked and disliked on a station and write a short brief for the station's
songwriter. Be concrete: tempos, instruments, vocal types, moods, topics, kinds of lyrics and storytelling. Three lines exactly:
More of: ...
Less of: ...
Lyrics: ...
At most 90 words in total. No preamble."""


def update_taste(sid):
    """Rewrite the station's taste notes from thumbs (and playlist saves). Runs in a thread after feedback."""
    s = stations.get(sid)
    if not s:
        return
    with app.lock:
        by_id = {j["id"]: j for j in app.jobs.values() if j.get("station") == sid}
    loved = liked_songs(s)[:8]
    disliked = [by_id[f["track"]] for f in s.get("feedback", []) if f["kind"] == "dislike" and f["track"] in by_id][-6:]
    skipped = [by_id[f["track"]] for f in s.get("feedback", []) if f["kind"] == "skip" and f["track"] in by_id][-4:]
    rated = {f["track"] for f in s.get("feedback", [])} | {j["id"] for j in loved}
    soft = lambda kind, n: list({f["track"]: by_id[f["track"]] for f in s.get("implicit", [])
                                 if f["kind"] == kind and f["track"] in by_id and f["track"] not in rated}.values())[-n:]
    heard, left = soft("heard", 6), soft("left", 4)
    if len(loved) + len(disliked) + (len(heard) + len(left)) // 2 < 2:
        return
    line = lambda j: f"- {j['title']} [{j['style'][:110]}] " + " / ".join(l for l in (j.get("lyrics") or "").splitlines() if l.strip() and not l.startswith("["))[:140]
    ask = (f"Station: {s['name']} — {s.get('description') or ''}\nLIKED (thumbs up or saved):\n" + ("\n".join(map(line, loved)) or "-")
           + "\nDISLIKED:\n" + ("\n".join(map(line, disliked)) or "-") + "\nSKIPPED (mild):\n" + ("\n".join(map(line, skipped)) or "-")
           + "\nLISTENED ALL THE WAY THROUGH (mild like):\n" + ("\n".join(map(line, heard)) or "-")
           + "\nTUNED OUT EARLY (mild dislike):\n" + ("\n".join(map(line, left)) or "-"))
    for base, model, label in gpu_first(llm_candidates()):
        try:
            body = {"model": model, "temperature": 0.3, "max_tokens": 300, "chat_template_kwargs": {"enable_thinking": False},
                    "messages": [{"role": "system", "content": TASTE_SYSTEM}, {"role": "user", "content": ask}], **app.LLM_EXTRA}
            app.llm_used(model)
            r = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(),
                           {"Content-Type": "application/json"}), timeout=120).read())
            notes = r["choices"][0]["message"]["content"].strip()[:900]
            if "More of" in notes or "Less of" in notes:
                with lock:
                    s["taste"] = {"notes": notes, "updated": time.time(), "from": len(loved) + len(disliked) + len(heard) + len(left)}
                    save_station(s)
                app.log(f"radio: taste notes updated for {s['name']} from {len(loved)} likes / {len(disliked)} dislikes"
                        f" / {len(heard)} heard through / {len(left)} tuned out")
                return
        except Exception as e:
            app.log(f"radio: taste notes ({label}) failed: {e}")


def forget_taste(sid):
    s = stations.get(sid)
    if not s:
        raise app.BadRequest("No such station")
    with lock:
        s["taste"] = None
        s["feedback"] = []
        s["implicit"] = []
        save_station(s)
    return {"ok": True}


def track_details(tid):
    with app.lock:
        j = app.jobs.get(tid)
    if not j:
        raise app.BadRequest("No such song")
    return track_view(j) | {"critic": j.get("critic"), "writer": j.get("spec_writer"), "story": j.get("story"), "created": j.get("created"),
                            "approved_at": j.get("approved_at"), "airings": j.get("airings", 0), "status": j.get("radio_status"),
                            "requested": bool(j.get("requested")), "liked_from": j.get("liked_from")}


# ----------------------------------------------------------------------------- spool

def station_tracks(sid, statuses=("approved",)):
    with app.lock:
        return [j for j in app.jobs.values() if j.get("source") == "radio" and j.get("station") == sid and j.get("radio_status") in statuses]


def inflight(sid):
    with app.lock:
        n = sum(1 for j in app.jobs.values() if j.get("source") == "radio" and j.get("station") == sid and
                (j["status"] in ("queued", "running") or j.get("radio_status") == "judging"))
    return n + writing.get(sid, 0)


def spool_loop():
    while True:
        try:
            time.sleep(2)
            if time.time() - state.get("heartbeat", 0) > 20:
                save_state()                               # heartbeat: lets a quick restart resume the broadcast
            settle_stragglers()
            sid = state["station"]
            if not state["on"] or sid not in stations:
                continue
            s = stations[sid]
            if s["kind"] == "folder" and s["analysis"].get("status") != "done":
                continue
            b = backoff.get(sid)
            if b and time.time() < b["until"]:
                continue
            with app.lock:
                last = sorted((j for j in app.jobs.values() if j.get("source") == "radio" and j.get("station") == sid
                               and j["status"] in ("done", "failed") and not (j.get("error") or "").startswith("Interrupted")),
                              key=lambda j: -j.get("finished", 0))[:3]
            if len(last) == 3 and all(j["status"] == "failed" for j in last) and not (b and b.get("seen") == last[0]["id"]):
                delay = min(600, (b["delay"] * 2) if b else 60)
                backoff[sid] = {"until": time.time() + delay, "delay": delay, "seen": last[0]["id"],
                                "reason": (last[0].get("error") or "songs keep failing")[:160]}
                app.log(f"radio: 3 failed takes in a row on {s['name']}; pausing generation {delay}s")
                continue
            if last and last[0]["status"] == "done":
                backoff.pop(sid, None)
            ready = len(station_tracks(sid, ("approved",)))
            fly = inflight(sid)
            limit = state["max_inflight"] + 1 if ready else 2 * len(app.DEVICES)     # spool empty: both GPUs flat out
            if ready + fly < state["target_ahead"] and fly < limit:
                with lock:
                    writing[sid] = writing.get(sid, 0) + 1
                threading.Thread(target=produce, args=(sid,), daemon=True).start()
        except Exception:
            app.log("radio spool error:", traceback.format_exc())


def request_song(sid, like=None):
    """🎵 Create a song now: write and record one song for this station straight away, whatever the radio is playing."""
    if sid not in stations:
        raise app.BadRequest("No such station")
    with app.lock:
        busy = sum(1 for j in app.jobs.values() if j.get("station") == sid and j.get("requested") and j["status"] in ("queued", "running"))
    with lock:
        if busy + requested.get(sid, 0) >= 3:
            raise app.BadRequest("Three songs for this station are already on the way")
        requested[sid] = requested.get(sid, 0) + 1
        writing[sid] = writing.get(sid, 0) + 1
    lj = None
    if like:
        with app.lock:
            lj = dict(app.jobs.get(like) or {})
        if not lj:
            raise app.BadRequest("No such song")
    threading.Thread(target=produce, args=(sid, True, lj), daemon=True).start()
    return {"ok": True, "banked": bool(stations[sid].get("bank")) and not lj}


def takes_for(ready):
    """Takes to render for the next song, by how many approved songs are waiting. A take costs about half a song's
    length in GPU time, so 3 takes per song can't keep up with a listener: the full count only when the spool is nearly
    full, at most 2 while it is half empty, 1 when it is about to run dry."""
    target = state["target_ahead"]
    if ready >= max(2, target - 1):
        return TAKES
    if ready >= 2:
        return min(TAKES, 2)
    return 1


def produce(sid, on_request=False, like=None):
    try:
        s = stations.get(sid)
        if not s:
            return
        with app.lock:   # nothing ready and nothing being made for this station -> open with a short song
            busy = any(j.get("station") == sid and j.get("source") == "radio" and j["status"] in ("queued", "running")
                       for j in app.jobs.values())
        ready = len(station_tracks(sid, ("approved",)))
        short = not busy and not ready and writing.get(sid, 0) <= 1 and not on_request
        takes = takes_for(ready)                # fewer takes when the spool runs low, so the broadcast never runs dry
        spec = None
        if not short and not like and s.get("bank"):     # a song the GPU lyricist wrote ahead of time
            with lock:
                spec = s["bank"].pop(0) if s.get("bank") else None
                save_station(s)
            recent_titles = [j["title"] for j in sorted((j for j in app.jobs.values() if j.get("station") == sid and j.get("source") == "radio"),
                                                        key=lambda j: -j["created"])[:12]]
            if spec and similar_title(spec["title"], recent_titles, last=recent_titles[:5]):
                spec = None                               # too close to something just played: write a fresh one
            elif spec and not spec.get("instrumental") and sung_lines(spec.get("lyrics")) < 14:
                app.log(f"radio: dropping banked song {spec['title']!r}: its lyrics are missing")
                spec = None
            if spec:
                app.log(f"radio: using banked song {spec['title']!r} ({spec['writer']})")
        if not spec:
            spec = write_spec(s, short=short, like=like)  # quality over speed: the big songwriter + editor, requests too
        if not on_request and (not state["on"] or state["station"] != sid):
            app.log("radio: discarding a spec written after the radio was switched off / retuned")
            return
        body = {"mode": "instrumental" if spec["instrumental"] else "song", "title": spec["title"], "author": "radio",
                "style": spec["style"], "lyrics": spec["lyrics"], "cot": "full", "max_seconds": OPENER_SECONDS if short else MAX_SECONDS, "takes": takes,
                "ode_steps": ODE_STEPS, "cfg_scale": CFG_SCALE, "semantic_sampling": dict(SONG_SAMPLING),
                "abc_sampling": dict(SCORE_SAMPLING)}   # a safety cap only: songs should end on their own
        if not spec["instrumental"] and not spec["lyrics"].strip():
            body["mode"] = "instrumental"
        app.new_jobs(body, extra={"source": "radio", "station": sid, "priority": 0 if on_request else 1, "radio_status": "rendering",
                                  "spec_writer": spec["writer"], "story": spec.get("story"), **({"liked_from": like.get("id")} if like else {}),
                                  **({"requested": True} if on_request else {})})
        with lock:
            s["stats"]["generated"] += 1; save_station(s)
    except Exception:
        app.log("radio produce error:", traceback.format_exc())
    finally:
        with lock:
            writing[sid] = max(0, writing.get(sid, 1) - 1)
            if on_request:
                requested[sid] = max(0, requested.get(sid, 1) - 1)


def judge(job):
    """Called (in a thread) when a radio job finishes rendering. Each take is scored by the CLAP critic; once every
    take of the song is in, the best one faces the station's bar and the others become runners-up (filler)."""
    sid = job.get("station"); s = stations.get(sid)
    if not s:
        return
    if job["status"] != "done":
        job["radio_status"] = "failed"; app.save_job(job)
        return settle_takes(job, s)
    job["radio_status"] = "judging"
    try:
        score, sane, dup, checks = critic_score(job, s)
        job["critic"] = {"score": round(score, 4), "sane": sane, "dup": round(dup, 3), "checks": checks}
        if checks.get("notes"):
            app.log("radio critic notes", job["title"], f"take {job.get('take', 1)}:", "; ".join(checks["notes"]))
    except Exception:
        app.log("radio critic error:", traceback.format_exc())
        job["critic"] = {"score": None, "sane": True, "dup": 0.0}      # critic broke: don't hold the song back
    app.save_job(job)
    settle_takes(job, s)


def station_bar(s):
    """The score a song must reach to air. Text stations keep the top KEEP_TEXT share, learned ones KEEP_LEARNED,
    once WARMUP_SCORES scores exist; before that, only clear misses (under WARMUP_FLOOR x the station's median) are
    dropped. Scores live on each station's own scale (CLAP similarity to its description), so the bar is relative."""
    hist = s["scores"][-20:]
    keep_frac = KEEP_LEARNED if s["kind"] == "folder" else KEEP_TEXT
    if len(hist) >= WARMUP_SCORES:
        return float(np.quantile(hist, 1 - keep_frac))
    if len(hist) >= 2:
        return WARMUP_FLOOR * float(np.median(hist))
    return -1.0


def settle_takes(job, s):
    """Once all takes of a song are finished and scored, approve the best take (if it clears the bar)."""
    batch = job.get("batch")
    with app.lock:
        takes = [j for j in app.jobs.values() if j.get("source") == "radio" and j.get("batch") == batch] if batch else [job]
    if any(j["status"] in ("queued", "running") or (j["status"] == "done" and "critic" not in j) for j in takes):
        return                                           # wait for the other takes
    with lock:
        if any(j.get("radio_status") in ("approved", "rejected", "played") for j in takes):
            return                                       # already settled by another thread
        scored = [j for j in takes if j["status"] == "done" and j.get("critic")]
        if not scored:
            return
        rank = lambda j: (j["critic"]["sane"], j["critic"]["score"] if j["critic"]["score"] is not None else float("inf"))
        best = max(scored, key=rank)
        bar = station_bar(s)
        score = best["critic"]["score"]
        asked = any(j.get("requested") for j in takes)    # someone asked for this song: keep it unless it's broken
        verdict = "approved" if best["critic"]["sane"] and (asked or score is None or score >= bar) else "rejected"
        if job.get("spec_writer") != "fallback":
            s["scores"] = (s["scores"] + [j["critic"]["score"] for j in scored if j["critic"]["score"] is not None])[-50:]
        s["stats"]["approved" if verdict == "approved" else "rejected"] += 1
        save_station(s)
        now = time.time()
        for j in scored:
            j["critic"].update(bar=round(bar, 4), takes=len(takes))
            j.update(radio_status=verdict if j is best else "rejected", approved_at=now)
            app.save_job(j)
    if asked and not generating():                       # made on request while the radio is off: give the GPUs back
        app.gpu["release_on_idle"] = True
    for j in scored:
        sc = j["critic"]["score"]
        app.log("radio critic", j["title"], f"take {j.get('take', 1)}/{len(takes)}",
                verdict if j is best else "runner-up", "n/a" if sc is None else round(sc, 3), "bar", round(bar, 3))


def settle_stragglers():
    """Songs whose remaining takes were cancelled (radio switched off) or that were scored before a restart."""
    with app.lock:
        waiting = [j for j in app.jobs.values() if j.get("source") == "radio" and j.get("radio_status") == "judging"
                   and "critic" in j]
    for j in waiting:
        s = stations.get(j.get("station"))
        if s:
            settle_takes(j, s)


def audio_checks(y, sr=48000):
    """Cheap signal checks on a take (mono float audio). Broken takes from the music model show up as long dropouts
    or mostly-quiet audio; clipping is a lesser flaw."""
    hop = sr // 2
    n = len(y) // hop
    if n < 8:
        return {"dropout_s": 0.0, "quiet_share": 1.0, "clip_share": 0.0}
    db = 20 * np.log10(np.sqrt((y[: n * hop].reshape(n, hop) ** 2).mean(1)) + 1e-9)
    edge = max(2, n // 20)                       # ignore the first/last 5%: quiet intros and fade-outs are fine
    quiet = db[edge:-edge] < -45
    run = longest = 0
    for q in quiet:
        run = run + 1 if q else 0
        longest = max(longest, run)
    return {"dropout_s": longest / 2, "quiet_share": round(float(quiet.mean()), 3),
            "clip_share": round(float((np.abs(y) >= 0.995).mean()), 5)}


def quality_flags(job, s, y, emb):
    """(penalty, broken, notes) for one take, on top of the CLAP fit score (station scores spread over ~0.1)."""
    c = audio_checks(y)
    notes, pen = [], 0.0
    broken = c["dropout_s"] >= 8 or c["quiet_share"] > 0.25
    if broken:
        notes.append(f"dropout {c['dropout_s']:.0f}s / {c['quiet_share']:.0%} quiet")
    if c["clip_share"] > 0.001:
        pen += 0.02; notes.append("clipping")
    secs = job.get("seconds") or 0
    if secs >= job.get("max_seconds", MAX_SECONDS) - 1:            # never ended on its own: the length cap cut it off
        pen += 0.03; notes.append("hit the length cap")
    lines = [l for l in (job.get("lyrics") or "").splitlines() if l.strip() and not l.strip().startswith("[")]
    if job.get("mode") == "song" and len(lines) >= 12:
        pace = secs / len(lines)                 # healthy takes sing ~3.5-8 s per line
        if pace < 3.0:
            pen += 0.03; notes.append("rushed / skipped lyrics")
        elif pace > 9.0:
            pen += 0.02; notes.append("long stretches without lyrics")
    if job.get("mode") == "song" and (job.get("lyrics") or "").strip():
        try:
            p_vocal = tag_scores(emb)["p_vocal"]
            c["p_vocal"] = round(p_vocal, 3)
            if p_vocal < 0.2:                    # a song with lyrics that came out without a voice
                pen += 0.04; notes.append("no audible vocals")
        except Exception:
            pass
    return pen, broken, notes, c


def critic_score(job, s):
    """CLAP critic: how well this take fits the station, nudged by likes/dislikes, minus a novelty penalty."""
    sid = s["id"]
    path = app.DATA / job["id"] / "song.mp3"
    if not path.is_file():
        path = app.DATA / job["id"] / "song" / "audio.flac"
    emb, y = audio_embedding(path)
    np.save(app.DATA / job["id"] / "clap.npy", emb)
    rms = float(np.sqrt(np.mean(y ** 2)))
    sane = (job.get("seconds") or 0) >= MIN_SONG_SECONDS and rms > 0.01
    try:
        penalty, broken, notes, checks = quality_flags(job, s, y, emb)
    except Exception:
        app.log("radio critic checks error:", traceback.format_exc())
        penalty, broken, notes, checks = 0.0, False, [], {}
    sane = sane and not broken
    if s["kind"] == "folder" and (sdir(sid) / "corpus.npy").is_file():
        ref = np.load(sdir(sid) / "corpus.npy")
    else:
        ref = text_embeddings([s["description"][:300] or s["name"]])
    base = float(np.sort(ref @ emb)[::-1][:5].mean())
    likes, dislikes, recent = [], [], []
    for f in s["feedback"]:
        p = app.DATA / f["track"] / "clap.npy"
        if p.is_file() and f["kind"] in ("keep", "dislike"):
            (likes if f["kind"] == "keep" else dislikes).append(np.load(p))
    with app.lock:
        aired = sorted((j for j in app.jobs.values() if j.get("station") == sid and j.get("source") == "radio" and
                        j["id"] != job["id"] and j.get("radio_status") in ("approved", "played")),
                       key=lambda j: -j.get("approved_at", 0))[:12]
    for j in aired:
        p = app.DATA / j["id"] / "clap.npy"
        if p.is_file():
            recent.append(np.load(p))
    like_b = 0.15 * (float(np.max(np.stack(likes) @ emb)) - 0.5) if likes else 0.0       # gentle nudge only
    dis_p = 0.5 * max(0.0, float(np.max(np.stack(dislikes) @ emb)) - 0.6) if dislikes else 0.0
    dup = float(np.max(np.stack(recent) @ emb)) if recent else 0.0
    score = base + like_b - dis_p - 1.0 * max(0.0, dup - 0.93) - penalty    # CLAP saturates within a genre: soft novelty penalty
    if dup > 0.99:
        sane = False          # only a true clone; same-station songs routinely score 0.95-0.97 (CLAP saturates)
        notes.append("near-copy of a recent song")
    return score, sane, dup, {"penalty": round(penalty, 3), "notes": notes, **checks}


# ----------------------------------------------------------------------------- broadcast
# One shared live MP3 stream: a single playout thread picks the next approved track and every listener
# (browser deck, VLC, phone) hears the same audio. 192 kbit/s CBR at 48 kHz -> exactly 24000 bytes per second,
# so a byte offset in the stream is also a time offset; clients use that to know which song is audible.
BPS = 24000                         # bytes per second of the stream (BITRATE / 8)
FRAME = 576                         # 144 * 192000 / 48000: at 48 kHz CBR every frame is exactly this size (no padding)
# whole frames sent at once to a new listener: playback starts immediately and the player holds this much slack
# against network hiccups (phones on Wi-Fi/cell). The listener hears the broadcast this far behind live.
BURST = int(os.environ.get("RADIO_BURST_SECONDS", "15")) * BPS // FRAME * FRAME
LEAD = BURST / BPS                # seconds the stream runs ahead of real time (see _broadcast)
STALE = 60                        # after this long without listeners the buffered audio is dropped; a phone that
                                  # reconnects sooner (Wi-Fi/cell handover) gets its slack back at once
ENCODE = ["-vn", "-ac", "2", "-ar", "48000", "-c:a", "libmp3lame", "-b:a", "192k", "-id3v2_version", "0",
          "-write_xing", "0", "-map_metadata", "-1", "-f", "mp3", "pipe:1"]
ACTIVE_BITRATE = 192                # what the stream is encoded at right now


def set_buffer(seconds):
    global BURST, LEAD
    BURST = int(seconds) * BPS // FRAME * FRAME
    LEAD = BURST / BPS


def _switch_format():
    """Apply a new BITRATE between songs: listeners are dropped (players reconnect by themselves) and the stream
    clock restarts, because byte offsets map to time differently at another bitrate."""
    global BPS, FRAME, ACTIVE_BITRATE
    seconds = LEAD
    with bc_lock:
        BPS, FRAME, ACTIVE_BITRATE = BITRATE * 1000 // 8, 144 * BITRATE * 1000 // 48000, BITRATE
        ENCODE[ENCODE.index("-b:a") + 1] = f"{BITRATE}k"
        set_buffer(seconds)
        bc["listeners"].clear()
        bc.update(bytes=0, epoch=time.time(), buf=bytearray(), aired=[], now=None)
    _make_silence()
    app.log(f"radio: stream now {BITRATE} kbit/s")


def _make_silence():
    try:
        bc["silence"] = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                        "anullsrc=r=48000:cl=stereo", "-t", "1.152", *ENCODE], capture_output=True, timeout=30).stdout
    except Exception:
        app.log("radio: could not make the silence frames:", traceback.format_exc())


COMPRESSORS = {"light": "acompressor=threshold=-18dB:ratio=2:attack=20:release=250:makeup=2",
               "medium": "acompressor=threshold=-20dB:ratio=3:attack=10:release=200:makeup=4",
               "heavy": "acompressor=threshold=-24dB:ratio=6:attack=5:release=150:makeup=7"}


def audio_filters(seconds):
    """ffmpeg -af chain for one song: tone, stereo width, compression, loudness, limiter, fades."""
    f = []
    if BASS_DB:
        f.append(f"bass=g={BASS_DB:g}:f=100")
    if TREBLE_DB:
        f.append(f"treble=g={TREBLE_DB:g}:f=8000")
    if STEREO_WIDTH == 0:
        f.append("pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1")
    elif STEREO_WIDTH != 1:
        f.append(f"extrastereo=m={STEREO_WIDTH:g}:c=false")
    if COMPRESSION in COMPRESSORS:
        f.append(COMPRESSORS[COMPRESSION])
    if LOUDNESS is not None:
        f.append(f"loudnorm=I={LOUDNESS:g}:TP=-1.5:LRA=11")
    if LIMITER:
        f.append("alimiter=limit=0.95:level=false")
    if FADE_IN:
        f.append(f"afade=t=in:d={FADE_IN:g}")
    if FADE_OUT and seconds and seconds > FADE_OUT * 2:
        f.append(f"afade=t=out:st={seconds - FADE_OUT:.2f}:d={FADE_OUT:g}")
    return ["-af", ",".join(f)] if f else []
bc = {"listeners": {}, "buf": bytearray(), "bytes": 0, "epoch": 0.0, "now": None, "aired": [],
      "skip": threading.Event(), "wake": threading.Event(), "silence": b"", "last": 0.0,
      "cut": 0, "cut_next": False}         # cut: bumped when a skip/retune drops the buffered old song (players reconnect)
bc_lock = threading.Lock()
listen_lock = threading.Lock()


def _broadcast(chunk):
    """Pace the stream at real time (bytes sent == elapsed seconds * BPS), then fan the chunk out to listeners.
    The stream runs LEAD seconds ahead of the clock, so every player - also the first one after the stream was
    idle, or one that just reconnected - holds that much audio in its buffer."""
    ahead = (bc["bytes"] + len(chunk)) / BPS - (time.time() - bc["epoch"]) - LEAD
    if ahead > 0:
        time.sleep(ahead)
    with bc_lock:
        if ahead < -1.0 - LEAD:         # we were idle (no listeners): restart the clock instead of bursting to catch up
            if time.time() - bc["last"] > STALE:
                bc["buf"].clear()       # silent for long: the buffered audio is stale
            # a newcomer gets the kept buffer at once; send only the rest of LEAD ahead, so the slack stays ~LEAD
            bc["epoch"] = time.time() - (bc["bytes"] + len(chunk) - len(bc["buf"])) / BPS
        bc["bytes"] += len(chunk)
        bc["last"] = time.time()
        bc["buf"] += chunk
        del bc["buf"][:-BURST]                  # BURST is a whole number of frames, so the burst starts on a frame
        for lid, li in list(bc["listeners"].items()):
            try:
                li["q"].put_nowait(chunk)
            except queue.Full:          # a listener that cannot keep up gets dropped (it reconnects)
                bc["listeners"].pop(lid, None)


def _air(entry):
    with bc_lock:
        entry = dict(entry or {"id": None}, start_pos=round(bc["bytes"] / BPS, 3))
        bc["now"] = entry if entry.get("id") else None
        bc["aired"] = (bc["aired"] + [entry])[-8:]
    if entry.get("id") and entry.get("airing") != "history":
        with nav_lock:
            if not nav["hist"] or nav["hist"][-1] != entry["id"]:     # the same song twice in a row is one step back
                nav["hist"].append(entry["id"])
    if entry.get("id"):
        log_listen(entry)               # playout only runs while someone listens, so every airing was heard


def _play_track(t):
    d = app.DATA / t["id"]
    src = d / "song.mp3"
    if not src.is_file():
        src = d / "song" / "audio.flac"
    proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src), *audio_filters(t.get("seconds")),
                             *ENCODE], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    bc["skip"].clear()
    if t.get("tuned", t.get("station")) == state["station"]:
        bc["retuned"] = False                  # the new station's own song is on: nothing left to cut over
    if bc["cut_next"]:                     # skipped or retuned: drop the old song's buffered audio and refill LEAD at once,
        with bc_lock:                      # so players that reconnect start right on this song with a full cushion
            bc["buf"].clear(); bc["epoch"] = time.time() - bc["bytes"] / BPS
            bc["cut"] += 1; bc["cut_next"] = False
    _air(t | {"duration": t.get("seconds")})
    app.log("radio on air:", t["title"], f"({len(bc['listeners'])} listening)")
    checked, start, pending, empty_since = 0.0, bc["bytes"], b"", None
    aired_at, present, finished = time.time(), set(bc["listeners"]), False
    try:
        while True:
            chunk = os.read(proc.stdout.fileno(), 8192)
            if not chunk:
                finished = True                    # the whole song went out
                break
            pending += chunk                       # only send whole MP3 frames so a skip never leaves a torn frame
            n = len(pending) // FRAME * FRAME
            if not n:
                continue
            _broadcast(pending[:n]); pending = pending[n:]
            if bc["skip"].is_set() or not state["on"]:
                bc["cut_next"] = state["on"]
                break
            if bc["listeners"]:
                empty_since = None
            elif empty_since is None:
                empty_since = time.time()
            elif time.time() - empty_since > 5:    # everyone left (a player reconnecting after ⏮/⏭ is back within a second)
                if (bc["bytes"] - start) / BPS < 30 and t.get("airing") == "new":   # barely started -> back on the spool
                    with app.lock:
                        j = app.jobs.get(t["id"])
                        if j:
                            j.update(radio_status="approved", played_at=None); app.save_job(j)
                break
            if time.time() - checked > 1:          # retuned to another station: cut over once it has a song ready
                checked = time.time()               # (only a real retune: a filler rerun plays to its end when a new song is ready)
                if bc.get("retuned") and t.get("tuned", t.get("station")) != state["station"] and station_tracks(state["station"]):
                    bc["retuned"] = False
                    bc["cut_next"] = True
                    break
    finally:
        proc.kill(); proc.wait()
        try:
            _listening_signal(t, aired_at, present, finished and not bc["skip"].is_set())
        except Exception:
            app.log("radio listening-signal error:", traceback.format_exc())


# ----------------------------------------------------------------------------- implicit feedback
# Few listeners press the thumbs, so how people listen counts as mild feedback: a song someone heard through to the
# end is a soft like ("heard"); tuning out 10-60 s into a song, after listening for a while, is a soft skip ("left").
# Both feed the taste notes (more weakly than thumbs); explicit thumbs on a song always win.
IMPLICIT_EVERY = 5                      # rewrite the taste notes after this many new listening signals
_departures = collections.deque(maxlen=200)   # (listener id, left at, joined at)


def _listening_signal(t, aired_at, present, finished):
    if not t.get("id") or not present:
        return
    stayed = [lid for lid in present if lid in bc["listeners"]]
    lo, hi = aired_at + LEAD + 10, aired_at + LEAD + 60        # listeners hear the stream LEAD seconds late
    left = [lid for lid, at, joined in list(_departures)
            if lid in present and lid not in bc["listeners"] and lo <= at <= hi and aired_at - joined >= 120]
    if finished and stayed:
        implicit(t["id"], "heard")
    elif left and not bc["skip"].is_set():
        implicit(t["id"], "left")


def implicit(track_id, kind):
    with app.lock:
        j = app.jobs.get(track_id)
    if not j or j.get("source") != "radio" or j.get("radio_feedback"):
        return                                  # thumbs already given: they speak louder
    s = stations.get(j.get("station"))
    if not s:
        return
    with lock:
        s["implicit"] = ([f for f in s.get("implicit", []) if not (f["track"] == track_id and f["kind"] == kind)]
                         + [{"track": track_id, "kind": kind, "t": time.time()}])[-200:]
        s["stats"][kind] = s["stats"].get(kind, 0) + 1
        s["implicit_new"] = s.get("implicit_new", 0) + 1
        due = s["implicit_new"] >= IMPLICIT_EVERY
        if due:
            s["implicit_new"] = 0
        save_station(s)
    with app.lock:
        sig = j.setdefault("listen_signals", {})
        sig[kind] = sig.get(kind, 0) + 1
    app.save_job(j)
    app.log(f"radio: listening signal '{kind}' for {j['title']}")
    if due:
        threading.Thread(target=update_taste, args=(s["id"],), daemon=True).start()


def _play_silence():
    """Dead air while the radio is off or the next song is still being made (keeps listener connections open)."""
    _air(None)
    while bc["listeners"]:
        for i in range(0, len(bc["silence"]), 8 * FRAME):
            _broadcast(bc["silence"][i:i + 8 * FRAME])
        if nav["queued"] or _upnext_job() or (state["on"] and state["station"] and next_track(state["station"], mark=False)):   # ⏮, a new song, runner-up or rerun
            return


def playout_loop():
    _make_silence()
    while True:
        try:
            if not bc["listeners"]:                 # nobody listening: don't burn songs into the void
                if bc["now"] is not None:
                    _air(None)
                bc["wake"].wait(2); bc["wake"].clear()
                continue
            if BITRATE != ACTIVE_BITRATE:
                _switch_format()
                continue
            t = (_take_queued() or _take_upnext() or _nav_next() or next_track(state["station"])) if state["on"] and state["station"] else None
            if t:
                _play_track(t)
                for _ in range(round(GAP / 1.152)):       # a little silence between songs
                    if not bc["listeners"] or bc["cut_next"]:     # skipped: straight to the next song
                        break
                    for i in range(0, len(bc["silence"]), 8 * FRAME):
                        _broadcast(bc["silence"][i:i + 8 * FRAME])
            else:
                _play_silence()
        except Exception:
            app.log("radio playout error:", traceback.format_exc()); time.sleep(1)


def stream(h):
    """GET /radio/stream.mp3 - the live broadcast. ?l=<id> lets the deck match up what it hears with the playlist."""
    lid = re.sub(r"\W", "", (h.path.split("l=", 1)[1] if "l=" in h.path else "").split("&")[0])[:40] or uuid.uuid4().hex[:12]
    q = queue.Queue(maxsize=400)                    # ~2 minutes of slack before a stuck client is dropped
    with bc_lock:
        if time.time() - bc["last"] > STALE:        # stream was idle for long: the buffer holds stale audio
            bc["buf"].clear()
        burst = bytes(bc["buf"])
        bc["listeners"][lid] = {"q": q, "p0": (bc["bytes"] - len(burst)) / BPS, "since": time.time(),
                                "agent": (h.headers.get("User-Agent") or "")[:80], "device": _agent(h), "sent": 0,
                                "samples": collections.deque(maxlen=64)}
    _joined[(lid, id(q))] = time.time()
    peer = h.headers.get("X-Forwarded-For") or h.client_address[0]
    app.log(f"radio: listener {lid[:6]} joined from {peer} ({_agent(h)}), {len(burst) / BPS:.0f} s cushion")
    why = "left"
    bc["wake"].set()
    try:
        h.send_response(200)
        for k, v in (("Content-Type", "audio/mpeg"), ("Cache-Control", "no-cache, no-store"), ("Connection", "close"),
                     ("Access-Control-Allow-Origin", "*"), ("icy-name", "Agentora"), ("X-Content-Type-Options", "nosniff")):
            h.send_header(k, v)
        h.end_headers()
        li = bc["listeners"][lid]
        if burst:
            h.wfile.write(burst)
            _count(li, len(burst))
        while lid in bc["listeners"] and bc["listeners"][lid]["q"] is q:
            try:
                chunk = q.get(timeout=5)
            except queue.Empty:
                continue
            h.wfile.write(chunk)
            _count(li, len(chunk))
        else:
            why = "dropped by the server (too slow to keep up, or a format change)"
    except (BrokenPipeError, ConnectionResetError, OSError) as e:
        why = f"connection closed ({type(e).__name__})"
    finally:
        with bc_lock:
            if bc["listeners"].get(lid, {}).get("q") is q:
                bc["listeners"].pop(lid, None)
        _departures.append((lid, time.time(), _joined.get((lid, id(q)), time.time())))
        app.log(f"radio: listener {lid[:6]} {why} after {time.time() - bc_since(lid, q):.0f} s")
    h.close_connection = True


def _count(li, n):
    li["sent"] += n
    li["samples"].append((time.time(), li["sent"]))


def listeners_view(me=None):
    """Per-listener metrics: what the server delivered over the last ~10 s and how much is still queued for it."""
    now, out = time.time(), []
    with bc_lock:
        for lid, li in bc["listeners"].items():
            recent = [x for x in li["samples"] if now - x[0] <= 10]
            rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0]) if len(recent) > 1 and recent[-1][0] > recent[0][0] else 0
            out.append({"id": lid[:6], "me": lid == me, "device": li.get("device", "?"), "seconds": round(now - li["since"]),
                        "kbps": round(rate * 8 / 1000), "sent_mb": round(li["sent"] / 2**20, 1), "sent": li["sent"],
                        "backlog_s": round(li["q"].qsize() * 8192 / BPS, 1)})
    return sorted(out, key=lambda x: (not x["me"], x["seconds"]))


_noise_cache = b""


def _noise(n):
    """Incompressible bytes for the speed test (cached)."""
    global _noise_cache
    if len(_noise_cache) < n:
        _noise_cache = os.urandom(n)
    return _noise_cache[:n]

_joined = {}


def bc_since(lid, q):
    return _joined.pop((lid, id(q)), time.time())


def _agent(h):
    ua = h.headers.get("User-Agent") or ""
    for key, name in (("VLC", "VLC"), ("Android", "Android"), ("iPhone", "iPhone"), ("Windows", "Windows"), ("Macintosh", "Mac"),
                      ("Linux", "Linux"), ("curl", "curl"), ("Python", "script")):
        if key in ua:
            return name + (" Chrome" if "Chrome" in ua and key != "VLC" else "")
    return ua[:30] or "unknown"

def skip(track_id, kind="skip"):
    """Skip (or NOPE) the song on air - for everyone, it's one broadcast. After ⏮, ⏭ steps forward through the history."""
    if track_id and (kind != "skip" or nav["cursor"] is None):      # stepping through the history isn't a skip vote
        feedback(track_id, kind)
    now, cut = bc["now"], bc["cut"]
    if now and track_id and now["id"] != track_id and now.get("start_pos") is not None:
        # the listener still hears the song before (the stream runs LEAD ahead) and the next one is already on air:
        # drop the old song's tail from the buffer, so a reconnecting player starts on the current song
        with bc_lock:
            keep = round((bc["bytes"] / BPS - now["start_pos"]) * BPS / FRAME) * FRAME
            if 0 < keep < len(bc["buf"]):
                del bc["buf"][:len(bc["buf"]) - keep]
                bc["cut"] += 1
        return {"ok": True, "cut": bc["cut"]}
    if now and (not track_id or now["id"] == track_id):
        bc["skip"].set()
        return _wait_cut(cut)
    return {"ok": True, "cut": bc["cut"]}


def _wait_cut(cut):
    t0 = time.time()
    while bc["cut"] == cut and time.time() - t0 < 8:      # the next song is on air: players can reconnect to it now
        time.sleep(0.1)
    return {"ok": True, "cut": bc["cut"]}


# ---- ⏮ / ⏭ through the song history, like any player: ⏮ restarts the song, ⏮ again goes to the one before, and so
# on; ⏭ then walks forward again, and past the newest song new songs resume. One broadcast: it moves everyone listening.
nav = {"hist": collections.deque(maxlen=200), "cursor": None, "queued": None}   # hist: song ids as they aired
nav_lock = threading.Lock()


def _hist_track(i):
    with app.lock:
        j = app.jobs.get(nav["hist"][i])
    if not j or j.get("status") != "done":
        return None
    return track_view(j) | {"airing": "history", "tuned": state["station"]}


def _upnext_job(take=False):
    """📻 Play next: the first song on the list that has finished recording (songs still recording wait their turn;
    failed or deleted ones drop off). take=True removes it from the list."""
    with lock:
        ids = state.setdefault("upnext", [])
        for jid in list(ids):
            j = app.jobs.get(jid)                     # a plain dict read: no app.lock while holding ours
            if not j or j.get("status") in ("failed", "cancelled"):
                ids.remove(jid); save_state(); continue
            if j.get("status") == "done" and (app.DATA / jid / "song.mp3").is_file():
                if take:
                    ids.remove(jid); save_state()
                return j
    return None


def _take_upnext():
    j = _upnext_job(take=True)
    if j:
        app.log(f"radio: playing next on request: {j['title']!r}")
        return track_view(j) | {"airing": "request", "tuned": state["station"]}
    return None


def play_next(ids):
    """Add songs (any song in the library, e.g. a Music Studio rewrite) to the front of the broadcast, in order."""
    with app.lock:
        ok = [i for i in ids if isinstance(i, str) and i in app.jobs]
    if not ok:
        raise app.BadRequest("No such song")
    with lock:
        lst = state.setdefault("upnext", [])
        lst.extend(i for i in ok if i not in lst)
        save_state()
    bc["wake"].set()
    return {"ok": True, "upnext": upnext_view()}


def upnext_view():
    out = []
    for jid in state.get("upnext") or []:
        with app.lock:
            j = app.jobs.get(jid)
        if j:
            out.append({"id": jid, "title": j["title"], "ready": j.get("status") == "done", "status": j.get("status")})
    return out


def _take_queued():
    with nav_lock:
        t, nav["queued"] = nav["queued"], None
    return t


def _nav_next():
    """The next song while walking the history (after ⏮); None once past the newest one: new songs again."""
    with nav_lock:
        while nav["cursor"] is not None and nav["cursor"] < len(nav["hist"]) - 1:
            nav["cursor"] += 1
            t = _hist_track(nav["cursor"])
            if t:
                return t
        nav["cursor"] = None
    return None


def back(track_id=None, steps=0):
    """⏮: steps=0 restarts the song (the one the caller hears), steps=N goes N songs further back."""
    with nav_lock:
        hist = nav["hist"]
        if len(hist) < 2 and state["station"]:          # just restarted: rebuild the history from the station's play log
            older, keep = [t["id"] for t in reversed(history(state["station"], 50))], list(hist)   # oldest first
            hist.clear(); hist.extend([x for x in older if x not in keep] + keep)
        if not hist:
            return {"ok": False, "cut": bc["cut"], "error": "Nothing has played yet"}
        if nav["cursor"] is None or (track_id and hist[nav["cursor"]] != track_id):
            idx = [k for k, x in enumerate(hist) if x == track_id]
            nav["cursor"] = idx[-1] if idx else len(hist) - 1
        c = max(0, nav["cursor"] - max(0, steps))
        t = _hist_track(c)
        while not t and c > 0:                          # a deleted song: one further back
            c -= 1; t = _hist_track(c)
        if not t:
            return {"ok": False, "cut": bc["cut"], "error": "That song isn't available any more"}
        nav["cursor"], nav["queued"] = c, t
    app.log(f"radio: ⏮ steps={steps} heard={track_id} -> {t['title']!r} ({c + 1} of {len(hist)} in the history)")
    cut = bc["cut"]
    bc["cut_next"] = True
    bc["skip"].set()
    out = _wait_cut(cut)
    return out | {"title": t["title"], "first": c == 0}


def stream_view(listener=None):
    with bc_lock:
        li = bc["listeners"].get(listener) if listener else None
        return {"pos": round(bc["bytes"] / BPS, 3), "listeners": len(bc["listeners"]), "aired": bc["aired"],
                "now": bc["now"], "p0": li["p0"] if li else None, "url": "/radio/stream.mp3",
                "clock": round(time.time() - bc["epoch"], 3) if bc["listeners"] else None,   # real-time position
                "bitrate": ACTIVE_BITRATE, "lead": round(LEAD, 1), "cut": bc["cut"]}


# ----------------------------------------------------------------------------- API

def track_view(j):
    cover = art.stamp(art.song_path(j["id"]))
    return {"cover": f"/files/{j['id']}/cover.jpg?v={cover}" if cover else None, "id": j["id"], "title": j["title"], "style": j["style"], "lyrics": j.get("lyrics", ""), "mode": j["mode"],
            "seconds": j.get("seconds"), "station": j.get("station"), "critic": j.get("critic"), "feedback": j.get("radio_feedback"),
            "played_at": j.get("played_at"), "audio": f"/files/{j['id']}/song.mp3", "in_playlist": bool(j.get("playlist_at")),
            "station_name": stations[j["station"]]["name"] if j.get("station") in stations else None}


def rerun_library():
    with app.lock:
        aired = [j for j in app.jobs.values() if j.get("source") == "radio" and j["status"] == "done" and j.get("played_at")
                 and j.get("radio_feedback") not in ("skip", "dislike")]
    return {"id": RERUNS, "songs": len(aired), "stations": len({j.get("station") for j in aired}),
            "favorites": sum(1 for j in aired if loved(j))}


def api_state(listener=None):
    with lock:
        st = []
        for s in sorted(stations.values(), key=lambda s: s["created"]):
            st.append({k: s.get(k) for k in ("id", "name", "kind", "description", "vocals", "language", "theme", "stats", "analysis")} |
                      {"art": art.stamp(art.station_path(s["id"])), "asked": requested.get(s["id"], 0),
                       "taste": (s.get("taste") or {}).get("notes"), "banked": len(s.get("bank") or []), "ready": len(station_tracks(s["id"])), "inflight": inflight(s["id"]),
                       "problem": (backoff[s["id"]]["reason"] + f" — retrying in {int(backoff[s['id']]['until'] - time.time())}s")
                                  if s["id"] in backoff and time.time() < backoff[s["id"]]["until"] else None,
                       "profile": {k: v for k, v in (s.get("profile") or {}).items() if k in ("tracks", "top", "bpm_median", "bpm_range", "keys", "vocal_share")} or None,
                       "files": len([p for p in (sdir(s["id"]) / "music").rglob("*") if p.suffix.lower() in AUDIO_EXT]) if s["kind"] == "folder" and (sdir(s["id"]) / "music").exists() else None})
    with app.lock:
        making = [{"title": j["title"], "status": j["status"], "stage": j.get("stage"), "progress": j.get("progress"), "station": j.get("station"),
                   "requested": bool(j.get("requested"))}
                  for j in app.jobs.values() if j.get("source") == "radio" and j["status"] in ("queued", "running")]
        judging = sum(1 for j in app.jobs.values() if j.get("radio_status") == "judging")
    return {"on": state["on"], "station": state["station"], "target_ahead": state["target_ahead"], "stations": st,
            "making": making, "judging": judging, "upnext": upnext_view(), "gpu": app.state()["gpu"], "stream": stream_view(listener),
            "reruns": rerun_library()}


def _filler(sid):
    """Nothing approved is ready: rather than dead air, air the best recent runner-up (a sane take that narrowly
    missed the critic's bar), else a rerun of one of the station's own songs (favorites first, then its best-scored;
    nothing aired in the last 45 min, nothing skipped or disliked)."""
    now = time.time()
    with app.lock:
        mine = [j for j in app.jobs.values() if j.get("source") == "radio" and j.get("station") == sid and j["status"] == "done"
                and (app.DATA / j["id"] / "song.mp3").is_file()]
    sung = {j.get("batch") for j in mine if j.get("radio_status") in ("approved", "played")}   # songs whose best take airs
    runners = [j for j in mine if j.get("radio_status") == "rejected" and not j.get("played_at") and (j.get("critic") or {}).get("sane")
               and now - j.get("finished", 0) < 12 * 3600 and j.get("batch") not in sung]   # a spare take is no new song
    if runners:
        return max(runners, key=lambda j: j["critic"]["score"]), "runner-up"
    reruns = [j for j in mine if j.get("radio_status") == "played" and now - (j.get("played_at") or 0) > 45 * 60
              and j.get("radio_feedback") not in ("skip", "dislike")]
    if reruns:
        reruns.sort(key=lambda j: (loved(j), (j.get("critic") or {}).get("score", 0)), reverse=True)
        return random.choice(reruns[:5]), "rerun"
    return None, None


def _rerun_pick():
    """Rerun mode: a weighted shuffle over every song that has aired on any station - favorites 3x, better critic
    scores a little more, nothing aired in the last 3 h, never skipped/disliked songs, and preferably not the
    same station twice in a row."""
    now = time.time()
    with app.lock:
        pool = [j for j in app.jobs.values() if j.get("source") == "radio" and j["status"] == "done" and j.get("played_at")
                and j.get("radio_feedback") not in ("skip", "dislike") and (app.DATA / j["id"] / "song.mp3").is_file()
                and (j.get("mode") == "instrumental" or not j.get("lyrics") or sung_lines(j["lyrics"]) >= 8)]   # not the tags-only ones
    if not pool:
        return None
    fresh = [j for j in pool if now - j["played_at"] > 3 * 3600]
    if not fresh:                                   # small library: the least recently aired third
        fresh = sorted(pool, key=lambda j: j["played_at"])[:max(1, len(pool) // 3)]
    last = (bc["aired"][-1] if bc["aired"] else {}).get("station")
    w = [(3.0 if loved(j) else 1.0) * (0.5 + max(0.0, (j.get("critic") or {}).get("score", 0.7)))
         * (0.4 if j.get("station") == last else 1.0) for j in fresh]
    return random.choices(fresh, weights=w)[0]


def next_track(sid, mark=True):
    if sid == RERUNS:
        j, airing, ready = _rerun_pick(), "rerun", None
    else:
        with app.lock:
            ready = sorted(station_tracks(sid, ("approved",)), key=lambda j: j.get("approved_at", 0))
        j, airing = (ready[0], "new") if ready else _filler(sid)
    if not j:
        return None
    if mark:
        with app.lock:
            j.update(radio_status="played", played_at=time.time(), airings=j.get("airings", 0) + 1); app.save_job(j)
        if sid in stations:
            with lock:
                stations[sid]["stats"]["played"] += 1; save_station(stations[sid])
        if airing != "new" and sid != RERUNS:
            app.log(f"radio: spool empty, airing a {airing}: {j['title']}")
    return track_view(j) | {"airing": airing}


def favorites():
    """Every radio song a listener kept (KEEP / ♥), across all stations, newest favorite first."""
    with app.lock:
        fav = [j for j in app.jobs.values() if j.get("source") == "radio" and j.get("radio_feedback") == "keep"]
    kept = {}
    with lock:
        for s in stations.values():
            for f in s["feedback"]:
                if f["kind"] == "keep":
                    kept[f["track"]] = f["t"]
    out = []
    for j in fav:
        st = stations.get(j.get("station"))
        out.append(track_view(j) | {"station_name": st["name"] if st else "(deleted station)",
                                    "kept_at": kept.get(j["id"]) or j.get("played_at") or j.get("finished") or 0})
    return sorted(out, key=lambda t: -t["kept_at"])


def history(sid, limit=40, upcoming=False):
    """Songs a station has aired, newest first; upcoming=True also lists the recorded songs waiting to air (first)."""
    with app.lock:
        played = sorted((j for j in app.jobs.values() if j.get("source") == "radio" and j.get("played_at")
                         and (sid == RERUNS or j.get("station") == sid)),
                        key=lambda j: -j["played_at"])[:limit]
    ready = sorted(station_tracks(sid, ("approved",)), key=lambda j: j.get("approved_at", 0)) if upcoming and sid != RERUNS else []
    return [track_view(j) | {"up_next": True} for j in ready] + [track_view(j) | {"airings": j.get("airings", 1)} for j in played]


def feedback(track_id, kind):
    if kind not in ("keep", "skip", "dislike", "clear"):
        raise app.BadRequest("feedback must be keep, skip, dislike or clear")
    with app.lock:
        j = app.jobs.get(track_id)
    if not j:
        raise app.BadRequest("No such song")
    # any library song can be on air (📻 Play next); a Music Studio rewrite teaches its original's station
    orig = app.jobs.get(j.get("rewrite_of") or "") or {}
    s = stations.get(j.get("station") or orig.get("station"))
    with lock:
        if s:
            s["feedback"] = [f for f in s["feedback"] if f["track"] != track_id]
            if kind != "clear":
                s["feedback"].append({"track": track_id, "kind": kind, "title": j["title"], "style": j["style"], "t": time.time()})
                s["feedback"] = s["feedback"][-200:]
                s["stats"][{"keep": "kept", "skip": "skipped", "dislike": "disliked"}[kind]] += 1
            save_station(s)
    j["radio_feedback"] = None if kind == "clear" else kind
    app.save_job(j)
    if s and kind in ("keep", "dislike", "clear"):
        threading.Thread(target=update_taste, args=(s["id"],), daemon=True).start()
    return {"ok": True}


def set_power(on, sid=None):
    with lock:
        if sid:
            if sid not in stations and sid != RERUNS:
                raise app.BadRequest("No such station")
            if sid != state["station"]:
                bc["retuned"] = True               # the song on air cuts over once the new station has one ready
                with nav_lock:
                    nav["hist"].clear(); nav["cursor"] = None; nav["queued"] = None
            state["station"] = sid
        if on and not state["station"]:
            raise app.BadRequest("Pick a station first")
        state["on"] = bool(on)
        save_state()
    if not on or state["station"] == RERUNS:
        with app.lock:                         # drop queued (and, when switching off, in-progress) radio work so the GPUs free up
            for j in app.jobs.values():
                if j.get("requested"):
                    continue                           # songs someone asked for are made anyway
                if j.get("source") == "radio" and j["status"] == "queued":
                    j.update(status="cancelled", stage="", finished=time.time(), radio_status="cancelled"); app.save_job(j)
                elif j.get("source") == "radio" and j["status"] == "running" and not on:
                    j["_cancel"] = True
            studio_busy = any(j["status"] in ("queued", "running") and j.get("source") != "radio" for j in app.jobs.values())
            if not studio_busy:
                app.gpu["release_on_idle"] = True   # radio off -> give the GPUs back once running takes finish
    else:
        app.gpu["release_on_idle"] = False
    return {"ok": True, "on": state["on"], "station": state["station"]}


def upload(sid, name, data):
    s = stations.get(sid)
    if not s or s["kind"] != "folder":
        raise app.BadRequest("Uploads go to a folder station")
    name = re.sub(r"[^\w .()\[\]-]+", "_", Path(name).name).strip()[:120]
    if Path(name).suffix.lower() not in AUDIO_EXT:
        raise app.BadRequest("Unsupported file type")
    d = sdir(sid) / "music"; d.mkdir(parents=True, exist_ok=True)
    stem, ext, n = Path(name).stem, Path(name).suffix, 2
    while (d / name).exists():
        name = f"{stem} ({n}){ext}"; n += 1
    (d / name).write_bytes(data)
    return {"ok": True, "file": name}


def start_analysis(sid):
    s = stations.get(sid)
    if not s or s["kind"] != "folder":
        raise app.BadRequest("Only folder stations are analyzed")
    if s["analysis"].get("status") == "running":
        return {"ok": True}
    threading.Thread(target=analyze_station, args=(sid,), daemon=True).start()
    return {"ok": True}


# ----------------------------------------------------------------------------- playlist + listen history

def loved(j):
    """Thumbs up or saved to the playlist: such songs return more often in reruns and as filler."""
    return j.get("radio_feedback") == "keep" or bool(j.get("playlist_at"))


def all_songs(limit=3000):
    """🎵 All songs: every finished song with audio - aired or waiting radio songs from every station, and Music Studio songs."""
    with app.lock:
        js = [j for j in app.jobs.values() if j.get("status") == "done" and (app.DATA / j["id"] / "song.mp3").is_file()
              and (j.get("source") != "radio" or j.get("radio_status") in ("approved", "played"))]
    js.sort(key=lambda j: -(j.get("played_at") or j.get("finished") or j.get("created") or 0))
    return [track_view(j) | {"created": j.get("created"), "airings": j.get("airings", 0), "source": j.get("source", "studio"),
                             "up_next": j.get("source") == "radio" and j.get("radio_status") == "approved"} for j in js[:limit]]


def playlist():
    """Songs saved with +, any station, most recently added first."""
    with app.lock:
        saved = [j for j in app.jobs.values() if j.get("playlist_at")]
    return [track_view(j) | {"added_at": j["playlist_at"]} for j in sorted(saved, key=lambda j: -j["playlist_at"])]


def set_playlist(track_id, on):
    with app.lock:
        j = app.jobs.get(track_id)
        if not j:
            raise app.BadRequest("No such song")
        j["playlist_at"] = time.time() if on else None
        app.save_job(j)
    if j.get("station") in stations:                       # a saved song counts as a like
        threading.Thread(target=update_taste, args=(j["station"],), daemon=True).start()
    return {"ok": True, "in_playlist": on}


LISTENED_KEEP = 3000                    # history entries kept on disk


def _listened_path():
    p = RADIO / "listened.jsonl"
    if not p.exists():                  # first run: seed from what already aired (one entry per song, its last airing)
        with app.lock:
            aired = sorted((j for j in app.jobs.values() if j.get("source") == "radio" and j.get("played_at")),
                           key=lambda j: j["played_at"])
        p.write_text("".join(json.dumps({"t": j["played_at"], "track": j["id"], "station": j.get("station")}) + "\n" for j in aired))
    return p


def log_listen(entry):
    try:
        p = _listened_path()
        with listen_lock:
            with p.open("a") as f:
                f.write(json.dumps({"t": round(time.time(), 1), "track": entry["id"], "station": state.get("station"),
                                    "airing": entry.get("airing")}) + "\n")
            if p.stat().st_size > 400_000:          # trim now and then
                lines = p.read_text().splitlines()[-LISTENED_KEEP:]
                p.write_text("\n".join(lines) + "\n")
    except Exception:
        app.log("radio listen-history error:", traceback.format_exc())


def listened(limit=100):
    """What was played to listeners, newest first, across every station (reruns included)."""
    with listen_lock:
        lines = _listened_path().read_text().splitlines()[-limit:]
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        with app.lock:
            j = app.jobs.get(e["track"])
        if j:
            out.append(track_view(j) | {"listened_at": e["t"], "airing": e.get("airing"), "via_reruns": e.get("station") == RERUNS})
    return out


# ----------------------------------------------------------------------------- settings helpers

def gpu_memory():
    """GPU memory in use / total, for the sidebar meter (NVIDIA only; empty elsewhere)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()[0]
        name, used, total = [x.strip() for x in out.split(",")]
        return {"name": name, "used_mb": int(used), "total_mb": int(total)}
    except Exception:
        return {}

def writer_models():
    """Models each configured songwriter server offers (OpenAI /models), for the settings dialog's pickers."""
    out = {}
    for base in dict.fromkeys(u for u, _, _ in app.LYRICS_LLMS):
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=2) as r:
                out[base] = sorted(m["id"] for m in json.loads(r.read()).get("data", []))
        except Exception:
            out[base] = []
    return out


def update_station(sid, body):
    s = stations.get(sid)
    if not s:
        raise app.BadRequest("No such station")
    upd = {}
    if "name" in body:
        upd["name"] = app.clean_text(body.get("name"), "Station name", 60, required=True)
    if "description" in body:
        upd["description"] = app.clean_text(body.get("description"), "Description", 2000, required=(s["kind"] == "text"))
    if "vocals" in body:
        if body["vocals"] not in ("vocals", "instrumental", "mix"):
            raise app.BadRequest("vocals must be vocals, instrumental or mix")
        upd["vocals"] = body["vocals"]
    if "language" in body:
        upd["language"] = app.clean_text(body.get("language"), "Language", 40)
    if "theme" in body:
        upd["theme"] = app.clean_text(body.get("theme"), "Lyrics theme", 1000)
    with lock:
        if upd.get("description", s["description"]) != s["description"]:
            art.station_path(sid).unlink(missing_ok=True)   # a new sound gets new art
        if upd.get("description", s["description"]) != s["description"] and s["kind"] == "text":
            s["scores"] = []            # the critic scores against the description: a new one starts a new scale
        s.update(upd)
        save_station(s)
    return s


def clear_history():
    with listen_lock:
        _listened_path().write_text("")
    return {"ok": True}


ASSIST_SYSTEM = """You help someone set up a station on an AI radio. A songwriter model reads the station's description and
writes each song's style tags and lyrics; the YuE music model then sings it. Good descriptions are one paragraph of 25-70 words
naming: genres and sub-genres, signature instruments, vocal type (or instrumental), mood and energy, a tempo range in BPM,
and era or production feel. The music model knows no artists or song titles, so describe a named artist's SOUND instead of
naming them. Keep every specific thing the person asked for. No hashtags, no lists, no quotes.
Reply with JSON only."""


def station_assist(body):
    """✨ Improve a station's description (and theme), or 💡 suggest three directions, with the radio's songwriter model."""
    mode = body.get("mode")
    if mode not in ("improve", "suggest"):
        raise app.BadRequest("mode must be improve or suggest")
    cur = {k: str(body.get(k) or "")[:2000] for k in ("name", "description", "vocals", "language", "theme")}
    have = "\n".join(f"{k}: {v}" for k, v in cur.items() if v.strip()) or "(nothing yet)"
    if mode == "improve":
        if not (cur["description"].strip() or cur["name"].strip()):
            raise app.BadRequest("Write a name or a few words about the sound first")
        ask = ("EDIT this station description; do not replace it. It is the person's own idea and must stay recognisably theirs.\n"
               f"ORIGINAL DESCRIPTION: {cur['description'] or cur['name']}\n"
               + (f"Station name: {cur['name']}\n" if cur["name"] else "") + (f"Lyrics theme: {cur['theme']}\n" if cur["theme"] else "")
               + (f"Vocals: {cur['vocals']}\n" if cur["vocals"] else "") + (f"Language: {cur['language']}\n" if cur["language"] else "")
               + "\nRules: keep EVERY genre, instrument, artist, mood, place and detail the original mentions, in roughly the same order. "
               "Fix spelling (artist names especially). Keep artist names, and add a few words on what their sound is like. "
               "Only ADD what is missing: tempo range in BPM, era or production feel, vocal type, mood. Do not change the style, "
               "do not invent a different sound, do not drop anything. One paragraph, at most about twice the original length.\n"
               "Return {\"name\": the station name unchanged (or a short one if there was none), \"description\": the edited paragraph, "
               "\"theme\": the lyrics theme lightly polished, or \"\" if there was none}")
    else:
        ask = ("Suggest three DIFFERENT directions for this station: one close to what they have, one bolder twist, one surprising "
               "neighbouring style. Keep each description to 25-40 words. What they have so far:\n"
               f"{have}\n\nReturn {{\"ideas\": [{{\"name\", \"description\", "
               "\"vocals\": \"mix\"|\"vocals\"|\"instrumental\", \"theme\": one-sentence lyrics theme or \"\"}} x3]}}")
    last, best = None, None
    keep = _key_words(cur["description"] or cur["name"])          # what an improvement must not lose
    for base, model, label in [c for c in gpu_first(llm_candidates()) for _ in range(2 if mode == "improve" else 1)]:
        try:
            req = {"model": model, "temperature": 0.8 if mode == "suggest" else 0.3, "top_p": 0.9, "max_tokens": 900,
                   "chat_template_kwargs": {"enable_thinking": False},
                   "messages": [{"role": "system", "content": ASSIST_SYSTEM}, {"role": "user", "content": ask}], **app.LLM_EXTRA}
            app.llm_used(model)
            r = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/chat/completions", json.dumps(req).encode(),
                           {"Content-Type": "application/json"}), timeout=max(app.LLM_TIMEOUT, 300)).read())
            out = parse_json_reply(r["choices"][0]["message"]["content"])
            clean = lambda d: {"name": str(d.get("name") or "").strip()[:60], "description": str(d.get("description") or "").strip()[:2000],
                               "theme": str(d.get("theme") or "").strip()[:1000],
                               **({"vocals": d["vocals"]} if d.get("vocals") in ("mix", "vocals", "instrumental") else {})}
            if mode == "improve":
                res = clean(out)
                if not res["description"]:
                    raise ValueError("no description in the reply")
                if not cur["theme"]:
                    res["theme"] = ""                    # don't invent a lyrics theme they didn't ask for
                kept = _kept(keep, res["description"])
                if not best or kept > best[0]:
                    best = (kept, {"improved": res, "writer": label, "kept": round(kept, 2)})
                if kept >= 0.66:
                    return best[1]
                last = f"it dropped too much of the original ({kept:.0%} kept)"
                app.log(f"station assist ({label}): {last}, trying again")
                continue
            ideas = [clean(i) for i in (out.get("ideas") or []) if isinstance(i, dict)]
            ideas = [i for i in ideas if i["description"]][:3]
            if not ideas:
                raise ValueError("no ideas in the reply")
            return {"ideas": ideas, "writer": label}
        except Exception as e:
            last = e
            app.log(f"station assist ({label}): {e}")
    if best:
        return best[1]                                  # the most faithful version we got
    raise app.BadRequest(f"The songwriter model couldn't help right now ({last}). Try again in a moment.")


_STOP = set("with that this from have them they their there what when your into over than then also just like some very much more most "
            "about after before music sound songs song style station".split())


def _key_words(text):
    return {w for w in re.findall(r"[a-z][a-z0-9'/-]{3,}", str(text or "").lower()) if w not in _STOP}


def _kept(words, text):
    """Share of the original's meaningful words still in the result (prefix match, so 'guitars' keeps 'guitar')."""
    if not words:
        return 1.0
    low = str(text or "").lower()
    return sum(1 for w in words if w[:max(4, len(w) - 2)] in low) / len(words)


def handle(h, method, path):
    """Route /api/radio/* requests. Returns True if handled."""
    q = {}
    if "?" in h.path:
        q = dict(x.split("=", 1) for x in h.path.split("?", 1)[1].split("&") if "=" in x)
    try:
        if method == "GET" and path == "/api/radio/favorites":
            return h.send(200, {"tracks": favorites()}) or True
        if method == "GET" and path == "/api/radio/songs":
            return h.send(200, {"tracks": all_songs()}) or True
        if method == "GET" and path == "/api/radio/playlist":
            return h.send(200, {"tracks": playlist()}) or True
        if method == "POST" and path == "/api/radio/playlist":
            b = h.body(); return h.send(200, set_playlist(b.get("track", ""), bool(b.get("on")))) or True
        if method == "GET" and path == "/api/radio/ping":
            return h.send(200, {"t": time.time()}) or True
        if method == "GET" and path == "/api/radio/speedtest":
            n = max(1, min(int(q.get("mb") or 2), 20)) * 2**20
            return h.send(200, _noise(n), "application/octet-stream") or True
        if method == "GET" and path == "/api/radio/listeners":
            return h.send(200, {"listeners": listeners_view(q.get("me"))}) or True
        if method == "GET" and path == "/api/radio/gpu":
            return h.send(200, gpu_memory()) or True
        if method == "GET" and path == "/api/radio/settings":
            import settings
            return h.send(200, settings.view() | {"models": writer_models()}) or True
        if method == "PUT" and path == "/api/radio/settings":
            import settings
            return h.send(200, settings.update(h.body()) | {"models": writer_models()}) or True
        if method == "POST" and path == "/api/radio/history/clear":
            return h.send(200, clear_history()) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)", path)
        if method == "PUT" and m:
            return h.send(200, {"station": update_station(m.group(1), h.body())}) or True
        if method == "GET" and path == "/api/radio/listened":
            return h.send(200, {"tracks": listened(min(500, int(q.get("limit") or 100)))}) or True
        if method == "GET" and path == "/api/radio/state":
            return h.send(200, api_state(q.get("listener"))) or True
        if method == "POST" and path == "/api/radio/upnext":
            b = h.body(); return h.send(200, play_next(b.get("ids") or [])) or True
        if method == "POST" and path == "/api/radio/back":
            b = h.body(); return h.send(200, back(b.get("track"), int(b.get("steps") or 0))) or True
        if method == "POST" and path == "/api/radio/skip":
            b = h.body(); return h.send(200, skip(b.get("track"), "dislike" if b.get("kind") == "dislike" else "skip")) or True
        if method == "GET" and path == "/api/radio/next":
            t = next_track(q.get("station") or state["station"], mark=q.get("peek") != "1")
            return h.send(200, {"track": t}) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/history", path)
        if method == "GET" and m:
            return h.send(200, {"tracks": history(m.group(1), min(1000, int(q.get("limit") or 40)), q.get("upcoming") == "1")}) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/art", path)
        if method == "GET" and m:
            f = art.station_path(m.group(1))
            if not f.is_file():
                return h.send(404, {"error": "No art yet"}) or True
            return h.send(200, f.read_bytes(), "image/jpeg", {"Cache-Control": "private, max-age=604800"}) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/song", path)
        if method == "POST" and m:
            return h.send(200, request_song(m.group(1), (h.body() or {}).get("like"))) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/taste", path)
        if method == "DELETE" and m:
            return h.send(200, forget_taste(m.group(1))) or True
        m = re.fullmatch(r"/api/radio/tracks/([\w-]+)", path)
        if method == "GET" and m:
            return h.send(200, track_details(m.group(1))) or True
        if method == "POST" and path == "/api/radio/art":
            b = h.body()
            return h.send(200, art.request("station", b["station"]) if b.get("station") else art.request("song", str(b.get("track") or ""))) or True
        if method == "POST" and path == "/api/radio/stations/assist":
            return h.send(200, station_assist(h.body())) or True
        if method == "POST" and path == "/api/radio/stations":
            return h.send(200, {"station": create_station(h.body())}) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)", path)
        if method == "DELETE" and m:
            delete_station(m.group(1)); return h.send(200, {"ok": True}) or True
        if method == "POST" and path == "/api/radio/power":
            b = h.body(); return h.send(200, set_power(b.get("on"), b.get("station"))) or True
        if method == "POST" and path == "/api/radio/feedback":
            b = h.body(); return h.send(200, feedback(b.get("track", ""), b.get("kind", ""))) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/analyze", path)
        if method == "POST" and m:
            return h.send(200, start_analysis(m.group(1))) or True
        m = re.fullmatch(r"/api/radio/stations/([\w]+)/files/(.+)", path)
        if method == "PUT" and m:
            n = int(h.headers.get("Content-Length") or 0)
            if n > 200 * 2**20:
                raise app.BadRequest("File too large (200 MB max)")
            from urllib.parse import unquote
            return h.send(200, upload(m.group(1), unquote(m.group(2)), h.rfile.read(n))) or True
    except app.BadRequest as e:
        return h.send(400, {"error": str(e)}) or True
    except json.JSONDecodeError:
        return h.send(400, {"error": "Invalid JSON"}) or True
    return False
