"""The `fix` command — repair what ffmpeg can, report what it cannot."""
from __future__ import annotations

from . import fix as FX


def run(args, out, fmt_ts) -> int:
    r = FX.fix(args.source, args.out,
               normalise_audio=not args.no_audio_fix,
               trim_edges=not args.no_trim,
               reaudit=not args.no_verify)
    payload = r.to_dict()

    def human(_):
        if not r.changed:
            print("Nothing to repair. Every finding needs an editorial decision "
                  "or a re-render.")
        else:
            print(f"{r.output}")
            for rep in r.repairs:
                print(f"  fixed  {rep.check}: {rep.action} - {rep.detail}")
        if r.before and r.after:
            b, a = r.before["counts"], r.after["counts"]
            print()
            print(f"  before: {b['blocker']} blocker, {b['major']} major, "
                  f"{b['minor']} minor  ({r.before['verdict']})")
            print(f"  after:  {a['blocker']} blocker, {a['major']} major, "
                  f"{a['minor']} minor  ({r.after['verdict']})")
        if r.skipped:
            print()
            print("  NOT fixed, and deliberately so:")
            for s in r.skipped:
                print(f"    [{s['severity']}] {s['check']}: {s['why']}")
    out(payload, args.json, human)
    return 0
