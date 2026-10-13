#!/usr/bin/env python3
"""Agentora speech service: text-to-speech and speech-to-text for Hermes, the chat panels and any other tool.

  Voice:     Qwen3-TTS 1.7B VoiceDesign (the voice is described in words; a fixed seed keeps it consistent), on the
             GPU under a GPU-broker lease. It loads on first use and unloads after SPEECH_IDLE seconds, so it never
             sits on the card.
  Listening: faster-whisper small.en on the CPU (int8): quick for spoken phrases and never needs the card.

OpenAI-compatible, so anything that speaks OpenAI audio can use it (Hermes: tts/stt provider "openai" with this
base_url):
  POST /v1/audio/speech         {input, voice?, instructions?, response_format?: mp3|wav|opus|aac|flac|pcm, speed?}
  POST /v1/audio/transcriptions multipart: file (any audio ffmpeg reads), language?, response_format?: json|text
  GET  /v1/voices  /v1/models  /health
`voice` is one of VOICES (or an OpenAI name, which maps to the default); `instructions` describes a voice in words.
Runs in ~/venv-speech (faster-qwen3-tts, faster-whisper). Usage: speech_server.py [--port 7895]
"""
import argparse, json, logging, os, subprocess, threading, time, urllib.request
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import logsetup

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=7895)
ap.add_argument("--tts-model", default="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign")
ap.add_argument("--stt-model", default="small.en")
A = ap.parse_args()
log = logging.getLogger("speech")

BROKER = os.environ.get("GPU_BROKER", "http://127.0.0.1:7890")
IDLE_S = int(os.environ.get("SPEECH_IDLE", "120"))            # unload the voice after this long unused
TTS_MB = int(os.environ.get("SPEECH_TTS_MB", "5500"))        # its footprint on the card (Qwen3-TTS 1.7B, bf16: ~5 GB measured)
SEED = 1234
VOICES = {   # name -> description (Qwen3-TTS voice design). "agentora" is the default.
    "agentora": "A warm, friendly, clear adult voice with a natural, relaxed pace and gentle expression, like a "
                "helpful friend explaining something.",
    "narrator": "A calm, rich, deep male narrator voice, unhurried and expressive, like an audiobook reader.",
    "bright": "A bright, cheerful young female voice, upbeat and quick, with a smile in it.",
    "jack": "A deep, gravelly, raspy old male monster voice, like a grumpy cartoon villain. Furious and wildly "
            "over-the-top theatrical, with big swings in pitch and volume, shouting some words, sarcastic and comedic.",
}
OPENAI_NAMES = {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"}
FORMATS = {"mp3": ("mp3", "audio/mpeg"), "wav": ("wav", "audio/wav"), "opus": ("opus", "audio/ogg"),
           "aac": ("adts", "audio/aac"), "flac": ("flac", "audio/flac")}

tts = {"model": None, "lease": None, "used": 0.0}
tts_lock = threading.Lock()
stt = {"model": None}


# ---------------------------------------------------------------- the GPU broker
def broker(method, path, body=None, timeout=30):
    r = urllib.request.Request(BROKER + path, json.dumps(body).encode() if body is not None else None,
                               {"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def take_lease():
    try:
        d = broker("POST", "/lease", {"client": "speech", "kind": "interactive", "mem_mb": TTS_MB, "wait_s": 10, "ttl_s": IDLE_S + 30}, 25)
        return d.get("id")
    except Exception as e:
        log.warning("GPU broker not reachable (%s): loading the voice anyway", e)
        return None


def renew_lease():
    if tts["lease"]:
        try:
            broker("POST", f"/lease/{tts['lease']}/renew", {"ttl_s": IDLE_S + 30}, 5)
        except Exception:
            pass


# ---------------------------------------------------------------- voice
def load_tts():
    if tts["model"] is None:
        tts["lease"] = take_lease()
        t0 = time.time()
        from faster_qwen3_tts import FasterQwen3TTS
        tts["model"] = FasterQwen3TTS.from_pretrained(A.tts_model)
        log.info("voice loaded on the GPU in %.1f s (%s)", time.time() - t0, A.tts_model)
    tts["used"] = time.time()
    renew_lease()
    return tts["model"]


def unload_when_idle():
    while True:
        time.sleep(10)
        with tts_lock:
            if tts["model"] is not None and time.time() - tts["used"] > IDLE_S:
                import torch
                tts["model"] = None
                import gc; gc.collect(); torch.cuda.empty_cache()
                if tts["lease"]:
                    try:
                        broker("DELETE", f"/lease/{tts['lease']}", timeout=5)
                    except Exception:
                        pass
                tts["lease"] = None
                log.info("voice unloaded after %d s unused: the GPU is free again", IDLE_S)


def synth(text, description):
    """Float32 mono samples and the sample rate."""
    import numpy as np, torch
    with tts_lock:
        m = load_tts()
        torch.manual_seed(SEED)                          # the same described voice every time
        chunks, sr = [], 24000
        for chunk, sr, _ in m.generate_voice_design_streaming(text=text, instruct=description, language="English"):
            chunks.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
        tts["used"] = time.time()
    return (np.concatenate(chunks) if chunks else np.zeros(0, np.float32)), sr


def encode(samples, sr, fmt, speed=1.0):
    if fmt == "pcm":
        import numpy as np
        return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes(), "audio/pcm"
    container, ctype = FORMATS.get(fmt, FORMATS["mp3"])
    af = ["-af", f"atempo={max(0.5, min(2.0, speed)):.3f}"] if abs(speed - 1.0) > 0.01 else []
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
                          *af, "-f", container, "pipe:1"], input=samples.astype("float32").tobytes(), capture_output=True, timeout=120)
    if out.returncode:
        raise RuntimeError(out.stderr.decode()[:200])
    return out.stdout, ctype


# ---------------------------------------------------------------- listening
def load_stt():
    if stt["model"] is None:
        from faster_whisper import WhisperModel
        t0 = time.time()
        stt["model"] = WhisperModel(A.stt_model, device=os.environ.get("SPEECH_STT_DEVICE", "cpu"), compute_type="int8")
        log.info("listening model %s ready on the CPU in %.1f s", A.stt_model, time.time() - t0)
    return stt["model"]


def transcribe(data, language=None):
    """Any audio ffmpeg can read (a browser's webm/ogg recording, mp3, wav...) -> text. Decoded with ffmpeg to 16 kHz
    mono float32: the PyAV that faster-whisper bundles can't open everything."""
    import numpy as np
    t0 = time.time()
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
                         input=data, capture_output=True, timeout=120)
    if out.returncode or not out.stdout:
        raise ValueError("couldn't read that audio: " + out.stderr.decode()[:200])
    audio = np.frombuffer(out.stdout, dtype=np.float32)
    segments, info = load_stt().transcribe(audio, language=language or "en", vad_filter=True, beam_size=1)
    text = " ".join(s.text.strip() for s in segments).strip()
    log.info("transcribed %.1f s of audio in %.1f s", info.duration, time.time() - t0)
    return text, info


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        log.debug("http %s", fmt % a)

    def send(self, code, body, ctype="application/json"):
        b = json.dumps(body).encode() if ctype == "application/json" else body
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def fail(self, code, msg):
        self.send(code, {"error": {"message": msg, "type": "invalid_request_error"}})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/v1/health"):
            return self.send(200, {"ok": True, "voice_loaded": tts["model"] is not None, "listening_loaded": stt["model"] is not None})
        if path == "/v1/voices":
            return self.send(200, {"default": "agentora", "voices": VOICES})
        if path == "/v1/models":
            return self.send(200, {"object": "list", "data": [{"id": "qwen3-tts", "object": "model"}, {"id": f"whisper-{A.stt_model}", "object": "model"}]})
        self.fail(404, "not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            if path in ("/v1/audio/speech", "/audio/speech"):
                b = json.loads(raw or b"{}")
                text = str(b.get("input") or "").strip()[:4000]
                if not text:
                    return self.fail(400, "input is empty")
                v = str(b.get("voice") or "agentora")
                desc = (b.get("instructions") or "").strip() or VOICES.get(v) or VOICES["agentora"]
                t0 = time.time()
                samples, sr = synth(text, desc)
                audio, ctype = encode(samples, sr, str(b.get("response_format") or "mp3"), float(b.get("speed") or 1.0))
                log.info("spoke %d characters as %s: %.1f s of audio in %.1f s", len(text), v if v in VOICES else "a described voice",
                         len(samples) / sr, time.time() - t0)
                return self.send(200, audio, ctype)
            if path in ("/v1/audio/transcriptions", "/audio/transcriptions"):
                msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + self.headers.get("Content-Type", "").encode() + b"\r\n\r\n" + raw)
                fields = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()} if msg.is_multipart() else {}
                if "file" not in fields:
                    return self.fail(400, "send the audio as multipart field 'file'")
                lang = (fields["language"].get_content().strip() if "language" in fields else "") or None
                fmt = (fields["response_format"].get_content().strip() if "response_format" in fields else "json")
                text, info = transcribe(fields["file"].get_payload(decode=True), lang)
                if fmt == "text":
                    return self.send(200, text.encode(), "text/plain; charset=utf-8")
                return self.send(200, {"text": text, "language": info.language, "duration": round(info.duration, 2)})
        except Exception as e:
            log.error("%s failed: %s", path, e, exc_info=True)
            return self.fail(500, f"{type(e).__name__}: {e}"[:300])
        self.fail(404, "not found")


class Server(logsetup.QuietBrokenPipes, ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    logsetup.setup("speech")
    threading.Thread(target=unload_when_idle, daemon=True, name="unloader").start()
    threading.Thread(target=load_stt, daemon=True, name="stt-load").start()
    log.info("speech service on http://127.0.0.1:%d (voice: Qwen3-TTS on the GPU when used; listening: whisper %s on the CPU)", A.port, A.stt_model)
    Server(("127.0.0.1", A.port), Handler).serve_forever()
