"""One chronological timeline instead of three parallel lists.

The index returns `scenes`, `speech` and `audio_events` as separate arrays.
Each is accurate, but nothing connects them — so "a close-up of the dashboard"
and "he says the range is wrong" sit in different lists, and the fact that they
happen at the same moment is left for the reader to reconstruct.

That co-occurrence IS the context. A shot means something different depending
on what is being said over it. This module interleaves everything onto one
track so a scene carries the words spoken during it.

It also lets a locally-transcribed track replace the model's speech. The video
model's audio timestamps drift by about a second, which is enough to attach a
line to the wrong shot at a fast cut; faster-whisper gives word-level timing
that does not drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .schema import normalise_timestamp


def _sec(v, default: float = -1.0) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    t = normalise_timestamp(v)
    return t if t >= 0 else default


def _fmt(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@dataclass
class Beat:
    """One stretch of screen time, with everything that happens during it."""
    start: float
    end: float
    description: str = ""
    shot: str = ""
    on_screen_text: list[str] = field(default_factory=list)
    illegible_text: list[str] = field(default_factory=list)
    speech: list[dict] = field(default_factory=list)
    audio: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    is_key_moment: bool = False
    key_moment_why: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def spoken(self) -> str:
        return " ".join(s.get("text", "").strip() for s in self.speech).strip()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = round(self.start, 2)
        d["end"] = round(self.end, 2)
        d["duration"] = round(self.duration, 2)
        d["start_label"] = _fmt(self.start)
        d["end_label"] = _fmt(self.end)
        return d


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def build(index: dict, duration: float = 0.0,
          transcript: dict | None = None) -> list[Beat]:
    """Interleave scenes, speech and audio onto one track.

    Speech is attached to the scene it overlaps MOST, not the first one it
    touches. A line straddling a cut belongs with the shot it was mostly spoken
    over, and picking the first overlap systematically biases it earlier.
    """
    scenes = index.get("scenes") or []
    beats: list[Beat] = []
    for sc in scenes:
        s = _sec(sc.get("start"), -1)
        e = _sec(sc.get("end"), -1)
        if s < 0:
            continue
        if e < 0 or e <= s:
            e = duration if duration > s else s + 1.0
        texts, illegible = [], []
        for item in (sc.get("on_screen_text") or []):
            if isinstance(item, dict):
                if item.get("legibility") == "illegible" or not item.get("text"):
                    where = item.get("where") or "on screen"
                    illegible.append(where)
                else:
                    texts.append(item["text"])
            elif item:
                texts.append(str(item))
        beats.append(Beat(
            start=s, end=e,
            description=sc.get("description", ""),
            shot=" ".join(x for x in (sc.get("camera"), sc.get("shot_size")) if x),
            on_screen_text=texts, illegible_text=illegible,
            actions=list(sc.get("actions") or []),
        ))
    beats.sort(key=lambda b: b.start)

    if not beats and duration > 0:
        beats = [Beat(start=0.0, end=duration, description="(no scenes reported)")]

    # Prefer a real transcript over the model's speech — its timings do not drift.
    lines = []
    if transcript and transcript.get("segments"):
        for seg in transcript["segments"]:
            lines.append({"start": float(seg.get("start", 0.0)),
                          "end": float(seg.get("end", 0.0)),
                          "text": (seg.get("text") or "").strip(),
                          "source": "transcript"})
    else:
        for sp in (index.get("speech") or []):
            s = _sec(sp.get("start"), -1)
            if s < 0:
                continue
            lines.append({"start": s, "end": _sec(sp.get("end"), s + 2.0),
                          "text": (sp.get("text") or "").strip(),
                          "speaker": sp.get("speaker"), "source": "index"})

    for line in lines:
        if not line["text"]:
            continue
        best, best_ov = None, 0.0
        for b in beats:
            ov = _overlap(line["start"], max(line["end"], line["start"] + 0.1),
                          b.start, b.end)
            if ov > best_ov:
                best, best_ov = b, ov
        if best is None:
            best = min(beats, key=lambda b: abs(b.start - line["start"]),
                       default=None) if beats else None
        if best is not None:
            best.speech.append(line)

    for ev in (index.get("audio_events") or []):
        s = _sec(ev.get("start"), -1)
        if s < 0:
            continue
        desc = ev.get("description", "")
        if not desc:
            continue
        e = _sec(ev.get("end"), s + 1.0)
        best, best_ov = None, 0.0
        for b in beats:
            ov = _overlap(s, max(e, s + 0.1), b.start, b.end)
            if ov > best_ov:
                best, best_ov = b, ov
        if best is None and beats:
            # No overlap at all — attach to the nearest beat rather than dropping it.
            best = min(beats, key=lambda b: abs(b.start - s))
        if best is not None:
            best.audio.append(desc)

    for km in (index.get("key_moments") or []):
        t = _sec(km.get("timestamp"), -1)
        if t < 0:
            continue
        for b in beats:
            if b.start <= t <= b.end:
                b.is_key_moment = True
                b.key_moment_why = km.get("why", "")
                break
    return beats


def render(beats: list[Beat], width: int = 92) -> str:
    """A single readable track. This is what an agent should reason over."""
    out: list[str] = []
    for b in beats:
        head = f"{_fmt(b.start)}-{_fmt(b.end)}  ({b.duration:.1f}s)"
        if b.is_key_moment:
            head += "   *KEY*"
        out.append(head)
        if b.shot:
            out.append(f"    shot   {b.shot}")
        if b.description:
            out.append(f"    see    {b.description}")
        for a in b.actions[:4]:
            out.append(f"    does   {a}")
        for t in b.on_screen_text:
            out.append(f'    text   "{t}"')
        for w in b.illegible_text:
            out.append(f"    text   (present but not legible: {w})")
        spoken = b.spoken
        if spoken:
            out.append(f'    says   "{spoken[:width * 2]}"')
        for a in b.audio[:3]:
            out.append(f"    hear   {a}")
        if b.key_moment_why:
            out.append(f"    why    {b.key_moment_why}")
        out.append("")
    return "\n".join(out).rstrip()


def coverage(beats: list[Beat], duration: float) -> dict:
    """How much of the runtime the timeline actually accounts for.

    A timeline with holes is a timeline that missed something, and saying so is
    more useful than presenting a partial account as complete.
    """
    # Always return every key. A URL input has no local probe, so duration is 0,
    # and an early return with a partial dict crashed the caller.
    base = {
        "covered_seconds": 0.0, "duration": round(duration, 2), "ratio": 0.0,
        "gaps": [],
        "with_speech": sum(1 for b in beats if b.speech),
        "with_text": sum(1 for b in beats if b.on_screen_text),
        "beats": len(beats),
        "duration_known": duration > 0,
    }
    if duration <= 0 or not beats:
        if beats:
            # Without a known runtime, report the span the beats themselves cover.
            base["covered_seconds"] = round(max(b.end for b in beats)
                                            - min(b.start for b in beats), 2)
        return base
    covered, gaps, cursor = 0.0, [], 0.0
    for b in sorted(beats, key=lambda x: x.start):
        if b.start > cursor + 0.75:
            gaps.append({"start": round(cursor, 2), "end": round(b.start, 2)})
        covered += max(0.0, min(b.end, duration) - max(b.start, cursor))
        cursor = max(cursor, b.end)
    if duration - cursor > 0.75:
        gaps.append({"start": round(cursor, 2), "end": round(duration, 2)})
    base.update({
        "covered_seconds": round(covered, 2),
        "ratio": round(covered / duration, 3),
        "gaps": gaps,
    })
    return base
