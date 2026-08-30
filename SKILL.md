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

## The best view for understanding — `timeline`

```bash
llvideo timeline VIDEO
llvideo timeline VIDEO --transcribe    # word-exact speech timing
```

`index` returns `scenes`, `speech` and `audio_events` as three separate arrays. Each
is accurate, but nothing connects them — so "a close-up of the dashboard" and "he says
the range is wrong" sit in different lists, and the fact that they happen at the same
moment is left for you to reconstruct.

**That co-occurrence is the context.** A shot means something different depending on
what is being said over it. `timeline` interleaves everything onto one track:

```
00:10-00:18  (8.0s)   *KEY*
    shot   close-up facing driver
    see    Camera returns to the driver as a lit gas station glides past the window
    does   The vehicle drives past the illuminated canopy
    says   "The gas station appears at nine seconds into the clip."
    hear   Low interior car ambient hum
    why    The gas station canopy passes directly behind the driver
```

Use this over `index` whenever the question is about meaning rather than inventory.

**`--transcribe` fuses a local word-level transcript** instead of the model's speech.
The model's audio timestamps drift about a second, which is enough to attach a line to
the wrong shot at a fast cut. Speech is assigned to the beat it overlaps *most*, not
the first one it touches, so a line straddling a cut lands where it was mostly spoken.

It reports **coverage** and names any stretch of runtime nothing was said about. A
timeline with holes missed something, and saying so beats presenting a partial account
as complete.

### Follow-up questions are free

The index is cached for 48 hours against content identity and sampling settings.
Measured: a second `timeline` on the same video went from **12.5s and $0.02 to 0.4s and
nothing**. Ask as many questions as you need — iterating is how understanding is built,
and it should not cost anything after the first pass. `--no-cache` forces a fresh run.

---

## Repairing what you found — `fix`

Most audit findings do not need anything regenerated. Wrong loudness, a clipped peak,
a black frame on the head or tail — those are deterministic ffmpeg operations that
cost nothing and cannot hallucinate.

```bash
llvideo audit render.mp4        # find it
llvideo fix render.mp4          # repair it -> render_fixed.mp4
```

**Repaired automatically:** integrated loudness (two-pass `loudnorm` to −14 LUFS),
true-peak clipping (held at −1.0 dBFS), and black or white first/last frames — trimmed
by measuring where the black actually ends, not by assuming a frame count.

**Deliberately NOT repaired**, each with a stated reason:

| finding | why not |
|---|---|
| black gap mid-timeline | an editing decision, not a defect to silently delete |
| freeze | a held frame may be intentional — shorten it in the timeline |
| low resolution | upscaling invents detail that was never captured |
| safe margin | layout is a composition choice; move the element, do not crop |
| duration / aspect | set at render time; trimming to fit would cut content |

`fix` **re-audits afterwards and reports before/after counts**, because claiming a
repair worked without re-measuring is the habit this tool exists to break. Measured on
a deliberately broken render: 5 major + 1 minor became 1 major + 1 minor, and the two
left standing were the two that need a human decision.

It never overwrites the source. Output goes to `<name>_fixed.mp4` unless you pass `--out`.

---

## Generating supplemental clips — `gen`

Short clips that slot into a timeline built elsewhere: an establishing shot, b-roll,
texture, a logo sting. Not whole films.

```bash
llvideo gen "slow aerial push over a misty pine forest at dawn" --seconds 5
llvideo gen "logo settles into frame" --image logo.png --seconds 3    # image-to-video
llvideo gen "same scene, colder grade" --video clip.mp4 --seconds 5   # video-to-video
llvideo gen-image "weathered wood title card" --out title.png
```

Runs on **Grok Imagine** through your existing `XAI_API_KEY`. Note this is xAI's Grok,
not Groq — different company. **1 to 15 seconds** in one-second steps, which is more
flexible than Veo's fixed 4/6/8.

| resolution | measured cost |
|---|---|
| 480p | $0.05/s |
| 720p | $0.07/s |
| 1080p | ~$0.11/s (extrapolated, not measured) |

Those rates came from the API's own usage field on real jobs, not from documentation.
Anything over $1.00 needs `--yes`.

**Every clip is audited on arrival**, and that is not decoration. Grok's own defaults
produce 848x480 at about -21 LUFS — both of which the auditor flags. `gen` defaults to
720p for that reason. Then look at it yourself:

```bash
llvideo sheet clip.mp4
```

`--audio` switches to `grok-imagine-video-1.5`, which generates a soundtrack.
Generated audio tends to come back quiet; expect a loudness finding and normalise in
post if the clip is going next to other material.

---

## Auditing a video you MADE — `spec` + `--from-project`

This is the whole loop. HyperFrames, brag and Remotion all already know what they
meant to build, so nobody should hand-write an intent spec:

```bash
llvideo audit render.mp4 --from-project ./my-hyperframes-project
llvideo audit brag-output/brag.mp4 --from-project ./brag-output
llvideo audit out.mp4 --from-project ./my-remotion-project
```

Or extract the spec on its own to inspect or edit it:

```bash
llvideo spec ./project --out intent.json
```

**Where the intent comes from, and how much to trust it:**

| Source | What is extracted | Confidence |
|---|---|---|
| HyperFrames `index.html` | scene start/end from `data-start` / `data-duration`, on-screen text from the DOM | exact |
| HyperFrames `STORYBOARD.md` | transition type and duration per frame (`transition_in`) | authored intent |
| brag | reads the HyperFrames project it builds under `composition/`; cross-checks `brag-plan.md` scene count and total | exact + cross-check |
| Remotion `.tsx` | `<Sequence from= durationInFrames=>` ÷ fps; `<TransitionSeries.Transition presentation=>` | exact for literal and `N * fps`; computed values are **skipped, not guessed** |

Anything that cannot be read statically is reported as a note rather than assumed —
a `springTiming` duration or a `from={computeIt()}` is flagged and excluded.

**Why this exists.** HyperFrames `check` validates the seeked composition timeline in
headless Chrome. brag's delivery gate stops at pre-render snapshots. Remotion has no
equivalent gate at all. **None of them look at the exported MP4**, so an encoder bug,
a stale render, or dropped frames at export ship unnoticed. That gap is what this
fills — and it is why `audit` is worth running even when the project's own checks pass.

A fade-to-black the spec asked for is not reported as a black-frame defect. Only
undeclared black is a finding.

---

## Transitions, camera and pacing — `craft`

For "how is this cut", "what transitions are used", "analyse the edit", "what is the
camera doing", "why does this feel fast" — use `craft`, not `index`.

```bash
llvideo craft VIDEO
llvideo craft VIDEO -q "does the cut at 00:14 land on the beat?"
```

`index` samples the whole video at 1 fps, which is fine for *what happens* and useless
for *how it is cut*: a hard cut occupies a single frame, so at 1 fps you see the shots
either side and never the transition. `craft` works differently:

1. A free local pass scores every frame and finds candidate transitions. This catches
   soft blends a normal detector cannot — a 1-second crossfade peaks around **0.025**,
   where scene detection triggers at 0.12.
2. Each candidate is then re-examined **at 8 fps in a tight window**, so the model sees
   the transition happen frame by frame.
3. Brightness through each window is **measured with ffmpeg**, which is what separates a
   fade-through-black from a crossfade. Without it the model calls a fade-to-black a
   crossfade; with it, correct.

Returns shot list (size, camera move, composition), transition list (type, exact
duration, why the cut lands there), pacing statistics, colour and lighting.

Verified against a video with known transitions — **4/4 types correct**, durations
within 0.2s:

| built | detected |
|---|---|
| hard cut, instant | `hard_cut`, instant |
| crossfade, 1.0s | `crossfade`, 1.0s |
| fade-to-black, 0.6s | `fade_to_black`, 0.5s |
| wipe-left, 0.8s | `wipe`, 0.63s |

And on a continuous handheld take containing a whip pan it correctly reported **one
shot and zero transitions** rather than inventing a cut.

**A camera move is not a cut.** A whip pan, fast tilt or rack focus looks exactly like
an edit at low frame rates. `craft` is built to tell them apart; `index` is not. If a
question is about editing, use `craft`.

Flags: `--zoom-fps 12` for very fast cutting · `--max-windows 12` for busier edits ·
`--no-zoom` to skip the close pass (cheaper, much less accurate).

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

## Auditing a render — `audit`

After you make a video (Remotion, HyperFrames, brag, ffmpeg, Veo) you have never seen
it. Check it before shipping:

```bash
llvideo audit out.mp4                      # free measured checks only
llvideo audit out.mp4 --craft              # plus transitions and camera
llvideo audit out.mp4 --spec intent.json   # plus a diff against what you asked for
```

**The free checks catch the bugs that actually ship.** No model, no tokens, no network:

- black or white **first/last frame** — the single most common export defect, an
  off-by-one on the timeline
- black gaps and frozen stretches inside the body
- **EBU R128 loudness** and true-peak clipping, against the -14 LUFS platform target
- silence gaps, and "the audio track is effectively empty"
- declared vs implied frame rate, resolution, bitrate
- bright detail pressed against the frame edge (title-safe hint)

Verified: catches 5 of 5 deliberately planted defects, and returns **CLEAN with zero
findings** on real professional footage. It does not cry wolf.

Every finding says whether it is **measured** (ffmpeg said so, with the number) or
**judged** (a model thinks so). Never present a judged finding as fact.

Exit code is 1 only when there is a blocker, so it works as a CI gate.

### Diffing against intent

Write what the video was supposed to be, then diff:

```json
{
  "duration_seconds": 30,
  "duration_tolerance": 0.5,
  "aspect": "16:9",
  "transitions": [
    { "at": "00:04", "kind": "crossfade", "duration_seconds": 0.5 }
  ],
  "audio": { "must_have_music": true }
}
```

Leave out anything you cannot state reliably — a missing field is skipped, never
guessed. `--spec` implies the full two-pass craft analysis, because comparing
transitions against a single-pass reading produces mismatches that are not real.

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

Two backends:

- **`--backend local`** (default) — faster-whisper `small`, free, offline, no key.
  3.7× realtime, so a 10-minute track takes about 2.7 minutes. `medium` is 0.82× and
  `large-v3` is 0.18× on this CPU — both slower than the video itself, so `small` is
  the only practical local size. Needs `pip install faster-whisper`.
- **`--backend groq`** — hosted `whisper-large-v3-turbo`. Roughly 200× realtime and
  about **$0.007 for a 10-minute video**, with word-level timestamps. Better on both
  speed and accuracy than local `small`; local stays the default only because it needs
  no key, no network and costs nothing. Needs `GROQ_API_KEY`. 25 MB audio limit, so
  long files still go local.
- **`--backend auto`** — Groq when a key exists, local otherwise.

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
| `timeline` | ~$0.005/min | **one fused track** — visuals with the speech said over them |
| `ask -q` | ~$0.005/min | one question, answered with citations |
| `craft` | ~$0.02/min | transitions, camera moves, shots, pacing, grade |
| `audit` | **free** | render QA: black frames, loudness, dead air, spec diff |
| `spec` | **free** | extract intent from a HyperFrames / brag / Remotion project |
| `gen` | ~$0.07/s | generate a 1-15s supplemental clip (Grok Imagine), auto-audited |
| `gen-image` | ~$0.02 | generate a still — title card, texture, image-to-video seed |
| `fix` | **free** | repair loudness, clipping and black edge frames, then re-verify |
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
