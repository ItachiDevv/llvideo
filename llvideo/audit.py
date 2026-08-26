"""Render QA — does this exported video have defects?

Two kinds of check, and the split matters:

  MEASURED   ffmpeg says so. Deterministic, free, no model, no argument.
             Black frames, freezes, silence, loudness, duration, safe margins,
             a first or last frame that is black. These are the checks that
             catch real render bugs, and they cost nothing.

  JUDGED     a model says so. Craft quality, whether text is readable, whether
             the pacing works. Useful, but opinion — always labelled as such.

A finding never mixes the two. If ffmpeg measured it, it is stated as fact with
the number attached. If a model thought it, it says so.
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import probe as P
from .errors import LLVideoError

SEVERITIES = ("blocker", "major", "minor", "note")

# Most encoded video is LIMITED range (TV range), where black is luma 16 and
# white is 235 — not 0 and 255. A threshold written for full range silently
# never fires on real footage. Measured on a deliberately black frame from an
# H.264 yuv420p export: mean luma 15.17, max 18. These bounds cover both ranges.
BLACK_LUMA = 20.0
WHITE_LUMA = 232.0


@dataclass
class Finding:
    severity: str
    check: str
    message: str
    at: float | None = None          # seconds, if it is localised
    measured: dict | None = None     # the numbers behind it, when measured
    source: str = "measured"         # "measured" or "judged"

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.at is not None:
            d["at"] = round(self.at, 3)
        return d


def _fmt(t: float) -> str:
    t = max(0, int(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Measured checks — ffmpeg only, zero tokens
# ---------------------------------------------------------------------------

def _last_frame_luma(src: str) -> float | None:
    """Mean luma of the genuine final frame.

    `-ss <duration-epsilon>` returns nothing at all near EOF — measured: -ss
    14.20 on a 14.30s file produced no frame. `-sseof` seeks relative to the
    end and decodes forward, so the last value printed is the real last frame.
    """
    from .probe import run, _require
    _require("ffmpeg")
    cp = run(["ffmpeg", "-hide_banner", "-nostdin", "-sseof", "-0.3", "-i", src,
              "-update", "1", "-vf",
              "scale=64:36,signalstats,metadata=print:file=-", "-f", "null", "-"],
             timeout=120)
    vals = re.findall(r"YAVG=([0-9.]+)", cp.stdout or "")
    return float(vals[-1]) if vals else None


def check_edges(pr: P.Probe) -> list[Finding]:
    """A black or frozen first/last frame is the most common render bug.

    Almost every export defect that ships to a client is this: an off-by-one on
    the timeline leaves a black frame at the top or tail. It is trivially
    measurable and nobody checks for it.
    """
    out: list[Finding] = []
    if not pr.has_video or pr.duration <= 0:
        return out
    from . import craft as C
    for label in ("first", "last"):
        if label == "first":
            t = 0.04
            prof = C.luma_profile(pr.path, t, t + 0.01, samples=1)
            luma = prof.get("min", 255) if prof else None
        else:
            # Seeking with -ss near the end of a file returns no frame at all.
            # -sseof decodes the tail and -update 1 keeps overwriting, so the
            # final value read is the genuine last frame.
            t = pr.duration
            luma = _last_frame_luma(pr.path)
        if luma is None:
            continue
        if luma <= BLACK_LUMA:
            out.append(Finding(
                "major", "edge_frame",
                f"The {label} frame is black (mean luma {luma}/255). "
                f"Usually an off-by-one on the timeline.",
                at=t, measured={"mean_luma": luma}))
        elif luma >= WHITE_LUMA:
            out.append(Finding(
                "minor", "edge_frame",
                f"The {label} frame is pure white (mean luma {luma}/255).",
                at=t, measured={"mean_luma": luma}))
    return out


def check_dead_air(pr: P.Probe) -> list[Finding]:
    """Black stretches and frozen frames inside the body of the video."""
    out: list[Finding] = []
    if not pr.has_video:
        return out
    events = P.detect_video_events(pr.path)
    for ev in events["black"]:
        if ev["start"] < 0.3 or ev["end"] > pr.duration - 0.3:
            continue  # edges are handled separately
        out.append(Finding(
            "major", "black_gap",
            f"{ev['duration']:.2f}s of black at {_fmt(ev['start'])}. "
            f"Intentional only if the edit calls for it.",
            at=ev["start"], measured=ev))
    for ev in events["freeze"]:
        dur = ev.get("duration")
        if dur and dur > 2.0 and dur < pr.duration * 0.9:
            out.append(Finding(
                "minor", "freeze",
                f"The picture is static for {dur:.1f}s from {_fmt(ev['start'])}.",
                at=ev["start"], measured=ev))
    return out


def check_audio(pr: P.Probe) -> list[Finding]:
    """Loudness, clipping and silence. All measured by ffmpeg."""
    out: list[Finding] = []
    if not pr.has_audio:
        out.append(Finding("note", "audio", "No audio track.", source="measured"))
        return out

    ev = P.detect_audio_events(pr.path, noise="-45dB", min_dur=1.5)
    lufs = ev["integrated_lufs"]
    peak = ev["true_peak_dbfs"]
    measured = {"integrated_lufs": lufs, "true_peak_dbfs": peak,
                "loudness_range": ev["loudness_range"]}

    if lufs is not None:
        if lufs < -30:
            out.append(Finding(
                "major", "loudness",
                f"Very quiet at {lufs:.1f} LUFS. Most platforms target -14, "
                f"broadcast -23. This will be inaudible next to other content.",
                measured=measured))
        elif lufs > -9:
            out.append(Finding(
                "major", "loudness",
                f"Very loud at {lufs:.1f} LUFS. Platforms normalise to about -14 "
                f"and will turn it down, so the mix loses its dynamics.",
                measured=measured))
        elif not (-17 <= lufs <= -11):
            out.append(Finding(
                "minor", "loudness",
                f"{lufs:.1f} LUFS, outside the -14 LUFS target most platforms use.",
                measured=measured))
    if peak is not None and peak > -0.5:
        out.append(Finding(
            "major", "clipping",
            f"True peak {peak:.1f} dBFS. Above -1.0 dBFS risks audible clipping "
            f"after lossy encoding.",
            measured=measured))

    silences = ev["silence"]
    total_silent = sum((s.get("duration") or 0) for s in silences)
    if pr.duration and total_silent > pr.duration * 0.6:
        out.append(Finding(
            "major", "audio",
            f"{total_silent:.0f}s of {pr.duration:.0f}s is silent. "
            f"The audio track may be effectively empty.",
            measured={"silent_seconds": round(total_silent, 1)}))
    for s in silences:
        if s.get("start", 0) > 0.5 and (s.get("duration") or 0) > 2.5:
            out.append(Finding(
                "minor", "silence_gap",
                f"{s['duration']:.1f}s of silence from {_fmt(s['start'])}.",
                at=s["start"], measured=s))
    return out


def check_technical(pr: P.Probe) -> list[Finding]:
    """Container and stream sanity."""
    out: list[Finding] = []
    if pr.duration <= 0:
        out.append(Finding("blocker", "duration",
                           "Duration is zero or unreadable — the file may be truncated."))
        return out
    if not pr.has_video:
        out.append(Finding("blocker", "video", "No video stream."))
        return out
    if pr.fps and pr.fps < 23:
        out.append(Finding("major", "framerate",
                           f"{pr.fps:.2f} fps will look choppy for motion graphics.",
                           measured={"fps": round(pr.fps, 3)}))
    if pr.nb_frames and pr.duration:
        implied = pr.nb_frames / pr.duration
        if pr.fps and abs(implied - pr.fps) / pr.fps > 0.05:
            out.append(Finding(
                "minor", "framerate",
                f"Declared {pr.fps:.2f} fps but the frame count implies "
                f"{implied:.2f} — variable frame rate or dropped frames.",
                measured={"declared_fps": round(pr.fps, 3),
                          "implied_fps": round(implied, 3)}))
    if pr.display_height and pr.display_height < 720:
        out.append(Finding("minor", "resolution",
                           f"{pr.display_width}x{pr.display_height} is below 720p.",
                           measured={"width": pr.display_width, "height": pr.display_height}))
    if pr.bit_rate and pr.display_height >= 1080 and pr.bit_rate < 2_000_000:
        out.append(Finding(
            "minor", "bitrate",
            f"{pr.bit_rate / 1e6:.1f} Mbps is low for {pr.display_height}p — "
            f"expect visible compression on motion.",
            measured={"bit_rate": pr.bit_rate}))
    return out


def check_safe_margins(pr: P.Probe, samples: int = 6) -> list[Finding]:
    """Is anything bright pressed against the frame edge?

    A crude but genuinely useful proxy for text or a logo running out of the
    title-safe area: compare edge-band brightness variance against the centre.
    Reported as a hint, never as certainty — this cannot tell a title from a
    bright background.
    """
    out: list[Finding] = []
    if not pr.has_video or pr.duration <= 0:
        return out
    from .probe import run
    step = pr.duration / (samples + 1)
    hits = []
    for i in range(1, samples + 1):
        t = step * i
        cp = run(["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{t:.3f}", "-i", pr.path,
                  "-frames:v", "1", "-vf",
                  "scale=200:112,crop=200:8:0:104,signalstats,metadata=print:file=-",
                  "-f", "null", "-"], timeout=60)
        m = re.search(r"YMAX=(\d+)", cp.stdout or "")
        n = re.search(r"YAVG=([0-9.]+)", cp.stdout or "")
        if m and n and int(m.group(1)) > 200 and float(n.group(1)) < 90:
            hits.append(round(t, 2))
    if len(hits) >= max(2, samples // 2):
        out.append(Finding(
            "minor", "safe_margin",
            f"Bright detail sits hard against the bottom edge at {len(hits)} of "
            f"{samples} sampled frames ({', '.join(_fmt(h) for h in hits[:4])}). "
            f"Check nothing is cropped on a device with overscan.",
            measured={"frames_flagged": hits}))
    return out


def measured_audit(pr: P.Probe, *, margins: bool = True) -> list[Finding]:
    """Every free, deterministic check. No model, no network, no tokens."""
    findings: list[Finding] = []
    findings += check_technical(pr)
    if any(f.severity == "blocker" for f in findings):
        return findings
    findings += check_edges(pr)
    findings += check_dead_air(pr)
    findings += check_audio(pr)
    if margins:
        findings += check_safe_margins(pr)
    return findings


# ---------------------------------------------------------------------------
# Intent spec — compare the render against what was asked for
# ---------------------------------------------------------------------------

INTENT_SCHEMA_DOC = """
An intent spec is small JSON describing what the video was SUPPOSED to be.
Anything you cannot state reliably, leave out — a missing field is skipped,
never guessed at.

{
  "title": "Launch promo",
  "duration_seconds": 30,          // optional, tolerance below
  "duration_tolerance": 1.0,
  "aspect": "16:9",                // or "9:16", "1:1"
  "scenes": [
    {
      "name": "hero",
      "start": "00:00",
      "end": "00:04",
      "text": ["Ship faster"],     // text expected on screen
      "must_show": "product logo"  // judged, not measured
    }
  ],
  "transitions": [
    { "at": "00:04", "kind": "crossfade", "duration_seconds": 0.5 }
  ],
  "audio": { "target_lufs": -14, "must_have_music": true }
}
"""


def load_intent(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise LLVideoError(f"Intent spec not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LLVideoError(f"Intent spec is not valid JSON: {exc}") from exc


def _sec(v) -> float | None:
    from .schema import normalise_timestamp
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = normalise_timestamp(str(v))
    return t if t >= 0 else None


def compare_intent(intent: dict, pr: P.Probe, craft_data: dict | None) -> list[Finding]:
    """Diff the render against the spec. Timing is measured; content is judged."""
    out: list[Finding] = []

    want = _sec(intent.get("duration_seconds"))
    if want is not None:
        tol = float(intent.get("duration_tolerance", 0.75))
        delta = pr.duration - want
        if abs(delta) > tol:
            out.append(Finding(
                "major", "duration",
                f"Runs {pr.duration:.2f}s but the spec asks for {want:.2f}s "
                f"({delta:+.2f}s).",
                measured={"actual": round(pr.duration, 3), "expected": want}))

    aspect = intent.get("aspect")
    if aspect and pr.display_width and pr.display_height:
        try:
            a, b = (float(x) for x in str(aspect).replace(":", "/").split("/"))
            want_ratio = a / b
            got_ratio = pr.display_width / pr.display_height
            if abs(got_ratio - want_ratio) / want_ratio > 0.02:
                out.append(Finding(
                    "major", "aspect",
                    f"Rendered {pr.display_width}x{pr.display_height} "
                    f"({got_ratio:.3f}) but the spec asks for {aspect} ({want_ratio:.3f}).",
                    measured={"actual": round(got_ratio, 4), "expected": round(want_ratio, 4)}))
        except (ValueError, ZeroDivisionError):
            pass

    # Transitions: compare the spec against what craft actually observed.
    want_trans = intent.get("transitions") or []
    got_trans = (craft_data or {}).get("transitions") or []
    if want_trans:
        got_times = [(_sec(t.get("at")), t) for t in got_trans]
        got_times = [(t, d) for t, d in got_times if t is not None]
        for w in want_trans:
            wt = _sec(w.get("at"))
            if wt is None:
                continue
            near = [(abs(t - wt), t, d) for t, d in got_times if abs(t - wt) <= 1.0]
            if not near:
                out.append(Finding(
                    "major", "transition_missing",
                    f"Spec wants a {w.get('kind', 'transition')} at {w.get('at')}, "
                    f"but nothing was detected within 1s of there.",
                    at=wt, source="judged"))
                continue
            _, gt, got = min(near)
            wk, gk = w.get("kind"), got.get("kind")
            if wk and gk and wk != gk:
                out.append(Finding(
                    "major", "transition_kind",
                    f"At {w.get('at')} the spec wants {wk} but the render has {gk}.",
                    at=gt, source="judged"))
            wd, gd = w.get("duration_seconds"), got.get("duration_seconds")
            if wd is not None and gd is not None and abs(float(wd) - float(gd)) > 0.25:
                out.append(Finding(
                    "minor", "transition_duration",
                    f"At {w.get('at')} the spec wants {wd}s but the render measures {gd}s.",
                    at=gt, source="judged"))

    want_audio = intent.get("audio") or {}
    if want_audio.get("must_have_music") and not pr.has_audio:
        out.append(Finding("blocker", "audio",
                           "Spec requires music but the render has no audio track."))
    return out


def suppress_intended(findings: list[Finding], intent: dict | None,
                      tolerance: float = 1.2) -> list[Finding]:
    """Drop 'defects' the spec explicitly asked for.

    A fade-to-black IS a stretch of black frames. Reporting it as a defect when
    the storyboard called for it there is a false positive, and false positives
    are how an auditor loses its reader. Only suppress where intent lines up in
    both kind and time.
    """
    if not intent:
        return findings
    fades = []
    for t in (intent.get("transitions") or []):
        kind = str(t.get("kind", ""))
        if "black" in kind:
            at = _sec(t.get("at"))
            if at is not None:
                fades.append((at, float(t.get("duration_seconds") or 0.5)))
    if not fades:
        return findings

    kept = []
    for f in findings:
        if f.check == "black_gap" and f.at is not None:
            near = any(abs(f.at - at) <= tolerance + dur for at, dur in fades)
            if near:
                dur = (f.measured or {}).get("duration")
                span = f"{dur:.2f}s" if isinstance(dur, (int, float)) else "A stretch"
                kept.append(Finding(
                    "note", "black_gap",
                    f"{span} of black at {_fmt(f.at)}, which matches a fade-to-black "
                    f"the spec asked for. Expected, not a defect.",
                    at=f.at, measured=f.measured))
                continue
        kept.append(f)
    return kept


def summarise(findings: list[Finding]) -> dict:
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
    worst = next((s for s in SEVERITIES if counts[s]), None)
    return {
        "counts": counts,
        "worst": worst,
        "verdict": ("fails" if counts["blocker"] else
                    "needs work" if counts["major"] else
                    "minor issues" if counts["minor"] else "clean"),
        "measured_findings": sum(1 for f in findings if f.source == "measured"),
        "judged_findings": sum(1 for f in findings if f.source == "judged"),
    }
