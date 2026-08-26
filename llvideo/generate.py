"""Generate short supplemental clips with Grok Imagine, then audit them.

Scope is deliberately narrow: small clips that slot into a timeline built
elsewhere. Establishing shots, b-roll, texture, a logo sting. Not whole films.

Why Grok rather than Veo or fal: the key is already on this machine, generation
is 1-15 seconds in one-second steps (Veo is fixed 4/6/8), and it costs about the
same. Nothing here stops another backend being added — the shape is the same
submit-and-poll everywhere.

The important part is not the generation. It is that every clip comes back
through `llvideo audit`, because a generated clip you have not watched is
exactly the thing this whole tool exists to stop shipping. Grok's own default
is 848x480 and around -21 LUFS; the audit says so, every time.

Verified live: an 8s 480p clip returned in ~6s for $0.40; a 5s 720p clip took
50s for $0.35. Both numbers came from the API's own usage field.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .errors import LLVideoError, MissingDependency, ProviderError
from .providers.base import read_key

BASE = "https://api.x.ai/v1"
SUBMIT = f"{BASE}/videos/generations"
POLL = f"{BASE}/videos"

VIDEO_MODEL = "grok-imagine-video"
VIDEO_MODEL_AUDIO = "grok-imagine-video-1.5"
IMAGE_MODEL = "grok-imagine-image-2.0"

# Straight from the API's own 422 responses — it names its accepted variants.
RESOLUTIONS = ("480p", "720p", "1080p")
ASPECTS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3")
MIN_SECONDS, MAX_SECONDS = 1, 15

# usage.cost_in_usd_ticks is in units of 1e-10 USD. Cross-checked two ways:
# grok-imagine-image reports 200,000,000 ticks (= $0.02, the published image
# price), and an 8s 480p video reported 4,000,000,000 (= $0.40).
USD_PER_TICK = 1e-10

# Measured, not documented — the API returns the real cost per job and these are
# the rates those jobs implied. Resolution changes the price, so a flat rate
# would under-quote 720p by 40%.
#   8s at 480p -> $0.40  = $0.050/s
#   5s at 720p -> $0.35  = $0.070/s
# 1080p was not measured; it is extrapolated and marked as such by estimate().
USD_PER_SECOND = {"480p": 0.05, "720p": 0.07, "1080p": 0.11}


def estimate(seconds: float, resolution: str = "720p") -> tuple[float, bool]:
    """Return (dollars, measured). `measured` is False where the rate is a guess."""
    rate = USD_PER_SECOND.get(resolution)
    return (seconds * (rate or 0.11), resolution in ("480p", "720p"))

# Grok defaults to 848x480, which the auditor flags as below 720p. Default
# higher here so the common case does not produce a finding on arrival.
DEFAULT_RESOLUTION = "720p"


def available() -> bool:
    return bool(read_key("XAI_API_KEY", "GROK_API_KEY"))


def _key() -> str:
    k = read_key("XAI_API_KEY", "GROK_API_KEY")
    if not k:
        raise MissingDependency(
            "XAI_API_KEY is not set. Get one at https://console.x.ai and put it in the "
            "environment or in ~/.itachi-api-keys."
        )
    return k


def _post(url: str, payload: dict, key: str, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", body)
        except json.JSONDecodeError:
            msg = body
        raise ProviderError(f"xAI rejected the request (HTTP {exc.code}): {str(msg)[:400]}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Could not reach xAI: {exc.reason}") from exc


def _data_uri(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise LLVideoError(f"Reference file not found: {p}")
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


@dataclass
class Generated:
    path: str
    url: str
    seconds: float
    model: str
    cost_usd: float
    wall_seconds: float
    request_id: str
    audit: dict | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path, "url": self.url, "seconds": self.seconds,
            "model": self.model, "cost_usd": round(self.cost_usd, 4),
            "wall_seconds": round(self.wall_seconds, 1),
            "request_id": self.request_id, "audit": self.audit, "notes": self.notes,
        }


def generate_video(prompt: str, out_path: str, *, seconds: int = 6,
                   resolution: str = DEFAULT_RESOLUTION, aspect: str = "16:9",
                   image: str | None = None, video: str | None = None,
                   model: str | None = None, with_audio: bool = False,
                   poll_seconds: float = 4.0, timeout: int = 900,
                   progress=None) -> Generated:
    """Make one clip. Submit, poll, download.

    `image` gives image-to-video; `video` gives video-to-video (supported by
    grok-imagine-video, whose input modalities include video). `with_audio`
    switches to the 1.5 model, which generates an audio track.
    """
    if not (MIN_SECONDS <= seconds <= MAX_SECONDS):
        raise LLVideoError(
            f"duration must be {MIN_SECONDS}-{MAX_SECONDS} seconds (asked for {seconds}). "
            f"For anything longer, generate several clips and join them.")
    if resolution not in RESOLUTIONS:
        raise LLVideoError(f"resolution must be one of {', '.join(RESOLUTIONS)}")
    if aspect not in ASPECTS:
        raise LLVideoError(f"aspect must be one of {', '.join(ASPECTS)}")

    key = _key()
    chosen = model or (VIDEO_MODEL_AUDIO if with_audio else VIDEO_MODEL)
    payload: dict = {
        "model": chosen,
        "prompt": prompt,
        "duration": int(seconds),
        "resolution": resolution,
        "aspect_ratio": aspect,
    }
    if image:
        payload["image"] = _data_uri(image)
    if video:
        if chosen != VIDEO_MODEL:
            raise LLVideoError(
                f"video-to-video needs {VIDEO_MODEL}; {chosen} does not accept video input.")
        payload["video"] = _data_uri(video)

    t0 = time.time()
    started = _post(SUBMIT, payload, key)
    rid = started.get("request_id")
    if not rid:
        raise ProviderError(f"xAI did not return a request_id: {json.dumps(started)[:300]}")
    if progress:
        progress(f"submitted {rid}")

    deadline = time.time() + timeout
    info: dict = {}
    while True:
        if time.time() > deadline:
            raise ProviderError(f"Generation {rid} did not finish within {timeout}s.")
        req = urllib.request.Request(f"{POLL}/{rid}",
                                     headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status, body = r.status, r.read()
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"Polling {rid} failed: HTTP {exc.code} "
                f"{exc.read().decode('utf-8', 'replace')[:300]}") from exc
        if status == 200:
            info = json.loads(body)
            break
        if progress:
            progress(f"working ({int(time.time() - t0)}s)")
        time.sleep(poll_seconds)

    if info.get("status") not in (None, "done", "completed", "succeeded"):
        raise ProviderError(f"Generation ended with status "
                            f"{info.get('status')}: {json.dumps(info)[:300]}")
    vid = info.get("video") or {}
    url = vid.get("url")
    if not url:
        raise ProviderError(f"No video URL in the response: {json.dumps(info)[:300]}")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=300) as r:
            out.write_bytes(r.read())
    except (urllib.error.URLError, OSError) as exc:
        raise ProviderError(f"Generated clip could not be downloaded: {exc}") from exc

    ticks = (info.get("usage") or {}).get("cost_in_usd_ticks") or 0
    notes = []
    if vid.get("respect_moderation"):
        notes.append("xAI applied content moderation to this generation.")
    return Generated(
        path=str(out), url=url,
        seconds=float(vid.get("duration") or seconds),
        model=info.get("model", chosen),
        cost_usd=ticks * USD_PER_TICK,
        wall_seconds=time.time() - t0,
        request_id=rid, notes=notes)


def generate_image(prompt: str, out_path: str, *, model: str = IMAGE_MODEL,
                   reference: str | None = None) -> dict:
    """A still, for title cards, textures or an image-to-video seed."""
    key = _key()
    payload: dict = {"model": model, "prompt": prompt, "response_format": "b64_json"}
    if reference:
        payload["image"] = _data_uri(reference)
    t0 = time.time()
    res = _post(f"{BASE}/images/generations", payload, key, timeout=300)
    items = res.get("data") or []
    if not items:
        raise ProviderError(f"No image returned: {json.dumps(res)[:300]}")
    item = items[0]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if item.get("b64_json"):
        out.write_bytes(base64.b64decode(item["b64_json"]))
    elif item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=180) as r:
            out.write_bytes(r.read())
    else:
        raise ProviderError(f"Image response had neither b64_json nor url: "
                            f"{json.dumps(item)[:200]}")
    ticks = (res.get("usage") or {}).get("cost_in_usd_ticks") or 0
    return {"path": str(out), "model": model,
            "cost_usd": round(ticks * USD_PER_TICK, 4),
            "wall_seconds": round(time.time() - t0, 1),
            "revised_prompt": item.get("revised_prompt")}
