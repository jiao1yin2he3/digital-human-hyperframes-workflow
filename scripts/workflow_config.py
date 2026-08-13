"""Shared configuration helpers for the HyperFrames workflow."""

from __future__ import annotations

import os
from pathlib import Path

import yaml


DEFAULT_CONFIG = {
    "paths": {
        "sys_python": "venv/bin/python",
        "indextts_root": "index-tts",
        "indextts_checkpoints": "index-tts/checkpoints",
        "indextts_python": "indextts-venv/bin/python",
        "sadtalker_python": "digital-human/venv/bin/python",
        "sadtalker_dir": "digital-human/SadTalker",
        "sadtalker_checkpoints": "digital-human/SadTalker/checkpoints",
        "style_guide": "docs/VIDEO_STYLE_DIVERSITY_GUIDE.md",
        "projects_dir": "projects",
        "hyperframes_bin": "node_modules/.bin/hyperframes",
        "biliup_root": "~/biliup-data",
    },
    "pipeline": {
        "audio_speed": 1.30,
        "max_final_duration": 59.5,
        "tts_interval_silence": 0.10,
        "tts_emo_alpha": 0.4,
        "tts_use_emo_text": True,
        "tts_use_random": False,
        "tts_temperature": 0.75,
        "tts_top_p": 0.85,
        "tts_top_k": 30,
        "tts_repetition_penalty": 10.0,
        "tts_emo_text": "",
        "tts_emo_audio_prompt": "",
        "tts_required_keywords": "",
        "voiceover_min_chars": 260,
        "voiceover_max_chars": 290,
        "sadtalker_pose_style": "12",
        "sadtalker_source_max_height": 720,
        "end_padding_seconds": 1.2,
        "end_video_fade_seconds": 0.45,
        "end_audio_fade_seconds": 0.25,
        "allow_resume": False,
    },
}


def _deep_merge(base, override):
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_workflow_config(root: Path) -> dict:
    """Load workflow config from HFW_CONFIG or workflow.local.yaml if present."""
    root = Path(root)
    config = DEFAULT_CONFIG
    config_file = os.environ.get("HFW_CONFIG")
    candidates = [Path(config_file).expanduser()] if config_file else [root / "workflow.local.yaml"]
    for candidate in candidates:
        if candidate.is_file():
            with open(candidate, encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
            if not isinstance(loaded, dict):
                raise ValueError(f"workflow config must be a YAML object: {candidate}")
            config = _deep_merge(config, loaded)
            config["_config_file"] = str(candidate)
            break
    return config


def get_config_value(config: dict, dotted_key: str, default=None):
    current = config
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def config_path(root: Path, config: dict, dotted_key: str) -> Path:
    value = get_config_value(config, dotted_key)
    if value is None:
        raise KeyError(dotted_key)
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else Path(root) / path
