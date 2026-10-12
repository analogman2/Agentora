# YuE2 music studio + AI radio

Written by jt's friend (github.com/zebgop-ops/yue2-xpu-radio) and adapted for this NVIDIA machine. jt's changes live on
the local git branch `local`; never push anywhere. Served by the user systemd unit `yue2-studio` on
http://127.0.0.1:7860 (radio at /radio, classic deck at /radio/classic, music studio at /).

## How it works
- `studio/app.py`: plain `http.server` (ThreadingHTTPServer) + a worker that runs YuE2 (`m-a-p/YuE2-3B`, from ~/YuE,
  venv ~/venv-yue2) in subprocesses on the GPU. `Worker.acquire/release` hand the GPU between YuE2 and other users;
  `unload_ollama_gpu_models()` frees Ollama models first.
- `studio/radio.py`: stations, the song pipeline (an LLM writes style + lyrics, YuE2 sings 2 takes, a critic keeps the
  better one), the live MP3 broadcast, feedback, playlist, history. API under /api/radio/*.
- `studio/settings.py`: every radio/audio setting with ranges (served at /api/radio/settings).
- `studio/assistant.py`: the radio's Assistant panel, relaying chat to Hermes (http://127.0.0.1:8642) for signed-in
  Studio admins only.
- `studio/radio.html`: the radio page (one file). Here `$` takes a bare element id: `$("powerBtn")`, not `$("#powerBtn")`.
  `esc()` escapes HTML; `api()` is the fetch helper.
- Songwriter LLM: `qwen3.6-songwriter` in Ollama (Qwen 3.6 35B-A3B, CPU only). Fallback `qwen3:4b-instruct` on the GPU.
- Data (not in git): `library/` (every song), `radio/` (stations, state, history), `gpu.json`.

## How to change it safely
1. `git status`; work on branch `local`.
2. Syntax-check Python with `~/venv-yue2/bin/python -c "import ast;ast.parse(open('studio/app.py').read())"` and the
   page script with `node --check`.
3. Restarting `yue2-studio` interrupts the live stream for listeners and any song being made. Check
   `curl -s http://127.0.0.1:7860/api/radio/state` (stream.listeners, making) and tell jt before restarting when
   someone is listening.
4. After restarting, confirm `systemctl --user is-active yue2-studio` and that /radio loads.
5. Commit on `local` with a clear message.
