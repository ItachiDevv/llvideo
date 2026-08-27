"""Repair what the audit found.

The useful realisation: most audit findings do not need anything regenerated.
Wrong loudness, a clipped peak, a black frame on the head or tail — these are
deterministic ffmpeg operations that cost nothing and cannot hallucinate. Only
a genuinely wrong picture needs a model involved, and that is rare.

So the order is: fix locally where ffmpeg can, say plainly what it cannot fix,
and re-audit afterwards to prove the repair actually worked rather than
claiming it did.

Three classes of finding:

  REPAIRABLE   loudness, true-peak clipping, black edge frames. Fixed here.
  EDITORIAL    a black gap mid-timeline, a freeze, low resolution. These are
               decisions or need a re-render; reported, never silently altered.
  UNFIXABLE    a corrupt or truncated file.
"""
from __future__ import annotations

import shutil
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import audit as A
from . import probe as P
from .errors import LLVideoError

TARGET_LUFS = -14.0
TARGET_TRUE_PEAK = -1.0
TARGET_LRA = 11.0

# Findings this module can actually repair. Anything else is reported, not touched.
REPAIRABLE = {"loudness", "clipping", "edge_frame"}


@dataclass
class Repair:
    check: str
    action: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"check": self.check, "action": self.action, "detail": self.detail}


@dataclass
class FixResult:
    source: str
    output: str | None
    repairs: list[Repair] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    before: dict | None = None
    after: dict | None = None
    changed: bool = False

    def to_dict(self) -> dict:
        return {
            "source": self.source, "output": self.output, "changed": self.changed,
            "repairs": [r.to_dict() for r in self.repairs],
            "skipped": self.skipped,
            "before": self.before, "after": self.after,
        }


def measure_loudness(src: str) -> dict:
    """Pass one of a two-pass loudnorm. Single-pass loudnorm guesses; this does not."""
    log = P._ffmpeg_filter_log(
        src, ["-af", f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:"
                     f"LRA={TARGET_LRA}:print_format=json", "-vn"])
    m = re.search(r"\{[^{}]*input_i[^{}]*\}", log, re.S)
    if not m:
        return {}
    import json
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _trim_bounds(pr: P.Probe, findings: list[A.Finding]) -> tuple[float, float]:
    """How much to shave off each end.

    MEASURE the black run rather than assuming a couple of frames. A first
    attempt trimmed a fixed 2 frames and left the finding standing, because the
    black was 0.15s — five frames at 30fps. blackdetect already knows the exact
    span, so ask it.
    """
    flagged_head = any(f.check == "edge_frame" and (f.at or 0) < 0.5 for f in findings)
    flagged_tail = any(f.check == "edge_frame" and (f.at or 0) >= 0.5 for f in findings)
    if not (flagged_head or flagged_tail):
        return 0.0, 0.0

    frame = 1.0 / (pr.fps or 30.0)
    from . import craft as C

    # Read the actual per-frame luma at each end rather than trusting
    # blackdetect's segment boundaries. blackdetect reported the trailing black
    # starting at 14.23s on a file where it really began at 14.15s, so a trim
    # built on it left the finding standing. The luma curve does not round.
    window = min(1.5, max(pr.duration * 0.25, frame * 8))

    head = tail = 0.0
    if flagged_head:
        prof = C.luma_profile(pr.path, 0.0, window, samples=48)
        pts = prof.get("points") or []
        first_lit = next((t for t, v in pts if v > A.BLACK_LUMA), None)
        head = (first_lit + frame) if first_lit is not None else frame * 3
    if flagged_tail:
        start = max(pr.duration - window, 0.0)
        prof = C.luma_profile(pr.path, start, pr.duration, samples=48)
        pts = prof.get("points") or []
        last_lit = next((t for t, v in reversed(pts) if v > A.BLACK_LUMA), None)
        tail = (pr.duration - last_lit + frame) if last_lit is not None else frame * 3

    # Never eat more than a fifth of the video on either end — that would mean
    # the detection is wrong, and silently deleting content is worse than a
    # finding left standing.
    cap = pr.duration * 0.2
    return min(head, cap), min(tail, cap)


def fix(src: str, out_path: str | None = None, *, findings: list[A.Finding] | None = None,
        normalise_audio: bool = True, trim_edges: bool = True,
        reaudit: bool = True) -> FixResult:
    pr = P.probe(src)
    if findings is None:
        findings = A.measured_audit(pr, margins=False)

    res = FixResult(source=src, output=None)
    res.before = A.summarise(findings)

    todo = [f for f in findings if f.check in REPAIRABLE]
    for f in findings:
        if f.check not in REPAIRABLE:
            res.skipped.append({
                "check": f.check, "severity": f.severity, "message": f.message,
                "why": _why_skipped(f.check),
            })

    wants_audio = normalise_audio and pr.has_audio and any(
        f.check in ("loudness", "clipping") for f in todo)
    head, tail = _trim_bounds(pr, todo) if trim_edges else (0.0, 0.0)
    wants_trim = head > 0 or tail > 0

    if not (wants_audio or wants_trim):
        res.after = res.before
        return res

    dest = Path(out_path or _default_out(src))
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if head > 0:
        cmd += ["-ss", f"{head:.4f}"]
    cmd += ["-i", src]
    if tail > 0:
        cmd += ["-t", f"{max(pr.duration - head - tail, 0.05):.4f}"]

    if wants_audio:
        stats = measure_loudness(src)
        if stats.get("input_i"):
            # Two-pass: feed the measured values back so the filter corrects
            # exactly rather than estimating from a single look at the stream.
            af = (f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA={TARGET_LRA}"
                  f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
                  f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
                  f":offset={stats.get('target_offset', 0)}:linear=true:print_format=summary")
            detail = (f"{stats['input_i']} LUFS -> {TARGET_LUFS} LUFS, "
                      f"true peak held at {TARGET_TRUE_PEAK} dBFS")
        else:
            af = f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA={TARGET_LRA}"
            detail = f"normalised toward {TARGET_LUFS} LUFS (single pass)"
        cmd += ["-af", af, "-c:a", "aac", "-b:a", "192k"]
        res.repairs.append(Repair("loudness", "loudnorm", detail))
    elif pr.has_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-an"]

    if wants_trim:
        # Trimming needs a re-encode to land on an exact frame boundary.
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                "-pix_fmt", "yuv420p"]
        parts = []
        if head > 0:
            parts.append(f"{head * 1000:.0f}ms from the head")
        if tail > 0:
            parts.append(f"{tail * 1000:.0f}ms from the tail")
        res.repairs.append(Repair("edge_frame", "trim", " and ".join(parts)))
    else:
        cmd += ["-c:v", "copy"]

    cmd += ["-movflags", "+faststart", str(dest)]
    cp = P.run(cmd, timeout=7200)
    if cp.returncode != 0 or not dest.exists():
        raise LLVideoError(f"Repair failed.\nffmpeg said: {cp.stderr.strip()[:400]}")

    res.output = str(dest)
    res.changed = True

    if reaudit:
        # Prove it. Claiming a repair worked without re-measuring is the exact
        # habit this whole tool exists to break.
        after_pr = P.probe(str(dest))
        res.after = A.summarise(A.measured_audit(after_pr, margins=False))
    return res


def _default_out(src: str) -> str:
    p = Path(src)
    return str(p.with_name(f"{p.stem}_fixed{p.suffix or '.mp4'}"))


def _why_skipped(check: str) -> str:
    return {
        "black_gap": "A black stretch mid-timeline is an editing decision, not a "
                     "defect ffmpeg should silently remove.",
        "freeze": "A held frame may be intentional. Shorten it in the timeline, "
                  "not in the export.",
        "resolution": "Upscaling invents detail that was never captured. "
                      "Re-render or regenerate at the target size instead.",
        "framerate": "Frame rate is set at render time; re-encoding cannot recover "
                     "frames that were never rendered.",
        "bitrate": "Re-encoding a low-bitrate file cannot restore what the first "
                   "encode discarded.",
        "safe_margin": "Layout is a composition decision. Move the element, do not "
                       "crop the export.",
        "audio": "An empty or near-empty audio track needs new audio, not a filter.",
        "silence_gap": "A silent stretch may be intentional pacing.",
        "duration": "Duration comes from the timeline; trimming to fit would cut content.",
        "aspect": "Aspect is set at render time. Re-render at the intended size.",
    }.get(check, "Not something ffmpeg can repair without making an editorial choice.")
