# PRIMARY EVIDENCE — measured by the orchestrator on 2026-08-25
Every number below came from a live API call or a local ffmpeg run on THIS machine.
Nothing here is from documentation or memory. Trust these over any conflicting research.

## Environment (verified)
- ffmpeg 8.0.1 + ffprobe 8.0.1 (gyan.dev essentials build) — PRESENT on PATH
- Python 3.13.7, cv2 5.0.0, PIL 12.2.0, node v24.13.0 — PRESENT
- GEMINI_API_KEY — present in ~/.itachi-api-keys AND env; live-tested OK (50 models listed)
- FAL_KEY, XAI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY — present
- OPENROUTER_API_KEY — **NOT FOUND** anywhere. User believes they have OpenRouter; the key is not on disk.
- Host is Windows 11. Git Bash `/tmp` does NOT resolve for Windows Python — must use explicit paths.

## Gemini models available on this key (live /v1beta/models)
gemini-3.7-flash, gemini-3.6-flash, gemini-3.5-flash, gemini-3.5-flash-lite,
gemini-3.1-pro-preview, gemini-3-flash-preview, gemini-3.1-flash-lite,
veo-3.1-generate-preview / -fast / -lite (predictLongRunning), gemini-3.1-flash-tts-preview.

## Measured token economics — gemini-3.7-flash
Constant rate confirmed across 12s, 20s and 600s clips:
- VIDEO @ default 1fps = **71 tokens/second** (4,260/min)
- AUDIO                = **32 tokens/second** (1,920/min)

10-minute 720p video, real countTokens results:
| sampling | total | video | audio |
|---|---|---|---|
| default (1 fps) | 61,800 | 42,600 | 19,200 |
| videoMetadata.fps=0.2 | 27,720 | 8,520 | 19,200 |
| videoMetadata.fps=0.1 | 23,460 | 4,260 | 19,200 |

Audio is a FLOOR — reducing fps never reduces it. Use `-an` when visuals are all that matter.

## Baseline being replaced
Old skill = 2 fps frames to disk, 15-20 separate images per vision call.
Same 10-min video: ~1,200 frames x ~1,000 tokens = **~1,200,000 tokens + ~1,200 JPEGs on disk**.
=> Gemini native is **19x cheaper at 1fps, 43x at 0.2fps, and writes ZERO files.**

## Verified capability tests
1. **Scene segmentation** — 12s 3-scene synthetic clip. Returned 00:00-00:04 / 00:04-00:08 /
   00:08-00:11 with all three on-screen text strings transcribed exactly. Cost 852 tokens total.
2. **Structured output** — `responseMimeType: application/json` + `responseSchema` works with
   video input. Clean typed scene arrays, no parsing.
3. **Clipping** — `videoMetadata: {startOffset:"5s", endOffset:"10s"}` is frame-exact. Model read
   the burned-in counter as T=5s at the start and T=9s at the end. Cost dropped to 455 video tokens.
4. **Real footage** — 4K night-driving stock clip. Gemini returned "night drive, passenger profile,
   gas stations, dashboard view of multi-lane road at 00:07-00:10". I extracted a contact sheet and
   LOOKED at it myself: description is accurate, including the BP station and traffic lights at 9s.
5. **Audio** — audio track IS ingested (adds 640 tokens to a 20s clip). BUT: event timestamps drift
   **±1s**, and pure sine tones were mis-identified as "birds chirping". Speech will be fine; do not
   trust it for frame-exact audio QA — use ffmpeg `silencedetect`/`ebur128` for that.
6. **mediaResolution** — NOT a valid field inside `generationConfig` on v1beta. Rejected as unknown.

## Compression is mandatory before upload
- 235 MB 4K 19.5s clip -> **1.2 MB in 3 seconds** (196x) via
  `-vf scale=-2:720 -c:v libx264 -crf 30 -preset veryfast -c:a aac -b:a 64k`
- Upload dominates wall time: 295 MB took **92s** to upload+process; 1.2 MB took ~23s.
- Token cost is IDENTICAL either way (tokens depend on duration, not bytes). Compression buys
  pure wall-clock, nothing else — but it buys a lot of it.

## OpenRouter — definitive, from live /api/v1/models (419 models)
- input modalities across catalog: text 419, image 250, **video 69**, file 158, audio 41
- output modalities across catalog: text 419, image 11, audio 4, **video 0**
- => **OpenRouter CANNOT generate video. Zero models output video.** Outsourcing generation
  to OpenRouter is not possible today. Video gen must go direct (Veo on the Gemini key, or fal.ai
  on FAL_KEY).
- 69 models DO accept video input, including `google/gemini-3.7-flash` ($0.375/M in),
  `qwen/qwen3.8-max`, `moonshotai/kimi-k3`, `minimax/minimax-m3`, and FREE ones:
  `minimax/minimax-m3:free`, `google/gemma-4-31b-it:free`, `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`.
- A 10-min video analysis via OpenRouter gemini-3.7-flash = 61,800 tok x $0.375/M = **~$0.023**.

## Contact-sheet fallback — measured
Tiling frames into ONE image instead of N separate images:
- 4x4 grid @ 360px tiles -> 1455x823 px -> **~1,596 Claude tokens for 16 frames**
  vs ~17,600 tokens as 16 separate images = **11x saving**
- 4x4 grid @ 480px tiles -> 1935x1095 px -> ~2,825 tokens
- Readability limit (I read the sheets myself): source text >= ~40px at 1080p survives a
  4x4 @360 grid cleanly. 28px body text is at the edge — legible but strained.
  **Rule: fine text / dense UI -> use 2x2 @ 768px. Normal footage -> 4x4 @ 360px is safe.**
- ffmpeg one-liner (Windows, verified):
  `ffmpeg -i in.mp4 -vf "fps=1/3,scale=480:-2,drawtext=fontfile='C\:/Windows/Fonts/arial.ttf':text='%{eif\:n*3\:d}s':fontcolor=yellow:fontsize=34:box=1:boxcolor=black@0.6:x=8:y=8,tile=3x2:margin=4:padding=4" -frames:v 1 sheet.jpg`

## Existing skill defects (verified by reading the files)
`~/.claude/skills/video-analyzer/` documents 7 steps but `scripts/` has only 6 files.
- `scripts/technical_analysis.py` — **MISSING**, Step 4 cannot run
- `scripts/export_keypoints.py` — **MISSING**, Step 6b cannot run
- SKILL.md instructs `sudo apt-get install ffmpeg` and `pip --break-system-packages` on a Windows box
- writes to `/tmp/video_frames/` which Windows Python cannot resolve

## YouTube URL ingestion — VERIFIED WORKING (major feature)
Passing `{"fileData":{"fileUri":"https://www.youtube.com/watch?v=..."}}` with NO File API upload
works. Tested on Big Buck Bunny (10:34, CC-licensed):
- HTTP 200, 57,778 video tokens, correct timestamped moments returned
  (butterfly crushed at 03:17, trap building at 04:15, squirrel-kite at 08:06 — all real events)
- **No download, no ffmpeg, no disk, no upload wait.**
=> The skill must accept a YouTube URL as a first-class input, not only a local file.

## CORRECTION to published docs — token rate is 71/s, not ~258-300/s
Research agent found docs claiming ~258-300 video tokens/sec at default media resolution.
That figure is STALE (Gemini 2.x era). Measured live across every 3.x model on the user's key,
identical 20s file:

| model | total | video | audio | video tok/s |
|---|---|---|---|---|
| gemini-3.7-flash | 2060 | 1420 | 640 | 71.0 |
| gemini-3.6-flash | 2060 | 1420 | 640 | 71.0 |
| gemini-3.5-flash | 2060 | 1420 | 640 | 71.0 |
| gemini-3.5-flash-lite | 2060 | 1420 | 640 | 71.0 |
| gemini-3-flash-preview | 2060 | 1420 | 640 | 71.0 |
| gemini-3.1-pro-preview | 2060 | 1420 | 640 | 71.0 |
| gemini-3.1-flash-lite | 2060 | 1420 | 640 | 71.0 |

**Token count is model-independent.** Choose the model on price and reasoning quality alone.

## Real cost of a 10-minute video (61,800 tokens)
| route | $/1M in | cost |
|---|---|---|
| Gemini direct, gemini-3.7-flash ($0.75/M per docs) | $0.75 | **$0.046** |
| OpenRouter google/gemini-3.7-flash ($0.375/M, live catalogue) | $0.375 | **$0.023** |
| OpenRouter google/gemini-3.1-flash-lite ($0.25/M, live catalogue) | $0.25 | **$0.015** |
| OpenRouter minimax/minimax-m3:free | $0 | **$0.00** |
Under five cents either way. The old frame-extraction path burned ~1.2M Claude tokens instead.

## The old repo — FOUND (verified via authenticated gh)
`gh api user --jq .login` => ItachiDevv (keyring token, scopes: gist, read:org, repo, workflow)

**https://github.com/ItachiDevv/claude-video-analyzer** — PUBLIC (not private), 0 stars, 0 forks,
38 KB, last pushed 2026-03-11.
"Claude Code skill — analyze video files for content, FPS/smoothness, and export frames/clips"

Contents: SKILL.md (11,047 b), README.md, LICENSE, .gitignore, references/vision_prompts.md,
video-analyzer.skill (18,285 b bundle), and scripts/: estimate_cost.py, export_clips.py,
export_stills.py, extract_frames.py, generate_report.py, probe_video.py.

**CRITICAL: `technical_analysis.py` and `export_keypoints.py` are in NEITHER the repo NOR the
deployed copy — they were never written at all.** SKILL.md Step 4 and Step 6b instruct the agent
to run them. The skill has been documented-but-broken since first publication.

Deployed copy is byte-identical to the repo (SKILL.md 11,047 b both) — no drift, but both broken.

Also found: `ItachiDevv/reclip` (PRIVATE, pushed 2026-05-26) — a Flask/Docker web app
(app.py, templates/index.html, Dockerfile, assets/preview.mp4). Separate project, NOT the analyzer.

## Duration ceiling (derived from the verified linear rate, 71 video + 32 audio tok/s)
| duration | total tokens | % of 1M ctx | $ 3.7-flash | $ 3.1-flash-lite |
|---|---|---|---|---|
| 5 min | 30,900 | 3% | $0.023 | $0.009 |
| 10 min | 61,800 | 6% | $0.046 | $0.019 |
| 30 min | 185,400 | 18% | $0.139 | $0.056 |
| 1 hour | 370,800 | 35% | $0.278 | $0.111 |
| 2 hours | 741,600 | 71% | $0.556 | $0.222 |
| 2h 45m | 1,019,700 | 97% | $0.765 | $0.306 |
| 3 hours | 1,112,400 | **106% — DOES NOT FIT** | — | — |

- **Hard ceiling: 2h 50m** in one call at default fps.
- At `fps=0.2` audio dominates; ceiling moves to **6.3 h**. With `-an` (audio stripped): **20.5 h**.
- User's real recording `2025-09-30 18-46-10.mp4`: 7,239 s, **6.09 GB**, 1920x1080 @ 60fps.
  745,617 tokens = 71% of context, $0.56. Fits context — but File API caps at **2 GB/file**,
  so the 6 GB source is inadmissible until transcoded. Compression is a hard requirement, not an optimisation.

## Machine spec (from `hyperframes doctor --json`, corrects an earlier assumption)
**12th Gen i7-12700H, 20 cores, 63.7 GB RAM, 338 GB free.** Only the GPU is weak (Iris Xe, no CUDA).
Local CPU transcription is fast here — this likely removes the case for any paid transcription API.

## HyperFrames already ships the closed loop (verified by running --help, v0.8.14)
`hyperframes doctor` green: FFmpeg 8.0.1, FFprobe, headless Chrome, Node 24.
Optional-only failures: whisper-cpp, Kokoro TTS, MusicGen, Docker not running.
- `snapshot --at 3.0,10.5,18.0 --describe "<q>"` — stills at exact timestamps, NO full render.
  Help verbatim: "Gemini vision frame analysis. Runs by default when GEMINI_API_KEY is set."
- `compare a/ b/ c/ --at 5.0 --labels a,b,c` · `grade-compare` · `keyframes --selector --shot`
  (onion-skin motion trails, real pixel compositing) · `snapshot --zoom <selector>`
**Remotion is NOT covered** — `npx remotion --help` fails here; it only resolves inside a scaffolded project.

## Ecosystem (rf-ecosystem, all verified via gh/raw fetch)
- `claude-real-video` (crv) 2,062 stars, HN front page, pushed 2026-08-22 — ffmpeg scene-detect + dedup + Whisper. **No Gemini.**
- `claude-video-vision` 1,260 stars — ffmpeg frames for vision; Gemini only as an audio-transcription backend.
- 6x `gemini-video-mcp` repos: 0-2 stars each, stale or toys. Ignore all.
- `anthropics/skills` (171,437 stars): **no first-party video skill.**
- Reddit: ALL four fetch methods returned 403/blocked. No Reddit content cited. Documented dead end.
- Sibling repo `ItachiDevv/claude-screen-recorder` (public) = the deployed `screen-recorder` skill, capture only.

## ffmpeg primitives verified exact (rf-pipeline, against constructed ground truth)
- Zero-disk: `-f image2pipe -vcodec mjpeg -` + JPEG SOI/EOI parsing => 120 frames in **0.84 s, zero temp files**.
  Windows `subprocess.PIPE` is binary by default; no special handling, no `/dev/stdout`.
- `blackdetect=d=0.5:pic_th=0.98` => black_start:5 black_end:8 — exact.
- `freezedetect=n=-60dB:d=2` => 6 events, boundaries 0/5/10/15/20/25 — exact.
- `silencedetect=n=-25dB:d=0.5` => silence_end 10.008188 — exact to the millisecond.
- **BUG TO DESIGN AROUND: `select='gt(scene,X)'` NEVER detects the first cut.** `scene` scores frame N
  against N-1 and frame 1 has no predecessor. Scene-detect MUST be paired with a uniform floor,
  or the opening cut is silently lost.

## Veo 3.1 (on the user's existing key)
`predictLongRunning` + poll `.done`; result at `.response.generateVideoResponse.generatedSamples[0].video.uri`
| model | $/sec 720p | duration | audio | img2vid |
|---|---|---|---|---|
| veo-3.1-lite-generate-preview | **$0.05** | 4/6/8s | native | yes |
| veo-3.1-fast-generate-preview | $0.10 | 4/6/8s | native | yes |
| veo-3.1-generate-preview | $0.40 | 4/6/8s | native | yes (+ up to 3 ref images) |
`lastFrame` param is the clip-stitching primitive for longer pieces.

## TouchDesigner
`NousResearch/hermes-agent/skills/creative/touchdesigner-mcp` — open source, free. Drives a
`twozero.tox` component running a plain **MCP server on localhost:40404, 36 tools**
(`td_create_operator`, `td_execute_python`, `td_get_screenshot`, ...).
**Claude Code can connect directly — Hermes is not required.** Needs TD running with GUI; no headless mode.
Live-visuals control, not video comprehension. Separate project.

## CORRECTION — scene-detect thresholds on REAL footage (supersedes the synthetic sweep)
The first sweep used synthetic solid-colour cuts and was flat across 0.1-0.4. That test was too easy
and its 0.3-0.4 recommendation was WRONG. Re-run on real 4K stock footage concatenated with known
real cut points at t=19.52s and t~32.5s:

```
threshold=0.05  hits=[0.042, 19.5195, 54.888, 54.972]
threshold=0.10  hits=[19.5195]
threshold=0.15  hits=[19.5195]
threshold=0.20  hits=[]
threshold=0.30  hits=[]
threshold=0.40  hits=[]
```

1. **A real cut only scores between 0.05 and 0.15.** It is gone by 0.20. Two real shots of similar
   subject and lighting produce far less pixel delta than a solid-colour swap.
   **Default threshold for real footage is 0.10-0.15, not 0.3-0.4.**
2. **The second real cut (~32.5s) is NEVER detected at any threshold, down to 0.05.** Its scene score
   never crosses even the most sensitive setting. ffmpeg's `scene` filter can silently miss an entire
   real edit when two shots share a colour palette and exposure.
3. Genre-specific thresholds do not survive real footage. Use one default for all content.

=> **Scene-detect can never ship alone.** It must be the UNION of scene hits and a uniform floor
(1 frame per 10-15s). This is now proven twice: the first cut is structurally undetectable, AND
some interior cuts are undetectable at any threshold.

## faster-whisper measured on this machine (i7-12700H, 20 cores, int8, CPU)
| model | audio | transcribe | RTF | notes |
|---|---|---|---|---|
| small | 111.8s | 30.0s | **3.73x realtime** | 25 segments, 305 words, lang en 1.00, word timestamps accurate |
| small (2nd run) | 12.7s | 3.12s | 4.07x realtime | transcript verbatim correct |
| medium / large-v3 | — | — | pending | models still downloading (HF unauthenticated, rate-limited) |

Word timestamps verified realistic: [0.00]The [0.20]quick [0.44]brown [0.76]fox [1.02]jumps [1.38]over
A 10-minute audio track transcribes in ~2.7 min, free, word-level exact.
=> **Paid transcription APIs (Deepgram, ElevenLabs Scribe, AssemblyAI) are unnecessary on this hardware.**
No HF_TOKEN exists in ~/.itachi-api-keys; downloads are rate-limited but work.
