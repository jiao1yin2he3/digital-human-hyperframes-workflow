# External Dependencies

The public repository contains the workflow glue code. These projects and model
weights must be installed separately because they are large, independently
licensed, and hardware-specific.

## Required Engines

| Engine | Expected local path | Used by |
| --- | --- | --- |
| IndexTTS2 | `index-tts/` plus `indextts-venv/` | `scripts/tts_male_pipeline.py` |
| SadTalker | `digital-human/SadTalker/` plus `digital-human/venv/` | `scripts/pipeline_daily.py` |
| HyperFrames | npm package and `node_modules/.bin/hyperframes` | main HTML render |
| Whisper | `faster-whisper` in the system workflow environment | captions and audio gates |
| ffmpeg | executable in `PATH` | probing, conversion, and final composition |
| Pillow | available in a visual Python environment | avatar resize and projection/visual checks |

## Optional Engines

- CosyVoice can provide or prepare a stable reference voice. The default
  orchestration contract only requires a readable reference audio file.
- MuseTalk can be evaluated as an alternative talking-head backend, but the
  current production path remains SadTalker. A future adapter should preserve
  the same contract: input audio, source image/video, output MP4, and duration
  within 0.5 seconds of the final audio.
- GFPGAN is consumed through the selected talking-head engine and its weights
  must remain outside this repository.

## Install Third-Party Checkouts

Use the upstream installation instructions and verify their licenses before
commercial distribution:

```bash
git clone --recursive https://github.com/index-tts/index-tts.git index-tts
git clone https://github.com/OpenTalker/SadTalker.git digital-human/SadTalker
```

CosyVoice and MuseTalk are optional and should be checked out into the paths
used by your local configuration. Do not copy their source trees into this
repository; keep them as sibling or ignored directories.

## Models

Download checkpoints from the official project pages. Do not commit model
weights to GitHub. Configure checkpoint paths in `workflow.local.yaml` and run:

```bash
python3 scripts/doctor.py
```

The doctor command reports missing engines, checkpoints, Python imports, ffmpeg,
and HyperFrames. Bilibili is optional and is reported as skipped when its local
adapter is absent.

The lightweight orchestration dependencies are listed in
`requirements-workflow.txt`. Engine-specific dependencies belong to the
upstream engine environments and should not be merged into one environment.
