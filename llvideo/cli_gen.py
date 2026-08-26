"""The `gen` command — make a short clip, then immediately audit it.

The audit is not optional decoration. A generated clip nobody watched is the
exact failure this tool exists to prevent, and generation defaults make it
likely: Grok returns 848x480 at about -21 LUFS unless told otherwise.
"""
from __future__ import annotations

from pathlib import Path

from . import audit as A
from . import generate as G
from . import probe as P
from .errors import LLVideoError


def run(args, out, fmt_ts) -> int:
    if not G.available():
        raise LLVideoError(
            "XAI_API_KEY is not set. Get one at https://console.x.ai and put it in the "
            "environment or in ~/.itachi-api-keys.")

    dest = Path(args.out or f"clip_{int(args.seconds)}s.mp4")
    est, measured = G.estimate(args.seconds, args.resolution)
    if est > 1.0 and not args.yes:
        qual = "" if measured else " (1080p rate is extrapolated, not measured)"
        raise LLVideoError(
            f"{args.seconds}s at {args.resolution} is roughly ${est:.2f}{qual}. "
            f"Re-run with --yes to approve.")

    def progress(msg: str) -> None:
        if not args.json:
            print(f"  {msg}")

    g = G.generate_video(
        args.prompt, str(dest),
        seconds=args.seconds, resolution=args.resolution, aspect=args.aspect,
        image=args.image, video=args.video, with_audio=args.audio,
        model=args.model, progress=progress)

    if not args.no_audit:
        pr = P.probe(g.path)
        findings = A.measured_audit(pr, margins=False)
        g.audit = {
            "verdict": A.summarise(findings)["verdict"],
            "findings": [f.to_dict() for f in findings],
        }

    payload = g.to_dict()

    def human(_):
        print()
        print(f"{g.path}")
        print(f"  {g.seconds:.0f}s  {g.model}  ${g.cost_usd:.3f}  "
              f"in {g.wall_seconds:.0f}s wall clock")
        for n in g.notes:
            print(f"  ! {n}")
        if g.audit:
            print(f"  audit: {g.audit['verdict'].upper()}")
            for f in g.audit["findings"]:
                print(f"    [{f['severity']}] {f['message']}")
            if not g.audit["findings"]:
                print("    nothing flagged")
        print()
        print(f"  Look at it before you use it:  llvideo sheet {g.path}")
    out(payload, args.json, human)
    return 0


def run_image(args, out, fmt_ts) -> int:
    if not G.available():
        raise LLVideoError("XAI_API_KEY is not set.")
    dest = Path(args.out or "image.png")
    r = G.generate_image(args.prompt, str(dest), reference=args.reference)

    def human(_):
        print(f"{r['path']}  ${r['cost_usd']:.3f}  in {r['wall_seconds']}s")
        if r.get("revised_prompt"):
            print(f"  prompt was rewritten to: {r['revised_prompt'][:160]}")
    out(r, args.json, human)
    return 0
