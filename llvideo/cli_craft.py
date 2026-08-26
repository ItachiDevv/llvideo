"""The `craft` command — how a video is shot and cut.

Kept separate from cli.py because it has its own two-pass strategy: a free local
pass to find candidate transitions, then a high-fps pass on each one. Sampling
the whole timeline at 1 fps would show the shots either side of every cut and
never the cut itself, which is the thing being asked about.
"""
from __future__ import annotations

from . import craft as C
from . import frames as F
from . import probe as P
from .analyze import UploadCache, is_url, scratch_dir
from .errors import LLVideoError
from .providers import pick_provider
from .schema import CRAFT_PROMPT, CRAFT_SCHEMA


def _resolve_upload(prov, source: str, url: bool):
    """Upload once and reuse. Files live 48h, so check before trusting a cached uri."""
    if url or prov.name != "gemini":
        return None
    cache = UploadCache()
    key = UploadCache.fingerprint(source)
    hit = cache.get(key)
    if hit and prov.file_alive(hit["name"]):
        return hit["uri"]
    if hit:
        cache.drop(key)
    info = prov.upload(source)
    cache.put(key, info["name"], info["uri"], info.get("mimeType", "video/mp4"))
    return info["uri"]


def run(args, out, fmt_ts) -> int:
    url = is_url(args.source)
    pr = None if url else P.probe(args.source)
    duration = pr.duration if pr else 0.0

    # Pass 1 — free, local, zero tokens.
    cands, stats, hint = [], {}, ""
    if pr and pr.has_video:
        cands = C.find_candidates(C.frame_scores(args.source))
        stats = C.shot_stats(cands, duration)
        hint = C.describe_candidates(cands)

    prov = pick_provider(args.provider, needs_upload=not url, needs_clipping=True)
    file_uri = _resolve_upload(prov, args.source, url)

    prompt = CRAFT_PROMPT + (("\n\n" + hint) if hint else "")
    if args.question:
        prompt += "\n\nThe user also asks: " + args.question

    tin = tout = 0
    cost = 0.0

    overall = prov.analyse(args.source, prompt, CRAFT_SCHEMA, is_url=url,
                           model=args.model, file_uri=file_uri,
                           fps=(args.fps if args.fps else None))
    tin += overall.usage.input_tokens
    tout += overall.usage.output_tokens
    cost += overall.usage.cost_usd or 0.0
    data = dict(overall.data)

    # Pass 2 — high fps, only inside the candidate windows.
    windows: list[tuple[float, float]] = []
    if cands and prov.name == "gemini" and not args.no_zoom:
        windows = C.windows_for(cands, duration, limit=args.max_windows)
        detailed = []
        for (a, b) in windows:
            wprompt = (
                CRAFT_PROMPT
                + f"\n\nYou are seeing ONLY {a:.2f}s to {b:.2f}s of this video, sampled at "
                  f"{args.zoom_fps} frames per second so the transition itself is visible "
                  f"frame by frame.\n\n"
                  "Report the transition in this window and its exact duration. If the "
                  "frames flow into each other with motion blur and no content jump, this "
                  "is CAMERA MOVEMENT, not an edit — classify the move and say there is no "
                  "cut here. If you see frames containing two images mixed together, it is "
                  "a blend, and you must say how long it lasts."
            )
            # Measure brightness locally first. A model reading sparse frames
            # cannot reliably tell a fade-through-black from a crossfade; a luma
            # dip to near zero settles it objectively.
            if not url:
                wprompt += C.describe_luma(C.luma_profile(args.source, a, b))
            r = prov.analyse(args.source, wprompt, CRAFT_SCHEMA, start=a, end=b,
                             fps=args.zoom_fps, model=args.model, file_uri=file_uri)
            tin += r.usage.input_tokens
            tout += r.usage.output_tokens
            cost += r.usage.cost_usd or 0.0
            for t in (r.data.get("transitions") or []):
                t["_window"] = f"{a:.2f}-{b:.2f}"
                detailed.append(t)
        if detailed:
            data["transitions"] = detailed

    # Frames either side of every candidate, so the classification can be checked.
    sheet = None
    if pr and pr.has_video and cands:
        times = sorted({round(max(c.time - 0.12, 0.0), 3) for c in cands}
                       | {round(min(c.time + 0.12, duration), 3) for c in cands})[:16]
        target = args.sheet or str(
            scratch_dir() / f"craft_{UploadCache.fingerprint(args.source)}.jpg")
        try:
            sheet = F.contact_sheet(args.source, times, target, tile_width=360)
        except LLVideoError:
            sheet = None

    payload = {
        "overall": data.get("overall"),
        "shots": data.get("shots"),
        "transitions": data.get("transitions"),
        "rhythm": data.get("rhythm"),
        "uncertainties": data.get("uncertainties"),
        "measured": {
            "candidates": [c.to_dict() for c in cands],
            "pacing": stats,
            "zoom_windows": [{"start": round(a, 2), "end": round(b, 2)} for a, b in windows],
        },
        "usage": {"input_tokens": tin, "output_tokens": tout,
                  "cost_usd": round(cost, 4) if cost else None},
        "contact_sheet": ({"path": sheet.path, "frame_times": sheet.frame_times}
                          if sheet else None),
    }

    def human(_):
        u = payload["usage"]
        line = f"[{prov.name}]  {u['input_tokens']:,} in / {u['output_tokens']:,} out"
        if u["cost_usd"]:
            line += f"  ${u['cost_usd']:.4f}"
        print(line)
        o = payload["overall"] or {}
        print()
        print(o.get("style", ""))
        print(f"  pacing    {o.get('pacing')} - {o.get('pacing_note', '')}")
        print(f"  colour    {o.get('colour', '')}")
        print(f"  lighting  {o.get('lighting', '')}")
        # These are raw local CANDIDATES, not confirmed shots. Saying "9 shots"
        # next to an analysis that found one continuous take reads as a
        # contradiction, when it is really the detector casting a wide net.
        n_shots = len(payload["shots"] or [])
        if stats:
            print(f"  detector  {len(cands)} candidate boundaries "
                  f"(before classification; many are camera movement, not cuts)")
        if n_shots:
            print(f"  confirmed {n_shots} shot{'s' if n_shots != 1 else ''}, "
                  f"{len(payload['transitions'] or [])} transition"
                  f"{'s' if len(payload['transitions'] or []) != 1 else ''}")
        print()
        print("SHOTS")
        for sh in (payload["shots"] or []):
            print(f"  {sh.get('start')}-{sh.get('end')}  {sh.get('shot_size')}, "
                  f"{sh.get('camera_move')}  {sh.get('subject')}")
            if sh.get("composition"):
                print(f"        {sh['composition']}")
            if sh.get("notable"):
                print(f"        note: {sh['notable']}")
        print()
        print("TRANSITIONS")
        for t in (payload["transitions"] or []):
            d = t.get("duration_seconds") or 0
            dur = "instant" if d == 0 else f"{d}s"
            conf = t.get("confidence", "")
            mark = "" if conf == "high" else f"   [{conf} confidence]"
            print(f"  {t.get('at')}  {t.get('kind')}  ({dur}){mark}")
            print(f"        {t.get('from_shot')}  ->  {t.get('to_shot')}")
            if t.get("motivation"):
                print(f"        why: {t['motivation']}")
        if payload["uncertainties"]:
            print()
            print("UNCERTAIN")
            for x in payload["uncertainties"]:
                print(f"  - {x}")
        if sheet:
            print()
            print(f"  frames either side of every transition: {sheet.path}")
            print("    LOOK AT THIS to check the classifications yourself.")

    out(payload, args.json, human)
    return 0
