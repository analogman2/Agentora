"""Shared radio state and settings: stations, on/off and tuning, tunables changed live by settings.py, the broadcast's
buffers and timing, the history cursor, the lyricist's bank flag. Other modules reach these as rs.NAME, so a value
changed at run time is seen everywhere.
"""
import collections, json, os, queue, random, re, shutil, subprocess, sys, threading, time, traceback, urllib.request, uuid
from pathlib import Path
import numpy as np
import art                          # cover art for stations and songs (drawn by ComfyUI between songs)

import app
import radio_stations as rst, radio_songwriter as rsw, radio_spool as rsp, radio_broadcast as rbc, radio_library as rlb
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
writing = {}                        # station id -> number of specs being written right now
requested = {}                      # station id -> specs being written because someone pressed "Create a song now"
backoff = {}                        # station id -> {"until": t, "delay": s, "reason": str}


def save_state():
    state["heartbeat"] = time.time()
    (RADIO / "state.json").write_text(json.dumps({k: state.get(k) for k in ("station", "target_ahead", "on", "heartbeat", "upnext")}))


def sdir(sid):
    return RADIO / "stations" / sid


def save_station(s):
    d = sdir(s["id"]); d.mkdir(parents=True, exist_ok=True)
    tmp = d / "station.json.tmp"; tmp.write_text(json.dumps(s, indent=1)); tmp.replace(d / "station.json")


LYRICIST = ("http://localhost:11434/v1", "qwen3.6-agent", "Qwen 3.6 27B (GPU lyricist)")   # one loaded copy shared with Hermes's AGI
BANK_SIZE = 3
bank_writing = threading.Event()        # the worker waits for this before loading YuE2


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
bc = {"listeners": {}, "buf": bytearray(), "bytes": 0, "epoch": 0.0, "now": None, "aired": [],
      "skip": threading.Event(), "wake": threading.Event(), "silence": b"", "last": 0.0,
      "cut": 0, "cut_next": False}         # cut: bumped when a skip/retune drops the buffered old song (players reconnect)
bc_lock = threading.Lock()
listen_lock = threading.Lock()


# ---- ⏮ / ⏭ through the song history, like any player: ⏮ restarts the song, ⏮ again goes to the one before, and so
# on; ⏭ then walks forward again, and past the newest song new songs resume. One broadcast: it moves everyone listening.
nav = {"hist": collections.deque(maxlen=200), "cursor": None, "queued": None}   # hist: song ids as they aired
nav_lock = threading.Lock()


LISTENED_KEEP = 3000                    # history entries kept on disk
