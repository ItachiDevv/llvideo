"""Gemini direct — File API upload plus generateContent.

The only backend that takes a large local video file. Verified working:
  - 71 video tokens/sec, 32 audio tokens/sec, constant across every 3.x model
  - responseSchema returns clean typed JSON from video input
  - videoMetadata start/endOffset clipping is frame-exact
  - YouTube fileUri works with no download at all
  - files live 48 hours, 2 GB per file
"""
from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..errors import ProviderError
from .base import Provider, Result, Usage, http_json, parse_json_text, read_key

BASE = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-3.7-flash"

# Input $/1M tokens. One rate covers text, image, video and audio on these models.
PRICING = {
    "gemini-3.7-flash": 0.75,
    "gemini-3.6-flash": 0.75,
    "gemini-3.5-flash": 1.50,
    "gemini-3.5-flash-lite": 0.30,
    "gemini-3.1-flash-lite": 0.30,
    "gemini-3.1-pro-preview": 2.00,
}
FILE_TTL_SECONDS = 48 * 3600
MAX_FILE_BYTES = 2 * 1024 ** 3


class GeminiProvider(Provider):
    name = "gemini"
    supports_upload = True
    supports_url = True
    max_bytes = MAX_FILE_BYTES

    def __init__(self, model: str | None = None):
        self.key = read_key("GEMINI_API_KEY", "GOOGLE_API_KEY")
        self.model = model or DEFAULT_MODEL

    def available(self) -> bool:
        return bool(self.key)

    # -- upload -----------------------------------------------------------
    def upload(self, path: str, wait: bool = True, timeout: int = 900) -> dict:
        p = Path(path)
        size = p.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ProviderError(
                f"{p.name} is {size / 1024**3:.2f} GB. The Gemini File API caps at 2 GB. "
                f"Transcode to a smaller proxy first."
            )
        mime = mimetypes.guess_type(str(p))[0] or "video/mp4"
        # The upload URL arrives as a response HEADER, so this call is made at
        # the urllib level rather than through http_json.
        req = urllib.request.Request(
            f"{BASE}/upload/v1beta/files?key={self.key}",
            data=json.dumps({"file": {"display_name": p.name}}).encode(),
            headers={"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                     "X-Goog-Upload-Header-Content-Length": str(size),
                     "X-Goog-Upload-Header-Content-Type": mime,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                upload_url = resp.headers.get("X-Goog-Upload-URL") or \
                             resp.headers.get("x-goog-upload-url")
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"Upload could not start: HTTP {exc.code} "
                                f"{exc.read().decode('utf-8', 'replace')[:300]}") from exc
        if not upload_url:
            raise ProviderError("Gemini did not return an upload URL.")

        data = p.read_bytes()
        req = urllib.request.Request(
            upload_url, data=data,
            headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                     "X-Goog-Upload-Command": "upload, finalize"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                info = json.loads(resp.read())["file"]
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"Upload failed: HTTP {exc.code} "
                                f"{exc.read().decode('utf-8', 'replace')[:300]}") from exc

        if wait:
            deadline = time.time() + timeout
            while info.get("state") == "PROCESSING":
                if time.time() > deadline:
                    raise ProviderError(f"{p.name} was still PROCESSING after {timeout}s.")
                time.sleep(2)
                info = self.get_file(info["name"])
            if info.get("state") == "FAILED":
                raise ProviderError(f"Gemini failed to process {p.name}: "
                                    f"{info.get('error', {}).get('message', 'no reason given')}")
        return info

    def get_file(self, name: str) -> dict:
        status, body = http_json(f"{BASE}/v1beta/{name}?key={self.key}")
        if status != 200:
            raise ProviderError(f"files.get failed: {body.get('error', {}).get('message', status)}")
        return body

    def file_alive(self, name: str) -> bool:
        """Files expire after 48h. Never trust a cached uri — check first."""
        try:
            return self.get_file(name).get("state") == "ACTIVE"
        except ProviderError:
            return False

    def delete_file(self, name: str) -> bool:
        status, _ = http_json(f"{BASE}/v1beta/{name}?key={self.key}", method="DELETE")
        return status in (200, 204)

    # -- parts ------------------------------------------------------------
    @staticmethod
    def _video_part(uri: str, mime: str = "video/mp4", fps: float | None = None,
                    start: float | None = None, end: float | None = None) -> dict:
        part: dict = {"fileData": {"mimeType": mime, "fileUri": uri}}
        meta: dict = {}
        if fps is not None:
            meta["fps"] = fps
        if start is not None:
            meta["startOffset"] = f"{start:.3f}s"
        if end is not None:
            meta["endOffset"] = f"{end:.3f}s"
        if meta:
            part["videoMetadata"] = meta
        return part

    def _resolve(self, source: str, is_url: bool) -> tuple[str, str]:
        if is_url:
            return source, "video/*"
        info = self.upload(source)
        return info["uri"], info.get("mimeType", "video/mp4")

    # -- api --------------------------------------------------------------
    def count_tokens(self, source: str, *, is_url: bool = False,
                     fps: float | None = None, model: str | None = None,
                     mime: str = "video/mp4") -> Usage | None:
        uri = source if (is_url or source.startswith("http")) else None
        if uri is None:
            return None
        status, body = http_json(
            f"{BASE}/v1beta/models/{model or self.model}:countTokens?key={self.key}",
            {"contents": [{"parts": [self._video_part(uri, mime, fps)]}]},
            {"Content-Type": "application/json"}, timeout=180)
        if status != 200:
            return None
        det = {d["modality"]: d["tokenCount"] for d in body.get("promptTokensDetails", [])}
        return Usage(input_tokens=body.get("totalTokens", 0),
                     video_tokens=det.get("VIDEO", 0), audio_tokens=det.get("AUDIO", 0))

    def analyse(self, source: str, prompt: str, schema: dict, *,
                is_url: bool = False, fps: float | None = None,
                start: float | None = None, end: float | None = None,
                model: str | None = None, file_uri: str | None = None,
                mime: str = "video/mp4") -> Result:
        if not self.key:
            raise ProviderError("GEMINI_API_KEY is not set.")
        model = model or self.model
        if file_uri:
            uri = file_uri
        else:
            uri, mime = self._resolve(source, is_url)

        payload = {
            "contents": [{"parts": [
                self._video_part(uri, mime, fps, start, end),
                {"text": prompt},
            ]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        status, body = http_json(
            f"{BASE}/v1beta/models/{model}:generateContent?key={self.key}",
            payload, {"Content-Type": "application/json"}, timeout=900)
        if status != 200 or "error" in body:
            msg = body.get("error", {}).get("message", f"HTTP {status}")
            raise ProviderError(f"Gemini rejected the request: {msg}")

        cands = body.get("candidates") or []
        if not cands:
            fb = body.get("promptFeedback", {})
            raise ProviderError(f"Gemini returned no candidates. Feedback: {json.dumps(fb)[:300]}")
        parts = cands[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        data = parse_json_text(text)

        um = body.get("usageMetadata", {})
        det = {d["modality"]: d["tokenCount"] for d in um.get("promptTokensDetails", [])}
        in_tok = um.get("promptTokenCount", 0)
        out_tok = um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)
        rate = PRICING.get(model)
        cost = (in_tok * rate / 1e6 + out_tok * rate * 5 / 1e6) if rate else None
        return Result(data=data, model=model, provider=self.name, raw_text=text,
                      usage=Usage(in_tok, out_tok, det.get("VIDEO", 0),
                                  det.get("AUDIO", 0), cost))
