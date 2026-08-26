# /llvideo - Analyze a video

Watch and understand a video file or YouTube link.

## Usage

```
/llvideo <path-or-url>
/llvideo <path-or-url> <question>
```

Examples:
- `/llvideo C:\Users\newma\Videos\clip.mp4`
- `/llvideo clip.mp4 what error message appears?`
- `/llvideo https://youtube.com/watch?v=abc123 when do they show the price?`

## Instructions

The user ran `/llvideo` with arguments. Parse them: the first token is the video
path or URL, everything after it is an optional question.

**If no path was given**, ask which video. Do not guess.

**Run the tool.** It is installed as `llvideo` on PATH.

With a question:
```bash
llvideo ask "<path>" -q "<question>"
```

Without a question:
```bash
llvideo index "<path>"
```

**Then do these two things, always:**

1. **Read the contact sheet.** The output ends with a path to a JPEG. Open it with
   the Read tool and look at it yourself before you answer. It is one image of the
   whole video, about 600-2500 tokens. You cannot watch video; this is how you see it.

2. **Answer the user in a few sentences.** Lead with what happens in the video. Do
   not paste the raw tool output, do not show token counts, do not explain the
   pipeline. If they asked a question, answer that question first, then add the
   timestamps that support it.

## Rules

- Cost is roughly 2 cents per 10 minutes. Just run it. Do not ask permission unless
  the tool itself refuses (it stops on its own above $1.00, then pass `--yes`).
- If on-screen text matters — an error message, a price, code, a dashboard number —
  add `--verify 3`. It runs three times and flags readings that changed between runs,
  because vision models read blurred text confidently and wrongly.
- For a specific moment, zoom in instead of re-running the whole video:
  ```bash
  llvideo clip "<path>" --start 02:10 --end 02:40 --fps 4 -q "<question>"
  ```
- To pull a full-resolution frame and look closely yourself:
  ```bash
  llvideo stills "<path>" --at 02:14 --width 1920
  ```
- Say plainly when the tool marks something unstable or illegible. Never present a
  guessed reading as fact.
- A local file is uploaded to the provider and kept 48 hours. If the video looks
  private, say so once and offer `llvideo clean --uploads`.

## If it fails

- `llvideo: command not found` → `pip install -e C:\Users\newma\Desktop\llvideo`
- No API key → `llvideo providers` shows what is configured. Needs `GEMINI_API_KEY`
  or `OPENROUTER_API_KEY` in the environment or `~/.itachi-api-keys`.
- `ffmpeg not found` → `winget install Gyan.FFmpeg`
