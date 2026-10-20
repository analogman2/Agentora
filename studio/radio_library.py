"""The library: song views, next track and fillers (runner-ups, reruns), history, feedback, playlist, all songs, the
listen log, and switching the radio on / off.
"""
import collections, json, os, queue, random, re, shutil, subprocess, sys, threading, time, traceback, urllib.request, uuid
from pathlib import Path
import numpy as np
import art                          # cover art for stations and songs (drawn by ComfyUI between songs)

import app
import radio_state as rs, radio_stations as rst, radio_songwriter as rsw, radio_spool as rsp, radio_broadcast as rbc


def track_details(tid):
    with app.lock:
        j = app.jobs.get(tid)
    if not j:
        raise app.BadRequest("No such song")
    return track_view(j) | {"critic": j.get("critic"), "writer": j.get("spec_writer"), "story": j.get("story"), "wildcard": j.get("wildcard"), "created": j.get("created"),
                            "approved_at": j.get("approved_at"), "airings": j.get("airings", 0), "status": j.get("radio_status"),
                            "requested": bool(j.get("requested")), "liked_from": j.get("liked_from")}


# ----------------------------------------------------------------------------- API

def credit(j):
    """🎤 covers keep the original title; the credit says whose song it is (author "Spotify cover <id> · <artist>")."""
    a = j.get("author") or ""
    if not a.startswith("Spotify cover "):
        return None
    artist = a.split(" · ", 1)[1].strip() if " · " in a else ""
    return "🎤 Cover" + (f" · original by {artist}" if artist else "")


def track_view(j):
    cover = art.stamp(art.song_path(j["id"]))
    return {"cover": f"/files/{j['id']}/cover.jpg?v={cover}" if cover else None, "id": j["id"], "title": j["title"], "style": j["style"], "lyrics": j.get("lyrics", ""), "mode": j["mode"],
            "seconds": j.get("seconds"), "station": j.get("station"), "critic": j.get("critic"), "feedback": j.get("radio_feedback"),
            "played_at": j.get("played_at"), "audio": f"/files/{j['id']}/song.mp3", "in_playlist": bool(j.get("playlist_at")),
            "station_name": rs.stations[j["station"]]["name"] if j.get("station") in rs.stations else None, "credit": credit(j)}


def rerun_library():
    with app.lock:
        aired = [j for j in app.jobs.values() if j.get("source") == "radio" and j["status"] == "done" and j.get("played_at")
                 and j.get("radio_feedback") not in ("skip", "dislike")]
    return {"id": rs.RERUNS, "songs": len(aired), "stations": len({j.get("station") for j in aired}),
            "favorites": sum(1 for j in aired if loved(j))}


def api_state(listener=None):
    with rs.lock:
        st = []
        for s in sorted(rs.stations.values(), key=lambda s: s["created"]):
            st.append({k: s.get(k) for k in ("id", "name", "kind", "description", "vocals", "language", "theme", "stats", "analysis")} | {"variety": s.get("variety", 25)} |
                      {"art": art.stamp(art.station_path(s["id"])), "asked": rs.requested.get(s["id"], 0),
                       "taste": (s.get("taste") or {}).get("notes"), "banked": len(s.get("bank") or []), "ready": len(rsp.station_tracks(s["id"])), "inflight": rsp.inflight(s["id"]),
                       "problem": (rs.backoff[s["id"]]["reason"] + f" — retrying in {int(rs.backoff[s['id']]['until'] - time.time())}s")
                                  if s["id"] in rs.backoff and time.time() < rs.backoff[s["id"]]["until"] else None,
                       "profile": {k: v for k, v in (s.get("profile") or {}).items() if k in ("tracks", "top", "bpm_median", "bpm_range", "keys", "vocal_share")} or None,
                       "files": len([p for p in (rs.sdir(s["id"]) / "music").rglob("*") if p.suffix.lower() in rst.AUDIO_EXT]) if s["kind"] == "folder" and (rs.sdir(s["id"]) / "music").exists() else None})
    with app.lock:
        making = [{"title": j["title"], "status": j["status"], "stage": j.get("stage"), "progress": j.get("progress"), "station": j.get("station"),
                   "requested": bool(j.get("requested"))}
                  for j in app.jobs.values() if j.get("source") == "radio" and j["status"] in ("queued", "running")]
        judging = sum(1 for j in app.jobs.values() if j.get("radio_status") == "judging")
    return {"on": rs.state["on"], "station": rs.state["station"], "target_ahead": rs.state["target_ahead"], "stations": st,
            "making": making, "judging": judging, "upnext": rbc.upnext_view(), "next": rbc.peek_next(), "gpu": app.state()["gpu"], "stream": rbc.stream_view(listener),
            "reruns": rerun_library()}


def _filler(sid):
    """Nothing approved is ready: rather than dead air, air the best recent runner-up (a sane take that narrowly
    missed the critic's bar), else a rerun of one of the station's own songs (favorites first, then its best-scored;
    nothing aired in the last 45 min, nothing skipped or disliked)."""
    now = time.time()
    with app.lock:
        mine = [j for j in app.jobs.values() if rsp.on_station(j, sid) and j["status"] == "done"
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
    if (rs.stations.get(sid) or {}).get("kind") == "spotify":       # few songs yet: the least recently aired, not the last one
        last = (rs.bc["aired"][-1] if rs.bc["aired"] else {}).get("id")
        played = sorted((j for j in mine if j.get("played_at") and j.get("radio_feedback") not in ("skip", "dislike") and j["id"] != last),
                        key=lambda j: j["played_at"])
        if played:
            return played[0], "rerun"
    return None, None


def _rerun_pick():
    """Rerun mode: a weighted shuffle over every song that has aired on any station - favorites 3x, better critic
    scores a little more, nothing aired in the last 3 h, never skipped/disliked songs, and preferably not the
    same station twice in a row."""
    now = time.time()
    with app.lock:
        pool = [j for j in app.jobs.values() if j.get("source") == "radio" and j["status"] == "done" and j.get("played_at")
                and j.get("radio_feedback") not in ("skip", "dislike") and (app.DATA / j["id"] / "song.mp3").is_file()
                and (j.get("mode") == "instrumental" or not j.get("lyrics") or rsw.sung_lines(j["lyrics"]) >= 8)]   # not the tags-only ones
    if not pool:
        return None
    fresh = [j for j in pool if now - j["played_at"] > 3 * 3600]
    if not fresh:                                   # small library: the least recently aired third
        fresh = sorted(pool, key=lambda j: j["played_at"])[:max(1, len(pool) // 3)]
    last = (rs.bc["aired"][-1] if rs.bc["aired"] else {}).get("station")
    w = [(3.0 if loved(j) else 1.0) * (0.5 + max(0.0, (j.get("critic") or {}).get("score", 0.7)))
         * (0.4 if j.get("station") == last else 1.0) for j in fresh]
    return random.choices(fresh, weights=w)[0]


def next_track(sid, mark=True):
    if sid == rs.RERUNS:
        j, airing, ready = _rerun_pick(), "rerun", None
    else:
        with app.lock:
            ready = sorted(rsp.station_tracks(sid, ("approved",)), key=lambda j: j.get("approved_at", 0))
        if (rs.stations.get(sid) or {}).get("kind") == "spotify" and (rs.stations[sid].get("spotify_mode") or "cover") == "cover":
            ready.sort(key=lambda j: not (j.get("author") or "").startswith("Spotify cover "))   # 🎤 covers before inspired songs
        j, airing = (ready[0], "new") if ready else _filler(sid)
    if not j:
        return None
    if mark:
        with app.lock:
            j.update(radio_status="played", played_at=time.time(), airings=j.get("airings", 0) + 1); app.save_job(j)
        if sid in rs.stations:
            with rs.lock:
                rs.stations[sid]["stats"]["played"] += 1; rs.save_station(rs.stations[sid])
        if airing != "new" and sid != rs.RERUNS:
            app.log(f"radio: spool empty, airing a {airing}: {j['title']}")
    return track_view(j) | {"airing": airing}


def favorites():
    """Every radio song a listener kept (KEEP / ♥), across all stations, newest favorite first."""
    with app.lock:
        fav = [j for j in app.jobs.values() if j.get("source") == "radio" and j.get("radio_feedback") == "keep"]
    kept = {}
    with rs.lock:
        for s in rs.stations.values():
            for f in s["feedback"]:
                if f["kind"] == "keep":
                    kept[f["track"]] = f["t"]
    out = []
    for j in fav:
        st = rs.stations.get(j.get("station"))
        out.append(track_view(j) | {"station_name": st["name"] if st else "(deleted station)",
                                    "kept_at": kept.get(j["id"]) or j.get("played_at") or j.get("finished") or 0})
    return sorted(out, key=lambda t: -t["kept_at"])


def history(sid, limit=40, upcoming=False):
    """Songs a station has aired, newest first; upcoming=True also lists the recorded songs waiting to air (first)."""
    with app.lock:
        played = sorted((j for j in app.jobs.values() if j.get("played_at")
                         and ((sid == rs.RERUNS and j.get("source") == "radio") or rsp.on_station(j, sid))),
                        key=lambda j: -j["played_at"])[:limit]
    ready = sorted(rsp.station_tracks(sid, ("approved",)), key=lambda j: j.get("approved_at", 0)) if upcoming and sid != rs.RERUNS else []
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
    s = rs.stations.get(j.get("station") or orig.get("station"))
    with rs.lock:
        if s:
            s["feedback"] = [f for f in s["feedback"] if f["track"] != track_id]
            if kind != "clear":
                s["feedback"].append({"track": track_id, "kind": kind, "title": j["title"], "style": j["style"], "t": time.time()})
                s["feedback"] = s["feedback"][-200:]
                s["stats"][{"keep": "kept", "skip": "skipped", "dislike": "disliked"}[kind]] += 1
            rs.save_station(s)
    j["radio_feedback"] = None if kind == "clear" else kind
    app.save_job(j)
    if s and kind in ("keep", "dislike", "clear"):
        threading.Thread(target=rsw.update_taste, args=(s["id"],), daemon=True).start()
    return {"ok": True}


def set_power(on, sid=None):
    with rs.lock:
        if sid:
            if sid not in rs.stations and sid != rs.RERUNS:
                raise app.BadRequest("No such station")
            if sid != rs.state["station"]:
                rs.bc["retuned"] = True               # the song on air cuts over once the new station has one ready
                with rs.nav_lock:
                    rs.nav["hist"].clear(); rs.nav["cursor"] = None; rs.nav["queued"] = None
            rs.state["station"] = sid
        if on and not rs.state["station"]:
            raise app.BadRequest("Pick a station first")
        rs.state["on"] = bool(on)
        rs.save_state()
    if not on or rs.state["station"] == rs.RERUNS:
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
    return {"ok": True, "on": rs.state["on"], "station": rs.state["station"]}


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
    if j.get("station") in rs.stations:                       # a saved song counts as a like
        threading.Thread(target=rsw.update_taste, args=(j["station"],), daemon=True).start()
    return {"ok": True, "in_playlist": on}


def _listened_path():
    p = rs.RADIO / "listened.jsonl"
    if not p.exists():                  # first run: seed from what already aired (one entry per song, its last airing)
        with app.lock:
            aired = sorted((j for j in app.jobs.values() if j.get("source") == "radio" and j.get("played_at")),
                           key=lambda j: j["played_at"])
        p.write_text("".join(json.dumps({"t": j["played_at"], "track": j["id"], "station": j.get("station")}) + "\n" for j in aired))
    return p


def log_listen(entry):
    try:
        p = _listened_path()
        with rs.listen_lock:
            with p.open("a") as f:
                f.write(json.dumps({"t": round(time.time(), 1), "track": entry["id"], "station": rs.state.get("station"),
                                    "airing": entry.get("airing")}) + "\n")
            if p.stat().st_size > 400_000:          # trim now and then
                lines = p.read_text().splitlines()[-rs.LISTENED_KEEP:]
                p.write_text("\n".join(lines) + "\n")
    except Exception:
        app.log("radio listen-history error:", traceback.format_exc())


def listened(limit=100):
    """What was played to listeners, newest first, across every station (reruns included)."""
    with rs.listen_lock:
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
            out.append(track_view(j) | {"listened_at": e["t"], "airing": e.get("airing"), "via_reruns": e.get("station") == rs.RERUNS})
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


def clear_history():
    with rs.listen_lock:
        _listened_path().write_text("")
    return {"ok": True}
