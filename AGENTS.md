# Agentora: guide for agents (Claude Code, Hermes, anyone editing this repo)

Agentora is jt's local AI media suite on an RTX 4090, plus an RX 7900 XTX that runs Hermes's model:
- an endless AI radio and a Music Studio (YuE2);
- an Image & Video Studio and Home page (ComfyUI);
- a local assistant (Hermes).

It's a fork of github.com/zebgop-ops/yue2-xpu-radio (jt's friend). Branch `local`, pushed to the remote `agentora` (github.com/analogman2/Agentora).

Read this whole file before changing anything. `README.md` has the user-facing docs, and `image-studio/AGENTS.md` has the Studio's own notes.

## Rules (these protect jt's listening and data)

1. **Git**
   - Never push to `origin` (the friend's repo; its push URL is disabled on purpose). Never push `backup-before-email*` branches.
   - **Never `git add -A` / `git add .`**: Hermes and other sessions edit this repo too. Stage only the files you changed, by name. A change you didn't make belongs to someone else: leave it, don't revert it, and mention it.
   - Commit with a clear message; `git push` goes to `agentora/local`.
2. **Secrets**
   - Never print, commit or paste the contents of `image-studio/agent.token`, `settings.json`, `users.json`, `sessions.json`, any `.env`, or `~/.hermes/auth.json`.
   - The agent token may be read by code and sent only to 127.0.0.1.
   - Never put passwords anywhere.
3. **No sudo, no system settings.** jt types sudo commands themselves; give them the exact command.
4. **Restarts interrupt jt.** jt listens for hours on phone and car Bluetooth. Before restarting anything, check:
   - **`yue2-studio`**: a restart keeps listeners connected (the stream relay bridges the gap) but kills a song being recorded. Wait until nothing is recording (`/api/radio/state` → `making[].status == "running"`) and the Music Studio queue (`/api/state`) is idle.
   - **`agentora-stream`** and **`tailnet-relay`** disconnect everyone. Restart them only when `/api/radio/listeners` is empty, or when jt says "restart now".
   - **`genstudio`**: no Studio job running or queued (`image-studio/jobs.json`).
   - Page-only changes (HTML, `studio/web/*.js`, `image-studio/shared/*`) need no restart.
   - Batch Python changes and restart once.
5. **The GPU is shared through the broker.** New GPU work takes a lease (`POST 127.0.0.1:7890/lease`) instead of grabbing the card. Leases are for the 4090; the Radeon belongs to the models in `HOMES` (`gpu_broker.py`), which the broker keeps loaded there. `GET :7890/status` shows both cards, leases with queue positions, and `placement` (which card runs each model). Hermes reaches Ollama only through the broker's proxy (`:11435`); never point it back at `:11434`.
6. **Hermes config:** `model.context_length` in `~/.hermes/config.yaml` must stay ≥ 64000, or every chat fails.
7. **Privacy in logs:** the activity log never records passwords, prompts or lyrics.
8. **Verify before saying it works.** Run the tests below after every change, and report failures as they are.

## Services and ports

| Service (user unit) | Port | Code | What |
|---|---|---|---|
| `yue2-studio` | 7860 | `studio/app.py`, `studio/radio*.py` | radio (`/radio`), Music Studio (`/`), classic deck (`/radio/classic`) |
| `agentora-stream` | 7861 (feed 127.0.0.1:7862) | `studio/stream_relay.py` | the live MP3 stream: listeners, cushion, silence bridge |
| `agentora-gpu` | 7890, Ollama proxy 11435 | `studio/gpu_broker.py` | GPU leases (`GET :7890/status`), virtual model `agentora-auto` |
| `agentora-speech` | 7895 | `studio/speech_server.py` (`~/venv-speech`) | OpenAI audio API: Qwen3-TTS voice, whisper listening, both under leases |
| `genstudio` | 7870 | `image-studio/server.py` | Image & Video Studio (`/`), Home (`/home`), accounts, shared look `/agentora/agentora.{css,js}` |
| `hermes-gate` | 7880 | `image-studio/hermes_gate.py` | Studio-admin sign-in in front of the Hermes dashboard |
| `tailnet-relay` | all of the above | `tools/tailnet-relay.py` (symlinked from `~/.local/bin`) | serves the apps on the Tailscale and LAN addresses (jt's phone), notes real client IPs |
| `agentora-ollama-amd` | 11436 | `systemd/agentora-ollama-amd.service` (`~/.local/ollama-rocm`) | second Ollama, RX 7900 XTX only (ROCm); the broker's proxy sends Hermes's 35B here without a lease |
| `comfyui` | 8188 | `~/ComfyUI` | image/video backend |
| Hermes | 8642 (API), dashboard behind the gate | `~/.hermes` | the assistant (Lemming 9B / Auto / AGI = Qwen 3.6 35B-A3B `qwen3.6-agent-moe`) |

`~/genstudio` is a symlink to `image-studio/`, because the units use that path. Unit files live in `systemd/`.

## How the code is laid out

- **Radio and Music Studio** (`studio/`)
  - `app.py`: a `ThreadingHTTPServer` plus the YuE2 worker (`m-a-p/YuE2-3B`, from ~/YuE, venv `~/venv-yue2`).
  - The radio is split by job:
    - `radio.py`: routes and the façade.
    - `radio_state.py`: all state and settings. Read and write them as `rs.NAME`; never copy them.
    - `radio_stations.py`, `radio_songwriter.py` (lyrics, the 🎲 variety slider), `radio_spool.py` (recording and the critic), `radio_broadcast.py`, `radio_library.py`.
  - `settings.py`: every setting, with its range.
  - `assistant.py`: chat relay to Hermes.
  - `art.py`: cover art.
  - `useractivity.py`: who did what on 7860.
- **Radio page:** `radio.html` plus `studio/web/radio-{core,player,library,panel,extras}.js`. These are plain scripts sharing one global scope, loaded in order. `$("id")` takes a bare id, `esc()` escapes HTML, and `api()` fetches.
- **Image & Video Studio** (`image-studio/`)
  - `server.py` (aiohttp), `auth.py` (accounts and sessions; each sign-in keeps its IP and device), `workflows.py`, `models.py`, `ratings.py`.
  - `shared/agentora.{css,js}`: the look shared by every app. It provides the app switcher, the now-playing bar, the ☰ panel (`data-agentora-panel`), the 👤 account card (`data-agentora-account`) and the brain switch.
- **Logging:** `logsetup.py` (the same file in `studio/` and `image-studio/`; keep them identical).
  - Logs go to the journal and to `logs/<component>.log`. Use `logging` or `app.log()`, never `print`.
  - User activity goes to `logs/activity-YYYY-MM.jsonl` via `logsetup.activity(...)`. Read it with `tools/activity.py` (who's here now) or `tools/activity.py --since 24h`.
- **Models (Ollama):**

  | Model | What runs it | Used for |
  |---|---|---|
  | `qwen3.6-agent-moe` on the Radeon (:11436) | shares Hermes's loaded copy (same weights and sampling) | songwriter (first choice), Studio ✨Improve |
  | `qwen3.6-songwriter` (35B-A3B) | CPU only | songwriter fallback when the Radeon is down |
  | `qwen3.6-agent` (27B) | GPU | lyricist and Hermes AGI |
  | `qwen3.5-agent-fast` (9B) | GPU | fallback writer and Hermes Lemming |
  | `qwen3.6-agent-moe` (35B-A3B, 64k) | the RX 7900 XTX, always loaded (4090 if that Ollama is down) | Hermes AGI and Auto |
  | `agentora-auto` | the 35B while the Radeon is up, else the broker picks it or the 9B | Hermes Auto |
- **Data (not in git):**
  - `library/`: every song.
  - `radio/`: stations, state, history.
  - `image-studio/*.json`: users, sessions, jobs, settings.
  - `logs/`.

## Check your work

```bash
cd ~/yue2-xpu-radio
python3 -c "import ast,sys;[ast.parse(open(f).read()) for f in sys.argv[1:]]" studio/*.py image-studio/*.py   # syntax
for f in studio/web/*.js image-studio/shared/agentora.js; do node --check "$f"; done                          # page scripts
systemctl --user is-active yue2-studio agentora-stream agentora-gpu agentora-speech genstudio hermes-gate tailnet-relay
python3 tests/e2e_radio.py --quick                                      # API, library, stream, skip/back, broker, speech (--quick if someone listens)
uv run -q --no-project --with playwright python tests/e2e_ui.py          # every page in a headless browser, fails on JS errors
grep -E "WARNING|ERROR" logs/radio.log | tail                             # anything new?
```

Done means:
- the tests pass and the services are active;
- the README (and `docs/whats-new.md` for anything a user would notice) is updated;
- the work is committed on `local` and pushed to `agentora`.
