#!/usr/bin/env python3
"""Register a repaired project after rerendering artifacts manually."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from workflow_config import config_path, load_workflow_config
except ModuleNotFoundError:
    from scripts.workflow_config import config_path, load_workflow_config


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_workflow_config(ROOT)
SYS_PY = Path(os.environ.get("HFW_SYS_PY", config_path(ROOT, CONFIG, "paths.sys_python"))).expanduser()
SAD_PY = config_path(ROOT, CONFIG, "paths.sadtalker_python")
INDX_PY = config_path(ROOT, CONFIG, "paths.indextts_python")
STYLE_GUIDE = config_path(ROOT, CONFIG, "paths.style_guide")
PROJECTS_DIR = config_path(ROOT, CONFIG, "paths.projects_dir")


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def run_checked(step, command, timeout=600, cwd=None):
    result = subprocess.run(
        [str(item) for item in command],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd or ROOT),
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"{step} failed ({result.returncode}): {output[-1200:]}")
    return {"status": "success", "returncode": 0, "output_tail": output[-2000:]}


def find_pil_python():
    env_python = os.environ.get("HFW_PIL_PY")
    candidates = [
        Path(env_python).expanduser() if env_python else None,
        SYS_PY,
        Path(sys.executable),
        Path(shutil.which("python3")) if shutil.which("python3") else None,
        Path("/usr/bin/python3"),
        Path("/Library/Developer/CommandLineTools/usr/bin/python3"),
        SAD_PY,
        INDX_PY,
    ]
    seen = set()
    for python in candidates:
        if not python:
            continue
        python = Path(python)
        if str(python) in seen:
            continue
        seen.add(str(python))
        if python.exists():
            result = subprocess.run([str(python), "-c", "import PIL"], capture_output=True)
            if result.returncode == 0:
                return python
    raise RuntimeError("未找到包含 Pillow 的 Python 解释器")


def ffprobe_value(path, entries, stream=None):
    command = ["ffprobe", "-v", "error"]
    if stream:
        command.extend(["-select_streams", stream])
    command.extend(["-show_entries", entries, "-of", "csv=p=0", path])
    result = subprocess.run([str(item) for item in command], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="项目目录，例如 projects/topic")
    parser.add_argument("--name", help="项目 slug；默认取项目目录名")
    parser.add_argument("--html", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--digital-human", required=True)
    parser.add_argument("--main-video", required=True)
    parser.add_argument("--final-video", required=True)
    parser.add_argument("--style-plan", required=True)
    parser.add_argument("--style-history", default=str(ROOT / "STYLE_HISTORY.md"))
    parser.add_argument("--run-id", help="默认 repair-YYYYmmdd-HHMMSS")
    return parser.parse_args()


def main():
    args = parse_args()
    project = Path(args.project).expanduser().resolve()
    name = args.name or project.name
    run_id = args.run_id or f"repair-{datetime.now().astimezone():%Y%m%d-%H%M%S}"
    run_dir = project / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    pil_python = find_pil_python()

    paths = {
        "html": Path(args.html).expanduser().resolve(),
        "audio": Path(args.audio).expanduser().resolve(),
        "digital_human": Path(args.digital_human).expanduser().resolve(),
        "main_video": Path(args.main_video).expanduser().resolve(),
        "final_video": Path(args.final_video).expanduser().resolve(),
        "style_plan": Path(args.style_plan).expanduser().resolve(),
        "style_history": Path(args.style_history).expanduser().resolve(),
    }
    missing = [label for label, path in paths.items() if not path.is_file()]
    if missing:
        raise RuntimeError(f"missing repair inputs: {', '.join(missing)}")

    steps = {}
    steps["style-html-gate"] = run_checked(
        "style-html-gate",
        [
            SYS_PY,
            ROOT / "scripts" / "validate_style.py",
            "--plan",
            paths["style_plan"],
            "--history",
            paths["style_history"],
            "--guide",
            STYLE_GUIDE,
            "--project",
            name,
            "--html",
            paths["html"],
        ],
    )
    steps["timeline-full"] = run_checked(
        "timeline-full",
        [
            SYS_PY,
            ROOT / "scripts" / "pre_composite_check.py",
            "--project",
            project,
            "--full",
            "--audio",
            paths["audio"],
            "--digital-human",
            paths["digital_human"],
            "--main-video",
            paths["main_video"],
            "--html",
            paths["html"],
        ],
    )
    visual_report = run_dir / "visual-report.json"
    steps["visual-gate"] = run_checked(
        "visual-gate",
        [
            pil_python,
            ROOT / "scripts" / "validate_video_visuals.py",
            "--video",
            paths["final_video"],
            "--style-video",
            paths["main_video"],
            "--project",
            project,
            "--history-root",
            PROJECTS_DIR,
            "--report",
            visual_report,
        ],
    )

    duration = float(ffprobe_value(paths["final_video"], "format=duration"))
    resolution = ffprobe_value(paths["final_video"], "stream=width,height", stream="v:0").replace(",", "x")
    audio_streams = [
        line for line in ffprobe_value(paths["final_video"], "stream=index", stream="a").splitlines() if line.strip()
    ]
    if resolution != "1920x1080":
        raise RuntimeError(f"final resolution must be 1920x1080, got {resolution}")
    if len(audio_streams) != 1:
        raise RuntimeError(f"final video must have exactly one audio stream, got {len(audio_streams)}")

    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "project": name,
        "status": "validated",
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "repair_run": True,
        "steps": steps,
        "outputs": {
            "html": str(paths["html"]),
            "final_audio": str(paths["audio"]),
            "digital_human": str(paths["digital_human"]),
            "main_video": str(paths["main_video"]),
            "final_video": str(paths["final_video"]),
            "canonical_final_video": str(project / "final" / "final_video.mp4"),
            "visual_report": str(visual_report),
            "final_video_sha256": sha256_file(paths["final_video"]),
            "duration": duration,
            "resolution": resolution,
            "audio_streams": len(audio_streams),
        },
        "upload": {"status": "not_requested"},
    }
    atomic_json(run_dir / "run-manifest.json", manifest)
    atomic_json(project / "run-manifest.json", manifest)
    print(f"repair manifest written: {project / 'run-manifest.json'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"repair failed: {exc}", file=sys.stderr)
        sys.exit(1)
