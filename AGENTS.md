# YuE2 music studio + AI radio

Written by jt's friend (github.com/zebgop-ops/yue2-xpu-radio) and adapted for this NVIDIA machine. jt's changes live on
the local git branch `local`; never push anywhere. Served by the user systemd unit `yue2-studio` on
http://127.0.0.1:7860 (radio at /radio, classic deck at /radio/classic, music studio at /).

## How it works
- `studio/app.py`: plain `http.server` (ThreadingHTTPServer) + a worker that runs YuE2 (`m-a-p/YuE2-3B`, from ~/YuE,
  venv ~/venv-yue2) in subprocesses on the GPU. `Worker.acquire/release` hand the GPU between YuE2 and other users;
  `unload_ollama_gpu_models()` frees Ollama models first.
- The radio is split by job (see each module's docstring): `studio/radio.py` (startup, `/api/radio/*` routes, forwards
  `radio.NAME`), `radio_state.py` (shared state + every setting: read and write them as `rs.NAME`, never copy them),
  `radio_stations.py`, `radio_songwriter.py`, `radio_spool.py`, `radio_broadcast.py`, `radio_library.py`.
- `studio/stream_relay.py`: the **stream relay**, its own user service `agentora-stream` (port 7861 for players,
  127.0.0.1:7862 for the radio's feed). It holds the listeners, the burst buffer and the stream position, and bridges
  with silence when the feed stops, so restarting `yue2-studio` no longer disconnects anyone. Restarting
  `agentora-stream` itself does.
- `studio/gpu_broker.py`: the **GPU broker**, user service `agentora-gpu` (leases on 127.0.0.1:7890, Ollama proxy for
  Hermes on 127.0.0.1:11435; `GET :7890/status`). The radio asks `app.broker_yield()` before starting GPU work and
  reports `app.broker_holding()`; the Studio's `prepare_gpu()` takes a lease. Anything new that uses the GPU should
  take a lease (`POST :7890/lease`) instead of grabbing the card.
- `studio/speech_server.py`: the **speech service**, user service `agentora-speech` on 127.0.0.1:7895, run with
  `~/venv-speech` (not venv-yue2). OpenAI audio API: `/v1/audio/speech` (Qwen3-TTS voice design, GPU under a
  broker lease "speech", unloads after 2 min) and `/v1/audio/transcriptions` (whisper small.en, CPU).
- `studio/logsetup.py`: logging for every component (journal + `logs/<component>.log`); use `app.log(...)` or
  `logging.getLogger(__name__)`, never `print`.
- `studio/art.py`: cover art via ComfyUI (Z-Image), drawn between songs only; scenes are name-free.
- Lyrics: `SPEC_SYSTEM` brief + `STORY_SEEDS` + `edit_lyrics` editor pass; the GPU lyricist (`qwen3.6-agent`, shared with Hermes's AGI;
  27B) fills each station's lyric bank when the card is free (`bank_loop`); taste notes from thumbs (`update_taste`).
- ✏️ Rewrite: a song page (radio) or library row (Music Studio) opens `/#rewrite=<id>`; the form is filled from the song and `/api/rewrite/suggest` (GPU-first) gives ideas / turns a request into a lyrics instruction + style line. Easy/Advanced mode hides `.adv-only` controls.
- `studio/settings.py`: every radio/audio setting with ranges (served at /api/radio/settings).
- `studio/assistant.py`: the radio's Assistant panel, relaying chat to Hermes (http://127.0.0.1:8642) for signed-in
  Studio admins only.
- `studio/radio.html` + `studio/web/radio-{core,player,library,panel,extras}.js`: the radio page. Plain scripts loaded in
  order that share one global scope (a file can't call a later file's function while the page loads). Here `$` takes a
  bare element id: `$("powerBtn")`, not `$("#powerBtn")`. `esc()` escapes HTML; `api()` is the fetch helper.
- Songwriter LLM: `qwen3.6-songwriter` in Ollama (Qwen 3.6 35B-A3B, CPU only). Fallback `qwen3.5-agent-fast` (Qwen 3.5 9B, Hermes's Lemming) on the GPU. `app.SHARED_WITH_ASSISTANT` models count as the radio's only while it used them in the last 7 min.
- Data (not in git): `library/` (every song), `radio/` (stations, state, history), `gpu.json`.

## How to change it safely
1. `git status`; work on branch `local`.
2. Syntax-check Python with `~/venv-yue2/bin/python -c "import ast;ast.parse(open('studio/app.py').read())"` and the
   page script with `node --check`.
3. Restarting `yue2-studio` keeps listeners connected (the stream relay bridges the gap) but kills any song being
   recorded: wait until nothing is running (`/api/radio/state` making with status "running", `/api/state` queue).
   Restarting `agentora-stream` disconnects every listener: only when `/api/radio/listeners` is empty.
4. After restarting, confirm `systemctl --user is-active yue2-studio agentora-stream` and run the end-to-end tests:
   `~/venv-yue2/bin/python tests/e2e_radio.py` (add `--quick` if someone is listening) and `tests/e2e_ui.py`.
   Check `logs/radio.log` for WARNING / ERROR lines.
5. Commit on `local` with a clear message (`git push` goes to the Agentora fork).
