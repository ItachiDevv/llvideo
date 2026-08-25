"""OpenRouter — cheaper per token, but no upload endpoint.

Verified against the live catalogue (419 models):
  - 69 models accept `video` input; ZERO output video
  - google/gemini-3.7-flash costs $0.375/M in, half the direct Gemini price
  - video requests need at least $1.00 of purchased credit, even on :free
    models. A key with no credits returns HTTP 402 for anything with video.

No File API here. A local video must be inlined as a base64 data URL, so this
backend is for URLs and small clips. Large local files go to Gemini direct.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from ..errors import ProviderError
from .base import Provider, Result, Usage, http_json, parse_json_text, read_key

BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemini-3.7-flash"

# Inlining costs ~33% on top in base64. Keep well under typical body limits.
MAX_INLINE_BYTES = 40 * 1024 ** 2


class OpenRouterProvider(Provider):
    name = "openrouter"
    supports_upload = False
    supports_url = True
    max_bytes = MAX_INLINE_BYTES

    def __init__(self, model: str | None = None):
        self.key = read_key("OPENROUTER_API_KEY", "OPENROUTER_KEY")
        self.model = model or DEFAULT_MODEL

    def available(self) -> bool:
        return bool(self.key)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/ItachiDevv/llvideo", "X-Title": "llvideo"}

    def credit_status(self) -> dict:
        """Video needs >= $1.00 of purchased credit. Check before promising anything."""
        status, body = http_json(f"{BASE}/key", headers=self._headers(), timeout=60)
        if status != 200:
            return {"ok": False, "reason": f"HTTP {status}"}
        d = body.get("data", {})
        usage = d.get("usage") or 0
        limit = d.get("limit")
        remaining = d.get("limit_remaining")
        free_tier = d.get("is_free_tier", False)
        return {
            "ok": not free_tier,
            "usage_usd": usage,
            "limit_usd": limit,
            "remaining_usd": remaining,
            "is_free_tier": free_tier,
            "video_ready": not free_tier,
            "reason": ("This account has never purchased credits. OpenRouter requires at "
                       "least $1.00 of balance for any video request, including on :free "
                       "models. Add credit at https://openrouter.ai/settings/credits."
                       if free_tier else "ready"),
        }

    @staticmethod
    def _video_part(source: str, is_url: bool) -> dict:
        if is_url:
            return {"type": "video_url", "video_url": {"url": source}}
        p = Path(source)
        size = p.stat().st_size
        if size > MAX_INLINE_BYTES:
            raise ProviderError(
                f"{p.name} is {size / 1024**2:.0f} MB. OpenRouter has no upload endpoint, so a "
                f"local file must be inlined as base64, and this is too large. Either transcode "
                f"it smaller or use the gemini provider, which uploads properly."
            )
        b64 = base64.b64encode(p.read_bytes()).decode()
        return {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}}

    def analyse(self, source: str, prompt: str, schema: dict, *,
                is_url: bool = False, fps: float | None = None,
                start: float | None = None, end: float | None = None,
                model: str | None = None, **_) -> Result:
        if not self.key:
            raise ProviderError("OPENROUTER_API_KEY is not set.")
        model = model or self.model
        if fps is not None or start is not None or end is not None:
            # OpenRouter's chat schema has no videoMetadata equivalent. Clip
            # locally before calling instead of silently ignoring the request.
            raise ProviderError(
                "OpenRouter cannot resample or clip server-side. Clip the file with ffmpeg "
                "first, or use the gemini provider which supports fps and start/end offsets."
            )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                self._video_part(source, is_url),
            ]}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "video_index", "strict": True,
                                "schema": _to_json_schema(schema)},
            },
        }
        status, body = http_json(f"{BASE}/chat/completions", payload,
                                 self._headers(), timeout=900)
        if status != 200 or "error" in body:
            msg = body.get("error", {}).get("message", f"HTTP {status}")
            if status == 402:
                msg += ("\n\nOpenRouter requires at least $1.00 of purchased credit for video "
                        "requests. Add credit at https://openrouter.ai/settings/credits, "
                        "or use the gemini provider instead.")
            raise ProviderError(f"OpenRouter rejected the request: {msg}")

        choices = body.get("choices") or []
        if not choices:
            raise ProviderError(f"OpenRouter returned no choices: {json.dumps(body)[:300]}")
        text = choices[0].get("message", {}).get("content") or ""
        data = parse_json_text(text)
        u = body.get("usage", {}) or {}
        return Result(data=data, model=body.get("model", model), provider=self.name,
                      raw_text=text,
                      usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                                  cost_usd=u.get("cost")))


def _to_json_schema(google_schema: dict) -> dict:
    """Google's Schema dialect -> standard JSON Schema for OpenRouter."""
    if not isinstance(google_schema, dict):
        return google_schema
    out: dict = {}
    for k, v in google_schema.items():
        if k == "type" and isinstance(v, str):
            out["type"] = v.lower()
        elif k == "properties" and isinstance(v, dict):
            out["properties"] = {pk: _to_json_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out["items"] = _to_json_schema(v)
        else:
            out[k] = v
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
        # strict mode requires every property listed as required
        props = list((out.get("properties") or {}).keys())
        if props:
            out["required"] = props
    return out
