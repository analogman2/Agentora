"""Stations: create / edit / delete, learning a station from a folder of songs (CLAP + librosa), and the ✨ Improve /
💡 Suggest helper for station descriptions.
"""
import collections, json, os, queue, random, re, shutil, subprocess, sys, threading, time, traceback, urllib.request, uuid
from pathlib import Path
import numpy as np
import art                          # cover art for stations and songs (drawn by ComfyUI between songs)

import app
import radio_state as rs, radio_songwriter as rsw, radio_spool as rsp, radio_broadcast as rbc, radio_library as rlb
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
         "theme": app.clean_text(body.get("theme"), "Lyrics theme", 1000), "variety": _variety(body.get("variety", 25)), "created": time.time(),
         "stats": {"generated": 0, "approved": 0, "rejected": 0, "played": 0, "kept": 0, "skipped": 0, "disliked": 0},
         "feedback": [], "scores": [], "analysis": {"status": "none" if kind == "folder" else "n/a"}, "profile": None}
    with rs.lock:
        rs.stations[s["id"]] = s
        rs.save_station(s)
    if kind == "folder":
        (rs.sdir(s["id"]) / "music").mkdir(parents=True, exist_ok=True)
    return s


def delete_station(sid):
    with rs.lock:
        s = rs.stations.pop(sid, None)
        if rs.state["station"] == sid:
            rs.state["station"] = None; rs.state["on"] = False
    if s:
        with app.lock:
            for j in list(app.jobs.values()):
                if j.get("station") == sid:
                    if j["status"] == "queued":
                        j.update(status="cancelled", finished=time.time())
                    elif j["status"] == "running":
                        j["_cancel"] = True
        shutil.rmtree(rs.sdir(sid), ignore_errors=True)
    rs.save_state()


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
    s = rs.stations[sid]
    music = rs.sdir(sid) / "music"
    files = sorted(p for p in music.rglob("*") if p.suffix.lower() in AUDIO_EXT)
    s["analysis"] = {"status": "running", "done": 0, "total": len(files), "started": time.time(), "errors": 0}
    rs.save_station(s)
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
        s["analysis"].update(status="failed", error="No readable audio files"); rs.save_station(s)
        return
    E = np.stack(embs)
    np.save(rs.sdir(sid) / "corpus.npy", E)
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
    rs.save_station(s)
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


def upload(sid, name, data):
    s = rs.stations.get(sid)
    if not s or s["kind"] != "folder":
        raise app.BadRequest("Uploads go to a folder station")
    name = re.sub(r"[^\w .()\[\]-]+", "_", Path(name).name).strip()[:120]
    if Path(name).suffix.lower() not in AUDIO_EXT:
        raise app.BadRequest("Unsupported file type")
    d = rs.sdir(sid) / "music"; d.mkdir(parents=True, exist_ok=True)
    stem, ext, n = Path(name).stem, Path(name).suffix, 2
    while (d / name).exists():
        name = f"{stem} ({n}){ext}"; n += 1
    (d / name).write_bytes(data)
    return {"ok": True, "file": name}


def start_analysis(sid):
    s = rs.stations.get(sid)
    if not s or s["kind"] != "folder":
        raise app.BadRequest("Only folder stations are analyzed")
    if s["analysis"].get("status") == "running":
        return {"ok": True}
    threading.Thread(target=analyze_station, args=(sid,), daemon=True).start()
    return {"ok": True}


def _variety(v):
    """🎲 Variety 0-100: 0 = always the station's core sound, 25 = default, 100 = adventurous (still in the station's sound)."""
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        raise app.BadRequest("variety must be a number from 0 to 100")


def update_station(sid, body):
    s = rs.stations.get(sid)
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
    if "variety" in body:
        upd["variety"] = _variety(body["variety"])
        if upd["variety"] != s.get("variety", 25):
            s["bank"] = []               # pre-written songs used the old setting: write new ones
    with rs.lock:
        if upd.get("description", s["description"]) != s["description"]:
            art.station_path(sid).unlink(missing_ok=True)   # a new sound gets new art
        if upd.get("description", s["description"]) != s["description"] and s["kind"] == "text":
            s["scores"] = []            # the critic scores against the description: a new one starts a new scale
        s.update(upd)
        rs.save_station(s)
    return s


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
    for base, model, label in [c for c in rsw.gpu_first(rsw.llm_candidates()) for _ in range(2 if mode == "improve" else 1)]:
        try:
            req = {"model": model, "temperature": 0.8 if mode == "suggest" else 0.3, "top_p": 0.9, "max_tokens": 900,
                   "chat_template_kwargs": {"enable_thinking": False},
                   "messages": [{"role": "system", "content": ASSIST_SYSTEM}, {"role": "user", "content": ask}], **app.LLM_EXTRA}
            app.llm_used(model)
            r = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/chat/completions", json.dumps(req).encode(),
                           {"Content-Type": "application/json"}), timeout=max(app.LLM_TIMEOUT, 300)).read())
            out = rsw.parse_json_reply(r["choices"][0]["message"]["content"])
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
