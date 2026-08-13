#!/usr/bin/env python3
"""校验音频、字幕、数字人和主视频的时间轴一致性。"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys

MAX_FINAL_DURATION = 59.5


def ffprobe_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"ffprobe 失败: {path}: {result.stderr.strip()}")
    return float(result.stdout.strip())


def newest(paths):
    existing = [path for path in paths if os.path.isfile(path)]
    return max(existing, key=os.path.getmtime) if existing else None


def manifest_output(project_dir, key):
    try:
        with open(os.path.join(project_dir, "run-manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    value = manifest.get("outputs", {}).get(key)
    return value if value and os.path.isfile(value) else None


def find_audio(project_dir, explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    from_manifest = manifest_output(project_dir, "final_audio")
    if from_manifest:
        return from_manifest
    candidates = [
        os.path.join(project_dir, "audio", "voiceover_130.wav"),
        os.path.join(project_dir, "voiceover_130.wav"),
    ]
    return newest(candidates)


def find_digital_human(project_dir, explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    from_manifest = manifest_output(project_dir, "digital_human")
    if from_manifest:
        return from_manifest
    try:
        with open(os.path.join(project_dir, "run-manifest.json"), encoding="utf-8") as handle:
            run_id = json.load(handle).get("run_id")
    except (OSError, ValueError, json.JSONDecodeError):
        run_id = None
    if not run_id:
        return None
    run_dir = os.path.join(project_dir, "digital_human", str(run_id))
    return newest(glob.glob(os.path.join(run_dir, "**", "*.mp4"), recursive=True))


def find_main_video(project_dir, explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    from_manifest = manifest_output(project_dir, "main_video")
    if from_manifest:
        return from_manifest
    candidates = [
        os.path.join(project_dir, "renders", "main_video.mp4"),
        os.path.join(project_dir, "main_video.mp4"),
    ]
    return newest(candidates)


def find_html(project_dir, explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    from_manifest = manifest_output(project_dir, "html")
    if from_manifest:
        return from_manifest
    path = os.path.join(project_dir, "index.html")
    return path if os.path.isfile(path) else None


def subtitle_times(html_path):
    if not html_path:
        return None
    with open(html_path, encoding="utf-8") as handle:
        html = handle.read()
    captions = re.findall(
        r'["\']?start["\']?\s*:\s*([\d.]+)\s*,\s*["\']?end["\']?\s*:\s*([\d.]+)',
        html,
    )
    return [(float(start), float(end)) for start, end in captions]


def stage_duration(html_path):
    if not html_path:
        return None
    with open(html_path, encoding="utf-8") as handle:
        html = handle.read()
    match = re.search(
        r'<[^>]+(?=[^>]*\bid=["\']stage["\'])(?=[^>]*\bdata-duration\s*=)[^>]*>',
        html,
        flags=re.DOTALL,
    )
    if not match:
        return None
    value = re.search(r'\bdata-duration\s*=\s*["\']([\d.]+)["\']', match.group(0))
    return float(value.group(1)) if value else None


def subtitle_end(html_path):
    times = subtitle_times(html_path)
    return times[-1][1] if times else None


def check_timeline(args):
    mode = "快速校验" if args.quick else "数字人校验" if args.dh_only else "完整校验"
    print(f"\n{'=' * 60}\n{mode}: {args.project}\n{'=' * 60}\n")

    audio = find_audio(args.project, args.audio)
    html = find_html(args.project, args.html)
    digital_human = find_digital_human(args.project, args.digital_human)
    main_video = find_main_video(args.project, args.main_video)

    missing = []
    if not audio:
        missing.append("定稿音频")
    if not html:
        missing.append("HTML")
    if not args.quick and not digital_human:
        missing.append("数字人视频")
    if not args.quick and not args.dh_only and not main_video:
        missing.append("主视频")
    if missing:
        print(f"❌ 缺少必要产物: {', '.join(missing)}")
        return False

    try:
        audio_duration = ffprobe_duration(audio)
        digital_human_duration = ffprobe_duration(digital_human) if digital_human else None
        main_video_duration = ffprobe_duration(main_video) if main_video else None
        subtitle_ranges = subtitle_times(html)
        last_subtitle_end = subtitle_ranges[-1][1] if subtitle_ranges else None
        target_video_duration = stage_duration(html) or audio_duration
    except (RuntimeError, ValueError) as exc:
        print(f"❌ {exc}")
        return False

    print(f"1. 定稿音频: {audio_duration:.3f}s ({audio})")
    print(
        f"2. 数字人:   {digital_human_duration:.3f}s ({digital_human})"
        if digital_human_duration is not None
        else "2. 数字人:   快速模式不检查"
    )
    print(
        f"3. 主视频:   {main_video_duration:.3f}s ({main_video})"
        if main_video_duration is not None
        else "3. 主视频:   当前模式不检查"
    )
    print(
        f"4. 字幕末条: {last_subtitle_end:.3f}s ({html})"
        if last_subtitle_end is not None
        else "4. 字幕末条: 未找到"
    )
    print(f"5. 目标成片: {target_video_duration:.3f}s (HTML stage)")

    checks = []
    captions_valid = bool(subtitle_ranges) and all(
        0 <= start < end and (index == 0 or start >= subtitle_ranges[index - 1][1])
        for index, (start, end) in enumerate(subtitle_ranges)
    )
    if not captions_valid:
        checks.append(("字幕时间轴存在", False, "未找到 captions start/end"))
    else:
        difference = abs(last_subtitle_end - audio_duration)
        checks.append(("字幕末条≈音频 (<1.5s)", difference < 1.5, f"差值 {difference:.3f}s"))

    if digital_human_duration is not None:
        difference = abs(digital_human_duration - audio_duration)
        checks.append(("数字人≈音频 (<0.5s)", difference < 0.5, f"差值 {difference:.3f}s"))

    if main_video_duration is not None:
        difference = main_video_duration - target_video_duration
        checks.append(("主视频≈目标成片 (<0.75s)", abs(difference) < 0.75, f"差值 {difference:+.3f}s"))

    checks.append((
        "全局成片时长 <=59.5s",
        target_video_duration <= MAX_FINAL_DURATION,
        f"{target_video_duration:.3f}s / {MAX_FINAL_DURATION:.3f}s",
    ))
    if main_video_duration is not None:
        checks.append((
            "主视频硬上限 <=59.5s",
            main_video_duration <= MAX_FINAL_DURATION,
            f"{main_video_duration:.3f}s / {MAX_FINAL_DURATION:.3f}s",
        ))

    print(f"\n{'=' * 60}\n校验结果\n{'=' * 60}")
    for label, passed, detail in checks:
        print(f"  {'✅' if passed else '❌'} {label}: {detail}")

    passed = bool(checks) and all(item[1] for item in checks)
    if passed:
        message = "时间轴基础校验通过" if args.quick else "数字人时间轴校验通过" if args.dh_only else "所有时间轴校验通过"
        print(f"\n✅ {message}")
    else:
        print("\n❌ 时间轴校验失败")
    return passed


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="只检查音频和字幕")
    mode.add_argument("--dh-only", action="store_true", help="检查音频、字幕和数字人")
    mode.add_argument("--full", action="store_true", help="检查全部必要产物（默认）")
    parser.add_argument("--audio")
    parser.add_argument("--digital-human")
    parser.add_argument("--main-video")
    parser.add_argument("--html")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(0 if check_timeline(parse_args()) else 1)
