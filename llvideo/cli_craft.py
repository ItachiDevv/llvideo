"""The `craft` command — how a video is shot and cut.

Two passes, and the split is the whole design. A free local pass finds candidate
transitions from per-frame scene scores; then each candidate is re-examined at
high fps so the transition is actually visible. Sampling the whole timeline at
1 fps shows the shots either side of every cut and never the cut itself, which
is the thing being asked about.

There is exactly one implementation of that, `analyse_craft`. The audit path
needs the same answers, and an earlier version had the audit run a cheaper
single-pass call — which reported a wipe and a fade-to-black as `hard_cut` and
invented spec mismatches that were not real. One path, no shortcuts.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import craft as C
from . import frames as F
from . import probe as P
from .analyze import UploadCache, is_url, scratch_dir
from .errors import LLVideoError
from .providers import pick_provider
from .schema import CRAFT_PROMPT, CRAFT_SCHEMA

# Windows are independent API calls against an already-uploaded file, so running
# them concurrently is pure wall-clock saving. Measured sequential: 4 windows in
# 61s. Capped low to stay friendly to provider rate limits.
MAX_PARALLEL = 6


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


@dataclass
class CraftResult:
    data: dict
    stats: dict
    candidates: list
    windows: list = field(default_factory=list)
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def analyse_craft(source: str, *, provider=None, model=None, zoom_fps: float = 8.0,
                  max_windows: int = 8, no_zoom: bool = False,
                  extra_prompt: str = "", fps: float | None = None) -> CraftResult:
    url = is_url(source)
    pr = None if url else P.probe(source)
    duration = pr.duration if pr else 0.0

    # Pass 1 — free, local, zero tokens.
    cands, stats, hint = [], {}, ""
    if pr and pr.has_video:
        cands = C.find_candidates(C.frame_scores(source))
        stats = C.shot_stats(cands, duration)
        hint = C.describe_candidates(cands)

    prov = provider or pick_provider(None, needs_upload=not url, needs_clipping=True)
    uri = _resolve_upload(prov, source, url)

    prompt = CRAFT_PROMPT + (("\n\n" + hint) if hint else "") + extra_prompt
    overall = prov.analyse(source, prompt, CRAFT_SCHEMA, is_url=url,
                           model=model, file_uri=uri, fps=fps)
    res = CraftResult(data=dict(overall.data), stats=stats, candidates=cands,
                      provider=prov.name,
                      input_tokens=overall.usage.input_tokens,
                      output_tokens=overall.usage.output_tokens,
                      cost_usd=overall.usage.cost_usd or 0.0)

    # Pass 2 — high fps, only inside the candidate windows, run concurrently.
    if cands and prov.name == "gemini" and not no_zoom:
        res.windows = C.windows_for(cands, duration, limit=max_windows)

        def one(window):
            a, b = window
            wp = (CRAFT_PROMPT
                  + f"\n\nYou are seeing ONLY {a:.2f}s to {b:.2f}s of this video, sampled "
                    f"at {zoom_fps} frames per second so the transition itself is visible "
                    f"frame by frame.\n\n"
                    "Report the transition in this window and its exact duration. If the "
                    "frames flow into each other with motion blur and no content jump, "
                    "this is CAMERA MOVEMENT, not an edit — classify the move and say "
                    "there is no cut here. If you see frames containing two images mixed "
                    "together, it is a blend, and you must say how long it lasts.")
            # Measure brightness locally first. A model reading sparse frames
            # cannot reliably tell a fade-through-black from a crossfade; a luma
            # dip to near zero settles it objectively.
            if not url:
                wp += C.describe_luma(C.luma_profile(source, a, b))
            r = prov.analyse(source, wp, CRAFT_SCHEMA, start=a, end=b, fps=zoom_fps,
                             model=model, file_uri=uri)
            found = []
            for t in (r.data.get("transitions") or []):
                t["_window"] = f"{a:.2f}-{b:.2f}"
                found.append(t)
            return found, r.usage

        detailed = []
        if res.windows:
            workers = min(MAX_PARALLEL, len(res.windows))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                # pool.map preserves input order, so transitions stay on the
                # timeline in the order they happen regardless of who finishes first.
                for found, usage in pool.map(one, res.windows):
                    detailed.extend(found)
                    res.input_tokens += usage.input_tokens
                    res.output_tokens += usage.output_tokens
                    res.cost_usd += usage.cost_usd or 0.0
        if detailed:
            res.data["transitions"] = detailed
    return res


def run(args, out, fmt_ts) -> int:
    url = is_url(args.source)
    pr = None if url else P.probe(args.source)
    duration = pr.duration if pr else 0.0

    extra = ("\n\nThe user also asks: " + args.question) if args.question else ""
    res = analyse_craft(args.source, provider=pick_provider(
                            args.provider, needs_upload=not url, needs_clipping=True),
                        model=args.model, zoom_fps=args.zoom_fps,
                        max_windows=args.max_windows, no_zoom=args.no_zoom,
                        extra_prompt=extra, fps=(args.fps if args.fps else None))
    data, stats, cands = res.data, res.stats, res.candidates

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
            "zoom_windows": [{"start": round(a, 2), "end": round(b, 2)}
                             for a, b in res.windows],
        },
        "usage": {"input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                  "cost_usd": round(res.cost_usd, 4) if res.cost_usd else None},
        "contact_sheet": ({"path": sheet.path, "frame_times": sheet.frame_times}
                          if sheet else None),
    }

    def human(_):
        u = payload["usage"]
        line = f"[{res.provider}]  {u['input_tokens']:,} in / {u['output_tokens']:,} out"
        if u["cost_usd"]:
            line += f"  ${u['cost_usd']:.4f}"
        print(line)
        o = payload["overall"] or {}
        print()
        print(o.get("style", ""))
        print(f"  pacing    {o.get('pacing')} - {o.get('pacing_note', '')}")
        print(f"  colour    {o.get('colour', '')}")
        print(f"  lighting  {o.get('lighting', '')}")
        n_shots = len(payload["shots"] or [])
        n_trans = len(payload["transitions"] or [])
        if stats:
            print(f"  detector  {len(cands)} candidate boundaries "
                  f"(before classification; many are camera movement, not cuts)")
        if n_shots:
            print(f"  confirmed {n_shots} shot{'s' if n_shots != 1 else ''}, "
                  f"{n_trans} transition{'s' if n_trans != 1 else ''}")
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
