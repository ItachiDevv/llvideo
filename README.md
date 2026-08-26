# llvideo

**Give any coding agent real video understanding.**

LLM coding harnesses read images. None of them read video. The usual workaround —
extract a few hundred frames to disk and feed them in one by one — is expensive, fills
the disk, and throws away time itself: you get a slideshow, not a video.

llvideo hands the video to a model that actually watches it, makes that model return
**structured timestamped evidence instead of prose**, and then gives the agent real
frames to look at with its own eyes.

Works with Claude Code, Codex, or any agent that can run a shell command.

```bash
llvideo index recording.mp4
llvideo ask clip.mp4 -q "what error message appears?"  --verify 3
llvideo craft edit.mp4                       # transitions, camera moves, pacing
llvideo audit out.mp4                        # QA your own render before shipping
llvideo audit out.mp4 --from-project ./proj  # diff it against what the project meant
llvideo ask "https://youtube.com/watch?v=..." -q "when do they announce the price?"
```

---

## Why not just extract frames

Measured on a real 10-minute video, on the same machine:

| | frames-to-disk, 2 fps | llvideo |
|---|---|---|
| tokens | ~1,200,000 | **61,800** |
| files written | ~1,200 JPEGs | **0** |
| cost | — | **$0.046** |
| temporal context | lost | preserved |
| audio | ignored | transcribed with timestamps |

**19× cheaper at default sampling, 43× at `--fps 0.2`, and it writes nothing to disk.**

Token cost is a flat **71/sec for video + 32/sec for audio**, linear and identical
across every Gemini 3.x model — so pick a model on price alone.

---

## Install

```bash
git clone https://github.com/ItachiDevv/llvideo
cd llvideo
bash install.sh
```

Installs the CLI and deploys the agent wrappers to `~/.claude/skills/llvideo/` and
`~/.agents/skills/llvideo/`.

**Requires** `ffmpeg` on PATH and one API key:

| key | uploads local files | clipping / fps control | price |
|---|---|---|---|
| `GEMINI_API_KEY` | yes, up to 2 GB | yes | $0.75/M in |
| `OPENROUTER_API_KEY` | no — URLs and small clips only | no | $0.375/M in |

OpenRouter is half the price but requires **$1.00 of purchased credit** before it will
accept any video request, including on `:free` models. Keys are read from the
environment, then from `~/.itachi-api-keys`.

Optional: `pip install Pillow faster-whisper` for exact sheet dimensions and local
word-level transcription.

---

## What it does

**Structured, not chatty.** Every call returns typed JSON — scenes with start/end
timestamps, on-screen text, actions, speech, key moments, and an explicit
`uncertainties` list. An agent reasons over that. It cannot reason over a paragraph.

**The agent still looks.** Every `index` writes a contact sheet: the whole timeline
tiled into one image, timestamps burned in, ~600–1,600 tokens. The agent reads it
directly. Claims get grounded in pixels, not in another model's opinion.

**Exact facts come from ffmpeg.** Scene cuts, black frames, freezes and silence are
measured, not estimated — `llvideo signals` is millisecond-exact and costs nothing.

**It knows when it is guessing.** See below.

---

## Auditing your own renders

You just rendered a video you have never watched. `audit` checks it, free:

```bash
llvideo audit out.mp4
llvideo audit out.mp4 --spec intent.json   # diff against what you asked for
```

Black or white first/last frame (the most common export bug), black gaps, freezes,
EBU R128 loudness and true-peak clipping, silence, frame-rate mismatch, title-safe
margins. No model, no tokens. Exit 1 on a blocker, so it drops into CI.

Verified: catches 5 of 5 planted defects, and returns CLEAN with zero findings on real
professional footage. Every finding is labelled **measured** (ffmpeg, with the number)
or **judged** (a model's opinion).

## Auditing a video you made

HyperFrames, brag and Remotion all record what they meant to build. `llvideo` reads it
and diffs the rendered file against it — no hand-written spec:

```bash
llvideo audit render.mp4 --from-project ./my-project
```

Scene timing comes from HyperFrames `data-start`/`data-duration` or Remotion
`<Sequence from= durationInFrames=>`; transition types from `STORYBOARD.md`. Values
that cannot be read statically (a `springTiming` duration, a computed `from=`) are
flagged and skipped rather than guessed.

**This fills a real gap.** HyperFrames `check` validates the pre-render timeline in
headless Chrome, brag stops at pre-render snapshots, and Remotion has no gate at all —
none of them inspect the exported MP4. An encoder bug or a stale render ships unseen.

## Analysing the edit, not the content

`craft` answers "how is this cut" — transitions with exact durations, shot sizes,
camera moves, pacing statistics, colour and lighting.

It cannot work by sampling the timeline at 1 fps, because a hard cut occupies one
frame. So it finds candidate transitions locally for free, then re-examines each one at
8 fps, and **measures brightness with ffmpeg** to separate a fade-through-black from a
crossfade. Verified against known transitions: 4/4 types correct, durations within 0.2s.

It also refuses the classic trap. On a continuous handheld take containing a whip pan it
reported one shot and zero transitions, instead of mistaking fast camera movement for an
edit — which is exactly what a sparse frame sample does.

## The on-screen text problem

Vision models read blurred text confidently and get it wrong. Same motion-blurred car
dashboard, asked three times:

```
call 1:  "642"   "55 F"   "330 mi"
call 2:  "23.9"  "65 F"   "330"
call 3:  "6:42"  "55 F"   "330 mi"
```

No single answer reveals the problem. So don't take a single answer:

```bash
llvideo index video.mp4 --verify 3
```

```
UNSTABLE "330"   (1/3 runs)
stable   "55 F"  (3/3 runs)
UNSTABLE "642"   (2/3 runs)
```

Stable readings were identical every run. Unstable ones were not, and are not
trustworthy. Use `--verify 3` whenever the text matters — error messages, prices,
code on screen, dashboard numbers. Each run costs about the same as the first.

---

## Commands

| command | cost | what it does |
|---|---|---|
| `probe` | free | duration, codec, resolution, token estimate |
| `plan` | free | sampling decision, transcode need, dollar estimate |
| `signals` | free | scene cuts, black, freeze, silence — exact |
| `sheet` | free | contact sheet to look at |
| `stills` | free | full-resolution frames at timestamps |
| `transcribe` | free | local word-level transcript, CPU only |
| `index` | ~$0.005/min | full structured timeline + contact sheet |
| `ask -q` | ~$0.005/min | one question, answered with citations |
| `craft` | ~$0.02/min | transitions, camera moves, shots, pacing, grade |
| `audit` | **free** | render QA: black frames, loudness, dead air, spec diff |
| `spec` | **free** | extract intent from a HyperFrames / brag / Remotion project |
| `clip --start --end` | ~$0.003 | frame-exact deep dive on a window |
| `providers` | free | which backends are usable |
| `clean` | free | delete scratch files and uploads |

All support `--json`.

---

## Long videos

| duration | tokens | cost |
|---|---|---|
| 10 min | 61,800 | $0.046 |
| 1 hour | 370,800 | $0.28 |
| 2 hours | 741,600 | $0.56 |
| 2h 50m | ~1M — single-call ceiling | |

Past the ceiling, `plan` lowers the sampling rate automatically, then splits into
segments and merges the indexes. `--fps 0.2` cuts video tokens five-fold. `--no-audio`
removes the 32/sec audio floor that no sampling rate can touch — worth 230,000 tokens
on a 2-hour video.

Large files are transcoded to a small proxy first. This is not an optimisation: the
upload API caps at 2 GB, so a 6 GB recording cannot be analysed at all without it. A
235 MB 4K clip becomes 1.2 MB in three seconds, with no loss of analysable content —
tokens scale with duration, not bytes.

---

## Storage

Disk use is **O(1), never O(video length)**. Frames bound for an API never touch disk;
ffmpeg pipes JPEG to stdout and the stream is split on SOI/EOI markers. Only frames the
agent needs to *look at* get written.

```bash
llvideo clean --uploads   # remove scratch files and delete remote uploads
```

Uploads expire after 48 hours on their own, and are reused inside that window so a
follow-up question costs nothing extra.

**Privacy:** analysing a local file uploads it to the provider for up to 48 hours. For
sensitive content, stay local — `signals`, `sheet`, `stills` and `transcribe` never
upload anything.

---

## Known limits

- **Scene detection is a hint, not a list.** Precision is perfect on real footage
  (55s of handheld night driving peaked at 0.047 — no false positives), but **recall is
  only ~50%**: it structurally cannot detect the first cut, and some real cuts score
  below any usable threshold. Frame selection unions scene hits with a uniform floor
  every 7 seconds, and the floor carries most of the coverage.
- **A sparse contact sheet does not prove a cut.** A fast camera pan looks like an edit
  at 1 fps. Verify suspected cuts with `signals` or denser sampling.
- **Audio event timestamps drift about ±1s.** For frame-exact audio work use
  `signals` (silencedetect) or `transcribe`.
- **Local transcription: `small` only.** Benchmarked on a 20-core CPU: `small` runs at
  3.73× realtime, `medium` at 0.82×, `large-v3` at 0.18×. The larger models are slower
  than the video itself.
- **No video generation.** Deliberately out of scope.

## License

MIT.
