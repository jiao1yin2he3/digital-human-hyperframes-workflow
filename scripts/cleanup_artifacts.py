#!/usr/bin/env python3
"""清理历史运行目录和渲染变体；默认仅预览，不删除。"""

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def candidates(keep_runs, keep_variants):
    removals = []
    for project in (ROOT / "projects").iterdir():
        if not project.is_dir():
            continue
        runs = sorted(
            (path for path in (project / "runs").glob("*") if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        removals.extend(runs[keep_runs:])
        variants = sorted(
            (
                path
                for path in (project / "renders").glob("main_video*.mp4")
                if path.name != "main_video.mp4"
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        removals.extend(variants[keep_variants:])
    output = ROOT / "output"
    removals.extend(path for path in output.glob("tts-segments-*") if path.is_dir())
    return removals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-runs", type=int, default=5)
    parser.add_argument("--keep-variants", type=int, default=2)
    parser.add_argument("--execute", action="store_true", help="实际删除；默认只预览")
    args = parser.parse_args()
    removals = candidates(args.keep_runs, args.keep_variants)
    total = sum(path.stat().st_size if path.is_file() else sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) for path in removals)
    for path in removals:
        print(f"{'DELETE' if args.execute else 'WOULD_DELETE'} {path}")
        if args.execute:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    print(f"候选 {len(removals)} 项，约 {total / 1024 / 1024:.1f} MiB")


if __name__ == "__main__":
    main()
