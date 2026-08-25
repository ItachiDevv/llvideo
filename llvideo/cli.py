"""llvideo command line.

Designed to be driven by a coding agent: every command takes --json and prints
one machine-readable object on stdout. Human output is the default so a person
can use it too.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import frames as F
from . import probe as P
from .analyze import (Analysis, UploadCache, analyse, cleanup, is_url, plan,
                      scratch_dir)
from .errors import LLVideoError
from .schema import normalise_timestamp


def _out(obj, as_json: bool, human=None) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    elif human:
        human(obj)
    else:
        print(json.dumps(obj, indent=2, default=str))


def _fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------

def cmd_probe(args) -> int:
    pr = P.probe(args.source)
    d = pr.to_dict()
    d["estimate_default"] = pr.estimate_tokens()
    d["estimate_low"] = pr.estimate_tokens(0.2, with_audio=False)
    d["is_screen_content"] = pr.is_screen_content

    def human(_):
        print(f"{Path(pr.path).name}")
        print(f"  duration    {_fmt_ts(pr.duration)}  ({pr.duration:.2f}s)")
        geom = f"{pr.display_width}x{pr.display_height}"
        if pr.rotation:
            geom += f" (stream {pr.width}x{pr.height}, rotated {pr.rotation})"
        print(f"  video       {geom} @ {pr.fps:.3f} fps  {pr.video_codec}  {pr.orientation}")
        print(f"  audio       {pr.audio_codec or 'none'}"
              + (f"  {pr.audio_channels}ch {pr.audio_sample_rate}Hz" if pr.has_audio else ""))
        print(f"  size        {pr.size_bytes / 1e6:.1f} MB")
        print(f"  looks like  {'screen recording' if pr.is_screen_content else 'camera footage'}")
        e = pr.estimate_tokens()
        print(f"  tokens      {e['total']:,} at default sampling "
              f"({e['video']:,} video + {e['audio']:,} audio)")
        for w in pr.warnings:
            print(f"  ! {w}")
    _out(d, args.json, human)
    return 0


def cmd_plan(args) -> int:
    pl = plan(args.source, fps=args.fps,
              want_audio=(False if args.no_audio else None), model=args.model)
    d = pl.to_dict()

    def human(_):
        print(f"source      {pl.source}")
        if pl.probe:
            print(f"duration    {_fmt_ts(pl.probe.duration)}")
        print(f"sampling    {pl.fps_ratio} fps   audio {'on' if pl.keep_audio else 'off'}")
        print(f"tokens      ~{pl.estimated_tokens:,}")
        if pl.estimated_cost is not None:
            print(f"cost        ~${pl.estimated_cost:.4f}   band: {pl.band}")
        if pl.needs_transcode:
            print(f"transcode   yes -> {pl.proxy_height}p")
        if pl.segments:
            print(f"segments    {len(pl.segments)}")
        for n in pl.notes:
            print(f"  - {n}")
    _out(d, args.json, human)
    return 0


def _print_analysis(a: Analysis) -> None:
    idx = a.index
    print(f"[{a.provider}/{a.model}]  {a.usage.input_tokens:,} in / "
          f"{a.usage.output_tokens:,} out"
          + (f"  ${a.usage.cost_usd:.4f}" if a.usage.cost_usd is not None else ""))
    print()
    if "answer" in idx:
        print(idx["answer"])
        print()
        for c in idx.get("citations") or []:
            print(f"  [{c.get('timestamp')}] {c.get('observation')}")
        if idx.get("caveats"):
            print("\n  caveats:")
            for c in idx["caveats"]:
                print(f"    - {c}")
        print(f"\n  confidence: {idx.get('confidence')}")
    else:
        print(idx.get("summary", ""))
        print(f"  kind: {idx.get('content_kind')}")
        print()
        for sc in idx.get("scenes") or []:
            print(f"  {sc.get('start')}-{sc.get('end')}  {sc.get('description')}")
            for t in sc.get("on_screen_text") or []:
                if isinstance(t, dict):
                    leg = t.get("legibility", "?")
                    if leg == "illegible" or not t.get("text"):
                        print(f"      text: (illegible) {t.get('where', '')}")
                    else:
                        mark = "" if leg == "clear" else f" [{leg}]"
                        print(f"      text: \"{t.get('text')}\"{mark}  {t.get('where', '')}")
                else:
                    print(f"      text: \"{t}\"")
        if idx.get("speech"):
            print("\n  speech:")
            for s in idx["speech"][:20]:
                print(f"    [{s.get('start')}] {s.get('text')}")
        if idx.get("key_moments"):
            print("\n  key moments:")
            for k in idx["key_moments"]:
                print(f"    [{k.get('timestamp')}] {k.get('why')}")
        if idx.get("uncertainties"):
            print("\n  uncertain:")
            for u in idx["uncertainties"]:
                print(f"    - {u}")
    if a.sheet:
        print(f"\n  contact sheet: {a.sheet.path}")
        print(f"    LOOK AT THIS FILE YOURSELF before you rely on anything above.")
        print(f"    {len(a.sheet.frame_times)} frames, ~{a.sheet.approx_tokens} tokens.")


def cmd_index(args) -> int:
    runs = []
    a = None
    for i in range(max(1, args.verify)):
        a = analyse(args.source, provider_name=args.provider, model=args.model,
                    fps=args.fps, want_audio=(False if args.no_audio else None),
                    sheet_path=args.sheet, deep_signals=args.deep and i == 0,
                    approved=args.yes)
        runs.append(a.index)
    payload = a.to_dict()
    if len(runs) > 1:
        from .consistency import check_text_claims, summarise
        payload["consistency"] = summarise(check_text_claims(runs))

    def human(_):
        _print_analysis(a)
        c = payload.get("consistency")
        if c and c["claims"]:
            print()
            print(f"  consistency over {len(runs)} runs:")
            for cl in c["claims"]:
                mark = "stable  " if cl["verdict"] == "stable" else "UNSTABLE"
                print(f"    {mark} \"{cl['text']}\"  ({cl['seen_in']}/{cl['of']} runs)")
            print(f"    {c['note']}")
    _out(payload, args.json, human)
    return 0


def cmd_ask(args) -> int:
    answers, a = [], None
    for _ in range(max(1, args.verify)):
        a = analyse(args.source, provider_name=args.provider, model=args.model,
                    fps=args.fps, want_audio=(False if args.no_audio else None),
                    question=args.question, sheet_path=args.sheet, approved=args.yes)
        answers.append(a.index.get("answer", ""))
    payload = a.to_dict()
    if len(answers) > 1:
        from .consistency import agree
        payload["agreement"] = agree(answers).to_dict()

    def human(_):
        _print_analysis(a)
        ag = payload.get("agreement")
        if ag:
            print()
            print(f"  agreement: {ag['verdict']} "
                  f"({ag['votes']}/{ag['trials']} runs matched)")
            for v in ag["variants"]:
                print(f"    differing run said: {v[:160]}")
            if ag["verdict"] == "unreliable":
                print("    The model did not answer the same way twice. Do not rely on this;")
                print("    export a still and look yourself.")
    _out(payload, args.json, human)
    return 0


def cmd_clip(args) -> int:
    """T3 — frame-exact deep dive on one window. Costs a few hundred tokens."""
    from .providers import pick_provider
    from .schema import ANSWER_PROMPT, ANSWER_SCHEMA, INDEX_PROMPT, VIDEO_INDEX_SCHEMA
    start = normalise_timestamp(args.start)
    end = normalise_timestamp(args.end)
    if start < 0 or end < 0 or end <= start:
        raise LLVideoError(f"Bad window: --start {args.start} --end {args.end}")

    url = is_url(args.source)
    prov = pick_provider(args.provider, needs_upload=not url, needs_clipping=True)
    file_uri = None
    if not url and prov.name == "gemini":
        cache = UploadCache()
        key = UploadCache.fingerprint(args.source)
        hit = cache.get(key)
        if hit and prov.file_alive(hit["name"]):
            file_uri = hit["uri"]
        else:
            info = prov.upload(args.source)
            cache.put(key, info["name"], info["uri"], info.get("mimeType", "video/mp4"))
            file_uri = info["uri"]

    prompt = (ANSWER_PROMPT + args.question) if args.question else INDEX_PROMPT
    schema = ANSWER_SCHEMA if args.question else VIDEO_INDEX_SCHEMA
    r = prov.analyse(args.source, prompt, schema, is_url=url, start=start, end=end,
                     fps=args.fps, model=args.model, file_uri=file_uri)

    payload = {"window": {"start": start, "end": end}, "result": r.data,
               "provider": r.provider, "model": r.model,
               "usage": {"input_tokens": r.usage.input_tokens,
                         "output_tokens": r.usage.output_tokens,
                         "cost_usd": r.usage.cost_usd}}

    def human(_):
        print(f"[{_fmt_ts(start)} - {_fmt_ts(end)}]  {r.usage.input_tokens:,} tokens"
              + (f"  ${r.usage.cost_usd:.4f}" if r.usage.cost_usd is not None else ""))
        print()
        if "answer" in r.data:
            print(r.data["answer"])
            for c in r.data.get("citations") or []:
                print(f"  [{c.get('timestamp')}] {c.get('observation')}")
        else:
            for sc in r.data.get("scenes") or []:
                print(f"  {sc.get('start')}-{sc.get('end')}  {sc.get('description')}")
    _out(payload, args.json, human)
    return 0


def cmd_sheet(args) -> int:
    """T4 — build a contact sheet for the agent to read with its own eyes."""
    pr = P.probe(args.source)
    if args.at:
        times = [normalise_timestamp(t) for t in args.at.split(",")]
        times = [t for t in times if t >= 0]
    else:
        cuts = P.detect_scenes(args.source) if not args.no_scenes else []
        # A fixed floor is too coarse for short clips: 7s over a 20s video gives
        # three frames. Scale it down so a short video still gets real coverage.
        interval = args.interval
        if interval is None:
            interval = max(1.0, min(7.0, pr.duration / 8))
        times = P.select_frame_times(pr.duration, cuts,
                                     floor_interval=interval, max_frames=args.max_frames)
    out = args.out or str(scratch_dir() / f"sheet_{Path(args.source).stem}.jpg")
    s = F.contact_sheet(args.source, times, out, tile_width=args.tile, cols=args.cols)
    payload = {"path": s.path, "frame_times": s.frame_times, "cols": s.cols, "rows": s.rows,
               "width": s.width, "height": s.height, "approx_tokens": s.approx_tokens,
               "frames": len(s.frame_times)}

    def human(_):
        print(f"{s.path}")
        print(f"  {len(s.frame_times)} frames  {s.cols}x{s.rows} grid  {s.width}x{s.height}")
        print(f"  ~{s.approx_tokens} tokens  (vs ~{len(s.frame_times) * 1100} as separate images)")
        print(f"  times: {', '.join(_fmt_ts(t) for t in s.frame_times)}")
    _out(payload, args.json, human)
    return 0


def cmd_stills(args) -> int:
    times = [normalise_timestamp(t) for t in args.at.split(",")]
    times = [t for t in times if t >= 0]
    outdir = Path(args.out or scratch_dir() / "stills")
    outdir.mkdir(parents=True, exist_ok=True)
    jpegs = F.frames_at(args.source, times, width=args.width, quality=2)
    paths = []
    for t, j in zip(times, jpegs):
        p = outdir / f"{_fmt_ts(t).replace(':', '-')}.jpg"
        p.write_bytes(j)
        paths.append(str(p))
    payload = {"paths": paths, "times": times}
    _out(payload, args.json, lambda _: [print(p) for p in paths])
    return 0


def cmd_signals(args) -> int:
    """T0 only — measured facts, zero tokens, no network."""
    pr = P.probe(args.source)
    out = {
        "probe": pr.to_dict(),
        "scene_cuts": P.detect_scenes(args.source, args.threshold) if pr.has_video else [],
        "black": P.detect_black(args.source) if pr.has_video else [],
        "freeze": P.detect_freeze(args.source) if pr.has_video else [],
        "silence": P.detect_silence(args.source) if pr.has_audio else [],
    }
    out["note"] = ("Scene detection never catches the first cut, and can miss interior cuts "
                   "between visually similar shots. Treat it as a hint, never a complete list.")

    def human(_):
        print(f"scene cuts ({len(out['scene_cuts'])}): "
              f"{', '.join(_fmt_ts(t) for t in out['scene_cuts']) or 'none'}")
        for label in ("black", "freeze", "silence"):
            ev = out[label]
            if ev:
                print(f"{label} ({len(ev)}):")
                for e in ev[:10]:
                    print(f"  {_fmt_ts(e['start'])} -> "
                          f"{_fmt_ts(e['end']) if e.get('end') else '?'}")
        print(f"\n! {out['note']}")
    _out(out, args.json, human)
    return 0


def cmd_transcribe(args) -> int:
    from .transcribe import transcribe
    r = transcribe(args.source, model=args.model_size, language=args.language)
    _out(r, args.json, lambda _: [
        print(f"[{_fmt_ts(s['start'])}] {s['text'].strip()}") for s in r["segments"]])
    return 0


def cmd_providers(args) -> int:
    from .providers import GeminiProvider, OpenRouterProvider
    g, o = GeminiProvider(), OpenRouterProvider()
    info = {
        "gemini": {"available": g.available(), "model": g.model,
                   "uploads": True, "max_bytes": g.max_bytes,
                   "clipping": True, "fps_control": True},
        "openrouter": {"available": o.available(), "model": o.model,
                       "uploads": False, "max_bytes": o.max_bytes,
                       "clipping": False, "fps_control": False},
    }
    if o.available():
        info["openrouter"]["credits"] = o.credit_status()

    def human(_):
        for name, d in info.items():
            mark = "ok " if d["available"] else "-- "
            print(f"{mark}{name}")
            if not d["available"]:
                print(f"     no API key found")
                continue
            print(f"     model      {d['model']}")
            if d["uploads"]:
                cap = f"yes, up to {d['max_bytes'] / 1024 ** 3:.0f} GB"
            else:
                cap = f"no - inline only, {d['max_bytes'] / 1024 ** 2:.0f} MB max"
            print(f"     uploads    {cap}")
            print(f"     clip/fps   {'yes' if d['clipping'] else 'no'}")
            c = d.get("credits")
            if c and not c.get("video_ready"):
                print(f"     ! {c['reason']}")
    _out(info, args.json, human)
    return 0


def cmd_clean(args) -> int:
    r = cleanup(delete_uploads=args.uploads)
    _out(r, args.json, lambda _: print(
        f"removed {r['files']} files ({r['bytes'] / 1e6:.1f} MB)"
        + (f", {r['remote']} remote uploads" if r["remote"] else "")))
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="llvideo",
        description="Give a coding agent real video understanding.")
    ap.add_argument("--version", action="version", version=f"llvideo {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, source=True):
        if source:
            p.add_argument("source", help="video file path or URL")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    def modelargs(p):
        p.add_argument("--provider", choices=["gemini", "openrouter"], default=None)
        p.add_argument("--model", default=None)
        p.add_argument("--fps", type=float, default=None,
                       help="frames per second to sample (default 1.0; lower is cheaper)")
        p.add_argument("--no-audio", action="store_true",
                       help="ignore the audio track (saves 32 tokens/sec)")
        p.add_argument("--yes", action="store_true", help="approve a job over $1.00")
        p.add_argument("--sheet", default=None, help="where to write the contact sheet")
        p.add_argument("--verify", type=int, default=1, metavar="N",
                       help="run the query N times and report which readings are stable "
                            "(3 is a good value when on-screen text matters)")
        return p

    p = common(sub.add_parser("probe", help="container and stream facts, zero cost"))
    p.set_defaults(func=cmd_probe)

    p = modelargs(common(sub.add_parser("plan", help="what this would cost, before spending")))
    p.set_defaults(func=cmd_plan)

    p = modelargs(common(sub.add_parser("index", help="full structured index of the video")))
    p.add_argument("--deep", action="store_true", help="also run black/freeze/silence detection")
    p.set_defaults(func=cmd_index)

    p = modelargs(common(sub.add_parser("ask", help="ask one question about the video")))
    p.add_argument("-q", "--question", required=True)
    p.set_defaults(func=cmd_ask)

    p = modelargs(common(sub.add_parser("clip", help="deep dive on one time window")))
    p.add_argument("--start", required=True, help="MM:SS or seconds")
    p.add_argument("--end", required=True, help="MM:SS or seconds")
    p.add_argument("-q", "--question", default=None)
    p.set_defaults(func=cmd_clip)

    p = common(sub.add_parser("sheet", help="contact sheet for the agent to read itself"))
    p.add_argument("--at", default=None, help="comma-separated timestamps")
    p.add_argument("--out", default=None)
    p.add_argument("--tile", type=int, default=360, help="tile width px (768 for dense text)")
    p.add_argument("--cols", type=int, default=None)
    p.add_argument("--interval", type=float, default=None,
                   help="uniform floor in seconds (default: adaptive, at most 7s — the "
                        "floor carries coverage because scene detection only reaches "
                        "~50%% recall on real footage)")
    p.add_argument("--max-frames", type=int, default=16)
    p.add_argument("--no-scenes", action="store_true")
    p.set_defaults(func=cmd_sheet)

    p = common(sub.add_parser("stills", help="export full-size frames at timestamps"))
    p.add_argument("--at", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--width", type=int, default=1280)
    p.set_defaults(func=cmd_stills)

    p = common(sub.add_parser("signals", help="measured ffmpeg facts, zero tokens"))
    p.add_argument("--threshold", type=float, default=0.12)
    p.set_defaults(func=cmd_signals)

    p = common(sub.add_parser("transcribe", help="local word-level transcript, free"))
    p.add_argument("--model-size", default="small",
                   help="small is the only practical size on CPU (3.7x realtime)")
    p.add_argument("--language", default=None)
    p.set_defaults(func=cmd_transcribe)

    p = common(sub.add_parser("providers", help="which backends are usable"), source=False)
    p.set_defaults(func=cmd_providers)

    p = common(sub.add_parser("clean", help="delete scratch files"), source=False)
    p.add_argument("--uploads", action="store_true",
                   help="also delete files already uploaded to the provider")
    p.set_defaults(func=cmd_clean)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LLVideoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
