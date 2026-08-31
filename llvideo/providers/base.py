"""Provider interface. One contract, several backends.

A provider takes a video (local path or URL) plus a prompt and a JSON schema,
and returns parsed structured data. Nothing above this layer knows or cares
which service answered.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ProviderError

# Optional convenience fallback: a KEY=value file, so keys can live outside the
# shell profile. Resolved at call time, not import time — a module-level
# Path.home() is captured once and ignores any later HOME change, which made a
# no-key environment still read the developer's real key file and report
# providers as available when they were not.
KEYFILE_NAME = ".itachi-api-keys"


def _keyfile() -> Path:
    return Path.home() / KEYFILE_NAME


def read_key(*names: str) -> str | None:
    """Environment first, then the shared key file."""
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    kf = _keyfile()
    if kf.exists():
        try:
            for line in kf.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                for n in names:
                    if line.startswith(f"{n}="):
                        v = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if v:
                            return v
        except OSError:
            pass
    return None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    video_tokens: int = 0
    audio_tokens: int = 0
    cost_usd: float | None = None

    def merge(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.video_tokens + other.video_tokens,
            self.audio_tokens + other.audio_tokens,
            (self.cost_usd or 0) + (other.cost_usd or 0) if (
                self.cost_usd is not None or other.cost_usd is not None) else None,
        )


@dataclass
class Result:
    data: dict
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    raw_text: str = ""


def http_json(url: str, payload: dict | None = None, headers: dict | None = None,
              timeout: int = 600, method: str | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"error": {"message": raw.decode("utf-8", "replace")[:600]}}
    except urllib.error.URLError as exc:
        raise ProviderError(f"Network error reaching {url}: {exc.reason}") from exc


def parse_json_text(text: str) -> dict:
    """Parse a model's JSON reply, tolerating code fences and leading prose."""
    if not text:
        raise ProviderError("Model returned an empty response.")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ProviderError(f"Model did not return valid JSON. First 300 chars:\n{text[:300]}")


class Provider:
    name = "base"
    supports_upload = False
    supports_url = False
    max_bytes = 0

    def available(self) -> bool:
        raise NotImplementedError

    def analyse(self, source: str, prompt: str, schema: dict, *,
                is_url: bool = False, fps: float | None = None,
                start: float | None = None, end: float | None = None,
                model: str | None = None) -> Result:
        raise NotImplementedError

    def count_tokens(self, source: str, *, is_url: bool = False,
                     fps: float | None = None, model: str | None = None) -> Usage | None:
        return None
