---
name: llvideo
description: >
  Understand video with context — what happens, when, who says what, what is on screen.
  Use for any .mp4/.mov/.mkv/.webm/.avi file or YouTube URL, and for questions like
  "what happens in this video", "analyse this clip", "find the moment where X",
  "what does the screen say at 2:14", "is this render correct", "QA this export",
  "check this screen recording", or "summarise this footage". Also use to verify a
  rendered video you just produced (Remotion, HyperFrames, Veo, ffmpeg output), to
  extract stills or clips, and to measure FPS, dropped frames, black frames, freezes
  and silence exactly. Runs a video-native model for the timeline and gives YOU the
  frames to look at yourself. Do not extract frames by hand — use this.
---

# llvideo

You cannot watch video. A video-native model can. This skill puts one behind a CLI,
makes it return **structured evidence instead of prose**, and then hands you real
frames so you form your own judgement.

That distinction is the whole point. If you relay a model's summary, you are a
middleman. If you reason over a typed index and confirm it against pixels you
looked at, you are the analyst.

## Setup check

```bash
llvideo providers
```

Needs `ffmpeg` on PATH and one API key. `GEMINI_API_KEY` handles everything.
`OPENROUTER_API_KEY` is about half the price but has no upload endpoint, so it is
URL-and-small-clip only — and OpenRouter requires **$1.00 of purchased credit** before
it will accept any video request, including on `:free` models.

Keys are read from the environment, then from `~/.itachi-api-keys`.

---

## The loop you should follow

### 1. Always start free

```bash
llvideo probe VIDEO      # duration, codec, resolution, token estimate — costs nothing
llvideo plan VIDEO       # what it will cost and how it will be handled — costs nothing
```

`plan` tells you the sampling rate, whether a transcode is needed, the token count
and the dollar estimate. Read it before spending.

### 2. Index the video

```bash
llvideo index VIDEO
llvideo index VIDEO --json        # for parsing
llvideo index VIDEO --verify 3    # when on-screen text matters — see below
```

Returns scenes with timestamps, on-screen text, actions, speech, key moments, and an
explicit `uncertainties` list. It also writes a **contact sheet**.

### 3. LOOK AT THE CONTACT SHEET. This step is not optional.

The `index` output ends with a path. **Read that image with your own vision before you
answer the user.** It is one image covering the whole timeline for roughly 600–1,600
tokens. You are cheap to ground and expensive to be wrong.

Everything you assert should be one of:
- something you saw yourself in a frame,
- an ffmpeg measurement, or
- a transcript quote.

Anything else must be attributed: *"the index reports…"*. Never launder a model's
guess into your own voice.

### 4. Go deeper where it matters

```bash
llvideo clip VIDEO --start 02:10 --end 02:40 --fps 4 -q "does the logo animate cleanly?"
llvideo stills VIDEO --at 02:14,02:19 --width 1920   # full-res, look at these yourself
llvideo sheet VIDEO --at 01:00,01:05,01:10 --tile 768 --cols 2   # dense UI text
```

Clipping is frame-exact and costs a few hundred tokens. Use it freely.

---

## On-screen text is the failure mode. Take it seriously.

A model will read blurred text confidently and get it wrong. Measured on real
footage — the same motion-blurred car dashboard, asked three times:

```
call 1:  "642"   "55 F"   "330 mi"
call 2:  "23.9"  "65 F"   "330"
call 3:  "6:42"  "55 F"   "330 mi"
```

Nothing in any single answer flags the problem. So do not rely on a single answer:

```bash
llvideo index VIDEO --verify 3
```

This runs the query three times and reports which readings are **stable** (identical
every run) and which are **UNSTABLE**. Treat unstable readings as "a display is
present but not legible". If an unstable reading matters, export a full-resolution
still and read it yourself:

```bash
llvideo stills VIDEO --at 00:11 --width 1920
```

Use `--verify 3` whenever the answer will be acted on: error messages in a screen
recording, numbers on a dashboard, code on screen, prices, names. Each extra run
costs about the same as the first — pennies.

---

## Exact facts come from ffmpeg, never from the model

```bash
llvideo signals VIDEO
```

Zero tokens, no network. Returns scene cuts, black frames, freezes and silence.
`blackdetect`, `freezedetect` and `silencedetect` are exact to the millisecond. The
model's audio-event timestamps drift by about a second, so for anything frame-exact —
FPS, dropped frames, A/V sync, a black flash — use `signals`, not the index.

**Scene detection is a hint, never a complete list.** Measured on real footage:
precision is perfect (55 seconds of handheld night driving, whip pans and headlight
flare, peaked at 0.047 — nothing false-positives), but **recall is only about 50%**.
It structurally cannot detect the first cut, and some real cuts score below any usable
threshold. The frame chooser therefore unions scene hits with a **uniform floor every
7 seconds**, and the floor does most of the coverage work. Never treat a bare cut list
as complete.

A related trap: a sparse contact sheet is **not** proof of a cut. A fast whip-pan looks
exactly like an edit at 1 frame per second. Confirm suspected cuts with `signals` or a
denser sample before believing your own eyes.

---

## Audio

The index includes speech with timestamps, which is enough for context. When you need
**word-level** timing, run the local transcriber — free, no network, no GPU:

```bash
llvideo transcribe VIDEO
```

`small` is the default and the only practical size on CPU (3.7× realtime; a 10-minute
track takes about 2.7 minutes). `medium` is 0.82× and `large-v3` is 0.18× — slower than
the video itself. The CLI warns if you pick one.

Needs `pip install faster-whisper`.

---

## Long videos

Token cost is **71/sec for video plus 32/sec for audio**, linear and identical on every
Gemini 3.x model. That gives hard ceilings for a single call:

| duration | tokens | cost |
|---|---|---|
| 10 min | 61,800 | ~$0.046 |
| 1 hour | 370,800 | ~$0.28 |
| 2 hours | 741,600 | ~$0.56 |
| **2h 50m** | **~1M — the ceiling** | |

Past that, `plan` automatically lowers the sampling rate, and past that again it splits
into segments and merges the indexes. You can force it: `--fps 0.2` cuts video tokens
five-fold; `--no-audio` removes the 32/sec floor that no sampling rate can touch.

Audio is a floor — on a 2-hour video it is 230,000 tokens on its own. Drop it when the
question is purely visual.

---

## Cost bands — when to just proceed

- **under $0.10** — proceed. Do not ask. Mention the actual cost afterwards.
- **$0.10 to $1.00** — proceed, and say the estimate in the same message.
- **over $1.00** — stop and ask. The CLI refuses without `--yes`.

A ten-minute video is four cents. Asking permission for that wastes more of the user's
time than the money is worth.

---

## Storage

Disk use is **O(1), never O(video length)**. Frames bound for an API never touch disk at
all. Only frames you need to *look at* get written, into a scratch directory.

```bash
llvideo clean              # delete scratch files
llvideo clean --uploads    # also delete files already uploaded to the provider
```

Run `clean --uploads` when the user's video was private. Uploads otherwise expire after
48 hours on their own, and are reused inside that window so a second question is free.

**Tell the user once per session** that a local file gets uploaded to the provider and
lives there for 48 hours. If the content is sensitive, stay local: use `signals`,
`sheet`, `stills` and `transcribe`, which never upload anything.

---

## Verifying video you just made

This is the closed loop for renders. If you produced an MP4 — Remotion, ffmpeg, Veo —
you have never seen it. Check it:

```bash
llvideo index out.mp4 --deep
llvideo signals out.mp4          # black frames, freezes, silence — exact
```

**For a HyperFrames composition you are still building, use `hyperframes` instead**:
`hyperframes snapshot --at 3.0,10.5 --describe "is the logo clipped?"` reads the live
DOM without a render, which is better than anything this skill can do on an encoded
file. Use llvideo on the **rendered output**; use hyperframes on the **live composition**.

---

## Command reference

| command | cost | what it does |
|---|---|---|
| `probe` | free | duration, codec, resolution, token estimate |
| `plan` | free | sampling decision, transcode need, dollar estimate |
| `signals` | free | scene cuts, black, freeze, silence — exact |
| `sheet` | free | contact sheet for you to read yourself |
| `stills` | free | full-resolution frames at timestamps |
| `transcribe` | free | local word-level transcript |
| `index` | ~$0.005/min | full structured timeline + contact sheet |
| `ask -q` | ~$0.005/min | one question, answered with citations |
| `clip --start --end` | ~$0.003 | frame-exact deep dive on a window |
| `providers` | free | which backends are usable |
| `clean` | free | delete scratch files and uploads |

Every command accepts `--json`.

## Flags worth knowing

- `--verify 3` — run it three times, report which readings are stable. Use for text.
- `--fps 0.2` — sample five times less video. Cheaper; still catches scenes.
- `--no-audio` — skip the audio track entirely.
- `--provider gemini|openrouter` — force a backend.
- `--tile 768 --cols 2` — bigger tiles for dense UI text. Default 360 needs source
  text of about 40px at 1080p to stay readable.
- `--yes` — approve a job over $1.00.
