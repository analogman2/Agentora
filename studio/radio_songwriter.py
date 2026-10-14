"""Songwriting: the spec writer (title, style, lyrics), the lyric editor, the GPU lyricist's bank, and the taste notes
learned from thumbs.
"""
import collections, json, os, queue, random, re, shutil, subprocess, sys, threading, time, traceback, urllib.request, uuid
from pathlib import Path
import numpy as np
import art                          # cover art for stations and songs (drawn by ComfyUI between songs)

import app
import radio_state as rs, radio_stations as rst, radio_spool as rsp, radio_broadcast as rbc, radio_library as rlb


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

# 🎲 Variety (a station's 0-100 slider). Every song stays in the station's sound; higher variety adds more "for variety"
# nudges, a warmer songwriter, and more often a WILDCARD: one ingredient borrowed from a neighbouring style.
WILDCARDS = [
    "borrow the rhythm or groove of a neighbouring genre", "blend in one instrument the station rarely uses",
    "cross it with a related genre from another decade", "give it a regional or world-music flavour that suits the sound",
    "reimagine it as a stripped-back acoustic version of the station's sound", "push it toward a harder, more energetic edge",
    "push it toward a softer, dreamier, more atmospheric side", "try an unusual tempo or time feel for this station (half-time, a waltz, double-time)",
    "fuse the station's sound with a jazz, soul, or funk touch", "add an electronic or synth texture to the station's sound",
    "make it a duet or call-and-response between two singers", "use a vocal style the station hasn't used (spoken verses, falsetto, choir, rap verse)"]
VARIETY_DEFAULT = 25                                     # the station sound plus one nudge per song (the old behaviour)


def variety_plan(s):
    """(nudges, wildcard or None, temperature) for one song of station s."""
    v = max(0, min(100, int(s.get("variety", VARIETY_DEFAULT))))
    nudges = random.sample(VARIATIONS, 0 if v < 10 else 1 if v < 45 else 2 if v < 75 else 3)
    wildcard = random.choice(WILDCARDS) if v >= 35 and random.random() < (v - 25) / 100 else None   # 10% at 35, 75% at 100
    temp = rs.SPEC_TEMPERATURE + (v - VARIETY_DEFAULT) / 75 * 0.2 if v > VARIETY_DEFAULT else rs.SPEC_TEMPERATURE - (VARIETY_DEFAULT - v) / VARIETY_DEFAULT * 0.2
    return nudges, wildcard, round(temp, 3)


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
    d = rs.sdir(s["id"]) / "music"
    if s["kind"] != "folder" or not d.exists():
        return []
    out = []
    for f in sorted(d.rglob("*")):
        if f.suffix.lower() not in rst.AUDIO_EXT:
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
    """Reachable LLMs in order. While the radio is on, the radio's own LLM is (or is about to be) paused for the
    GPUs, so the other servers go first."""
    cands = list(app.LYRICS_LLMS)
    if rs.state["on"] or app.gpu["owner"] != "llm":
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
    want_inst = s["vocals"] == "instrumental" or (s["vocals"] == "mix" and random.random() < rs.INSTRUMENTAL_SHARE)
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
        u.append("Learned from the station's own songs (automatic tags, can be wrong): " + rst.profile_text(s))
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
    nudges, wildcard, temperature = variety_plan(s)
    if nudges:
        u.append("For variety, this time: " + "; ".join(nudges) + ".")
    if wildcard and not like:
        u.append(f"WILDCARD for this song (the listener turned up this station's variety): {wildcard}. It must still be "
                 "clearly recognisable as this station - keep its core genre, mood and vocal style, and change only that one ingredient. "
                 "Name the borrowed element in the style tags.")
    elif (s.get("variety", VARIETY_DEFAULT)) < 10:
        u.append("Stay close to the station's core sound: no detours into other genres.")
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
        if not rs.state["on"]:
            break
        try:
            body = {"model": model, "temperature": temperature, "top_p": 0.95, "max_tokens": 2200,
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
            return {"title": title[:80], "style": style, "instrumental": inst, "lyrics": lyrics if not inst else "", "writer": label, "story": story,
                    **({"wildcard": wildcard} if wildcard and not like else {})}
        except Exception as e:
            app.log(f"radio: spec writer {label} failed ({e}); trying the next one")
    words = s["description"] or rst.profile_text(s)
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


def lyricist_may_run():
    """The 27B lyricist needs most of the card: only when nothing else is on it."""
    if app.gpu["owner"] in ("studio", "switching", "waiting") or art.inflight or app.comfy_busy() or app.assistant_on_gpu() \
            or app.broker_yield("radio-llm"):           # someone is waiting for the card (GPU broker)
        return False
    used = app.gpu_free_mib(0)
    return used is not None and used < 6000


def bank_loop():
    """Fill the current station's lyric bank with the GPU lyricist whenever the card is free."""
    while True:
        time.sleep(20)
        try:
            sid = rs.state["station"]
            if not rs.state["on"] or sid not in rs.stations:
                continue
            s = rs.stations[sid]
            if len(s.get("bank") or []) >= rs.BANK_SIZE or not lyricist_may_run():
                continue
            rs.bank_writing.set()
            try:
                t0 = time.time()
                spec = write_spec(s, writers=[rs.LYRICIST])
            finally:
                rs.bank_writing.clear()
                try:                                      # give the card straight back
                    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                           json.dumps({"model": rs.LYRICIST[1], "keep_alive": 0}).encode(), {"Content-Type": "application/json"}), timeout=30).read()
                except Exception:
                    pass
            if spec.get("writer") == rs.LYRICIST[2]:
                with rs.lock:
                    s.setdefault("bank", []).append(spec | {"banked": time.time()})
                    rs.save_station(s)
                app.log(f"radio: lyricist banked {spec['title']!r} for {s['name']} in {time.time() - t0:.0f}s")
        except Exception:
            rs.bank_writing.clear()
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
    s = rs.stations.get(sid)
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
                with rs.lock:
                    s["taste"] = {"notes": notes, "updated": time.time(), "from": len(loved) + len(disliked) + len(heard) + len(left)}
                    rs.save_station(s)
                app.log(f"radio: taste notes updated for {s['name']} from {len(loved)} likes / {len(disliked)} dislikes"
                        f" / {len(heard)} heard through / {len(left)} tuned out")
                return
        except Exception as e:
            app.log(f"radio: taste notes ({label}) failed: {e}")


def forget_taste(sid):
    s = rs.stations.get(sid)
    if not s:
        raise app.BadRequest("No such station")
    with rs.lock:
        s["taste"] = None
        s["feedback"] = []
        s["implicit"] = []
        rs.save_station(s)
    return {"ok": True}

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
