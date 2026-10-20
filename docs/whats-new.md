# What's new in Agentora

Agentora started as [yue2-xpu-radio](https://github.com/zebgop-ops/yue2-xpu-radio): an endless AI radio on two Intel Arc Pro B70 cards with a cassette-deck player. This page lists what changed in the fork and why. The short version:

- **Runs on one NVIDIA card** (RTX 4090) with local Ollama models instead of two Intel cards and vLLM.
- **A new radio design**: a clean player with cover art, a library laid out like folders, and a player that behaves like Spotify's (⏮ ⏭, play next, works over Bluetooth).
- **Better songs**: lyrics that tell one clear story, a lyric editor, taste learned from your thumbs, and ✏️ rewriting any song with AI help.
- **More than a radio**: a Music Studio with Easy and Advanced modes, a Home page, and a local AI assistant, all one click apart.

---

## Hardware and models

| | Original | Agentora |
|---|---|---|
| GPU | 2 × Intel Arc Pro B70 (XPU) | NVIDIA RTX 4090 (CUDA) for music, pictures and voice, plus an AMD RX 7900 XTX (ROCm) just for the assistant |
| Songwriter | Qwen3.8-27B on vLLM, paused while the radio runs | Qwen 3.6 35B-A3B in Ollama **on the CPU**, so it never competes for the GPU |
| Lyricist | (none) | Qwen 3.6 27B on the GPU, pre-writing songs into a bank whenever the card is free |
| Fast helper | (none) | Qwen 3.5 9B on the GPU: quick fallback songs, ✏️ rewrite ideas, ✨ Improve |
| YuE2 | patched for XPU graphs | stock YuE2 on CUDA (`nvidia-local.patch` adapts `app.py`) |

- **A second card for the assistant.** Hermes's model (Qwen 3.6 35B-A3B, 64k context) lives on the RX 7900 XTX full time and answers at about 90 words a second, even while a song records. The radio no longer steps aside for chat. The songwriter and the Studio's ✨ Improve share that same loaded model, so a song idea takes a second or two instead of minutes on the CPU, and Improve doesn't take memory from the picture you're about to make.
- **Sharing one card.** YuE2, the lyricist, ComfyUI (pictures, videos, cover art) and the chat assistant take turns. A **GPU priority** switch (Shared / Radio / Image & Video Studio / Assistant) decides who waits. The radio unloads only its own models and steps aside for whoever has priority.
- **Models are shared, not duplicated.** The radio's lyricist and the assistant's "AGI" mode use the same loaded copy of Qwen 3.6 27B, and the 9B model serves both the assistant's "Lemming" mode and the radio's fallback writer.
- **GPU memory safety.** A recording waits until the card has room, takes that run out of memory go back in the queue, and fewer takes are used when the spool runs low, so the broadcast never runs dry.

## The radio

### A cleaner player

- **Now Playing** shows the song's cover art, title, style and lyrics, with **Add to my playlist**, **Song details**, **Download** and **New cover**. The lyrics sit open under the song (tap **Lyrics** to fold them away; the page remembers) and change with each new song. A "Recently played" list sits underneath.
- **A player bar on every page**: 👎 ⏮ ▶ ⏭ 👍 ⊕, a progress bar and volume. Tapping the song opens its page.
- **A side panel (☰)** for the controls that used to crowd the deck: who's listening and how well it's connected, GPU and disk usage, GPU priority, links to the other apps, and Settings.
- **The cassette deck** from the original is still at `/radio/classic`.

### A library laid out like folders

- **Library**: ♥ My playlist, 🕘 Listen history, then every station, as a list (or as covers), each with a ▶ button.
- **A station** lists all of its songs, including the ones recorded and waiting to air. You can sort them newest first, favourites first or most played. The description, what it has learned from you, Edit and Delete sit in an "About this station" section underneath.
- **A song** shows its cover, style, lyrics, the story idea it was written from, its quality score, and when it last played.
- **Easy-to-read lyrics** on the song page and Now Playing: each [Verse], [Chorus] and [Bridge] gets its own labelled block, choruses are marked with a colored bar, and instrumentals (or older songs with no saved lyrics) say so plainly.
- **A path bar on every page** (`Library › Indie Road Trip › Final Load at the Dock`), with every step clickable, plus an ↑ button for going up one level.
- **Previous / Next** at the bottom of a song page moves through the folder you came from: the station, your playlist or your history.
- **Whole rows are clickable**: clicking anywhere on a song row opens it.
- **Playing a station opens it**: pressing ▶ on a station goes straight to its page, with the song in the player bar, instead of a separate Now Playing step.
- **🎲 Random** (Library, Now Playing and the start page) plays a station picked at random.
- **☑ Select** on any song list, with **Select all**, then 📻 Play next, + Playlist, − Playlist or Delete for all of them at once.
- **Search** across stations, titles, styles and lyrics.

### Plays like any music app

- **⏭ Skip** switches to the next song in about a second. The original had to play out the buffered audio first, about 15 seconds.
- **⏮ Back** restarts the song. In the first few seconds of a song it goes to the previous one, and pressing it again keeps going back through the station's history. ⏭ then walks forward again, and new songs resume after the newest one. Stepping through the history doesn't count as a skip vote.
- **📻 Play next** puts any song in the library, including Music Studio songs and rewrites, at the front of the broadcast. Songs still recording wait their turn.
- **Lock screen and car controls**: ▶ ⏸ ⏮ ⏭ work from the phone's lock screen and from Bluetooth car controls. After a pause, ▶ rejoins the live stream instead of playing stale audio. The song's title, station and cover are sent again whenever playback restarts (every ⏭, ⏮ or reconnect), because Android can otherwise blank them on Bluetooth. The tab title shows the song too.
- **Phones and Bluetooth**: phones use plain playback by default. In the original, routing audio through Web Audio for EQ and meters crackled over car Bluetooth. EQ and meters are still available as an option.
- **No more random stops.**
  - The stream runs 15 s ahead, so each phone holds a cushion against Wi-Fi and mobile hiccups.
  - A dropped stream (network blip, server restart) retries on its own, and a silent clip keeps a phone's background tab awake while it does.
  - A filler rerun now plays to its end when a new song becomes ready, instead of stopping mid-song.

### Stations that learn

- **Taste notes from your thumbs.** 👍, 👎, skips and playlist saves are turned into short notes (for example "likes story songs about small towns; skip slow ballads") that the songwriter follows. You can read them on the station page, and **Forget** clears them.
- **How you listen counts too.** Hearing a song to the end is a soft like, and tuning out early is a soft skip. Both count less than a thumb.
- **More like this** on any song writes and records a new song in its spirit.
- **🎲 Variety slider** in each station's settings, from "Core sound only" to "Adventurous".
  - Songs always stay in the station's genre.
  - Turning it up gives the songwriter more "for variety" nudges (tempo, topic, lead instrument, vocal approach) and a little more creative freedom.
  - It also adds a growing share of **wildcards**: songs that borrow one ingredient from a neighbouring style, such as a jazz touch, a synth texture, a waltz feel or a duet. That's about 25% of songs at the middle and 75% at the top.
  - The quality critic lets more of these through: a wildcard only has to avoid being a clear miss, and it doesn't shift the station's scoring scale.
  - The song page names the twist a song took.
- **🎵 Create a song now** puts a new song for a station at the front of the queue.
- **✨ Improve and 💡 Suggest** when creating or editing a station. Improve edits what you wrote rather than replacing it, and Suggest gives fresh ideas.

### Better songs

- **Lyrics that make sense.**
  - Each song is written around one story: who's singing, to whom, and what happened.
  - The writing instructions ask for plain, conversational lines that follow from each other, and a few details that belong to that story.
  - A **lyric editor** pass then checks that the story is clear and every line makes literal sense, and fixes clichés, forced rhymes and weak hooks.
  - Story ideas are only used when they fit the station.
- **No empty songs.** A song with no sung lines (only section headings) is rejected and rewritten, not recorded.
- **A GPU lyricist** pre-writes songs into each station's bank while the card is idle, so the next song often has its lyrics ready.
- **Quality over speed**: 2–3 takes per song, more diffusion steps, and the critic keeps the best take. Requested songs get the same care.
- **Audio checks** flag takes with long silent stretches or missing lyrics.
- **Cover art for every station and song**, drawn by Z-Image Turbo between songs. The prompt is a name-free description of the music, so the art has no text, logos or band likenesses. 🎨 **New cover** draws another.

## The Music Studio

- **Easy and Advanced modes.**
  - **Easy** shows presets, style, lyrics, ✨ Write lyrics for me, and Create.
  - **🎲 Lyrics from the library** fills in the lyrics of a random song from the radio library, naming the song. Each press brings another one, and none repeats until all have had a turn. Lyrics you typed yourself come back with **↩ Undo**.
  - **Advanced** brings back the tempo slider, style chips, writer options, takes, seeds, sampling and the score editor.
  - Your choice is remembered.
- **✏️ Rewrite a song**, started from the song itself (a radio song page or a library row):
  1. The Music Studio opens with the song's style, lyrics, title and quality settings filled in, a fresh take, and the original ready to play for comparison.
  2. The AI suggests 3–4 specific improvements (for example "replace 'sea of traffic lights' in the bridge with a concrete memory", or a new sound at 85 BPM). Each one applies in a few seconds, with **Undo**.
  3. Or type what should change ("slower and sadder, and make it about a brother"). The AI turns it into a lyric edit plus a new style line.
  4. Press **Create song**. The new version remembers which song it rewrote. A 👍 on it also teaches the original's station.
  5. **Stop rewriting** puts back whatever was in the form before.
- **📻 Play next** on every song sends it to the radio.
- **The Spotify picker loads at once** (2026-10-19): the Music Studio keeps your liked songs for 10 minutes instead of asking Spotify on every visit, and says "Spotify is busy" (showing the last list) when Spotify's rate limit kicks in.
- **🎤 Covers are the default** (2026-10-19): the Music Studio's Spotify picker starts on Cover and remembers your choice, the Spotify station plays its covers before any inspired songs, and Hermes's agent makes covers unless asked otherwise.
- **Newest first means newest made** (2026-10-19): on a station and in All songs it sorts by when the song was made, not when it last played; rows say "made …" or "played …".
- **Tap the song in the now-playing bar** on Home or the Music Studio (its title or cover) to open its page on the radio, in the same tab (2026-10-19).
- **🎵 From my Spotify likes** (2026-10-18), above the Library: pick 1–5 and Hermes's Spotify agent writes new, original songs inspired by liked songs it hasn't used yet. They join the queue within a minute or so and land in the Library when recorded; a second press makes nothing new. If Spotify isn't signed in yet, the button shows the command to do it.
- **✨ Inspired or 🎤 Cover** (2026-10-18): inspired songs are now written from each liked song's real lyrics (from LRCLIB) instead of just its title, and a 🎤 Cover sings the original lyrics word for word, keeping the original title, with a 🎤 Cover · original by … credit beside it. Pick one liked song from the list, or let it take the newest ones it hasn't used for that mode, going back through up to 2,000 likes. Covers are for your own listening.
- **A 🎵 From My Spotify Likes radio station** (2026-10-18) plays only those songs, the earlier ones too, and makes 🎤 covers when it runs low (since 2026-10-19). Tuned in, it asks the Spotify agent for more whenever it runs low, and replays its songs while new ones record. 🎵 Create a song now on its page makes one from the next unused liked song.

## The companion apps

Home lives in `home/` (port 7871) and shares the same look and app switcher.

- **The Image & Video Studio is its own app now** (2026-10-16): it moved to its own repo (`~/genstudio`, still port 7870) with its own accounts, copied from Agentora's. Home took over the accounts, sign-in, user management (👤 on the Home page), the Hermes chat and the admin gates. The two still share the graphics card through the GPU broker.

- **Home**: what's on the radio, what the studios are making, GPU and disk use, the GPU priority, and a chat with the local assistant.
- **Image Studio**: pictures with Z-Image Turbo, ✨ **Improve** to turn a short idea into a detailed prompt, LoRAs, aspect ratios, up to 4 at a time, plus downloaded ComfyUI workflows and models (installed with one click). Other accounts get PG-13 models only, unless an admin ticks **adults-only content** for them in Manage users; admins always can.
- **Video Studio**: Wan 2.2 videos from text, or starting from one of your pictures.
- **App switcher** in every app's top bar (🏠 📻 🎵, Alt+0–2). Each app keeps its own browser tab, so switching never stops the radio. A small now-playing bar with 👍 and ⏭ follows you into the other apps.

## The assistant (Hermes)

A local [Hermes Agent](https://github.com/NousResearch/hermes-agent) answers in the chat panel on Home, in the Image & Video Studio and on the radio. It can switch stations, make pictures and videos, install workflow models, and answer questions about the machine.

- A **🐹 Lemming / 🧠 AGI** switch picks the fast model (Qwen 3.5 9B) or the strongest local model (Qwen 3.6 27B).
- A **🌐 Internet** switch turns web search on or off.
- Only admin accounts of the Studio can use it.

## Under the hood

- **The music survives a restart.** The live stream now runs as its own small service, the **stream relay** (port 7861). It holds every listener's connection, the 15 s cushion and the stream position. The radio feeds it audio. When the radio restarts for an update, listeners stay connected and a silence bridge covers the few seconds until the next song. Before, every restart cut the stream for everyone.
- **Broken into modules by job.** The 2,000-line `radio.py` is now `radio_state`, `radio_stations`, `radio_songwriter`, `radio_spool`, `radio_broadcast` and `radio_library`. The radio page's 1,250-line script is now five files in `studio/web/` (core, player, library, panel, extras). There's no change in behaviour: the same end-to-end tests pass before and after.
- **GPU sharing that works.** A GPU broker hands out the card by leases.
  - A chat or a picture you're waiting for makes the radio step aside after its current take, with music still playing from the ready songs.
  - Idle models unload, and the order follows "Who goes first".
  - Hermes goes through the broker's Ollama proxy, which reloads a model squeezed onto the CPU as soon as there's room, and adds **✨ Auto**: the 27B model when the card is free within seconds, otherwise the 9B right away.
  - Ready for more tools and a second GPU. Measured: during a recording, Auto answered in 11 s fully on the GPU; AGI went from 45% to 87% on the GPU, and from 4 to 20 words per second.
- **A voice and ears.** A speech service gives Hermes and the apps text-to-speech with Qwen3-TTS (the talking pumpkin's voice engine; voices are described in words; ~2 s per sentence once loaded, on the GPU under a lease that fits beside the radio) and speech-to-text with whisper on the GPU (~0.1 s a phrase, about 1 GB, CPU fallback when the card is full), through the OpenAI audio API.
- **Logging everywhere.** Every component (radio, GPU workers, stream relay, Image & Video Studio, Hermes gate, network relay) logs with a level and its name. Logs go to the systemd journal and to rotating files (`logs/`), and uncaught errors are logged with their traceback. See [Logs](../README.md#logs).
- **End-to-end tests.**
  - `tests/e2e_radio.py` covers the pages, the API, the library, settings, playlist and thumbs, the stream, skip, back and play next. `--restart` restarts the radio mid-song and requires the stream not to stop. `--full` adds the AI helpers and one song made start to finish.
  - `tests/e2e_ui.py` drives every page in a headless browser and fails on any JavaScript error.

## Fixes along the way

- A song interrupted in its first 30 s is no longer put back as "unplayed" when it was a replay or a request; only new songs go back to the queue.
- A player reconnecting after ⏮ or ⏭ no longer stops the song for a moment and skips ahead.
- Skip, thumbs, playlist and the song page now work for any library song on air, not only radio-made ones.
- The radio no longer crashes at startup, and cover art waits out ComfyUI restarts instead of giving up.
- ✏️ Rewrite ideas no longer crash on songs from a station that has learned taste notes (found by the end-to-end tests).
- A browser leaving the page mid-reply no longer logs an error.
