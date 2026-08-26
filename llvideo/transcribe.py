"""Local transcription — free, word-level, no network.

Benchmarked on a 20-core i7-12700H with int8 on CPU, real speech:

    small      3.73x realtime   30s for 112s audio   <- the only practical size
    medium     0.82x realtime   136s for 112s audio
    large-v3   0.18x realtime   632s for 112s audio

`small` is the default and the recommendation. `medium` and `large-v3` are
slower than realtime on CPU, so a ten-minute video would take 12 and 56 minutes
respectively. They are selectable, but the CLI warns first.

Use this when word-level timing matters. The video model's own audio pass is
instant and gives context, but its event timestamps drift by about a second.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import MissingDependency, LLVideoError

PRACTICAL = {"tiny", "base", "small"}
SLOW = {"medium": 0.82, "large-v2": 0.2, "large-v3": 0.18, "large-v3-turbo": 1.5}


def extract_audio(src: str, out_path: str | None = None) -> str:
    """16 kHz mono wav — what whisper wants, and small."""
    if not shutil.which("ffmpeg"):
        raise MissingDependency("ffmpeg not found on PATH.")
    out = out_path or str(Path(tempfile.mkdtemp(prefix="llvideo_audio_")) / "audio.wav")
    cp = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", src,
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out],
        capture_output=True, text=True, timeout=3600, encoding="utf-8", errors="replace")
    if cp.returncode != 0 or not Path(out).exists():
        raise LLVideoError(f"Could not extract audio.\nffmpeg said: {cp.stderr.strip()[:300]}")
    return out


def transcribe(src: str, model: str = "small", language: str | None = None,
               threads: int = 0, beam_size: int = 5) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise MissingDependency(
            "faster-whisper is not installed. Install it with:\n"
            "  pip install faster-whisper\n"
            "It runs on CPU and needs no GPU."
        ) from exc

    import os
    import time

    if threads <= 0:
        threads = min(20, os.cpu_count() or 4)

    warning = None
    if model in SLOW:
        warning = (f"'{model}' runs at about {SLOW[model]}x realtime on CPU — slower than the "
                   f"video itself. 'small' is 3.7x realtime and is the practical choice.")

    audio_path = src
    cleanup_dir = None
    if Path(src).suffix.lower() not in {".wav", ".flac", ".mp3", ".m4a", ".ogg"}:
        audio_path = extract_audio(src)
        cleanup_dir = Path(audio_path).parent

    try:
        t0 = time.time()
        wm = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)
        load_s = time.time() - t0

        t1 = time.time()
        segments, info = wm.transcribe(audio_path, beam_size=beam_size, language=language,
                                       word_timestamps=True, vad_filter=True)
        segs = []
        words = []
        for s in segments:
            segs.append({"start": round(s.start, 3), "end": round(s.end, 3),
                         "text": s.text})
            for w in (s.words or []):
                words.append({"start": round(w.start, 3), "end": round(w.end, 3),
                              "word": w.word, "probability": round(w.probability, 3)})
        infer_s = time.time() - t1

        return {
            "text": " ".join(s["text"].strip() for s in segs).strip(),
            "segments": segs,
            "words": words,
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "duration": round(info.duration, 2),
            "model": model,
            "threads": threads,
            "load_seconds": round(load_s, 1),
            "inference_seconds": round(infer_s, 1),
            "realtime_factor": round(info.duration / infer_s, 2) if infer_s else None,
            "warning": warning,
        }
    finally:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Groq — hosted whisper-large-v3-turbo
#
# The reason this exists: on this CPU, local `small` runs at 3.73x realtime and
# is the weakest usable Whisper tier, while local `large-v3` runs at 0.18x —
# 56 minutes for a 10-minute video, unusable. Groq serves a distilled large-v3
# at roughly 200x realtime for $0.04 per hour of audio, so a 10-minute video
# costs about $0.007 and finishes in seconds.
#
# That is better on both speed and accuracy. Local stays the default only
# because it needs no key, no network, and costs nothing.
# ---------------------------------------------------------------------------

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_MAX_BYTES = 24 * 1024 ** 2      # 25MB free tier; stay under it


def groq_available() -> bool:
    from .providers.base import read_key
    return bool(read_key("GROQ_API_KEY"))


def transcribe_groq(src: str, model: str = GROQ_MODEL,
                    language: str | None = None) -> dict:
    """Hosted transcription with word-level timestamps."""
    import json as _json
    import time
    import urllib.error
    import urllib.request
    import uuid

    from .providers.base import read_key
    from .errors import ProviderError

    key = read_key("GROQ_API_KEY")
    if not key:
        raise MissingDependency(
            "GROQ_API_KEY is not set. Get one at https://console.groq.com/keys and put it "
            "in the environment or in ~/.itachi-api-keys.\n"
            "Or use the local backend instead — it is free and needs no key:\n"
            "  llvideo transcribe VIDEO --backend local"
        )

    audio_path = extract_audio(src)
    cleanup_dir = Path(audio_path).parent
    try:
        size = Path(audio_path).stat().st_size
        if size > GROQ_MAX_BYTES:
            raise LLVideoError(
                f"Extracted audio is {size / 1024**2:.0f} MB, over Groq's 25 MB limit. "
                f"Use the local backend for long files, or split the audio first."
            )
        data = Path(audio_path).read_bytes()

        boundary = f"----llvideo{uuid.uuid4().hex}"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n".encode())

        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"audio.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode())
        parts.append(data)
        parts.append(b"\r\n")
        field("model", model)
        field("response_format", "verbose_json")
        field("timestamp_granularities[]", "word")
        field("timestamp_granularities[]", "segment")
        if language:
            field("language", language)
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        req = urllib.request.Request(
            GROQ_URL, data=body,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                payload = _json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise ProviderError(f"Groq rejected the request: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"Could not reach Groq: {exc.reason}") from exc
        elapsed = time.time() - t0

        segs = [{"start": round(s.get("start", 0.0), 3),
                 "end": round(s.get("end", 0.0), 3),
                 "text": s.get("text", "")}
                for s in (payload.get("segments") or [])]
        words = [{"start": round(w.get("start", 0.0), 3),
                  "end": round(w.get("end", 0.0), 3),
                  "word": w.get("word", "")}
                 for w in (payload.get("words") or [])]
        duration = float(payload.get("duration") or 0.0)
        return {
            "text": (payload.get("text") or "").strip(),
            "segments": segs,
            "words": words,
            "language": payload.get("language"),
            "duration": round(duration, 2),
            "model": model,
            "backend": "groq",
            "inference_seconds": round(elapsed, 2),
            "realtime_factor": round(duration / elapsed, 1) if elapsed else None,
            "estimated_cost_usd": round(duration / 3600 * 0.04, 5),
            "warning": None,
        }
    finally:
        shutil.rmtree(cleanup_dir, ignore_errors=True)


def transcribe_auto(src: str, backend: str = "auto", model: str | None = None,
                    language: str | None = None) -> dict:
    """Pick a backend. Local is the default because it is free and offline."""
    if backend == "groq":
        return transcribe_groq(src, model=model or GROQ_MODEL, language=language)
    if backend == "local":
        return transcribe(src, model=model or "small", language=language)
    if backend == "auto":
        if groq_available():
            return transcribe_groq(src, model=model or GROQ_MODEL, language=language)
        return transcribe(src, model=model or "small", language=language)
    raise LLVideoError(f"Unknown transcription backend '{backend}'. Use local, groq or auto.")
