"""The `timeline` command — one chronological track instead of three lists."""
from __future__ import annotations

from . import timeline as T
from .analyze import analyse, is_url
from .errors import LLVideoError


def run(args, out, fmt_ts) -> int:
    a = analyse(args.source, provider_name=args.provider, model=args.model,
                fps=args.fps, want_audio=(False if args.no_audio else None),
                sheet_path=args.sheet, approved=args.yes,
                use_cache=not args.no_cache)

    transcript = None
    if args.transcribe:
        if is_url(args.source):
            raise LLVideoError(
                "--transcribe needs a local file; a URL is never downloaded. "
                "Download it first, or drop the flag and use the index's own speech.")
        from .transcribe import transcribe_auto
        transcript = transcribe_auto(args.source, backend=args.backend)

    duration = a.plan.probe.duration if a.plan.probe else 0.0
    beats = T.build(a.index, duration=duration, transcript=transcript)
    cov = T.coverage(beats, duration)

    payload = {
        "summary": a.index.get("summary"),
        "content_kind": a.index.get("content_kind"),
        "beats": [b.to_dict() for b in beats],
        "coverage": cov,
        "uncertainties": a.index.get("uncertainties"),
        "speech_source": "local transcript" if transcript else "index",
        "contact_sheet": (a.sheet.path if a.sheet else None),
        "usage": {"input_tokens": a.usage.input_tokens,
                  "cost_usd": a.usage.cost_usd},
        "cached": a.provider == "cache",
    }

    def human(_):
        head = f"[{a.provider}]"
        if a.usage.cost_usd:
            head += f"  ${a.usage.cost_usd:.4f}"
        elif a.provider == "cache":
            head += "  (reused cached index, no cost)"
        print(head)
        print()
        print(a.index.get("summary", ""))
        print()
        print(T.render(beats))
        print()
        if cov.get("duration_known"):
            print(f"  covers {cov['ratio'] * 100:.0f}% of the runtime "
                  f"({cov['covered_seconds']:.0f}s of {cov['duration']:.0f}s), "
                  f"{cov['with_speech']} with speech, {cov['with_text']} with text")
        else:
            # A URL is never probed locally, so the true runtime is unknown.
            print(f"  {cov['beats']} beats spanning {cov['covered_seconds']:.0f}s, "
                  f"{cov['with_speech']} with speech, {cov['with_text']} with text")
            print("  (runtime not verified — URL input is never probed locally)")
        for g in cov["gaps"][:4]:
            print(f"  ! nothing reported between {fmt_ts(g['start'])} and {fmt_ts(g['end'])}")
        if payload["speech_source"] == "local transcript":
            print("  speech timings come from the local transcript, not the model")
        for u in (a.index.get("uncertainties") or []):
            print(f"  ? {u}")
        if a.sheet:
            print()
            print(f"  contact sheet: {a.sheet.path}")
            print("    LOOK AT IT before you rely on any of the above.")
    out(payload, args.json, human)
    return 0
