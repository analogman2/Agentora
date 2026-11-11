# Spec: 📷 Song from photos (Music Studio)

Status: **phase 1 built** (2026-11-10): `studio/photosong.py`, `studio/web/photosong.js`, `tests/e2e_photosong.py`.
Phase 1 differs from this spec in a few places: photos are ordered by date (no drag to reorder yet), the place under a
photo can't be edited yet, the default engine is MiniMax with 2 takes, and the share link, video and painted cover are phase 2.
**💬 Questions** (added 2026-11-10): after reading the photos, the AI asks 1-2 questions per photo and 2-4 about the whole set
(who people are, by how they look; what happened; what the photo can't show). Answers feed the story and lyrics, and names
given there may be sung. Nothing is ever guessed from faces.
**Google Photos albums** (added 2026-11-10): paste an album's share link, pick photos (or ✨ Pick for me), and the originals
are downloaded with their metadata. Google's APIs no longer read a user's albums without an OAuth Picker app, so this
reads the share page like a browser does: unofficial, and it may need fixing if Google changes that page.

## What it is

You drop in a set of photos: a relationship, a trip, a wedding, a kid's first year. Agentora reads what's in each picture
and in its metadata (when and where it was taken), turns that into a story, writes lyrics from it, and records the song.
At the end you get a song made of *your* details: the red scarf in Lisbon, the dog asleep on the couch, the rain on the
first date. Not a greeting card about "love" in general.

**The main use is love songs**, made to be given to someone: anniversaries, Valentine's, proposals, weddings, "just because".
So the defaults, the prompts and the finished output are tuned for that (other occasions still work).

### Goals
- Concrete, specific lyrics that someone who was there would recognize from the photos.
- The person making it stays in charge of the story: they fix what the AI got wrong and add what no photo shows
  (names, inside jokes, what was actually said).
- A finished thing worth giving: the song, plus a slideshow video and a share link.
- Photos stay private: they never leave the machine and never reach the radio, the logs or anyone else's library.

### Not goals (v1)
- Recognizing who is in a photo (no face ID, no naming people from their faces).
- Singing in a cloned voice of the user or their partner.
- Video input (photos only; a video's first frame could come later).

---

## The user's flow (Create → 📷 From photos)

A new tab/card in Create, beside the normal "idea" box. Four steps on one page, each one editable before moving on.

1. **Photos.** Drag in or pick 1–30 photos (JPEG, PNG, HEIC, WebP; phones send HEIC). Thumbnails show right away.
   - Then a few questions, all optional, with love-song defaults:
     - **For:** a name ("Ana"). Used in the lyrics only if given.
     - **From:** your name, for the credit and the share page.
     - **Occasion:** Anniversary · Valentine's · Proposal · Wedding / first dance · Birthday · Just because · Not a love song (trip, family, memorial).
     - **How long together / since when** (optional): gives the story its arc.
     - **Feel:** Tender · Playful · Epic · Bittersweet · Funny.
     - **Must include** (free text): "the night the car broke down in Big Sur", "she calls me 'bear'".
     - **Leave out** (free text): an ex, a hard year, anything off limits.
2. **What we see.** Agentora reads the photos (progress bar, about 5–10 s a photo). Each photo gets a card: the date,
   the place, and a one-line description ("Two people laughing under an umbrella on a cobbled street at night"), plus
   the detail it picked for the song ("the yellow tram behind them").
   - Edit any line, star the photos that matter most, ✕ to leave one out, drag to reorder.
3. **The story.** The photos grouped into 3–6 *moments* (by date and place), each with a short summary, and a plan
   for which moment goes in which part of the song:

   | Song part | Default role in a love song |
   |---|---|
   | Verse 1 | How it started (the earliest moments) |
   | Verse 2 | The life you've built (trips, home, the small days) |
   | Chorus | What they are to you: the feeling, with one image from the photos as its hook |
   | Bridge | The turn: a hard time you got through, or the promise (proposal / wedding) |
   | Final chorus | Back to the hook, now meaning more |

   Edit the summaries or move a moment to another part. A **title** is suggested here.
4. **Song.** The lyrics stream in (the existing lyrics writer, with the photo story). Then the usual Studio controls:
   style (a suggested style plus the love presets below), engine (YuE2 | MiniMax), takes, length.
   ✏️ Rewrite and ✨ Improve work as they do today. **Make it** queues the song.

When it's done, the song page shows the song with its photos as a slideshow, and offers:
- ▶️ Play · 🎞️ **Video** (MP4: the photos with a slow pan and zoom, lyrics as captions, the song as the soundtrack) ·
  🔗 **Share link** (a private page with the song, photos and lyrics, no sign-in needed, can be revoked).

---

## How it works

```
photos ──► ingest ──► per-photo vision ──► story (moments → song parts) ──► lyrics ──► style ──► song job ──► cover, video, share page
          (EXIF,     (Qwen3.8 27B,        (LLM; user edits)               (stream_    (enhance_  (new_jobs,   (photo as cover,
           thumbs)    JSON per photo)                                       lyrics)     style)     YuE2/MiniMax) ffmpeg)
```

### 1. Ingest (CPU, instant)
- Save the originals to the draft folder (below). Convert HEIC with `pillow-heif`. Apply the EXIF orientation.
- Make a 1024 px JPEG for the vision model and a 320 px thumbnail for the page.
- Read the metadata with Pillow: `DateTimeOriginal` (plus the offset when present), GPS, camera model, and whether the
  photo is a screenshot (no camera, screen-sized) or was sent through a messaging app (no EXIF left at all: WhatsApp strips it).
  - With no date, fall back to the date in the file name (`IMG_20240214_…`, `PXL_…`), then to the upload order.
- **Place names offline:** turn GPS into "Lisbon, Portugal" / "Big Sur, California" with the `reverse_geocoder` package
  (the GeoNames cities file, local). No call to an outside service: the coordinates of your home must not leave the machine.
  Show place names down to the city, never the coordinates.

### 2. What's in each photo (GPU, Qwen3.8 27B vision)
- Through the GPU broker's Ollama proxy (`:11435`), which already hands vision requests to Qwen3.8 27B on the 4090.
  One photo per request (the 1024 px copy plus its date and place), `temperature` 0.3, JSON out:

  ```json
  {"scene": "two people laughing under one umbrella on a wet cobbled street at night",
   "setting": "city street", "time_of_day": "night", "season_weather": "rain",
   "people": 2, "together": true, "mood": ["joyful", "intimate"],
   "activity": "walking home", "details": ["yellow tram", "shared umbrella", "reflections of street lights"],
   "song_detail": "the yellow tram rattling past while we laughed in the rain",
   "text_in_photo": "", "kind": "photo"}
  ```
- The prompt rules: describe, don't identify. Never guess anyone's name, age, ethnicity or body; "two people",
  "a woman in a green coat" is the limit. Pick details someone who was there would remember. `kind` flags screenshots,
  documents and memes so they can sit out of the story.
- If the 4090 is busy recording a song, wait in the broker queue (the page says so). If Qwen3.8 is gone, the Radeon's 35B
  is the fallback only if it accepts images; otherwise the step waits (no blind captions).
- Cached per photo (hash of the file), so re-running the story or the lyrics never re-reads the photos.

### 3. The story (LLM, text only)
- Group photos into moments: sort by date, then split where there's a gap of more than a day or a change of place.
  Plain code, no LLM.
- Then one LLM call (the songwriter model, as `write_spec` uses) turns the moments plus the answers from step 1 into:
  a title, one summary line per moment, the song-part plan (the table above), the chorus hook, and 8–15 "lines worth
  using" (the strongest concrete details). JSON, shown as the editable step 3.
- The "Leave out" text goes into every later prompt as a hard rule.

### 4. Lyrics
- `stream_lyrics` gets a new `action: "photos"`. `lyrics_prompt` builds it from the edited story: the song-part plan,
  the hook, the lines worth using, the name (if given), the occasion and the feel. Same output format as today, so
  `normalize_lyrics` and both engines take it as is.
- Love-song rules added to the system prompt for this action:
  - Every verse uses at least two real details from the story; the chorus carries the hook image.
  - Names: the given name at most twice, never forced to rhyme. No other names.
  - Places and dates may be sung ("Lisbon in the rain"), never addresses or exact dates unless the user wrote them.
  - Avoid the stock words (`cliche_hits` in `radio_songwriter.py` already lists them): run the same check and, when it
    finds more than a few, do one `edit_lyrics` pass, as the radio does.
- POV defaults to "you" (singing *to* the person). Language as in Create today.

### 5. Style and engine
- ✨ Improve (`enhance_style`) gets the feel and the photo moods, and suggests a style line.
- A new **Love** preset group (8–10): acoustic first-dance ballad, piano soul, 90s R&B slow jam, indie folk duet,
  country love song, bossa nova, synth-pop crush song, wedding-band swing, bolero, a stripped voice-and-guitar demo.
- Engine: default **MiniMax** for these (up to 5 min, chosen for the vocals: confirm with the A/B listening that's
  already going on), YuE2 one click away. Default 2 takes, so there's a choice.

### 6. Song, cover, video, share page
- `new_jobs` with `source: "photos"`, `private: true` and `photosong: <draft id>`. The queue, GPU leases, takes and the
  song page all work as today.
- **Cover:** the starred photo (or the first), cropped square, with no AI picture made. `art.py` skips jobs that have a
  `photosong`. Option: "✨ Painted cover" sends that photo through ComfyUI img2img in a soft painted style.
- **Video** (on request, CPU): `ffmpeg` with the photos in story order, a slow pan and zoom on each, crossfades, time per
  photo set by the song's length, lyrics as soft captions (sections spaced evenly in v1; timed to the vocals later if a
  whisper pass is worth it: `agentora-speech` already runs whisper). 1080×1080 and 1080×1920. GPS and EXIF stripped.
- **Share page:** `/s/<random 22-char token>`: the song, the slideshow, the lyrics, "for Ana, from JT". Served through
  the existing tailnet relay / public path the radio uses, no sign-in, `noindex`, revocable from the song page
  (deleting the token kills the link). Photos served there are the 1024 px copies with metadata stripped.

---

## Privacy and safety (these are the rules, not nice-to-haves)
- Photos, captions and the story stay on this machine. No cloud vision, no outside geocoding.
- A photo song is **private**: never played on the radio, never fed into station taste (`update_taste`), never shown
  to other accounts; only its maker sees it (store the account from the `gs_session` sign-in on the draft and the job).
- The activity log records "photo song made, N photos", never photo names, captions, places or lyrics (rule 7 in AGENTS.md).
- Anything shared (video, share page) has EXIF and GPS stripped.
- 🗑️ Delete removes the originals, copies, captions, video and share token. Option on the song page: "Keep the song,
  delete the photos".
- The vision prompt never identifies people. Photos the model marks as showing a child only get used for family songs
  (occasion "Not a love song"); a love song with a child in a photo gets a notice: "this one looks like it belongs in a
  family song: leave it out?".
- Limits: 30 photos, 25 MB each, 300 MB per draft; drafts that weren't made into a song go after 30 days.

---

## Storage
```
~/yue2/studio/photosongs/<draft id>/
  draft.json          answers, per-photo cards (meta + vision JSON + user edits, order, stars), story, lyrics, account
  originals/          as uploaded
  view/<n>.jpg        1024 px, oriented, metadata stripped (vision + share page + video)
  thumb/<n>.jpg       320 px
  video-1x1.mp4, video-9x16.mp4  (once made)
```
The song itself stays in the normal library (`library/<job id>/`), with `photosong` pointing back at the draft.

## API (in `studio/app.py`, or a new `studio/photosong.py` imported by it like `art.py`)
| Method + path | Does |
|---|---|
| `POST /api/photosong` | multipart: photos + answers → `{id, photos:[{n, thumb, date, place}]}` |
| `POST /api/photosong/<id>/photos` / `DELETE …/photos/<n>` | add / remove photos |
| `POST /api/photosong/<id>/analyze` | SSE: one event per photo read, then the moments |
| `PUT /api/photosong/<id>` | save edits (cards, order, stars, answers, story) |
| `POST /api/photosong/<id>/story` | SSE: write (or rewrite) the story from the cards |
| `POST /api/lyrics` with `action: "photos", photosong: <id>` | the lyrics, streamed as today |
| `POST /api/jobs` with `photosong: <id>` | queue the song (cover from the photos) |
| `POST /api/photosong/<id>/video` | make the video in the background; `GET …/video.mp4` |
| `POST`/`DELETE /api/photosong/<id>/share` | create / revoke the share link |
| `GET /s/<token>` | the share page |
| `DELETE /api/photosong/<id>` | delete everything (`?keep_song=1` keeps the audio) |

Every `/api/photosong/*` call checks the signed-in account owns the draft.

## Page
- `studio/index.html`: a "📷 From photos" choice in Create beside "Write from an idea" / "My own lyrics", in Easy and Advanced.
- New script `studio/web/photosong.js` (the same plain-script style, `$`, `esc`, `api`), with the four steps as
  collapsible cards so the phone layout works (the photos will mostly come from phones).
- On the phone: the photo picker opens the camera roll (`accept="image/*" multiple`).

## Later: Hermes and WhatsApp
A Hermes tool `photosong_start(photos, for, occasion)`: send Hermes photos on WhatsApp ("make Ana a song from these for
our anniversary") → it makes the draft and replies with the link to step 2, so the story still gets a human check before
recording. (WhatsApp strips EXIF, so dates come from the message times.)

---

## Build order
1. **Core** (enough to make a real song): ingest + EXIF + offline places, vision cards, moments + story, `action: "photos"`
   lyrics, photo cover, private flag, kept off the radio, delete. Page steps 1–4.
2. **The gift:** share page, video, Love presets, "Painted cover".
3. **Polish:** lyric captions timed with whisper, the Hermes/WhatsApp start, the child-photo notice, auto-tidy of old drafts.

## How we'll know it works
- Test set: 3 real photo sets (a couple's year, a trip, a wedding), 15–25 photos each, some HEIC, some from WhatsApp with no EXIF.
- Each lyric has **≥ 4 details traceable to specific photos**, the name used correctly, nothing from "Leave out", and
  `cliche_hits` ≤ 2.
- The vision cards: the person who took the photos agrees with ≥ 90 % of them as written; no card names or guesses
  anything about a person.
- Photos → lyrics ready in under 4 min for 20 photos, with the 4090 free.
- Privacy checks (`tests/`): the photo song is missing from the radio's library and taste, from another account's
  `/api/state`, and from the activity log; the share-page images and the MP4 carry no EXIF/GPS (`exiftool`);
  a revoked token returns 404.

## Open questions for JT
1. **Engine:** MiniMax as the default for love songs, or wait for the A/B listening?
2. **Share link:** public through the relay (anyone with the link) or tailnet-only? A gift usually goes to someone
   outside the tailnet.
3. **Duet:** offer a "two voices" style (call and response between a man's and a woman's voice) as a first-dance option?
4. **Accounts:** the Studio library is shared today. Is "private to its maker" enough, or should a photo song also be
   hidden from admins?
