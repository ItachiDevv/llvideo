"""Orchestration — the tiers, the cost bands, and the upload cache.

T0  local ffmpeg signal extraction        zero tokens, always runs
T1  URL straight to the model             no download, no disk
T2  local file: transcode -> upload -> structured index
T3  clipped deep dive on a time window    frame-exact, hundreds of tokens
T4  frames into the agent's own context   the agent looks for itself
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import frames as F
from . import probe as P
from .errors import LLVideoError, TooLong
from .providers import pick_provider
from .providers.base import Result, Usage
from .schema import (ANSWER_PROMPT, ANSWER_SCHEMA, INDEX_PROMPT, VIDEO_INDEX_SCHEMA,
                     index_frame_times)

URL_RE = re.compile(r"^https?://", re.I)
YOUTUBE_RE = re.compile(r"(youtube\.com/|youtu\.be/)", re.I)

# 1M context. Stop well short so there is room to reason and answer.
CONTEXT_LIMIT = 1_048_576
ADMISSION_LIMIT = 700_000

CHEAP = 0.10      # proceed silently below this
ASK_ABOVE = 1.00  # stop and ask above this


def is_url(source: str) -> bool:
    return bool(URL_RE.match(source.strip()))


def is_youtube(source: str) -> bool:
    return bool(YOUTUBE_RE.search(source))


def scratch_dir() -> Path:
    """Session scratch. Everything here is temporary and gets cleaned up."""
    base = os.environ.get("LLVIDEO_SCRATCH") or os.path.join(tempfile.gettempdir(), "llvideo")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Upload cache — files expire after 48h, so never trust a stored uri blindly
# ---------------------------------------------------------------------------

class UploadCache:
    def __init__(self, path: Path | None = None):
        self.path = path or (scratch_dir() / "uploads.json")
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.data = {}

    @staticmethod
    def fingerprint(path: str) -> str:
        """Cheap content identity: size + mtime + head/tail bytes.

        Deliberately NOT a full content hash — hashing a 6 GB file to decide
        whether to reuse a cached answer defeats the point. mtime is included
        on purpose: without it, two videos with the same size and the same head
        and tail bytes but different middles would collide, and one file would
        be served the other's cached result.

        Known limit: mtime is truncated to whole seconds, so a copy made in the
        same second as its original shares a key. That is harmless — the bytes
        are identical — but it means the key is NOT a stable function of content
        alone, and no test should assert either outcome for a fresh copy.
        """
        p = Path(path)
        st = p.stat()
        h = hashlib.sha256()
        h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
        with p.open("rb") as fh:
            h.update(fh.read(65536))
            if st.st_size > 131072:
                fh.seek(-65536, os.SEEK_END)
                h.update(fh.read(65536))
        return h.hexdigest()[:24]

    def get(self, key: str) -> dict | None:
        entry = self.data.get(key)
        if not entry:
            return None
        if time.time() - entry.get("uploaded_at", 0) > 47 * 3600:
            self.data.pop(key, None)
            self.save()
            return None
        return entry

    def put(self, key: str, name: str, uri: str, mime: str) -> None:
        self.data[key] = {"name": name, "uri": uri, "mime": mime, "uploaded_at": time.time()}
        self.save()

    def drop(self, key: str) -> None:
        self.data.pop(key, None)
        self.save()

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass


class IndexCache:
    """Remember a completed index so follow-up questions are free.

    Every `ask` used to re-run the whole analysis: upload check, structured
    call, contact sheet. Asking a second question about the same video paid
    the full price again, which discourages exactly the back-and-forth that
    actually builds understanding.

    Keyed on (content fingerprint, sampling settings) so a different fps or an
    audio-stripped run does not collide with a full one. Bounded to 48h to
    match the provider-side file lifetime.
    """

    TTL_SECONDS = 47 * 3600

    def __init__(self, path: Path | None = None):
        self.path = path or (scratch_dir() / "indexes.json")
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.data = {}

    @staticmethod
    def key(source: str, fps_ratio: float, keep_audio: bool, model: str | None) -> str:
        if is_url(source):
            base = hashlib.sha256(source.encode()).hexdigest()[:24]
        else:
            try:
                base = UploadCache.fingerprint(source)
            except OSError:
                base = hashlib.sha256(source.encode()).hexdigest()[:24]
        return f"{base}:{fps_ratio}:{int(keep_audio)}:{model or 'default'}"

    def get(self, key: str) -> dict | None:
        e = self.data.get(key)
        if not e:
            return None
        if time.time() - e.get("at", 0) > self.TTL_SECONDS:
            self.data.pop(key, None)
            self.save()
            return None
        return e.get("index")

    def put(self, key: str, index: dict) -> None:
        self.data[key] = {"index": index, "at": time.time()}
        self.save()

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------

@dataclass
class Plan:
    source: str
    is_url: bool
    probe: P.Probe | None
    fps_ratio: float = 1.0
    keep_audio: bool = True
    needs_transcode: bool = False
    proxy_height: int = 720
    estimated_tokens: int = 0
    estimated_cost: float | None = None
    segments: list[tuple[float, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def band(self) -> str:
        c = self.estimated_cost
        if c is None:
            return "unknown"
        if c < CHEAP:
            return "cheap"
        if c <= ASK_ABOVE:
            return "notify"
        return "ask"

    def to_dict(self) -> dict:
        return {
            "source": self.source, "is_url": self.is_url,
            "fps_ratio": self.fps_ratio, "keep_audio": self.keep_audio,
            "needs_transcode": self.needs_transcode, "proxy_height": self.proxy_height,
            "estimated_tokens": self.estimated_tokens,
            "estimated_cost_usd": (round(self.estimated_cost, 4)
                                   if self.estimated_cost is not None else None),
            "band": self.band,
            "segments": [{"start": s, "end": e} for s, e in self.segments],
            "notes": self.notes,
            "probe": self.probe.to_dict() if self.probe else None,
        }


def plan(source: str, *, provider_name: str | None = None, fps: float | None = None,
         want_audio: bool | None = None, model: str | None = None) -> Plan:
    """Decide how to handle this video before spending anything."""
    url = is_url(source)
    if url:
        pl = Plan(source=source, is_url=True, probe=None)
        pl.notes.append("URL input — no download, no disk, no transcode.")
        if not is_youtube(source):
            pl.notes.append("Non-YouTube URL: the model may refuse it. YouTube is the "
                            "supported URL form; download other links first.")
        return pl

    pr = P.probe(source)
    pl = Plan(source=source, is_url=False, probe=pr)
    pl.notes.extend(pr.warnings)

    if not pr.has_video:
        pl.notes.append("Audio-only file — transcription path only, no visual analysis.")
    pl.keep_audio = pr.has_audio if want_audio is None else want_audio
    if not pr.has_audio:
        pl.notes.append("No audio track. Nothing to transcribe; no audio token cost.")

    # fps: default 1.0, drop it when the video is long enough to strain context
    if fps is not None:
        pl.fps_ratio = fps
    else:
        est = pr.estimate_tokens(1.0, pl.keep_audio)["total"]
        if est > ADMISSION_LIMIT:
            for candidate in (0.5, 0.2, 0.1):
                if pr.estimate_tokens(candidate, pl.keep_audio)["total"] <= ADMISSION_LIMIT:
                    pl.fps_ratio = candidate
                    pl.notes.append(
                        f"Video is {pr.duration / 60:.0f} min. Sampling at {candidate} fps "
                        f"to stay inside the context budget. Use a clipped deep dive for detail."
                    )
                    break
            else:
                pl.fps_ratio = 0.1

    est = pr.estimate_tokens(pl.fps_ratio, pl.keep_audio)
    pl.estimated_tokens = est["total"]

    if pl.estimated_tokens > ADMISSION_LIMIT:
        seg_seconds = ADMISSION_LIMIT / (
            P.VIDEO_TOKENS_PER_SEC * pl.fps_ratio
            + (P.AUDIO_TOKENS_PER_SEC if pl.keep_audio else 0))
        n = int(pr.duration // seg_seconds) + 1
        pl.segments = [(i * seg_seconds, min((i + 1) * seg_seconds, pr.duration))
                       for i in range(n)]
        pl.notes.append(
            f"Too long for one call even at {pl.fps_ratio} fps. Splitting into {n} segments; "
            f"the agent merges the per-segment indexes."
        )

    pl.proxy_height = 1080 if pr.is_screen_content else 720
    from .providers.gemini import MAX_FILE_BYTES
    if pr.size_bytes > MAX_FILE_BYTES:
        pl.needs_transcode = True
        pl.notes.append(
            f"Source is {pr.size_bytes / 1024**3:.2f} GB, over the 2 GB upload cap. "
            f"Transcoding is required before this file can be analysed at all."
        )
    elif pr.size_bytes > 25 * 1024 ** 2:
        pl.needs_transcode = True
        pl.notes.append("Transcoding to a small proxy — upload time dominates wall clock, "
                        "and token cost is unaffected by file size.")

    from .providers.gemini import PRICING, DEFAULT_MODEL
    rate = PRICING.get(model or DEFAULT_MODEL)
    if rate:
        pl.estimated_cost = pl.estimated_tokens * rate / 1e6
    return pl


# ---------------------------------------------------------------------------

@dataclass
class Analysis:
    index: dict
    plan: Plan
    usage: Usage
    provider: str
    model: str
    sheet: F.Sheet | None = None
    signals: dict = field(default_factory=dict)
    look_at: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "video_tokens": self.usage.video_tokens,
                "audio_tokens": self.usage.audio_tokens,
                "cost_usd": (round(self.usage.cost_usd, 4)
                             if self.usage.cost_usd is not None else None),
            },
            "signals": self.signals,
            "contact_sheet": (
                {"path": self.sheet.path, "frame_times": self.sheet.frame_times,
                 "approx_tokens": self.sheet.approx_tokens}
                if self.sheet else None),
            "look_at": self.look_at,
            "plan": self.plan.to_dict(),
        }


def local_signals(pr: P.Probe, src: str, *, deep: bool = False) -> dict:
    """T0. Facts, measured. These are exact where a model would only estimate."""
    out: dict = {"scene_cuts": [], "black": [], "freeze": [], "silence": []}
    if pr.has_video:
        out["scene_cuts"] = P.detect_scenes(src)
        out["scene_cuts_note"] = (
            "Scene detection misses the first cut structurally, and can miss interior cuts "
            "between visually similar shots. Frame selection unions these with a uniform floor."
        )
        if deep:
            out["black"] = P.detect_black(src)
            out["freeze"] = P.detect_freeze(src)
    if pr.has_audio and deep:
        out["silence"] = P.detect_silence(src)
    return out


def analyse(source: str, *, provider_name: str | None = None, model: str | None = None,
            fps: float | None = None, want_audio: bool | None = None,
            question: str | None = None, sheet_path: str | None = None,
            deep_signals: bool = False, keep_upload: bool = True,
            approved: bool = False, use_cache: bool = True) -> Analysis:
    """Run the full pipeline: plan, T0, upload, structured index, contact sheet."""
    pl = plan(source, provider_name=provider_name, fps=fps,
              want_audio=want_audio, model=model)

    if pl.band == "ask" and not approved:
        raise LLVideoError(
            f"This job is estimated at ${pl.estimated_cost:.2f} "
            f"({pl.estimated_tokens:,} tokens), above the ${ASK_ABOVE:.2f} auto-run limit.\n"
            f"Re-run with --yes to approve, or lower the cost with --fps 0.2 or --no-audio."
        )

    work = scratch_dir()
    temps: list[Path] = []
    signals: dict = {}
    upload_src = source

    try:
        if not pl.is_url:
            pr = pl.probe
            assert pr is not None
            signals = local_signals(pr, source, deep=deep_signals)
            if pl.needs_transcode:
                proxy_path = work / f"proxy_{UploadCache.fingerprint(source)}.mp4"
                if not proxy_path.exists():
                    px = F.transcode_proxy(pr, str(proxy_path), height=pl.proxy_height,
                                           keep_audio=pl.keep_audio)
                    signals["transcode"] = {
                        "from_mb": round(px.source_bytes / 1e6, 1),
                        "to_mb": round(px.size_bytes / 1e6, 2),
                        "ratio": round(px.ratio, 1),
                        "seconds": round(px.seconds, 1),
                    }
                temps.append(proxy_path)
                upload_src = str(proxy_path)

        needs_clip = pl.fps_ratio != 1.0 or bool(pl.segments)
        size = Path(upload_src).stat().st_size if not pl.is_url else 0
        prov = pick_provider(provider_name, needs_upload=not pl.is_url,
                             size_bytes=size, needs_clipping=needs_clip)

        prompt = (ANSWER_PROMPT + question) if question else INDEX_PROMPT
        schema = ANSWER_SCHEMA if question else VIDEO_INDEX_SCHEMA

        # A plain index is deterministic enough to reuse; a question is not,
        # so only the index path is cached.
        index_key = None
        if question is None and use_cache:
            idx_cache = IndexCache()
            index_key = IndexCache.key(source, pl.fps_ratio, pl.keep_audio, model)
            hit = idx_cache.get(index_key)
            if hit is not None:
                signals["index"] = "reused cached index"
                look = index_frame_times(hit, pl.probe.duration if pl.probe else 0.0)
                sheet = None
                if not pl.is_url and pl.probe and pl.probe.has_video:
                    times = look or P.select_frame_times(
                        pl.probe.duration, signals.get("scene_cuts"))
                    target = sheet_path or str(
                        work / f"sheet_{UploadCache.fingerprint(source)}.jpg")
                    tile = 768 if pl.probe.is_screen_content else 360
                    if pl.probe.is_screen_content and len(times) > 6:
                        step = len(times) / 6
                        times = [times[int(i * step)] for i in range(6)]
                    try:
                        sheet = F.contact_sheet(upload_src, times, target, tile_width=tile)
                    except LLVideoError:
                        sheet = None
                return Analysis(index=hit, plan=pl, usage=Usage(), provider="cache",
                                model=model or "", sheet=sheet, signals=signals,
                                look_at=look)

        file_uri = None
        cache_key = None
        if not pl.is_url and prov.name == "gemini":
            cache = UploadCache()
            cache_key = UploadCache.fingerprint(upload_src)
            hit = cache.get(cache_key)
            if hit and prov.file_alive(hit["name"]):
                file_uri = hit["uri"]
                signals["upload"] = "reused cached upload (still inside the 48h window)"
            else:
                if hit:
                    cache.drop(cache_key)
                info = prov.upload(upload_src)
                cache.put(cache_key, info["name"], info["uri"],
                          info.get("mimeType", "video/mp4"))
                file_uri = info["uri"]
                signals["upload"] = f"uploaded {Path(upload_src).name}"

        kwargs = dict(is_url=pl.is_url, model=model)
        if prov.name == "gemini":
            kwargs["file_uri"] = file_uri
            if pl.fps_ratio != 1.0:
                kwargs["fps"] = pl.fps_ratio

        if pl.segments and prov.name == "gemini":
            merged: dict = {"summary": "", "content_kind": "", "scenes": [], "speech": [],
                            "audio_events": [], "key_moments": [], "uncertainties": []}
            total = Usage()
            for (s, e) in pl.segments:
                r = prov.analyse(upload_src, prompt, schema, start=s, end=e, **kwargs)
                total = total.merge(r.usage)
                for k in ("scenes", "speech", "audio_events", "key_moments", "uncertainties"):
                    merged[k].extend(r.data.get(k) or [])
                merged["summary"] = (merged["summary"] + " " + r.data.get("summary", "")).strip()
                merged["content_kind"] = merged["content_kind"] or r.data.get("content_kind", "")
            result = Result(data=merged, usage=total, model=model or "", provider=prov.name)
        else:
            result = prov.analyse(upload_src, prompt, schema, **kwargs)

        duration = pl.probe.duration if pl.probe else 0.0
        look = index_frame_times(result.data, duration) if duration else []

        # Union with the uniform floor, for the same reason frame selection does:
        # a model's own idea of the interesting moments is not a coverage guarantee.
        # A sheet with two frames is a poor basis for looking at a video yourself.
        if duration:
            floor_interval = max(3.0, min(7.0, duration / 9))
            floor = P.select_frame_times(duration, signals.get("scene_cuts"),
                                         floor_interval=floor_interval)
            merged = list(look)
            for t in floor:
                if all(abs(t - u) > max(1.0, duration / 40) for u in merged):
                    merged.append(t)
            look = sorted(merged)[:16]

        sheet = None
        if not pl.is_url and pl.probe and pl.probe.has_video:
            times = look or P.select_frame_times(duration, signals.get("scene_cuts"))
            target = sheet_path or str(work / f"sheet_{UploadCache.fingerprint(source)}.jpg")
            # Screen recordings exist to be READ. A 360px tile renders terminal
            # and editor text illegible, which defeats the point of the sheet —
            # the agent cannot check the model's reading if it cannot read.
            # So for screen content: bigger tiles, and fewer of them to pay for it.
            if pl.probe.is_screen_content:
                tile = 768
                if len(times) > 6:
                    step = len(times) / 6
                    times = [times[int(i * step)] for i in range(6)]
            else:
                tile = 360
            try:
                sheet = F.contact_sheet(upload_src, times, target, tile_width=tile)
            except LLVideoError:
                sheet = None

        if index_key is not None:
            IndexCache().put(index_key, result.data)

        return Analysis(index=result.data, plan=pl, usage=result.usage,
                        provider=result.provider, model=result.model,
                        sheet=sheet, signals=signals, look_at=look)
    finally:
        if not keep_upload:
            for t in temps:
                try:
                    t.unlink(missing_ok=True)
                except OSError:
                    pass


def cleanup(delete_uploads: bool = False) -> dict:
    """Remove scratch artefacts. Disk usage must never grow with video length."""
    work = scratch_dir()
    removed = {"files": 0, "bytes": 0, "remote": 0}
    cache = UploadCache()
    if delete_uploads:
        from .providers.gemini import GeminiProvider
        g = GeminiProvider()
        if g.available():
            for key, entry in list(cache.data.items()):
                if g.delete_file(entry["name"]):
                    removed["remote"] += 1
                cache.drop(key)
    for p in work.glob("*"):
        if p.name == "uploads.json":
            continue
        try:
            if p.is_file():
                removed["bytes"] += p.stat().st_size
                p.unlink()
                removed["files"] += 1
            else:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    return removed
