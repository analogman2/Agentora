# Agentora

**Endless AI radio and a home media studio on one NVIDIA card.** Every station writes, sings and draws its own songs with [YuE2](https://github.com/multimodal-art-projection/YuE), local LLMs and Z-Image, and learns what you like from your thumbs. A Music Studio, an Image & Video Studio and a Home page with a local AI assistant sit beside it, all one click apart.

Agentora is a fork of [zebgop-ops/yue2-xpu-radio](https://github.com/zebgop-ops/yue2-xpu-radio), which ran on two Intel Arc Pro B70 cards. This fork runs on a single **RTX 4090** with [Ollama](https://ollama.com), and redesigns the radio around a cleaner, folder-style library. **[What's new in Agentora →](docs/whats-new.md)**

![Now playing: cover art drawn for the song, lyrics, playlist and the player bar](docs/screens/radio-now-playing.png)

## The apps

| Radio: library | Radio: a station | Radio: a song |
|---|---|---|
| <img src="docs/screens/radio-library.png" width="300"> | <img src="docs/screens/radio-station.png" width="300"> | <img src="docs/screens/radio-song.png" width="300"> |
| Your playlist, listen history and every station, laid out like folders | All of a station's songs, sortable, with **Select all** to play next, save or delete | Lyrics, the story idea, quality score, cover art, ✏️ **Rewrite** and **More like this** |

| Music Studio (Easy mode) | ✏️ Rewrite a song | Radio on a phone |
|---|---|---|
| <img src="docs/screens/music-studio.png" width="300"> | <img src="docs/screens/music-rewrite.png" width="300"> | <img src="docs/screens/radio-phone.png" width="150"> |
| Style, lyrics, Create. **Advanced** shows every control | Opened from a song: everything prefilled, AI ideas apply in one click | Plays over Bluetooth in the car, ⏮ ⏭ from the lock screen |

| Image Studio | Video Studio | Home |
|---|---|---|
| <img src="docs/screens/image-studio.png" width="300"> | <img src="docs/screens/video-studio.png" width="300"> | <img src="docs/screens/home.png" width="300"> |
| Pictures with Z-Image Turbo, ✨ Improve for prompts, LoRAs, ComfyUI workflows | Videos with Wan 2.2, from text or starting from one of your pictures | What's on, GPU and disk, and a chat with the local Hermes assistant |

The **radio and Music Studio** are in this repository (`studio/`). The **Image & Video Studio and Home page** are a companion app (a small aiohttp front end over ComfyUI on port 7870) in a separate repository, not published yet. They share one look and an app switcher (`/b70/b70.css`, `/b70/b70.js`), so moving between apps keeps the radio playing in its own tab.

## What it does

- **Stations that write their own songs.** Describe a station ("bell-bottom country, sassy female vocals") or teach it with a folder of songs. For each song an LLM writes a title, a style line and lyrics built around one clear story. An editor pass makes sure every line makes sense, and YuE2 sings it at 48 kHz. A CLAP critic keeps the best of 2–3 takes.
- **It learns from you.** 👍, 👎, skips, playlist saves and even how long you listen become short taste notes the songwriter follows. **More like this** on any song writes a new one in its spirit.
- **Cover art for every station and song**, drawn between songs by Z-Image Turbo from a name-free description of the music, so there's no text or band likenesses.
- **One live broadcast.** Everyone hears the same 192 kbps MP3 stream (`/radio/stream.mp3`). A 15 s cushion rides out Wi-Fi and mobile hiccups, ⏭ and ⏮ cut over in about a second, ⏮ walks back through the station's history like any player, and **📻 Play next** queues any song in the library.
- **A library laid out like folders:** Library › station › song, with a path bar, an ↑ button, and previous/next inside each folder.
- **✏️ Rewrite.** Open any song in the Music Studio with its settings and lyrics filled in. Get 3–4 specific AI suggestions, or type what should change ("slower and sadder, about a brother"), apply with one click, then record.
- **Shares one GPU politely.** YuE2, the GPU lyricist, ComfyUI and the chat assistant take turns on the card, and a **GPU priority** switch (Radio / Studio / Assistant / Shared) decides who waits.
- **Classic deck.** The original cassette-deck player is still at `/radio/classic`.

## How it works

```
 station + taste notes ──> songwriter (Qwen 3.6 35B, CPU) ──┐     GPU lyricist (Qwen 3.6 27B) pre-writes
                                                            ├──> songs into a bank while the card is free
                            lyric editor pass ──────────────┘
                                   │ spec: title · style · lyrics · story
                                   v
                       job queue ──> YuE2 on cuda:0 (2–3 takes) ──> CLAP critic + audio checks
                                                                           │ approved
                                                                           v
   ComfyUI Z-Image ──> cover art        spool (6 ahead) ──> playout (real-time, 15 s lead, clean cuts)
                                                                           │
                                     /radio/stream.mp3 ──> radio page · phones · VLC (same audio)
```

- `studio/app.py`: the HTTP server (Python stdlib), job queue, GPU worker and hand-off, lyrics writer, ✏️ rewrite suggestions, MP3 library.
- `studio/radio.py`: stations, songwriter and lyric editor, lyric bank, taste learning, critic, broadcast/playout, history navigation, play next, `/api/radio/*`.
- `studio/art.py`: cover art through ComfyUI. `studio/assistant.py`: the radio's chat panel, relayed to Hermes.
- `studio/radio.html`, `studio/index.html`: the radio and the Music Studio (no build step, no dependencies).

## Hardware and software

- NVIDIA GeForce RTX 4090 (24 GB), AMD Ryzen 9 3900X, 60 GB RAM, Ubuntu 26.04 (kernel 7.0)
- PyTorch 2.14 (CUDA 13), YuE2 (`m-a-p/YuE2-3B` + VAE), CLAP `laion/clap-htsat-unfused` (CPU), ffmpeg
- Ollama 0.40 with three models (created from Modelfiles, below)
  - `qwen3.6-songwriter`: Qwen 3.6 35B-A3B on the **CPU** (`num_gpu 0`), so writing never fights the GPU
  - `qwen3.6-agent`: Qwen 3.6 27B on the GPU, used as the lyricist; it's the same loaded copy as the assistant's "AGI" model
  - `qwen3.5-agent-fast`: Qwen 3.5 9B on the GPU, for the fast fallback writer, ✏️ rewrite ideas and Improve
- Optional: ComfyUI with Z-Image Turbo (cover art), [Hermes Agent](https://github.com/NousResearch/hermes-agent) (chat panel)

## Setup (one NVIDIA GPU)

```bash
# 1. YuE2 (unpatched on NVIDIA; patches/yue2-xpu.patch is only for Intel XPU)
git clone https://github.com/multimodal-art-projection/YuE.git ~/YuE
cd ~/YuE && git checkout 1dc1c50
python3.12 -m venv ~/venv-yue2
~/venv-yue2/bin/pip install torch --index-url https://download.pytorch.org/whl/cu130
~/venv-yue2/bin/pip install -e ~/YuE librosa soundfile transformers
sudo apt install ffmpeg

# 2. the songwriter models
ollama pull qwen3.6:35b-a3b && ollama pull qwen3.6:27b && ollama pull qwen3.5:9b
printf 'FROM qwen3.6:35b-a3b\nPARAMETER num_ctx 8192\nPARAMETER num_gpu 0\n' > Modelfile && ollama create qwen3.6-songwriter -f Modelfile
printf 'FROM qwen3.6:27b\nPARAMETER num_ctx 65536\n' > Modelfile && ollama create qwen3.6-agent -f Modelfile
printf 'FROM qwen3.5:9b\nPARAMETER num_ctx 65536\n' > Modelfile && ollama create qwen3.5-agent-fast -f Modelfile

# 3. run it (or install systemd/yue2-studio.service as a user service)
~/yue2-xpu-radio/start-local.sh
```

Open `http://<host>:7860/radio` (radio) or `http://<host>:7860/` (Music Studio). `nvidia-local.patch` holds the changes to `app.py` that make it run on CUDA; it's already applied on this branch.

Optional, and it makes the 27B model fit fully on a 24 GB card: `OLLAMA_KV_CACHE_TYPE=q8_0` and `OLLAMA_FLASH_ATTENTION=1` in Ollama's service environment.

### Configuration

| Variable | Here | Meaning |
|---|---|---|
| `STUDIO_DEVICES` | `cuda:0` | one YuE2 worker per device |
| `STUDIO_LYRICS_LLMS` | songwriter (CPU), then `qwen3.5-agent-fast` (GPU) | `base_url\|model\|label`, comma-separated, tried in order; a label containing "GPU" is preferred for quick jobs |
| `STUDIO_LLM_EXTRA` | `{"reasoning_effort": "none"}` | added to every LLM request (skips Ollama's thinking step) |
| `STUDIO_LLM_UNIT` | *(empty)* | a systemd LLM unit to pause for the GPU; not needed with Ollama |
| `RADIO_TAKES` / `RADIO_AHEAD` | `2` / `6` | takes per song (the critic keeps the best); songs kept ready |
| `RADIO_BURST_SECONDS` | `15` | how far the stream runs ahead, i.e. each player's cushion |
| `STUDIO_DATA` | `~/yue2-xpu-radio/library` | the song library; radio state lives next to it |

Most radio and audio settings (bitrate, loudness, EQ, quality steps, takes, writers, cover art) can also be changed live on the radio page under ☰ → Settings.

### Radio API (selection)

| | |
|---|---|
| `GET /radio/stream.mp3` | the live broadcast |
| `GET /api/radio/state` | stations, what's on air, what's being made, GPU owner, play-next list |
| `POST /api/radio/power` `{on, station}` | switch on/off or retune |
| `POST /api/radio/skip` `{track, kind}` / `POST /api/radio/back` `{track, steps}` | ⏭ / ⏮ (steps 0 = restart the song, 1 = previous) |
| `POST /api/radio/upnext` `{ids}` | 📻 play these library songs next |
| `POST /api/radio/feedback` `{track, kind}` | `keep` / `skip` / `dislike` / `clear` |
| `GET /api/radio/stations/<id>/history?limit=500&upcoming=1` | every song of a station, the ones waiting to air first |
| `POST /api/radio/stations/<id>/song` `{like}` | write and record a song now (optionally "more like" a song) |
| `POST /api/rewrite/suggest` `{id, ask}` | ✏️ ideas for rewriting a song, or turn a request into edits |

## Things we learned the hard way

This fork's additions; the original project's lessons follow below.

- **Coherent lyrics need to be asked for.** Prompts that pushed "concrete details" and banned clichés produced vivid nonsense ("a bowl of cold light", "cast iron butter"). Putting one clear story and plain language first, letting the editor check sense before style, and relaxing over-strict title rules fixed it. A spec with only section tags and no sung lines also has to be rejected, or the model sings nothing.
- **Clean cuts mean reconnects.** Dropping the buffered old song on ⏭ makes the new one start in a second, but every player then reconnects. Anything that cuts must not fire for fillers, and the playout must not treat the instant between disconnect and reconnect as "everyone left".
- **Phones freeze silent background tabs.** When a stream drops, the player keeps a silent clip looping while it retries, so Android doesn't suspend the page.
- **Web Audio and Bluetooth don't mix on Android.** Routing the stream through Web Audio (for EQ and VU meters) crackled over car Bluetooth; phones now default to plain playback.
- **One GPU, many guests.** Sharing one loaded copy of a model between the lyricist and the assistant avoids reloads, and a 35B model that doesn't fit is slower than a 9B that does. With a 24 GB card, what *fits* matters more than parameter count.

### From the original yue2-xpu-radio

- **A device fault poisons a process forever.** Workers restart on `DEVICE_LOST` or 3 consecutive failures and retire after repeated faults.
- **The length cap was cutting songs off.** The radio uses 300 s as a safety cap, sizes lyrics for about 2.5–3 min with an `[Outro]`, and fades out any take that still ends loud.
- **CLAP saturates within a genre**, so it's a soft novelty penalty, not a dedup; variety has to come from the songwriter prompt.
- **Liked songs over-anchor**: likes set the vibe only.
- **CLAP tags can be badly wrong**; passing the learned songs' names to the LLM fixes a station's sound.
- **Real-time MP3 pacing**: 192 kbps at 48 kHz gives 576-byte frames, so byte offsets are time offsets and cuts land on frame boundaries.

The Intel XPU port (`patches/yue2-xpu.patch`: XPU graphs, sync/RNG/cache, no AR offload before the VAE) is unchanged from the original; see its history for details.

## Credits and licenses

- Original project: [yue2-xpu-radio](https://github.com/zebgop-ops/yue2-xpu-radio) by zebgop-ops. Code in this repository: Apache-2.0 (see `LICENSE`).
- [YuE2](https://github.com/multimodal-art-projection/YuE) by M-A-P: code Apache-2.0; the model weights have their own license (`MODEL_LICENSE` upstream). Check it before using generated music.
- [Qwen](https://huggingface.co/Qwen) models by Alibaba, Z-Image Turbo and Wan 2.2 through [ComfyUI](https://github.com/comfyanonymous/ComfyUI), [CLAP](https://huggingface.co/laion/clap-htsat-unfused) by LAION, [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research. Idea: [infinite-tapedeck](https://github.com/CharlesMod/infinite-tapedeck) by CharlesMod.
- Songs you upload to learned stations, and everything the stations make, stay on your machine; none of it is part of this repository.
