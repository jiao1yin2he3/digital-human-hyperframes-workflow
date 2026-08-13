#!/usr/bin/env python3
"""Legacy final compositor for a Unitree-style project.

This script is kept as a small, configurable utility for older manual runs.
The standard workflow should use ``scripts/pipeline_daily.py`` instead.
"""

import argparse
import glob
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def ffprobe(path, entries="format=duration"):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def run(command, timeout=300):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    return (result.stdout or "") + (result.stderr or "")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-video", default=str(ROOT / "final_video_unitree.mp4"))
    parser.add_argument("--voiceover", default=str(ROOT / "final_voiceover_unitree_135.wav"))
    parser.add_argument("--digital-human-glob", default=str(ROOT / "digital-human" / "results_unitree_135" / "*.mp4"))
    parser.add_argument("--projection", default=str(ROOT / "shadow_layer.png"))
    parser.add_argument("--html", default=str(ROOT / "compositions" / "index_unitree.html"))
    parser.add_argument("--output", default=str(ROOT / "final_video_unitree_final.mp4"))
    parser.add_argument("--check-dir", default=str(ROOT / "output" / "unitree-checks"))
    return parser.parse_args()


def main():
    args = parse_args()
    main_video = Path(args.main_video).expanduser()
    voiceover = Path(args.voiceover).expanduser()
    projection = Path(args.projection).expanduser()
    html = Path(args.html).expanduser()
    output = Path(args.output).expanduser()
    check_dir = Path(args.check_dir).expanduser()

    digital_humans = sorted(glob.glob(str(Path(args.digital_human_glob).expanduser())))
    if not digital_humans:
        print("数字人视频未生成，请先跑 SadTalker")
        return 1
    digital_human = Path(digital_humans[0])

    for label, path in {
        "main video": main_video,
        "voiceover": voiceover,
        "digital human": digital_human,
        "projection": projection,
        "html": html,
    }.items():
        if not path.is_file():
            print(f"缺少 {label}: {path}")
            return 1

    main_duration = float(ffprobe(main_video))
    digital_duration = float(ffprobe(digital_human))
    audio_duration = float(ffprobe(voiceover))
    print(f"时长校验: 主视频={main_duration:.2f}s 数字人={digital_duration:.2f}s 音频={audio_duration:.2f}s")
    diff = abs(digital_duration - audio_duration)
    if diff > 0.5:
        print(f"数字人与音频时长不一致，差 {diff:.2f}s")
        return 1
    if main_duration < audio_duration - 0.5:
        print(f"主视频({main_duration:.2f}s)短于音频({audio_duration:.2f}s)")
        return 1

    content = html.read_text(encoding="utf-8")
    captions = re.findall(r"start:\s*([\d.]+),\s*end:\s*([\d.]+)", content)
    if captions:
        last_end = float(captions[-1][1])
        delta = abs(last_end - audio_duration)
        if delta > 1.5:
            print(f"字幕末条({last_end:.1f}s)与音频({audio_duration:.1f}s)偏差 {delta:.1f}s")
        else:
            print(f"字幕末条 {last_end:.1f}s ~= 音频 {audio_duration:.1f}s")

    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(main_video),
        "-i",
        str(digital_human),
        "-i",
        str(voiceover),
        "-i",
        str(projection),
        "-filter_complex",
        (
            "[1:v]scale=260:347[inner];"
            "[3:v]format=rgba[proj];"
            "[proj][inner]overlay=14:14[combo];"
            "[0:v]scale=1920:1080[bg];"
            "[bg][combo]overlay=W-w-16:H-h-16[v]"
        ),
        "-map",
        "[v]",
        "-map",
        "2:a",
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    result = run(command)
    if not output.is_file() or output.stat().st_size == 0:
        print(f"合成失败: {result[-300:]}")
        return 1
    print(f"合成完成: {output} ({output.stat().st_size / 1024 / 1024:.1f}MB)")

    sys.path.insert(0, str(ROOT))
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(output), language="zh")
    text = "".join(segment.text for segment in segments)
    print(f"最终音频转写 ({len(text)}字): {text[:100]}...")

    check_dir.mkdir(parents=True, exist_ok=True)
    for second in [5, 18, 32]:
        run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                str(second),
                "-i",
                str(output),
                "-frames:v",
                "1",
                str(check_dir / f"check_{second}.jpg"),
            ]
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
