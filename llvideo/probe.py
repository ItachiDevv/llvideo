"""T0 — local signal extraction. Zero tokens, always runs first.

Everything here is measured by ffmpeg/ffprobe, not inferred by a model.
Frame-exact facts come from this module and nowhere else.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .errors import MissingDependency, ProbeFailed

# Measured on gemini-3.x, constant across every model and duration tested.
# See PRIMARY-EVIDENCE.md.
VIDEO_TOKENS_PER_SEC = 71.0
AUDIO_TOKENS_PER_SEC = 32.0


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise MissingDependency(
            f"{tool} not found on PATH. Install ffmpeg (which bundles ffprobe):\n"
            f"  winget install Gyan.FFmpeg      # Windows\n"
            f"  brew install ffmpeg             # macOS\n"
            f"  sudo apt install ffmpeg         # Debian/Ubuntu"
        )
    return path


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


@dataclass
class Probe:
    path: str
    duration: float
    size_bytes: int
    container: str = ""
    video_codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    nb_frames: int = 0
    has_video: bool = False
    has_audio: bool = False
    audio_codec: str = ""
    audio_channels: int = 0
    audio_sample_rate: int = 0
    bit_rate: int = 0
    rotation: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def display_width(self) -> int:
        """Width after the container's rotation flag is applied — what you actually see."""
        return self.height if abs(self.rotation) % 180 == 90 else self.width

    @property
    def display_height(self) -> int:
        return self.width if abs(self.rotation) % 180 == 90 else self.height

    @property
    def is_portrait(self) -> bool:
        return self.display_height > self.display_width

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000

    @property
    def orientation(self) -> str:
        return "portrait" if self.is_portrait else "landscape"

    # Exact desktop resolutions. Necessary but NOT sufficient — 3840x2160 and
    # 1920x1080 are just as common for cameras.
    _DESKTOP_SIZES = {
        (1920, 1080), (2560, 1440), (3840, 2160),
        (1366, 768), (1440, 900), (1680, 1050), (2880, 1800), (1280, 800),
    }
    # Screen capture runs at an exact integer refresh rate. Camera and cinema
    # footage sits on 23.976 / 24 / 29.97, which never round clean.
    _CAPTURE_RATES = (25.0, 30.0, 50.0, 60.0, 120.0)

    @property
    def is_screen_content(self) -> bool:
        """Only a proxy-height hint: screen content needs 1080p so small text
        stays legible; camera footage is fine at 720p.

        Resolution alone gives false positives (4K UHD is a camera size too),
        so an exact integer capture frame rate is also required. Conservative
        by design — guessing 'camera' on real screen content only costs
        legibility, and the caller can force it.
        """
        if (self.display_width, self.display_height) not in self._DESKTOP_SIZES:
            return False
        return any(abs(self.fps - r) < 0.01 for r in self._CAPTURE_RATES)

    def estimate_tokens(self, fps_ratio: float = 1.0, with_audio: bool | None = None) -> dict:
        """Token estimate. fps_ratio 1.0 == Gemini default 1 fps sampling."""
        if with_audio is None:
            with_audio = self.has_audio
        video = VIDEO_TOKENS_PER_SEC * fps_ratio * self.duration
        audio = AUDIO_TOKENS_PER_SEC * self.duration if with_audio else 0.0
        return {
            "video": int(video),
            "audio": int(audio),
            "total": int(video + audio),
            "fps_ratio": fps_ratio,
            "with_audio": with_audio,
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        d["megapixels"] = round(self.megapixels, 2)
        d["display_width"] = self.display_width
        d["display_height"] = self.display_height
        d["orientation"] = self.orientation
        return d


def _num(value, cast, default=0):
    try:
        if value in (None, "", "N/A"):
            return default
        return cast(value)
    except (TypeError, ValueError):
        return default


def _parse_fraction(value: str) -> float:
    if not value or value == "0/0":
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        d = _num(den, float, 0.0)
        return _num(num, float, 0.0) / d if d else 0.0
    return _num(value, float, 0.0)


def probe(path: str | Path) -> Probe:
    """Read container/stream metadata. Raises ProbeFailed with ffprobe's own words."""
    _require("ffprobe")
    p = Path(path)
    if not p.exists():
        raise ProbeFailed(f"File does not exist: {p}")
    if p.stat().st_size == 0:
        raise ProbeFailed(f"File is empty (0 bytes): {p}")

    cp = run(["ffprobe", "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", str(p)])
    if cp.returncode != 0:
        raise ProbeFailed(
            f"ffprobe could not read {p.name}.\nffprobe said: {cp.stderr.strip() or '(no message)'}"
        )
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeFailed(f"ffprobe returned unparseable JSON for {p.name}: {exc}") from exc

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vs = next((s for s in streams if s.get("codec_type") == "video"), None)
    as_ = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _num(fmt.get("duration"), float, 0.0)
    if duration <= 0 and vs:
        duration = _num(vs.get("duration"), float, 0.0)

    out = Probe(
        path=str(p),
        duration=duration,
        size_bytes=_num(fmt.get("size"), int, p.stat().st_size),
        container=fmt.get("format_name", ""),
        bit_rate=_num(fmt.get("bit_rate"), int, 0),
        has_video=vs is not None,
        has_audio=as_ is not None,
    )
    if vs:
        out.video_codec = vs.get("codec_name", "")
        out.width = _num(vs.get("width"), int, 0)
        out.height = _num(vs.get("height"), int, 0)
        out.fps = _parse_fraction(vs.get("avg_frame_rate") or vs.get("r_frame_rate") or "")
        out.nb_frames = _num(vs.get("nb_frames"), int, 0)
        # Phone footage is stored landscape with a rotation flag. ffmpeg applies it
        # on decode, so the frames you get are portrait even though the stream is not.
        rot = 0
        for sd in (vs.get("side_data_list") or []):
            if "rotation" in sd:
                rot = _num(sd.get("rotation"), float, 0.0)
                break
        if not rot:
            rot = _num((vs.get("tags") or {}).get("rotate"), float, 0.0)
        out.rotation = int(rot) % 360
        if out.rotation > 180:
            out.rotation -= 360
    if as_:
        out.audio_codec = as_.get("codec_name", "")
        out.audio_channels = _num(as_.get("channels"), int, 0)
        out.audio_sample_rate = _num(as_.get("sample_rate"), int, 0)

    if not out.has_video:
        out.warnings.append("No video stream — audio-only file.")
    if duration <= 0:
        out.warnings.append("Duration unknown; the file may be truncated or still being written.")
    if out.fps and out.nb_frames and duration > 0:
        implied = out.nb_frames / duration
        if abs(implied - out.fps) / max(out.fps, 1e-6) > 0.05:
            out.warnings.append(
                f"Declared {out.fps:.2f} fps but frame count implies {implied:.2f} fps "
                f"— variable frame rate or dropped frames."
            )
    return out


# --------------------------------------------------------------------------
# Detectors. All verified exact to the millisecond against constructed
# ground truth — see PRIMARY-EVIDENCE.md.
# --------------------------------------------------------------------------

_TIME = r"[-+]?\d+\.?\d*"


def _ffmpeg_filter_log(src: str, args: list[str], timeout: int = 900) -> str:
    _require("ffmpeg")
    cp = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", src, *args, "-f", "null", "-"],
             timeout=timeout)
    return cp.stderr or ""


def detect_black(src: str, min_dur: float = 0.5, threshold: float = 0.98) -> list[dict]:
    log = _ffmpeg_filter_log(src, ["-vf", f"blackdetect=d={min_dur}:pic_th={threshold}"])
    out = []
    for m in re.finditer(
        rf"black_start:({_TIME})\s+black_end:({_TIME})\s+black_duration:({_TIME})", log
    ):
        out.append({"start": float(m.group(1)), "end": float(m.group(2)),
                    "duration": float(m.group(3))})
    return out


def detect_freeze(src: str, min_dur: float = 2.0, noise: str = "-60dB") -> list[dict]:
    log = _ffmpeg_filter_log(src, ["-vf", f"freezedetect=n={noise}:d={min_dur}"])
    starts = [float(m.group(1)) for m in re.finditer(rf"freeze_start:\s*({_TIME})", log)]
    ends = [float(m.group(1)) for m in re.finditer(rf"freeze_end:\s*({_TIME})", log)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        out.append({"start": s, "end": e, "duration": (e - s) if e is not None else None})
    return out


def detect_silence(src: str, noise: str = "-30dB", min_dur: float = 0.5) -> list[dict]:
    log = _ffmpeg_filter_log(src, ["-af", f"silencedetect=n={noise}:d={min_dur}"])
    starts = [float(m.group(1)) for m in re.finditer(rf"silence_start:\s*({_TIME})", log)]
    ends = [float(m.group(1)) for m in re.finditer(rf"silence_end:\s*({_TIME})", log)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        out.append({"start": s, "end": e, "duration": (e - s) if e is not None else None})
    return out


def detect_scenes(src: str, threshold: float = 0.12) -> list[float]:
    """Scene-change timestamps.

    WARNING — this is unreliable on its own and MUST NOT be used alone to pick
    frames. Two silent failure modes, both measured on real footage:

      1. The FIRST cut is structurally undetectable. The `scene` value scores
         frame N against frame N-1; frame 1 has no predecessor.
      2. Some interior cuts never register at ANY threshold, down to 0.05,
         when two shots share a colour palette and exposure.

    Always combine with a uniform floor — use `select_frame_times()`, which does.
    Default threshold 0.12 comes from a sweep on real footage: a genuine cut
    scored between 0.05 and 0.15 and was already lost by 0.20. Synthetic
    solid-colour test clips score far higher and will mislead you upward.
    """
    log = _ffmpeg_filter_log(src, ["-vf", rf"select='gt(scene\,{threshold})',showinfo"])
    times = sorted({round(float(m.group(1)), 3)
                    for m in re.finditer(rf"pts_time:({_TIME})", log)})
    return times


def select_frame_times(duration: float, scene_times: list[float] | None = None,
                       floor_interval: float = 12.0, max_frames: int = 64) -> list[float]:
    """Frame timestamps = uniform floor UNION scene hits.

    The union is the whole point. Scene detection alone silently drops cuts
    (see detect_scenes); a uniform floor alone misses fast cuts. Together they
    degrade gracefully.
    """
    if duration <= 0:
        return [0.0]
    n = max(2, min(max_frames, int(duration // floor_interval) + 1))
    step = duration / n
    times = {round(min(step * (i + 0.5), max(duration - 0.05, 0.0)), 3) for i in range(n)}
    for t in (scene_times or []):
        if 0.0 <= t <= duration:
            times.add(round(t + 0.15, 3))  # land just after the cut, not on it
    ordered = sorted(times)
    if len(ordered) <= max_frames:
        return ordered
    keep = max(1, len(ordered) // max_frames)
    return ordered[::keep][:max_frames]
