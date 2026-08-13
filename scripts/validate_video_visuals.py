#!/usr/bin/env python3
"""对最终视频执行空白帧、安全区和历史视觉相似度检查。"""

import argparse
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat


def run_checked(command):
    result = subprocess.run([str(item) for item in command], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stdout or "") + (result.stderr or ""))
    return result


def duration(path):
    result = run_checked(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]
    )
    return float(result.stdout.strip())


def extract_frames(video, output_dir, prefix):
    video_duration = duration(video)
    times = [max(0.1, video_duration * ratio) for ratio in (0.12, 0.5, 0.88)]
    paths = []
    for index, timestamp in enumerate(times, start=1):
        path = output_dir / f"{prefix}-{index}.jpg"
        run_checked(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                video,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                path,
            ]
        )
        paths.append(path)
    return paths


def average_hash(image, size=16):
    grayscale = ImageOps.grayscale(image).resize((size, size))
    pixels = list(grayscale.getdata())
    average = sum(pixels) / len(pixels)
    return [pixel >= average for pixel in pixels]


def hash_similarity(left, right):
    return sum(a == b for a, b in zip(left, right)) / len(left)


def frame_metrics(path):
    image = Image.open(path).convert("RGB")
    grayscale = ImageOps.grayscale(image)
    standard_deviation = ImageStat.Stat(grayscale).stddev[0]
    colors = grayscale.resize((160, 90)).getcolors(maxcolors=256 * 256) or []
    width, height = image.size
    margin_x = max(1, int(width * 0.035))
    margin_y = max(1, int(height * 0.035))
    safe_mask = Image.new("L", image.size, 0)
    safe_mask.paste(255, (margin_x, margin_y, width - margin_x, height - margin_y))
    border_mask = ImageOps.invert(safe_mask)
    bright = grayscale.point(lambda value: 255 if value >= 225 else 0)
    bright_border = ImageChops.multiply(bright, border_mask)
    bright_border_ratio = ImageStat.Stat(bright_border).sum[0] / (255 * width * height)
    hsv = image.convert("HSV")
    saturation = hsv.getchannel("S").point(lambda value: 255 if value >= 130 else 0)
    value = hsv.getchannel("V").point(lambda item: 255 if item >= 100 else 0)
    saturated_bright = ImageChops.multiply(saturation, value)
    saturated_border = ImageChops.multiply(saturated_bright, border_mask)
    saturated_border_ratio = ImageStat.Stat(saturated_border).sum[0] / (255 * width * height)
    return {
        "path": str(path),
        "stddev": round(standard_deviation, 3),
        "color_count": len(colors),
        "bright_border_ratio": round(bright_border_ratio, 6),
        "saturated_border_ratio": round(saturated_border_ratio, 6),
        "hash": average_hash(image),
    }


def discover_history_videos(history_root, project):
    candidates = []
    for directory in Path(history_root).iterdir():
        if not directory.is_dir() or directory.resolve() == Path(project).resolve():
            continue
        for relative in ("renders/main_video.mp4", "main_video.mp4"):
            video = directory / relative
            if video.is_file():
                candidates.append(video)
                break
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[:5]


def compare_history(style_video, history_root, project, temporary_dir):
    current_paths = extract_frames(style_video, temporary_dir, "current-style")
    current_hashes = [frame_metrics(path)["hash"] for path in current_paths]
    comparisons = []
    for index, video in enumerate(discover_history_videos(history_root, project), start=1):
        try:
            old_paths = extract_frames(video, temporary_dir, f"history-{index}")
            old_hashes = [frame_metrics(path)["hash"] for path in old_paths]
            similarities = [hash_similarity(current, old) for current, old in zip(current_hashes, old_hashes)]
            comparisons.append(
                {
                    "video": str(video),
                    "similarity": round(sum(similarities) / len(similarities), 4),
                }
            )
        except (OSError, RuntimeError, ValueError):
            continue
    return comparisons


def execute(args):
    video = Path(args.video).resolve()
    style_video = Path(args.style_video).resolve()
    if not video.is_file() or not style_video.is_file():
        raise RuntimeError("视觉校验输入视频不存在")
    report = {
        "schema_version": 1,
        "video": str(video),
        "style_video": str(style_video),
        "frames": [],
        "history_comparisons": [],
        "warnings": [],
        "errors": [],
    }
    with tempfile.TemporaryDirectory(prefix="hyperframes-visual-") as directory:
        temporary_dir = Path(directory)
        final_frames = extract_frames(video, temporary_dir, "final")
        final_hashes = []
        for frame in final_frames:
            metrics = frame_metrics(frame)
            final_hashes.append(metrics["hash"])
            metrics.pop("hash")
            report["frames"].append(metrics)
            if metrics["stddev"] < 7 or metrics["color_count"] < 12:
                report["errors"].append(f"疑似空白或无有效内容帧: {frame}")
            if metrics["bright_border_ratio"] > 0.012:
                report["errors"].append(
                    f"高亮内容进入 3.5% 安全区，可能发生文字或装饰裁切: {frame}"
                )
            if metrics["bright_border_ratio"] > 0.0005 and metrics["saturated_border_ratio"] > 0.0015:
                report["errors"].append(
                    f"高饱和强调元素紧贴画面边缘，可能发生装饰裁切: {frame}"
                )
        if len(final_hashes) >= 2:
            similarities = [
                hash_similarity(left, right)
                for left, right in zip(final_hashes, final_hashes[1:])
            ]
            report["frame_change"] = {
                "adjacent_similarities": [round(item, 4) for item in similarities],
                "max_similarity": round(max(similarities), 4),
                "average_similarity": round(sum(similarities) / len(similarities), 4),
            }
            if min(similarities) >= 0.985:
                report["errors"].append("关键帧之间变化不足，疑似时间轴未推进或主体内容未切换")
        report["history_comparisons"] = compare_history(
            style_video, args.history_root, args.project, temporary_dir
        )
        if report["history_comparisons"]:
            nearest = max(report["history_comparisons"], key=lambda item: item["similarity"])
            if nearest["similarity"] >= 0.97:
                report["errors"].append(
                    f"与历史主视频视觉相似度过高: {nearest['similarity']:.1%} ({nearest['video']})"
                )
            elif nearest["similarity"] >= 0.93:
                report["warnings"].append(
                    f"与历史主视频较相似: {nearest['similarity']:.1%} ({nearest['video']})"
                )
    report["status"] = "failed" if report["errors"] else "passed"
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for warning in report["warnings"]:
        print(f"⚠️ {warning}")
    for error in report["errors"]:
        print(f"❌ {error}")
    if report["errors"]:
        return 1
    print(f"✅ 视觉门禁通过: {report_path}")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    parser.add_argument("--style-video", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--history-root", required=True)
    parser.add_argument("--report", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(execute(parse_args()))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"❌ 视觉校验失败: {exc}")
        raise SystemExit(1)
