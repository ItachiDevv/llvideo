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
