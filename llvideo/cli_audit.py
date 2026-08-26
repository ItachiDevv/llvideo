"""The `audit` command — QA a rendered video.

Runs the free measured checks always. Adds craft analysis and an intent diff
only when they are asked for, because those cost money and the free checks
catch most real render bugs on their own.
"""
from __future__ import annotations

from pathlib import Path

from . import audit as A
from . import probe as P
from .errors import LLVideoError

_SEV_ORDER = {s: i for i, s in enumerate(A.SEVERITIES)}


def run(args, out, fmt_ts) -> int:
    pr = P.probe(args.source)

    findings = A.measured_audit(pr, margins=not args.no_margins)

    # A project directory is the better source: it already knows what it meant
    # to build, so nobody has to hand-write the spec.
    generated_intent = None
    from_project = getattr(args, "from_project", None)
    if from_project:
        from . import spec as SP
        sp = SP.extract(from_project)
        generated_intent = sp.to_intent()
        for n in sp.notes:
            findings.append(A.Finding("note", "spec", n, source="measured"))

    craft_data = None
    if args.craft or args.spec or generated_intent:
        # An intent diff needs observed transitions to compare against, so the
        # craft pass is implied by --spec even if it was not asked for.
        # Must use the full two-pass analysis. A single whole-video pass
        # classifies a wipe and a fade-to-black as `hard_cut`, which would
        # make the auditor report mismatches that are not real.
        from .cli_craft import analyse_craft
        _res = analyse_craft(args.source, model=args.model,
                             zoom_fps=args.zoom_fps, max_windows=args.max_windows)
        craft_data = _res.data
        for w in (craft_data.get("uncertainties") or []):
            findings.append(A.Finding("note", "craft", w, source="judged"))

    intent = generated_intent
    if args.spec:
        intent = A.load_intent(args.spec)
    if intent:
        findings += A.compare_intent(intent, pr, craft_data)
        # A fade-to-black is a run of black frames. If the spec asked for one
        # there, reporting it as a defect is a false positive.
        findings = A.suppress_intended(findings, intent)

    findings.sort(key=lambda f: (_SEV_ORDER.get(f.severity, 9),
                                 f.at if f.at is not None else 0.0))
    summary = A.summarise(findings)

    payload = {
        "file": str(Path(args.source).name),
        "duration": round(pr.duration, 3),
        "resolution": f"{pr.display_width}x{pr.display_height}",
        "fps": round(pr.fps, 3),
        "verdict": summary["verdict"],
        "summary": summary,
        "findings": [f.to_dict() for f in findings],
        "intent_checked": bool(intent),
        "craft_checked": bool(craft_data),
    }

    def human(_):
        print(f"{payload['file']}  {payload['resolution']} @ {payload['fps']}fps  "
              f"{fmt_ts(pr.duration)}")
        c = summary["counts"]
        print(f"VERDICT: {summary['verdict'].upper()}   "
              f"{c['blocker']} blocker, {c['major']} major, "
              f"{c['minor']} minor, {c['note']} note")
        if not findings:
            print("\nNothing flagged. Every measured check passed.")
            return
        print()
        for f in findings:
            where = f"  at {fmt_ts(f.at)}" if f.at is not None else ""
            tag = "" if f.source == "measured" else "  (judged, not measured)"
            print(f"  [{f.severity.upper()}] {f.check}{where}{tag}")
            print(f"      {f.message}")
        measured = summary["measured_findings"]
        print()
        print(f"  {measured} of {len(findings)} findings are ffmpeg measurements — "
              f"those are facts, not opinions.")
        if not craft_data:
            print("  Add --craft for transition and camera analysis, "
                  "or --spec FILE to diff against an intent spec.")

    out(payload, args.json, human)
    return 1 if summary["counts"]["blocker"] else 0
