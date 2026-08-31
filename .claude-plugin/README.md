# llvideo plugin

The skill and slash command are thin wrappers over the `llvideo` Python CLI, so
that has to be installed too:

```bash
pip install -e .        # from the repo root
llvideo providers       # confirm a backend is configured
```

Requires `ffmpeg` on PATH and one of `GEMINI_API_KEY` or `OPENROUTER_API_KEY`.

Install the plugin itself by adding this directory as a local marketplace:

```
/plugin marketplace add /path/to/llvideo
/plugin install llvideo@llvideo-local
```
