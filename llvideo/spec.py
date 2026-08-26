"""Extract an intent spec from a video project.

The point: nobody should hand-write the spec. The project already knows what it
meant to build — HyperFrames keeps scene timing in `data-*` attributes on the
composition DOM, brag builds a HyperFrames project underneath, and Remotion
declares it in `<Sequence from= durationInFrames=>`. All three are statically
parseable without executing anything.

This matters because of a real gap. HyperFrames `check` validates the seeked
composition timeline in headless Chrome. brag's delivery gate stops at
pre-render snapshots. Remotion has no equivalent gate at all. None of them look
at the exported MP4 — so an encoder bug, a stale render, or dropped frames at
export ships unnoticed. Extracting intent here and diffing it against the real
file closes that gap.

Everything extracted carries a `source` and a `confidence`, because a caption in
STORYBOARD.md is not the same kind of fact as a `data-start` attribute.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .errors import LLVideoError

# HyperFrames transition names -> the vocabulary llvideo's craft analysis uses.
TRANSITION_ALIASES = {
    "cut": "hard_cut", "hard cut": "hard_cut", "hardcut": "hard_cut",
    "crossfade": "crossfade", "cross fade": "crossfade", "dissolve": "crossfade",
    "fade": "crossfade", "fadein": "fade_from_black", "fade in": "fade_from_black",
    "fadeout": "fade_to_black", "fade out": "fade_to_black",
    "fade to black": "fade_to_black", "fadeblack": "fade_to_black",
    "fade from black": "fade_from_black",
    "wipe": "wipe", "clockwipe": "wipe", "clock wipe": "wipe", "iris": "wipe",
    "slide": "slide", "push": "slide", "pushslide": "slide", "push slide": "slide",
    "flip": "morph", "3dflip": "morph", "glitch": "glitch",
    "whip": "whip_pan", "whippan": "whip_pan", "whip pan": "whip_pan",
    "zoom": "zoom_transition", "none": "none",
}


def normalise_transition(name: str | None) -> str | None:
    if not name:
        return None
    key = re.sub(r"[^a-z ]", "", str(name).strip().lower())
    return TRANSITION_ALIASES.get(key, TRANSITION_ALIASES.get(key.replace(" ", ""), key))


@dataclass
class Scene:
    id: str
    start: float
    end: float
    text: list[str] = field(default_factory=list)
    transition_out: dict | None = None
    source: str = ""
    confidence: str = "high"      # high = parsed attribute, low = prose caption

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = round(self.start, 3)
        d["end"] = round(self.end, 3)
        return d


@dataclass
class Spec:
    kind: str                      # hyperframes | remotion | brag
    project: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration_seconds: float | None = None
    scenes: list[Scene] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_intent(self) -> dict:
        """The shape `llvideo audit --spec` consumes."""
        out: dict = {
            "title": Path(self.project).name,
            "_generated_from": self.kind,
            "_project": self.project,
        }
        if self.duration_seconds:
            out["duration_seconds"] = round(self.duration_seconds, 3)
            out["duration_tolerance"] = 0.5
        if self.width and self.height:
            from math import gcd
            g = gcd(self.width, self.height) or 1
            out["aspect"] = f"{self.width // g}:{self.height // g}"
        if self.scenes:
            out["scenes"] = [
                {k: v for k, v in s.to_dict().items() if v not in (None, [], "")}
                for s in self.scenes
            ]
        trans = []
        for s in self.scenes:
            if s.transition_out and s.transition_out.get("kind"):
                trans.append({
                    "at": _mmss(s.end),
                    "kind": s.transition_out["kind"],
                    "duration_seconds": s.transition_out.get("duration_seconds", 0),
                })
        if trans:
            out["transitions"] = trans
        if self.notes:
            out["_notes"] = self.notes
        return out


def _mmss(sec: float) -> str:
    sec = max(0.0, sec)
    m, s = divmod(sec, 60)
    return f"{int(m):02d}:{s:05.2f}".replace(".00", "") if s % 1 else f"{int(m):02d}:{int(s):02d}"


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).strip().rstrip("s"))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# HyperFrames
# ---------------------------------------------------------------------------

_CLIP_RE = re.compile(
    r"<([a-zA-Z][\w-]*)\b([^>]*\bclass\s*=\s*[\"'][^\"']*\bclip\b[^\"']*[\"'][^>]*)>",
    re.I | re.S)
_ATTR_RE = re.compile(r"""\b(data-[\w-]+|id)\s*=\s*["']([^"']*)["']""", re.I)
_ROOT_RE = re.compile(r"""<[^>]*\bid\s*=\s*["']root["'][^>]*>""", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _clip_text(html: str, tag: str, start_idx: int) -> list[str]:
    """Visible text inside one clip element. Shallow but adequate."""
    seg = html[start_idx:start_idx + 4000]
    close = seg.lower().find(f"</{tag.lower()}>")
    inner = seg[:close] if close != -1 else seg[:600]
    txt = _TAG_RE.sub(" ", inner)
    txt = re.sub(r"\s+", " ", txt).strip()
    out = [t.strip() for t in re.split(r"\s{2,}|\n", txt) if len(t.strip()) > 1]
    return out[:4]


def from_hyperframes(project_dir: str) -> Spec:
    root = Path(project_dir)
    index = root / "index.html"
    if not index.exists():
        raise LLVideoError(f"No index.html in {root} — is this a HyperFrames project?")
    html = index.read_text(encoding="utf-8", errors="ignore")

    spec = Spec(kind="hyperframes", project=str(root))

    cfg = root / "hyperframes.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            spec.width = int(data.get("width") or 0)
            spec.height = int(data.get("height") or 0)
            spec.fps = float(data.get("fps") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    m = _ROOT_RE.search(html)
    if m:
        attrs = dict((k.lower(), v) for k, v in _ATTR_RE.findall(m.group(0)))
        spec.duration_seconds = _num(attrs.get("data-duration"), 0.0) or None
        spec.width = spec.width or int(_num(attrs.get("data-width"), 0))
        spec.height = spec.height or int(_num(attrs.get("data-height"), 0))

    # Every timed element carries data-start / data-duration. This is the
    # load-bearing extraction — exact, static, no execution needed.
    for i, cm in enumerate(_CLIP_RE.finditer(html)):
        tag, attr_str = cm.group(1), cm.group(2)
        attrs = dict((k.lower(), v) for k, v in _ATTR_RE.findall(attr_str))
        if "data-start" not in attrs:
            continue
        start = _num(attrs.get("data-start"), 0.0)
        dur = _num(attrs.get("data-duration"), 0.0)
        if dur <= 0:
            continue
        spec.scenes.append(Scene(
            id=attrs.get("id") or f"clip-{i + 1}",
            start=start, end=start + dur,
            text=_clip_text(html, tag, cm.end()),
            source="hyperframes:data-attributes", confidence="high"))

    spec.scenes.sort(key=lambda s: s.start)

    # STORYBOARD.md carries authored transition intent, which the DOM does not.
    sb = root / "STORYBOARD.md"
    if sb.exists():
        _merge_storyboard(spec, sb.read_text(encoding="utf-8", errors="ignore"))
    else:
        spec.notes.append(
            "No STORYBOARD.md, so no authored transition types. Scene timing comes from "
            "data-* attributes and is exact; transitions were not declared anywhere "
            "machine-readable, so they are not checked.")

    if not spec.scenes:
        spec.notes.append(
            "No elements with class=\"clip\" and data-start found. Either the composition "
            "uses a different structure, or timing is set in script rather than markup.")
    return spec


_SB_FRAME = re.compile(r"^\s*#{2,4}\s*(?:Frame|Scene)\s*\d*\s*[-:—]?\s*(.*)$", re.I | re.M)
_SB_FIELD = re.compile(r"^\s*[-*]?\s*`?(\w[\w_]*)`?\s*[:=]\s*(.+?)\s*$", re.M)


def _merge_storyboard(spec: Spec, text: str) -> None:
    """STORYBOARD.md declares transition_in per frame. Map it onto the scenes."""
    blocks = []
    marks = list(_SB_FRAME.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        blocks.append((m.group(1).strip(), text[m.end():end]))
    if not blocks:
        return

    found = 0
    for i, (title, body) in enumerate(blocks):
        fields = {k.lower(): v for k, v in _SB_FIELD.findall(body)}
        tin = normalise_transition(fields.get("transition_in"))
        if not tin:
            continue
        found += 1
        # transition_in on frame N describes the boundary INTO N, which is the
        # transition OUT of frame N-1.
        idx = i - 1
        if 0 <= idx < len(spec.scenes):
            dur = _num(fields.get("transition_duration"), 0.0)
            if tin == "hard_cut":
                dur = 0.0
            spec.scenes[idx].transition_out = {
                "kind": tin,
                "duration_seconds": dur,
                "source": "STORYBOARD.md:transition_in",
            }
    if found:
        spec.notes.append(
            f"Transition types come from STORYBOARD.md ({found} declared). "
            f"These are authored intent, not measured from the composition.")


# ---------------------------------------------------------------------------
# Remotion
# ---------------------------------------------------------------------------

_SEQ_RE = re.compile(
    r"<Sequence\b[^>]*?\bfrom\s*=\s*\{([^}]+)\}[^>]*?\bdurationInFrames\s*=\s*\{([^}]+)\}",
    re.S)
_COMP_RE = re.compile(
    r"<Composition\b[^>]*?\bid\s*=\s*[\"']([^\"']+)[\"'][^>]*?>", re.S)
_PROP_RE = re.compile(r"\b(fps|width|height|durationInFrames)\s*=\s*\{?\s*([0-9.]+)", re.I)
_TRANS_RE = re.compile(
    r"presentation\s*=\s*\{\s*(fade|slide|wipe|flip|clockWipe|none)\s*\(", re.I)
_TIMING_RE = re.compile(r"linearTiming\s*\(\s*\{\s*durationInFrames\s*:\s*([0-9]+)")


def _eval_frames(expr: str, fps: float) -> float | None:
    """Handle the literal and `N * fps` forms this house style actually uses."""
    e = expr.strip()
    if re.fullmatch(r"[0-9.]+", e):
        return float(e)
    m = re.fullmatch(r"([0-9.]+)\s*\*\s*fps", e)
    if m and fps:
        return float(m.group(1)) * fps
    m = re.fullmatch(r"fps\s*\*\s*([0-9.]+)", e)
    if m and fps:
        return float(m.group(1)) * fps
    try:
        if re.fullmatch(r"[0-9.\s+\-*/()]+", e):
            return float(eval(e, {"__builtins__": {}}, {}))  # digits and operators only
    except Exception:
        return None
    return None


def from_remotion(project_dir: str) -> Spec:
    root = Path(project_dir)
    if not root.exists():
        raise LLVideoError(f"No such directory: {root}")
    sources = [p for p in root.rglob("*.tsx") if "node_modules" not in p.parts]
    if not sources:
        raise LLVideoError(f"No .tsx files under {root} — is this a Remotion project?")

    spec = Spec(kind="remotion", project=str(root))
    for p in sources:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if "<Composition" in txt:
            props = {k.lower(): float(v) for k, v in _PROP_RE.findall(txt)}
            spec.fps = spec.fps or props.get("fps", 0.0)
            spec.width = spec.width or int(props.get("width", 0))
            spec.height = spec.height or int(props.get("height", 0))
            if props.get("durationinframes") and spec.fps:
                spec.duration_seconds = props["durationinframes"] / spec.fps
    fps = spec.fps or 30.0
    if not spec.fps:
        spec.notes.append("fps not found in any <Composition>; assumed 30.")

    for p in sources:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for i, m in enumerate(_SEQ_RE.finditer(txt)):
            start_f = _eval_frames(m.group(1), fps)
            dur_f = _eval_frames(m.group(2), fps)
            if start_f is None or dur_f is None:
                spec.notes.append(
                    f"{p.name}: a <Sequence> uses a computed value "
                    f"(from={m.group(1).strip()[:30]}) that cannot be read statically. "
                    f"Skipped.")
                continue
            spec.scenes.append(Scene(
                id=f"{p.stem}-seq{i + 1}",
                start=start_f / fps, end=(start_f + dur_f) / fps,
                source=f"remotion:{p.name}", confidence="high"))

        kinds = [k.lower() for k in _TRANS_RE.findall(txt)]
        durs = [int(d) for d in _TIMING_RE.findall(txt)]
        if kinds:
            spec.notes.append(
                f"{p.name}: {len(kinds)} <TransitionSeries.Transition> found "
                f"({', '.join(sorted(set(kinds)))}). Transitions overlap scenes, so the "
                f"total duration is shorter than the sum of scene durations.")
            if len(durs) < len(kinds):
                spec.notes.append(
                    f"{p.name}: some transitions use springTiming, whose settled duration "
                    f"cannot be read from source. Those durations are not checked.")

    spec.scenes.sort(key=lambda s: s.start)
    if not spec.scenes:
        spec.notes.append("No <Sequence from= durationInFrames=> found.")
    return spec


# ---------------------------------------------------------------------------

def detect(path: str) -> str:
    """What kind of project is this?"""
    p = Path(path)
    if not p.exists():
        raise LLVideoError(f"No such path: {p}")
    if p.is_file():
        p = p.parent
    # brag builds a HyperFrames project underneath itself
    for cand in (p / "composition", p / "brag-output" / "composition"):
        if (cand / "index.html").exists():
            return "brag"
    if (p / "index.html").exists() or (p / "hyperframes.json").exists():
        return "hyperframes"
    if (p / "remotion.config.ts").exists() or (p / "remotion.config.js").exists():
        return "remotion"
    if any(x.name in ("Root.tsx", "Video.tsx") for x in p.rglob("*.tsx")
           if "node_modules" not in x.parts):
        return "remotion"
    raise LLVideoError(
        f"Could not tell what kind of project {p} is.\n"
        f"Expected a HyperFrames project (index.html + hyperframes.json), a brag output "
        f"folder (composition/index.html), or a Remotion project (Root.tsx / "
        f"remotion.config.ts). Pass --kind to force one."
    )


def extract(path: str, kind: str | None = None) -> Spec:
    kind = kind or detect(path)
    p = Path(path)
    if p.is_file():
        p = p.parent
    if kind == "brag":
        for cand in (p / "composition", p / "brag-output" / "composition"):
            if (cand / "index.html").exists():
                spec = from_hyperframes(str(cand))
                spec.kind = "brag"
                spec.notes.insert(0, f"brag output; composition read from {cand}")
                _merge_brag_plan(spec, p)
                return spec
        raise LLVideoError(f"No composition/index.html under {p}")
    if kind == "hyperframes":
        return from_hyperframes(str(p))
    if kind == "remotion":
        return from_remotion(str(p))
    raise LLVideoError(f"Unknown project kind '{kind}'. Use hyperframes, remotion or brag.")


_BRAG_SCENE = re.compile(
    r"^\s*#{2,4}\s*Scene\s*(\d+)\s*[-—]\s*(.+?)\s*[-—]\s*([0-9.]+)\s*s", re.I | re.M)


def _merge_brag_plan(spec: Spec, root: Path) -> None:
    """brag-plan.md names scenes and durations. Useful as a cross-check only —
    the built composition is the truth, the plan is what was asked for."""
    for cand in (root / "brag-plan.md", root / "brag-output" / "brag-plan.md"):
        if not cand.exists():
            continue
        rows = _BRAG_SCENE.findall(cand.read_text(encoding="utf-8", errors="ignore"))
        if not rows:
            continue
        planned = sum(float(d) for _, _, d in rows)
        spec.notes.append(
            f"brag-plan.md declares {len(rows)} scenes totalling {planned:.1f}s. "
            f"The built composition has {len(spec.scenes)} clips"
            + (f" totalling {spec.duration_seconds:.1f}s." if spec.duration_seconds else ".")
            + " A mismatch means the composition drifted from the plan.")
        return
