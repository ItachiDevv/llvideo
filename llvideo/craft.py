"""Craft analysis — how a video is shot and cut.

Different problem from "what happens in it", and it needs a different sampling
strategy. A hard cut occupies a single frame. At 1 fps you see the shot before
and the shot after, and never the transition itself — so you cannot tell a cut
from a dissolve from a whip pan, which is the entire question.

So this module works in two passes:

  1. Cheap, local, zero tokens: find every CANDIDATE transition point from
     per-frame scene scores. This casts a much wider net than `detect_scenes`,
     because a soft dissolve never spikes — it produces a low plateau instead.
  2. Expensive only where it matters: analyse a short window around each
     candidate at high fps, so the model sees the transition happen.

The whole video also gets one pass for shots, pacing, colour and lighting.
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .errors import LLVideoError
from .probe import Probe, _ffmpeg_filter_log

_TIME = r"[-+]?\d+\.?\d*"


@dataclass
class Candidate:
    time: float
    peak: float
    width: float          # how long the disturbance lasts, in seconds
    kind_hint: str        # "cut-like" or "blend-like", from the shape alone

    def to_dict(self) -> dict:
        return {"time": round(self.time, 3), "peak": round(self.peak, 4),
                "width": round(self.width, 3), "kind_hint": self.kind_hint}


def _scores_cache_path(src: str):
    """Cache key is content identity, not the path — a re-encode invalidates it."""
    try:
        from .analyze import UploadCache, scratch_dir
        return scratch_dir() / f"scores_{UploadCache.fingerprint(src)}.json"
    except Exception:
        return None


def frame_scores(src: str, max_frames: int = 200000) -> list[tuple[float, float]]:
    """Per-frame scene score for the whole video. Zero tokens, one decode pass.

    Note `metadata=print:file=-` writes to STDOUT, not stderr like ffmpeg's
    other filter logging, and the key is `lavfi.scene_score`. Output looks like:

        frame:1    pts:512     pts_time:0.0333333
        lavfi.scene_score=0.024003
    """
    from .probe import run, _require
    _require("ffmpeg")

    # This decodes every frame of the file, so it is the single most expensive
    # local operation here. `craft` and `audit --craft` both want it for the
    # same file, and a second question about the same video wants it again.
    # Cache on content identity so it is paid for exactly once.
    cache_path = _scores_cache_path(src)
    if cache_path is not None and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return [(float(t), float(v)) for t, v in cached]
        except (OSError, ValueError, TypeError):
            pass

    cp = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", src,
              "-vf", r"select='gte(scene\,0)',metadata=print:file=-",
              "-f", "null", "-"], timeout=1800)
    out: list[tuple[float, float]] = []
    t = None
    for line in (cp.stdout or "").splitlines():
        m = re.search(rf"pts_time:({_TIME})", line)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(rf"scene_score=({_TIME})", line)
        if m and t is not None:
            out.append((t, float(m.group(1))))
            t = None
        if len(out) >= max_frames:
            break

    if cache_path is not None and out:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(out), encoding="utf-8")
        except OSError:
            pass
    return out


def find_candidates(scores: list[tuple[float, float]], *,
                    spike: float = 0.10, plateau: float = 0.012,
                    min_gap: float = 0.8) -> list[Candidate]:
    """Find transition candidates from the score curve's SHAPE.

    Two signatures, and the difference is the point:

      A hard cut is one tall, narrow spike — a single frame disagrees violently
      with its predecessor.

      A dissolve, fade or wipe never spikes. Each frame differs only slightly
      from the last, so it shows up as a LOW, WIDE plateau that a spike-only
      detector like `detect_scenes` misses entirely.

    Detecting both is what makes transition classification possible at all.
    """
    if not scores:
        return []
    vals = [s for _, s in scores]
    try:
        median = statistics.median(vals)
    except statistics.StatisticsError:
        median = 0.0
    noise = max(median * 3.0, plateau)

    cands: list[Candidate] = []
    i, n = 0, len(scores)
    while i < n:
        t, s = scores[i]
        # The first decoded frame has no predecessor, so its score is an artifact.
        if s < noise or t < 0.1:
            i += 1
            continue
        # walk the whole disturbance
        j = i
        peak, peak_t = s, t
        while j + 1 < n and scores[j + 1][1] >= noise:
            j += 1
            if scores[j][1] > peak:
                peak, peak_t = scores[j][1], scores[j][0]
        width = scores[j][0] - scores[i][0]
        # a cut is tall and narrow; a blend is low and wide
        hint = "cut-like" if (peak >= spike and width <= 0.25) else "blend-like"
        cands.append(Candidate(time=peak_t, peak=peak, width=width, kind_hint=hint))
        i = j + 1

    # merge anything closer together than min_gap
    merged: list[Candidate] = []
    for c in cands:
        if merged and c.time - merged[-1].time < min_gap:
            if c.peak > merged[-1].peak:
                merged[-1] = Candidate(c.time, c.peak,
                                       max(c.width, merged[-1].width), merged[-1].kind_hint)
            continue
        merged.append(c)
    return merged


def shot_stats(candidates: list[Candidate], duration: float) -> dict:
    """Pacing statistics from the candidate boundaries. Free, no model needed."""
    bounds = [0.0] + [c.time for c in candidates] + [duration]
    lengths = [round(b - a, 3) for a, b in zip(bounds, bounds[1:]) if b > a]
    if not lengths:
        return {"shots": 0, "lengths": []}
    out = {
        "shots": len(lengths),
        "lengths": lengths,
        "mean_seconds": round(statistics.fmean(lengths), 2),
        "median_seconds": round(statistics.median(lengths), 2),
        "shortest": round(min(lengths), 2),
        "longest": round(max(lengths), 2),
        "cuts_per_minute": round(len(candidates) / (duration / 60), 1) if duration else 0,
    }
    if len(lengths) > 1:
        out["stdev_seconds"] = round(statistics.stdev(lengths), 2)
        out["rhythm"] = ("even" if out["stdev_seconds"] < out["mean_seconds"] * 0.4
                         else "varied")
    return out


def windows_for(candidates: list[Candidate], duration: float,
                pad: float = 1.0, limit: int = 12) -> list[tuple[float, float]]:
    """Tight windows around each candidate, for high-fps inspection."""
    # Rank by prominence, not raw peak. A 1s crossfade peaks around 0.02 while a
    # hard cut hits 0.7 — sorting on peak alone would discard every soft blend,
    # which are the transitions hardest to classify and most worth looking at.
    def prominence(c: Candidate) -> float:
        return c.peak * (1.0 + c.width * 4.0)

    ranked = sorted(candidates, key=prominence, reverse=True)[:limit]
    wins = []
    for c in sorted(ranked, key=lambda c: c.time):
        half = max(pad, c.width)
        wins.append((max(0.0, c.time - half), min(duration, c.time + half)))
    # merge overlaps so we do not pay twice for the same seconds
    merged: list[tuple[float, float]] = []
    for w in wins:
        if merged and w[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], w[1]))
        else:
            merged.append(w)
    return merged


def describe_candidates(candidates: list[Candidate]) -> str:
    """A hint block for the model — measured, so it does not have to guess."""
    if not candidates:
        return ("Local per-frame analysis found no transition candidates. This is "
                "probably one continuous shot. Confirm that rather than inventing cuts.")
    lines = ["Local per-frame scene-score analysis found these candidate transition "
             "points (measured, not guessed). Classify each one from what you SEE; "
             "the shape hint is only a starting point:"]
    for c in candidates:
        lines.append(
            f"  - {c.time:6.2f}s  peak={c.peak:.3f}  spread={c.width:.2f}s  -> {c.kind_hint}"
        )
    lines.append("A narrow spike means a hard cut. A low wide spread means a blend "
                 "(crossfade, fade, wipe) or fast camera movement — tell those apart "
                 "by looking at whether the frames contain two mixed images.")
    return "\n".join(lines)


def luma_profile(src: str, start: float, end: float, samples: int = 24) -> dict:
    """Mean brightness across a window. Measured, so the model need not guess.

    This is what separates a fade-to-black from a crossfade, and a model will get
    it wrong from sparse frames. A dissolve blends A into B and the brightness
    stays roughly between the two. A fade-to-black passes through actual black,
    which shows up as an unmistakable dip toward zero.

    Measured on a 0.6s xfade fadeblack: luma ran 52 -> 3.8 -> 15 -> 42 -> 77.
    The model called it a crossfade. The dip to 3.8 is not ambiguous.
    """
    from .probe import run, _require
    _require("ffmpeg")
    span = max(end - start, 0.01)

    # ONE decode pass over the window, not one ffmpeg process per sample.
    # The per-sample version spawned 24 processes per window — measured at 32.7s
    # for four windows, which was 80% of the whole craft command's runtime.
    # Seeking and re-decoding 96 times to read 96 numbers is the slow way to do
    # something ffmpeg will stream in a single pass.
    cp = run(["ffmpeg", "-hide_banner", "-nostdin",
              "-ss", f"{max(start, 0):.3f}", "-t", f"{span:.3f}", "-i", src,
              "-vf", "scale=64:36,signalstats,metadata=print:file=-",
              "-f", "null", "-"], timeout=180)

    raw: list[tuple[float, float]] = []
    t_cur = None
    for line in (cp.stdout or "").splitlines():
        m = re.search(rf"pts_time:({_TIME})", line)
        if m:
            t_cur = float(m.group(1))
            continue
        m = re.search(r"YAVG=([0-9.]+)", line)
        if m and t_cur is not None:
            raw.append((round(start + t_cur, 3), round(float(m.group(1)), 2)))
            t_cur = None
    if not raw:
        return {}

    # Thin to roughly `samples` evenly spaced points so the prompt stays small.
    if len(raw) > samples:
        stride = len(raw) / samples
        points = [raw[min(int(i * stride), len(raw) - 1)] for i in range(samples)]
    else:
        points = raw

    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    lo_t = points[vals.index(lo)][0]
    hi_t = points[vals.index(hi)][0]
    edges = [vals[0], vals[-1]]
    verdict = "no clear brightness event"
    # A dip well below BOTH endpoints means the picture passed through black.
    # Threshold is 20, not 0: limited-range video encodes black as luma 16, so
    # a full-range threshold would never fire on a normal H.264 export.
    if lo <= 20 and lo < min(edges) * 0.45:
        verdict = f"passes through BLACK at {lo_t}s (luma {lo}) - this is a fade through black, not a crossfade"
    elif hi >= 232 and hi > max(edges) * 1.4:
        verdict = f"passes through WHITE at {hi_t}s (luma {hi}) - this is a fade through white"
    elif abs(vals[0] - vals[-1]) > 40:
        verdict = "brightness steps between the two shots without passing through black or white"
    return {"points": points, "min": lo, "min_at": lo_t, "max": hi, "max_at": hi_t,
            "verdict": verdict}


def describe_luma(profile: dict) -> str:
    if not profile:
        return ""
    pts = ", ".join(f"{t}s={v}" for t, v in profile["points"][::3])
    return ("\n\nMEASURED brightness through this window (mean luma, 0-255, from ffmpeg — "
            f"this is fact, not inference):\n  {pts}\n  {profile['verdict']}\n"
            "Use this to decide between a fade-through-black, a fade-through-white and a "
            "direct crossfade. Your eyes on sparse frames can miss a brief black frame; "
            "this measurement cannot.")
