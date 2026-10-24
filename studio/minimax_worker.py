#!/usr/bin/env python3
"""MiniMax Music 3: the Music Studio's second song engine (lyrics + style -> a sung song, up to ~5 min).

Runs in its own venv (~/venv-minimax: torch + the diffusers MiniMax Music 3 pipeline), never in the YuE2 venv.
Weights: MINIMAX_MODEL (default ~/models/MiniMax-Music3, the diffusers subfolders of MiniMaxAI/MiniMax-Music3).

Two ways to run it:
  --serve   app.py's worker: JSON jobs on stdin (one per line), JSON events on stdout
            ({"event": "ready"|"update"|"done"|"failed"|"fatal", ...}). Loads the model once, holds a GPU broker lease
            while loaded, and exits on EOF (that frees all its GPU memory).
  --once    a single song from the command line: --style, --lyrics (a file), --seconds, --out <song dir>.

It writes <song dir>/audio.flac and <song dir>/result.json, like YuE2's save_artifacts; app.py makes the MP3.
"""
import argparse, json, os, re, signal, sys, threading, time, traceback, urllib.request
from pathlib import Path

MODEL = Path(os.environ.get("MINIMAX_MODEL", "~/models/MiniMax-Music3")).expanduser()
# Its modular_model_index.json must point at MODEL, not the hub id, or diffusers downloads all ~29 GB again into the
# HF cache; offline mode turns that mistake into an error instead.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
BROKER = os.environ.get("GPU_BROKER", "http://127.0.0.1:7890")
MAX_SECONDS = float(os.environ.get("MINIMAX_MAX_SECONDS", "300"))  # its KV cache for a 6-min song wouldn't fit beside the models
NEED_MB = int(os.environ.get("MINIMAX_NEED_MB", "18500"))    # free card memory a song needs: language model + depth decoder + KV cache
HOLD_MB = int(os.environ.get("MINIMAX_HOLD_MB", "2600"))     # working memory held for the song (KV cache, activations)
LEASE_MB = int(os.environ.get("MINIMAX_LEASE_MB", "19000"))   # peak with CPU offload: the 8B language model + its depth decoder
OUT = sys.stdout                                               # events go here; everything else is redirected to stderr
sys.stdout = sys.stderr

SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*(.*)$")


def to_minimax_lyrics(text):
    """Agentora's lyrics ([Verse] / [Chorus] tags, one sung line per line) in MiniMax's format: every tag on its own
    line (MiniMax drops words that share a line with a leading tag), no blank lines, a [verse] if nothing is tagged.
    Tags are lowercased by the pipeline itself; numbered tags ([Verse 2]) become plain ones."""
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = SECTION.match(line)
        if m:
            tag = re.sub(r"\s*\d+$", "", m.group(1).strip()) or "verse"
            out.append(f"[{tag.lower()}]")
            if m.group(2).strip():
                out.append(m.group(2).strip())
        else:
            out.append(line)
    if not out or not out[0].startswith("["):
        out.insert(0, "[verse]")
    return "\n".join(out)


VOCAL = re.compile(r"\b(vocal|vocals|voice|singer|singing|rap|rapper|rapping|scream|screams|growl|growls|harmon\w*|falsetto|"
                   r"male|female|baritone|tenor|alto|soprano|choir|spoken|whisper\w*|belt\w*|croon\w*)\b", re.I)
TEMPO = re.compile(r"\b(\d{2,3})\s*bpm\b", re.I)


def to_minimax_caption(style):
    """Agentora's style is usually a comma list of tags ("English, boom bap, male rap, dusty drums, 90 BPM").
    MiniMax was trained on prose captions (Global Metadata / Vocal Details / Arrangement), so a tag list is regrouped
    into those three parts. Free prose (e.g. from ✨ Enhance) is passed through unchanged."""
    style = style.strip()
    parts = [p.strip() for p in re.split(r"[,;\n]", style) if p.strip()]
    if len(parts) < 3 or sum(len(p.split()) for p in parts) / len(parts) > 6:      # already prose
        return style
    vocals = [p for p in parts if VOCAL.search(p)]
    tempo = next((m.group(1) for p in parts for m in [TEMPO.search(p)] if m), None)
    langs = [p for p in parts if p.lower() in ("english", "chinese", "mandarin", "cantonese", "japanese", "korean", "spanish",
                                                 "french", "german", "portuguese", "italian")]
    rest = [p for p in parts if p not in vocals and p not in langs and not TEMPO.search(p)]
    genre, arrangement = rest[:3], rest[3:]
    lines = ["Global Metadata",
             f"Basic Attributes: {'bpm is ' + tempo + '. ' if tempo else ''}{', '.join(genre) or style}."
             + (f" Sung in {langs[0]}." if langs else "")]
    lines += ["Vocal Details", f"Vocal Style: {', '.join(vocals)}." if vocals else "Vocal Style: a lead vocal that fits the genre."]
    lines += ["Arrangement", f"Instruments and production: {', '.join(arrangement or genre)}."]
    return "\n".join(lines)


class Lease:
    """A GPU broker lease on the 4090 for as long as the model is loaded (renewed every minute). The broker grants a
    lease even without room once wait_s runs out; it is kept anyway, because a granted lease is what makes the radio
    step aside (finish its take, unload YuE2), and render() waits until the card really has room."""
    def __init__(self, client="minimax", mem_mb=LEASE_MB, waiting=lambda text: None):
        self.id = None
        try:
            req = urllib.request.Request(f"{BROKER}/lease", json.dumps({"client": client, "kind": "interactive", "mem_mb": mem_mb,
                                         "wait_s": 120, "ttl_s": 300}).encode(), {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=150) as r:
                d = json.loads(r.read())
        except Exception as e:
            print("GPU broker not reachable, going ahead without a lease:", e, file=sys.stderr)
            return
        self.id, self.waited = d.get("id"), d.get("waited_s")
        if not d.get("roomy"):
            waiting("Waiting for GPU memory")
        print(f"broker lease {self.id}: waited {self.waited}s", file=sys.stderr)
        threading.Thread(target=self._renew, daemon=True).start()
        # a restart of yue2-studio stops this process too: hand the lease back first, or the radio waits for it to expire
        signal.signal(signal.SIGTERM, lambda *a: (self.drop(), os._exit(0)))

    def _renew(self):
        while self.id:
            time.sleep(60)
            try:
                urllib.request.urlopen(urllib.request.Request(f"{BROKER}/lease/{self.id}/renew", b"{}",
                                                              {"Content-Type": "application/json"}), timeout=5).read()
            except Exception:
                pass

    def drop(self):
        lid, self.id = self.id, None
        if lid:
            try:
                urllib.request.urlopen(urllib.request.Request(f"{BROKER}/lease/{lid}", method="DELETE"), timeout=5).read()
            except Exception:
                pass


def load():
    import torch
    from diffusers import ComponentsManager, ModularPipeline
    manager = ComponentsManager()
    pipe = ModularPipeline.from_pretrained(str(MODEL), components_manager=manager)
    pipe.load_components(dtype=torch.bfloat16)
    # Offload after loading (components added later get the default strategy): only the stage that runs sits on the
    # card, peak ~18 GB instead of ~23. The default 3 GB reserve is too much on a 24 GB card whose desktop holds ~3 GB:
    # the 16 GB language model and its 1.2 GB depth decoder must sit there together.
    manager.enable_auto_cpu_offload(device="cuda", memory_reserve_margin=os.environ.get("MINIMAX_RESERVE", "1.5GB"))
    hook_stages(pipe)
    return pipe


STAGE = [lambda phase, text: None]      # render() points this at its timer; the hooks below call it


def hook_stages(pipe):
    """Report each pipeline block as it starts (progress for the Studio, and a timing per stage)."""
    names = {"semantic_generator": ("song", "Generating the song"), "denoise": ("audio", "Rendering audio"),
             "decode": ("decode", "Decoding audio")}
    try:
        subs = pipe._blocks.sub_blocks           # pipe.blocks is a copy; the pipeline runs _blocks
    except Exception:
        return
    for name, (phase, text) in names.items():
        blk = subs.get(name)
        if blk is None:
            continue
        orig = type(blk).__call__
        blk.__class__ = type(type(blk).__name__, (type(blk),),
                             {"__call__": lambda self, *a, _o=orig, _p=phase, _t=text, **k: (STAGE[0](_p, _t), _o(self, *a, **k))[1]})


def render(pipe, job, out_dir, update=lambda **k: None):
    """One song: job has style, lyrics, seed, max_seconds and optionally steps. Writes audio.flac + result.json."""
    import numpy as np, soundfile as sf, torch
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    caption, lyrics = to_minimax_caption(job["style"]), to_minimax_lyrics(job["lyrics"])
    # The lease can be granted in the moment before YuE2 (which takes no lease) finishes loading, so check the card
    # itself: while it's short, keep the lease (the radio sees it and steps aside after its take) and wait.
    while (torch.cuda.mem_get_info()[0] + torch.cuda.memory_reserved()) / 2**20 < NEED_MB:
        update(stage="Waiting for GPU memory (the card is in use)", progress={})
        time.sleep(5)
    # Put the language model on the card now and hold its working memory in PyTorch's cache (freed tensors stay
    # reserved), so a model that loads beside us mid-song (a chat the broker lets in) sees less room and takes less,
    # instead of taking the memory the song is about to grow into and making it run out.
    for m in (pipe.language_model, pipe.rvq_depth_decoder):
        if getattr(m, "_hf_hook", None) is not None:
            m._hf_hook.pre_forward(m)
    hold = min(HOLD_MB, torch.cuda.mem_get_info()[0] // 2**20 - 300)
    if hold > 0:
        del_me = torch.empty(hold * 2**20, dtype=torch.uint8, device="cuda"); del del_me
    t0 = time.time()
    torch.cuda.reset_peak_memory_stats()
    timing, last = {}, [t0, "start"]

    def stage(name, text):                                # the pipeline's blocks run in order; time each one
        now = time.time()
        timing[last[1]] = round(now - last[0], 1); last[:] = [now, name]
        update(stage=text, progress={"phase": name})

    STAGE[0] = stage
    stage("text", "Reading the lyrics")
    audio = pipe(prompt=caption, lyrics=lyrics, audio_duration=min(float(job.get("max_seconds") or 240), MAX_SECONDS),
                 num_inference_steps=int(job.get("steps") or 30),
                 generator=torch.Generator("cuda").manual_seed(int(job["seed"]) % 2**63), output="audios")[0]
    stage("end", "Saving")
    timing.pop("start", None); timing.pop("end", None)
    y = audio.T.float().cpu().numpy() if hasattr(audio, "cpu") else np.asarray(audio).T
    sr = int(getattr(pipe, "sampling_rate", None) or 44100)
    sf.write(out_dir / "audio.flac", np.clip(y, -1, 1), sr, subtype="PCM_24")
    result = {"engine": "minimax-music3", "audio_seconds": round(len(y) / sr, 2), "sample_rate": sr,
              "gen_seconds": round(time.time() - t0, 1), "timing": timing,
              "peak_vram_mb": round(torch.cuda.max_memory_allocated() / 2**20), "seed": job["seed"],
              "caption": caption, "lyrics": lyrics, "truncated": len(y) / sr >= min(float(job.get("max_seconds") or 240), MAX_SECONDS) - 1}
    (out_dir / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def emit(**kw):
    OUT.write(json.dumps(kw) + "\n"); OUT.flush()


def serve():
    lease = Lease(waiting=lambda text: emit(event="waiting", stage=text))
    try:
        t0 = time.time()
        pipe = load()
        emit(event="ready", load_seconds=round(time.time() - t0, 1))
    except Exception as e:
        lease.drop()
        emit(event="fatal", error=f"{type(e).__name__}: {e}"[:500])
        return
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            job = json.loads(line)
            try:
                res = render(pipe, job, job["out_dir"], lambda **k: emit(event="update", id=job["id"], **k))
                emit(event="done", id=job["id"], result=res)
            except Exception as e:
                traceback.print_exc()
                emit(event="failed", id=job["id"], error=f"{type(e).__name__}: {e}"[:500])
    finally:
        lease.drop()


def once(a):
    lease = Lease()
    try:
        t0 = time.time()
        pipe = load()
        print(f"loaded in {time.time() - t0:.1f}s", file=sys.stderr)
        job = {"style": a.style, "lyrics": Path(a.lyrics).read_text(), "seed": a.seed, "max_seconds": a.seconds, "steps": a.steps}
        res = render(pipe, job, a.out, lambda **k: print(k.get("stage", ""), file=sys.stderr))
        emit(**{k: v for k, v in res.items() if k not in ("lyrics", "caption")})
    finally:
        lease.drop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--style", default="")
    ap.add_argument("--lyrics", help="a text file with the lyrics")
    ap.add_argument("--seconds", type=float, default=90)
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="minimax-song")
    a = ap.parse_args()
    serve() if a.serve else once(a)
