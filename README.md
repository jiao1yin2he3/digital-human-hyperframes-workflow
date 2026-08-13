# HyperFrames Workflow

[简体中文](README.zh-CN.md) | English

Auditable short-video production workflow built around HyperFrames, IndexTTS2,
SadTalker, Whisper alignment, ffmpeg composition, visual style gates, and
optional Bilibili publishing.

The repository contains workflow code, templates, tests, and sanitized examples.
It does not include model weights, private media assets, reference voices,
avatars, generated videos, cookies, or publishing queue data.

## What It Does

- Generates voiceover from a script with configurable pacing and TTS quality gates.
- Aligns captions with Whisper and injects them into a HyperFrames composition.
- Renders the main animation and a small talking-head overlay.
- Extends the final frame and fades audio/video to avoid abrupt endings.
- Targets 58.0–59.0 seconds with a hard 59.5-second final-video gate; scripts use 260–290 effective characters and a 1.30x final audio source.
- Validates style diversity, timeline consistency, audio recognizability, and visual output.
- Writes a machine-readable run manifest for resume, repair, and idempotent upload.
- Supports multilingual scripts at the orchestration layer through UTF-8 script
  files and configurable Whisper language codes such as `zh`, `en`, `ja`, `ko`,
  `es`, `fr`, and `de`. Actual speech quality depends on the local TTS engine,
  reference voice, and model support for the target language.

## Requirements

- macOS or Linux with `ffmpeg` and `ffprobe` in `PATH`.
- Node.js compatible with the locked `package-lock.json`.
- Python environments for the system workflow, IndexTTS2, and SadTalker.
- Local copies of IndexTTS2 checkpoints and SadTalker checkpoints.
- Optional: a local Bilibili upload queue compatible with `scripts/upload_video.py`.

Heavy engines and checkpoints are intentionally ignored by Git. Configure their
locations in `workflow.local.yaml` or through `HFW_CONFIG`.

## Setup

```bash
npm install
cp workflow.example.yaml workflow.local.yaml
cp config_daily.example.yaml config_daily.local.yaml
```

Edit `workflow.local.yaml` to point at your local Python environments, TTS
checkpoints, SadTalker checkout, and optional upload queue.

Then run:

```bash
npm run doctor
npm run check
```

`npm run doctor` checks local production dependencies. It may fail on a fresh
clone until the heavyweight engines and model checkpoints are installed.

## Run A Project

Use the sanitized demo structure as a starting point:

```bash
cp -R examples/demo-project projects/my-topic
cp STYLE_HISTORY.example.md STYLE_HISTORY.md
```

Update:

- `projects/my-topic/index.html`
- `projects/my-topic/STYLE_PLAN.yaml`
- `projects/my-topic/口播稿.txt` or another UTF-8 script file such as `script.en.txt`
- `projects/my-topic/素材.md` or another research-notes file
- `config_daily.local.yaml`

Run:

```bash
python3 scripts/pipeline_daily.py --config config_daily.local.yaml
```

Uploading is off by default. To publish after validation:

```bash
python3 scripts/pipeline_daily.py --config config_daily.local.yaml --upload --public
```

Without `--public`, upload jobs remain self-only when the upload adapter supports
visibility control.

## Configuration

- `workflow.example.yaml` describes machine-level paths and workflow defaults.
- `config_daily.example.yaml` describes one video project.
- `STYLE_PLAN.template.yaml` describes required visual style metadata.
- `STYLE_HISTORY.example.md` is a sanitized starting point for the local
  `STYLE_HISTORY.md` used by the style diversity gate.
- `.github/workflows/ci.yml` runs the dependency-light test and demo validation.
- `docs/ARCHITECTURE.md` explains the complete end-to-end flow.
- `docs/DEPENDENCIES.md` lists external AI engines, models, and hardware-specific setup.
- `docs/BILIBILI_UPLOAD.md` defines the optional Bilibili queue adapter contract.
- `requirements-workflow.txt` lists the Python packages used by the orchestration layer.
- `docs/DAILY_AGENT_PROMPT.template.md` is a sanitized automation prompt template.
- `pipeline.whisper_language` configures the Whisper language used by caption
  alignment and audio gates. Keep it aligned with the script and TTS voice.

`workflow.local.yaml`, media assets, generated project outputs, validation
artifacts, and private publishing markers are ignored by Git.

## Tests

```bash
npm test
npm run check
```

The unit tests exercise style validation, timeline checks, upload defaults, TTS
quality helpers, and key pipeline transformations. They do not download models
or generate a full video.

## Repository Safety

Before publishing a fork or release, run:

```bash
git status --short
git add -n .
rg -n --hidden -g '!node_modules/**' -g '!venv/**' -g '!*-venv/**' '/Users/|/home/|api[_-]?key|access[_-]?token|secret|cookie'
```

Do not commit:

- `workflow.local.yaml`
- model checkpoints and cloned third-party engines
- reference audio, avatars, generated audio/video/images
- `projects/` with real production topics or upload markers
- cookies, tokens, queue data, or account-specific automation notes

The repository is complete at the workflow-orchestration level. Third-party
engines and model weights are intentionally installed separately and connected
through `workflow.local.yaml`.

## License

MIT. See `LICENSE`.
