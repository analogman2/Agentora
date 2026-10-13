"""The spool: keeps a few approved songs ready per station - writes specs, queues takes on the GPU, scores them with the
CLAP critic and audio checks, and keeps the best.
"""
import collections, json, os, queue, random, re, shutil, subprocess, sys, threading, time, traceback, urllib.request, uuid
from pathlib import Path
import numpy as np
import art                          # cover art for stations and songs (drawn by ComfyUI between songs)

import app
import radio_state as rs, radio_stations as rst, radio_songwriter as rsw, radio_broadcast as rbc, radio_library as rlb


def generating():
    """True while the radio is on a real station (and so wants the GPUs); rerun mode leaves them to the LLM."""
    return rs.state["on"] and rs.state["station"] in rs.stations


# ----------------------------------------------------------------------------- spool

def station_tracks(sid, statuses=("approved",)):
    with app.lock:
        return [j for j in app.jobs.values() if j.get("source") == "radio" and j.get("station") == sid and j.get("radio_status") in statuses]


def inflight(sid):
    with app.lock:
        n = sum(1 for j in app.jobs.values() if j.get("source") == "radio" and j.get("station") == sid and
                (j["status"] in ("queued", "running") or j.get("radio_status") == "judging"))
    return n + rs.writing.get(sid, 0)


def spool_loop():
    while True:
        try:
            time.sleep(2)
            if time.time() - rs.state.get("heartbeat", 0) > 20:
                rs.save_state()                               # heartbeat: lets a quick restart resume the broadcast
            settle_stragglers()
            sid = rs.state["station"]
            if not rs.state["on"] or sid not in rs.stations:
                continue
            s = rs.stations[sid]
            if s["kind"] == "folder" and s["analysis"].get("status") != "done":
                continue
            b = rs.backoff.get(sid)
            if b and time.time() < b["until"]:
                continue
            with app.lock:
                last = sorted((j for j in app.jobs.values() if j.get("source") == "radio" and j.get("station") == sid
                               and j["status"] in ("done", "failed") and not (j.get("error") or "").startswith("Interrupted")),
                              key=lambda j: -j.get("finished", 0))[:3]
            if len(last) == 3 and all(j["status"] == "failed" for j in last) and not (b and b.get("seen") == last[0]["id"]):
                delay = min(600, (b["delay"] * 2) if b else 60)
                rs.backoff[sid] = {"until": time.time() + delay, "delay": delay, "seen": last[0]["id"],
                                "reason": (last[0].get("error") or "songs keep failing")[:160]}
                app.log(f"radio: 3 failed takes in a row on {s['name']}; pausing generation {delay}s")
                continue
            if last and last[0]["status"] == "done":
                rs.backoff.pop(sid, None)
            ready = len(station_tracks(sid, ("approved",)))
            fly = inflight(sid)
            limit = rs.state["max_inflight"] + 1 if ready else 2 * len(app.DEVICES)     # spool empty: both GPUs flat out
            if ready + fly < rs.state["target_ahead"] and fly < limit:
                with rs.lock:
                    rs.writing[sid] = rs.writing.get(sid, 0) + 1
                threading.Thread(target=produce, args=(sid,), daemon=True).start()
        except Exception:
            app.log("radio spool error:", traceback.format_exc())


def request_song(sid, like=None):
    """🎵 Create a song now: write and record one song for this station straight away, whatever the radio is playing."""
    if sid not in rs.stations:
        raise app.BadRequest("No such station")
    with app.lock:
        busy = sum(1 for j in app.jobs.values() if j.get("station") == sid and j.get("requested") and j["status"] in ("queued", "running"))
    with rs.lock:
        if busy + rs.requested.get(sid, 0) >= 3:
            raise app.BadRequest("Three songs for this station are already on the way")
        rs.requested[sid] = rs.requested.get(sid, 0) + 1
        rs.writing[sid] = rs.writing.get(sid, 0) + 1
    lj = None
    if like:
        with app.lock:
            lj = dict(app.jobs.get(like) or {})
        if not lj:
            raise app.BadRequest("No such song")
    threading.Thread(target=produce, args=(sid, True, lj), daemon=True).start()
    return {"ok": True, "banked": bool(rs.stations[sid].get("bank")) and not lj}


def takes_for(ready):
    """Takes to render for the next song, by how many approved songs are waiting. A take costs about half a song's
    length in GPU time, so 3 takes per song can't keep up with a listener: the full count only when the spool is nearly
    full, at most 2 while it is half empty, 1 when it is about to run dry."""
    target = rs.state["target_ahead"]
    if ready >= max(2, target - 1):
        return rs.TAKES
    if ready >= 2:
        return min(rs.TAKES, 2)
    return 1


def produce(sid, on_request=False, like=None):
    try:
        s = rs.stations.get(sid)
        if not s:
            return
        with app.lock:   # nothing ready and nothing being made for this station -> open with a short song
            busy = any(j.get("station") == sid and j.get("source") == "radio" and j["status"] in ("queued", "running")
                       for j in app.jobs.values())
        ready = len(station_tracks(sid, ("approved",)))
        short = not busy and not ready and rs.writing.get(sid, 0) <= 1 and not on_request
        takes = takes_for(ready)                # fewer takes when the spool runs low, so the broadcast never runs dry
        spec = None
        if not short and not like and s.get("bank"):     # a song the GPU lyricist wrote ahead of time
            with rs.lock:
                spec = s["bank"].pop(0) if s.get("bank") else None
                rs.save_station(s)
            recent_titles = [j["title"] for j in sorted((j for j in app.jobs.values() if j.get("station") == sid and j.get("source") == "radio"),
                                                        key=lambda j: -j["created"])[:12]]
            if spec and rsw.similar_title(spec["title"], recent_titles, last=recent_titles[:5]):
                spec = None                               # too close to something just played: write a fresh one
            elif spec and not spec.get("instrumental") and rsw.sung_lines(spec.get("lyrics")) < 14:
                app.log(f"radio: dropping banked song {spec['title']!r}: its lyrics are missing")
                spec = None
            if spec:
                app.log(f"radio: using banked song {spec['title']!r} ({spec['writer']})")
        if not spec:
            spec = rsw.write_spec(s, short=short, like=like)  # quality over speed: the big songwriter + editor, requests too
        if not on_request and (not rs.state["on"] or rs.state["station"] != sid):
            app.log("radio: discarding a spec written after the radio was switched off / retuned")
            return
        body = {"mode": "instrumental" if spec["instrumental"] else "song", "title": spec["title"], "author": "radio",
                "style": spec["style"], "lyrics": spec["lyrics"], "cot": "full", "max_seconds": rs.OPENER_SECONDS if short else rs.MAX_SECONDS, "takes": takes,
                "ode_steps": rs.ODE_STEPS, "cfg_scale": rs.CFG_SCALE, "semantic_sampling": dict(rs.SONG_SAMPLING),
                "abc_sampling": dict(rs.SCORE_SAMPLING)}   # a safety cap only: songs should end on their own
        if not spec["instrumental"] and not spec["lyrics"].strip():
            body["mode"] = "instrumental"
        app.new_jobs(body, extra={"source": "radio", "station": sid, "priority": 0 if on_request else 1, "radio_status": "rendering",
                                  "spec_writer": spec["writer"], "story": spec.get("story"), **({"wildcard": spec["wildcard"]} if spec.get("wildcard") else {}), **({"liked_from": like.get("id")} if like else {}),
                                  **({"requested": True} if on_request else {})})
        with rs.lock:
            s["stats"]["generated"] += 1; rs.save_station(s)
    except Exception:
        app.log("radio produce error:", traceback.format_exc())
    finally:
        with rs.lock:
            rs.writing[sid] = max(0, rs.writing.get(sid, 1) - 1)
            if on_request:
                rs.requested[sid] = max(0, rs.requested.get(sid, 1) - 1)


def judge(job):
    """Called (in a thread) when a radio job finishes rendering. Each take is scored by the CLAP critic; once every
    take of the song is in, the best one faces the station's bar and the others become runners-up (filler)."""
    sid = job.get("station"); s = rs.stations.get(sid)
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


def station_bar(s, wildcard=False):
    """The score a song must reach to air. Text stations keep the top KEEP_TEXT share, learned ones KEEP_LEARNED,
    once WARMUP_SCORES scores exist; before that, only clear misses (under WARMUP_FLOOR x the station's median) are
    dropped. Scores live on each station's own scale (CLAP similarity to its description), so the bar is relative.
    🎲 Variety lets more through (up to half the songs the bar would drop at 100), and a WILDCARD song, which borrows
    from another style on purpose, only has to avoid being a clear miss."""
    hist = s["scores"][-20:]
    keep_frac = rs.KEEP_LEARNED if s["kind"] == "folder" else rs.KEEP_TEXT
    v = max(0, min(100, int(s.get("variety", 25))))
    keep_frac += (1 - keep_frac) * 0.5 * max(0, v - 25) / 75
    if wildcard and len(hist) >= 2:
        return rs.WARMUP_FLOOR * float(np.median(hist))
    if len(hist) >= rs.WARMUP_SCORES:
        return float(np.quantile(hist, 1 - keep_frac))
    if len(hist) >= 2:
        return rs.WARMUP_FLOOR * float(np.median(hist))
    return -1.0


def settle_takes(job, s):
    """Once all takes of a song are finished and scored, approve the best take (if it clears the bar)."""
    batch = job.get("batch")
    with app.lock:
        takes = [j for j in app.jobs.values() if j.get("source") == "radio" and j.get("batch") == batch] if batch else [job]
    if any(j["status"] in ("queued", "running") or (j["status"] == "done" and "critic" not in j) for j in takes):
        return                                           # wait for the other takes
    with rs.lock:
        if any(j.get("radio_status") in ("approved", "rejected", "played") for j in takes):
            return                                       # already settled by another thread
        scored = [j for j in takes if j["status"] == "done" and j.get("critic")]
        if not scored:
            return
        rank = lambda j: (j["critic"]["sane"], j["critic"]["score"] if j["critic"]["score"] is not None else float("inf"))
        best = max(scored, key=rank)
        bar = station_bar(s, wildcard=bool(best.get("wildcard")))
        score = best["critic"]["score"]
        asked = any(j.get("requested") for j in takes)    # someone asked for this song: keep it unless it's broken
        verdict = "approved" if best["critic"]["sane"] and (asked or score is None or score >= bar) else "rejected"
        if job.get("spec_writer") != "fallback" and not job.get("wildcard"):   # wildcards don't shift the station's scale
            s["scores"] = (s["scores"] + [j["critic"]["score"] for j in scored if j["critic"]["score"] is not None])[-50:]
        s["stats"]["approved" if verdict == "approved" else "rejected"] += 1
        rs.save_station(s)
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
        s = rs.stations.get(j.get("station"))
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
    if secs >= job.get("max_seconds", rs.MAX_SECONDS) - 1:            # never ended on its own: the length cap cut it off
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
            p_vocal = rst.tag_scores(emb)["p_vocal"]
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
    emb, y = rst.audio_embedding(path)
    np.save(app.DATA / job["id"] / "clap.npy", emb)
    rms = float(np.sqrt(np.mean(y ** 2)))
    sane = (job.get("seconds") or 0) >= rs.MIN_SONG_SECONDS and rms > 0.01
    try:
        penalty, broken, notes, checks = quality_flags(job, s, y, emb)
    except Exception:
        app.log("radio critic checks error:", traceback.format_exc())
        penalty, broken, notes, checks = 0.0, False, [], {}
    sane = sane and not broken
    if s["kind"] == "folder" and (rs.sdir(sid) / "corpus.npy").is_file():
        ref = np.load(rs.sdir(sid) / "corpus.npy")
    else:
        ref = rst.text_embeddings([s["description"][:300] or s["name"]])
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
