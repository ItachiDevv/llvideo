#!/usr/bin/env bash
# Install llvideo and deploy the agent wrappers.
#   Claude Code -> ~/.claude/skills/llvideo/
#   Codex       -> ~/.agents/skills/llvideo/
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> installing the CLI"
python -m pip install -e "$HERE" --quiet || pip install -e "$HERE" --quiet
echo "    llvideo $(python -m llvideo --version 2>/dev/null | tail -1 || echo '(check PATH)')"

deploy() {
  local dest="$1" doc="$2" name="$3"
  mkdir -p "$dest"
  cp "$HERE/$doc" "$dest/$doc"
  # only copy references/ when it actually has content
  if [ -d "$HERE/references" ] && [ -n "$(ls -A "$HERE/references" 2>/dev/null)" ]; then
    cp -r "$HERE/references" "$dest/"
  fi
  echo "    $name -> $dest"
}

echo "==> regenerating AGENTS.md from SKILL.md"
python "$HERE/scripts/make_agents.py"

echo "==> deploying agent wrappers"
deploy "$HOME/.claude/skills/llvideo"  "SKILL.md"  "Claude Code"
deploy "$HOME/.agents/skills/llvideo"  "AGENTS.md" "Codex"

echo "==> checking dependencies"
command -v ffmpeg  >/dev/null && echo "    ffmpeg  ok" || echo "    ffmpeg  MISSING - install it, nothing works without it"
command -v ffprobe >/dev/null && echo "    ffprobe ok" || echo "    ffprobe MISSING"
python -c "import PIL" 2>/dev/null && echo "    Pillow  ok (optional)" || echo "    Pillow  not installed (optional: pip install Pillow)"
python -c "import faster_whisper" 2>/dev/null && echo "    faster-whisper ok (optional)" || echo "    faster-whisper not installed (optional: pip install faster-whisper)"

echo "==> providers"
python -m llvideo providers || true
echo
echo "done. try:  llvideo probe yourvideo.mp4"
