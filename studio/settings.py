"""Live settings for the Music Studio + radio, edited from the ⚙ Settings dialog.

Saved to <radio dir>/settings.json and applied immediately (no restart). Environment variables and the
code's built-in values are only the defaults: a saved setting wins over them. Things that need a restart
(GPU devices, port, data folder, network access) stay in the service's environment."""
import json, re, threading

lock = threading.Lock()
PATH = None
app = radio = None
defaults, values = {}, {}

# key, group, label, type, min, max, help
SCHEMA = [
    ("songs_ahead", "Broadcast", "Songs kept ahead", "int", 2, 12,
     "Songs ready or being made for the current station. More = fewer fill-ins after a station switch, more GPU time."),
    ("buffer_seconds", "Broadcast", "Stream buffer (seconds)", "int", 4, 60,
     "Audio each player holds against network hiccups. Listeners hear the broadcast this far behind live."),
    ("takes", "Song making", "Takes per song", "int", 1, 4,
     "Each song is recorded this many times and the critic airs the best take. 1 = fastest."),
    ("max_song_seconds", "Song making", "Longest song (seconds)", "int", 90, 420,
     "Safety cap; songs normally end on their own before this."),
    ("opener_seconds", "Song making", "Opening song length (seconds)", "int", 45, 240,
     "Cap for the short first song that starts a station with nothing ready."),
    ("instrumental_share", "Song making", "Instrumentals on 'mix' stations (%)", "pct", 0, 100,
     "How often a station set to 'mix' makes an instrumental instead of a song with vocals."),
    ("keep_text", "Quality filter", "Keep on text stations (%)", "pct", 50, 100,
     "Share of takes that pass the critic once a station has enough scores. Lower = pickier."),
    ("keep_learned", "Quality filter", "Keep on learned stations (%)", "pct", 30, 100,
     "Same, for stations that learn from your music."),
    ("warmup_scores", "Quality filter", "Scores before the full filter", "int", 2, 20,
     "Until a station has this many scores, only clear misses are dropped."),
    ("warmup_floor", "Quality filter", "Early filter strictness (% of median)", "pct", 0, 100,
     "While warming up, drop takes scoring under this share of the station's median score."),
    ("min_song_seconds", "Quality filter", "Shortest acceptable song (seconds)", "int", 10, 120,
     "Takes shorter than this are treated as broken and never air."),
    ("writers", "Songwriter", "Songwriter models", "writers", None, None,
     "OpenAI-compatible servers tried in order. The first that answers writes each song."),
    ("spec_temperature", "Songwriter", "Creativity (temperature)", "float", 0.1, 2.0,
     "Higher = more surprising titles and lyrics, lower = safer."),
    ("llm_timeout", "Songwriter", "Timeout per song (seconds)", "int", 30, 1800,
     "How long to wait for one song before trying the next model. CPU models answer one request at a time."),
    ("llm_extra", "Songwriter", "Extra request options (JSON)", "json", None, None,
     'Added to every songwriter request, e.g. {"reasoning_effort": "none"} so Ollama models skip their thinking step.'),
    ("stream_bitrate", "Sound", "Stream quality (kbit/s)", "choice", ["128", "192", "256", "320"], None,
     "MP3 bitrate of the broadcast. Higher sounds better and uses more data on phones. Applies at the next song; players reconnect by themselves."),
    ("loudness", "Sound", "Loudness evening (LUFS)", "choice", ["off", "-14", "-16", "-18", "-23"], None,
     "Brings every song to the same perceived volume. -14 is typical for streaming, -23 is broadcast TV (quieter, more dynamic)."),
    ("bass_db", "Sound", "Bass (dB)", "float", -12, 12, "Low-frequency tone control for everyone listening."),
    ("treble_db", "Sound", "Treble (dB)", "float", -12, 12, "High-frequency tone control for everyone listening."),
    ("stereo_width", "Sound", "Stereo width (%)", "pct", 0, 200, "0 = mono, 100 = as recorded, above 100 = wider."),
    ("compression", "Sound", "Compression", "choice", ["off", "light", "medium", "heavy"], None,
     "Evens out loud and quiet passages. Heavy sounds like FM radio; helps in cars and noisy rooms."),
    ("limiter", "Sound", "Peak limiter", "bool", None, None, "Prevents clipping and distortion on loud peaks."),
    ("fade_in", "Sound", "Fade-in (seconds)", "float", 0, 5, "Start of every song."),
    ("fade_out", "Sound", "Fade-out (seconds)", "float", 0, 10, "End of every song."),
    ("gap", "Sound", "Gap between songs (seconds)", "float", 0, 5, "A moment of silence between songs (rounded to ~1 s steps)."),
    ("ode_steps", "Music model", "Audio quality steps", "int", 8, 64,
     "Decoder steps when turning the score into sound. More = cleaner audio, slower renders. Default 32."),
    ("style_adherence", "Music model", "Style adherence", "float", 1.0, 5.0,
     "How strictly songs follow the style description. 1.0 = the model's natural default; higher can sound forced."),
    ("song_temperature", "Music model", "Song creativity (temperature)", "float", 0.1, 2.0,
     "Randomness of the sound itself. Lower = safer and more repetitive, higher = more adventurous and less stable."),
    ("song_top_p", "Music model", "Song top-p", "float", 0.5, 1.0, "Sampling cutoff for the sound. Lower = more conservative."),
    ("song_top_k", "Music model", "Song top-k", "int", 1, 1000, "How many candidate sound tokens are considered each step."),
    ("song_repetition_penalty", "Music model", "Repetition penalty", "float", 1.0, 2.0,
     "Discourages loops and stuck notes. Too high can make songs wander."),
    ("score_temperature", "Music model", "Melody creativity (temperature)", "float", 0.1, 2.0,
     "Randomness of the melody/score the song is built on."),
    ("return_gpu_minutes", "GPU", "Give the GPU back after idle (minutes)", "float", 0, 240,
     "When the radio has nothing to make for this long, it unloads YuE2. 0 = keep it loaded until the radio is switched off."),
    ("cover_art", "Art", "Cover art", "choice", ["stations and songs", "stations only", "off"], None,
     "Pictures for stations and songs, drawn by the Image & Video Studio's picture model between songs (never while one is recording)."),
    ("history_keep", "History", "Listen-history entries kept", "int", 100, 20000,
     "Older entries are dropped."),
]
KEYS = {row[0]: row for row in SCHEMA}


def current():
    """The values the running code uses right now (env vars and built-ins included)."""
    return {
        "songs_ahead": radio.state["target_ahead"], "buffer_seconds": round(radio.LEAD),
        "takes": radio.TAKES, "max_song_seconds": radio.MAX_SECONDS, "opener_seconds": radio.OPENER_SECONDS,
        "instrumental_share": radio.INSTRUMENTAL_SHARE, "keep_text": radio.KEEP_TEXT, "keep_learned": radio.KEEP_LEARNED,
        "warmup_scores": radio.WARMUP_SCORES, "warmup_floor": radio.WARMUP_FLOOR, "min_song_seconds": radio.MIN_SONG_SECONDS,
        "writers": [{"url": u, "model": m, "label": l} for u, m, l in app.LYRICS_LLMS],
        "spec_temperature": radio.SPEC_TEMPERATURE, "llm_timeout": app.LLM_TIMEOUT, "llm_extra": app.LLM_EXTRA,
        "return_gpu_minutes": app.RETURN_MINUTES, "history_keep": radio.LISTENED_KEEP,
        "stream_bitrate": str(radio.BITRATE), "loudness": "off" if radio.LOUDNESS is None else f"{radio.LOUDNESS:g}",
        "bass_db": radio.BASS_DB, "treble_db": radio.TREBLE_DB, "stereo_width": radio.STEREO_WIDTH,
        "compression": radio.COMPRESSION, "limiter": radio.LIMITER, "fade_in": radio.FADE_IN, "fade_out": radio.FADE_OUT,
        "gap": radio.GAP, "ode_steps": radio.ODE_STEPS, "style_adherence": radio.CFG_SCALE or 1.0,
        "song_temperature": radio.SONG_SAMPLING.get("temperature", 1.0), "song_top_p": radio.SONG_SAMPLING.get("top_p", 0.95),
        "song_top_k": radio.SONG_SAMPLING.get("top_k", 100), "song_repetition_penalty": radio.SONG_SAMPLING.get("repetition_penalty", 1.2),
        "score_temperature": radio.SCORE_SAMPLING.get("temperature", 1.0),
        "cover_art": __import__("art").MODE,
    }


def apply(v):
    radio.state["target_ahead"] = v["songs_ahead"]
    radio.set_buffer(v["buffer_seconds"])
    radio.BITRATE = int(v["stream_bitrate"])                # the playout switches format at the next song
    radio.LOUDNESS = None if v["loudness"] == "off" else float(v["loudness"])
    radio.BASS_DB, radio.TREBLE_DB, radio.STEREO_WIDTH = v["bass_db"], v["treble_db"], v["stereo_width"]
    radio.COMPRESSION, radio.LIMITER = v["compression"], v["limiter"]
    radio.FADE_IN, radio.FADE_OUT, radio.GAP = v["fade_in"], v["fade_out"], v["gap"]
    radio.ODE_STEPS = v["ode_steps"]
    radio.CFG_SCALE = None if v["style_adherence"] == 1.0 else v["style_adherence"]
    radio.SONG_SAMPLING = {"temperature": v["song_temperature"], "top_p": v["song_top_p"], "top_k": v["song_top_k"],
                           "repetition_penalty": v["song_repetition_penalty"]}
    radio.SCORE_SAMPLING = {"temperature": v["score_temperature"]}
    radio.TAKES = v["takes"]
    radio.MAX_SECONDS, radio.OPENER_SECONDS = v["max_song_seconds"], v["opener_seconds"]
    radio.INSTRUMENTAL_SHARE = v["instrumental_share"]
    radio.KEEP_TEXT, radio.KEEP_LEARNED = v["keep_text"], v["keep_learned"]
    radio.WARMUP_SCORES, radio.WARMUP_FLOOR, radio.MIN_SONG_SECONDS = v["warmup_scores"], v["warmup_floor"], v["min_song_seconds"]
    app.LYRICS_LLMS = [(w["url"], w["model"], w["label"]) for w in v["writers"]]
    radio.SPEC_TEMPERATURE = v["spec_temperature"]
    app.LLM_TIMEOUT, app.LLM_EXTRA = v["llm_timeout"], v["llm_extra"]
    app.RETURN_MINUTES = v["return_gpu_minutes"]
    radio.LISTENED_KEEP = v["history_keep"]
    __import__("art").MODE = v["cover_art"]


def validate(key, raw):
    _, _, label, kind, lo, hi, _ = KEYS[key]
    try:
        if kind == "int":
            v = int(raw)
        elif kind in ("float", "pct"):
            v = float(raw)
            if kind == "pct" and isinstance(raw, str):    # the dialog sends "85" (percent); stored as 0.85
                v = v / 100
        elif kind == "choice":
            v = str(raw)
            if v not in lo:
                raise ValueError
            return v
        elif kind == "bool":
            v = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "on", "yes")
            return v
        elif kind == "json":
            v = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(v, dict):
                raise ValueError
        elif kind == "writers":
            v = []
            for w in raw:
                url, model = str(w.get("url", "")).strip().rstrip("/"), str(w.get("model", "")).strip()
                if not re.match(r"^https?://", url) or not model:
                    raise ValueError
                v.append({"url": url, "model": model, "label": str(w.get("label") or model).strip()[:60]})
            if not v:
                raise ValueError
            return v
    except (TypeError, ValueError, AttributeError):
        raise app.BadRequest(f"{label}: invalid value")
    if kind == "pct":
        lo, hi = lo / 100, hi / 100
    if lo is not None and hi is not None and not lo <= v <= hi:
        shown = (f"{lo * 100:g}–{hi * 100:g} %" if kind == "pct" else f"{lo:g}–{hi:g}")
        raise app.BadRequest(f"{label}: must be {shown}")
    return v


def init(app_module, radio_module):
    """Called once at startup, after env vars are read: remember those as defaults, then apply saved settings."""
    global app, radio, PATH
    app, radio = app_module, radio_module
    PATH = radio.RADIO / "settings.json"
    defaults.update(current())
    saved = {}
    if PATH.exists():
        try:
            saved = json.loads(PATH.read_text())
        except ValueError:
            app.log("settings.json is not valid JSON; using defaults")
    v = dict(defaults)
    for k, raw in saved.items():
        if k in KEYS:
            try:
                v[k] = validate(k, raw)
            except app.BadRequest as e:
                app.log(f"settings: ignoring saved {k} ({e})")
    apply(v)
    values.clear(); values.update(v)


def update(changes):
    """Validate and apply a partial update (or {"reset": true} for all defaults); persist what differs from defaults."""
    with lock:
        if changes.get("reset"):
            v = dict(defaults)
        else:
            v = dict(values)
            for k, raw in changes.items():
                if k not in KEYS:
                    raise app.BadRequest(f"Unknown setting: {k}")
                v[k] = validate(k, raw)
        if v["opener_seconds"] > v["max_song_seconds"]:
            raise app.BadRequest("Opening song length can't be longer than the longest song")
        apply(v)
        values.clear(); values.update(v)
        PATH.write_text(json.dumps({k: x for k, x in v.items() if x != defaults[k]}, indent=1))
    return view()


def view():
    return {"values": values, "defaults": defaults,
            "schema": [{"key": k, "group": g, "label": lab, "type": t, "help": h,
                        **({"options": lo} if t == "choice" else {"min": lo, "max": hi})} for k, g, lab, t, lo, hi, h in SCHEMA]}
