# Workflow Architecture

This repository contains the orchestration and validation layer for a complete
short-video production workflow. Heavy AI engines, model checkpoints, private
media, and account credentials remain external dependencies.

## End-to-End Flow

```text
script + reference voice + avatar + HTML + STYLE_PLAN
                         |
                         v
                 style validation
                         |
                         v
                    IndexTTS2
                         |
                         v
          audio verification + F0/content gates
                         |
                         v
                 speed + loudness pass
                         |
                         v
                 Whisper caption alignment
                         |
                         v
             SadTalker talking-head render
                         |
                         v
                 HyperFrames main render
                         |
                         v
             projection layer + final ffmpeg
                         |
                         v
       timeline + duration + visual + audio gates
                         |
                         v
               validated run manifest
                         |
              optional idempotent upload
```

## Repository Components

| Component | Responsibility |
| --- | --- |
| `scripts/pipeline_daily.py` | Main ten-step orchestration, locking, resume, manifests, and final composition |
| `scripts/tts_male_pipeline.py` | Long-text IndexTTS2 synthesis, F0 audit, and content acceptance |
| `scripts/tts_quality.py` | Dependency-light TTS quality and duration helpers |
| `scripts/gen_caption_timeline.py` | Whisper transcription and script-to-time mapping |
| `scripts/prepare_sadtalker_source_image` | Pre-SadTalker avatar resize, capped at the configured height |
| `scripts/pre_composite_check.py` | Audio, caption, digital-human, and main-video timeline gates |
| `scripts/validate_style.py` | STYLE_PLAN schema, HTML metadata, and recent-history diversity gate |
| `scripts/validate_video_visuals.py` | Blank-frame, safe-area, and visual similarity checks |
| `scripts/gen_proj_strong.py` | Projection/shadow layer generation |
| `scripts/repair_run.py` | Revalidate manually repaired artifacts and write a manifest |
| `scripts/upload_video.py` | Bilibili queue adapter with SHA256 idempotency |
| `templates/` | HyperFrames HTML composition starting points |
| `examples/` | Public, media-free project structure |

## Run States

- `running`: a run has acquired the pipeline lock.
- `validated`: generation and all local gates passed; no upload is implied.
- `success`: upload completed and the upload marker matches the video hash.
- `failed`: a step failed; inspect the first failed manifest step and its output tail.

All expensive stages write outputs into a run-specific directory before replacing
the canonical project output. This makes failed renders visible without letting
partial files masquerade as successful output.
