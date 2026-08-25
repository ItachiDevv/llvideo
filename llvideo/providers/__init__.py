from .base import Provider, Result, Usage, read_key
from .gemini import GeminiProvider
from .openrouter import OpenRouterProvider

__all__ = ["Provider", "Result", "Usage", "read_key",
           "GeminiProvider", "OpenRouterProvider", "pick_provider", "available_providers"]


def available_providers() -> list[Provider]:
    return [p for p in (GeminiProvider(), OpenRouterProvider()) if p.available()]


def pick_provider(name: str | None = None, *, needs_upload: bool = False,
                  size_bytes: int = 0, needs_clipping: bool = False) -> Provider:
    """Choose a backend.

    Preference order when nothing is forced:
      - OpenRouter when it is usable and the job fits, because it costs half.
      - Gemini for large local files, fps resampling or clipping — the only
        backend with an upload endpoint and videoMetadata.
    """
    from ..errors import NoProvider

    if name:
        p = {"gemini": GeminiProvider, "openrouter": OpenRouterProvider}.get(name.lower())
        if p is None:
            raise NoProvider(f"Unknown provider '{name}'. Choose gemini or openrouter.")
        inst = p()
        if not inst.available():
            raise NoProvider(f"Provider '{name}' has no API key configured.")
        return inst

    gem, orr = GeminiProvider(), OpenRouterProvider()
    if needs_upload or needs_clipping or (size_bytes and size_bytes > orr.max_bytes):
        if gem.available():
            return gem
        if orr.available() and not (needs_upload or needs_clipping):
            return orr
    else:
        if orr.available() and orr.credit_status().get("video_ready"):
            return orr
        if gem.available():
            return gem

    raise NoProvider(
        "No usable video backend.\n"
        "  GEMINI_API_KEY     — handles local files up to 2 GB, clipping and fps control.\n"
        "  OPENROUTER_API_KEY — cheaper, but URLs and small clips only, and needs $1+ credit.\n"
        "Set either in the environment or in ~/.itachi-api-keys."
    )
