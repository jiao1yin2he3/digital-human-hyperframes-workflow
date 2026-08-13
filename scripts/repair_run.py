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
    parser.add_argument("--text", help="口播稿文件路径，默认自动从项目内读取")
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

    try:
        from pipeline_daily import validate_voiceover
    except ModuleNotFoundError:
        from scripts.pipeline_daily import validate_voiceover
    try:
        from tts_quality import effective_voiceover_chars, duration_gate
    except ModuleNotFoundError:
        from scripts.tts_quality import effective_voiceover_chars, duration_gate

    max_final_duration = float(get_config_value(CONFIG, "pipeline.max_final_duration", 59.5))
    end_padding_seconds = max(0.0, float(get_config_value(CONFIG, "pipeline.end_padding_seconds", 1.2)))
    audio_speed = float(get_config_value(CONFIG, "pipeline.audio_speed", 1.30))
    voiceover_min_chars = int(get_config_value(CONFIG, "pipeline.voiceover_min_chars", 260) or 260)
    voiceover_max_chars = int(get_config_value(CONFIG, "pipeline.voiceover_max_chars", 290) or 290)

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

    # 1. 校验口播稿
    text_path = None
    if args.text:
        text_path = Path(args.text).expanduser().resolve()
    else:
        candidates = [project / "口播稿.txt", project / "text.txt"]
        prev_manifest_path = project / "run-manifest.json"
        if prev_manifest_path.is_file():
            try:
                with open(prev_manifest_path, encoding="utf-8") as handle:
                    pm = json.load(handle)
                if pm.get("outputs", {}).get("text"):
                    candidates.insert(0, Path(pm["outputs"]["text"]))
            except Exception:
                pass
        for cand in candidates:
            if cand.is_file():
                text_path = cand.resolve()
                break

    if not text_path or not text_path.is_file():
        raise RuntimeError("无法取得有效口播稿路径 (--text 或项目内 口播稿.txt 不存在)，禁止生成 validated")
    try:
        text_path = text_path.resolve()
        text_path.relative_to(project)
    except (OSError, ValueError) as exc:
        raise RuntimeError("口播稿必须位于当前项目目录内，禁止使用项目外历史文件") from exc

    voiceover_text = text_path.read_text(encoding="utf-8").strip()
    validate_voiceover(voiceover_text)
    eff_chars = effective_voiceover_chars(voiceover_text)

    # 2. 校验音频文件名与时长
    if paths["audio"].name != "voiceover_130.wav":
        raise RuntimeError(f"定稿音频文件名必须为 voiceover_130.wav，当前为: {paths['audio'].name}")

    audio_duration = float(ffprobe_value(paths["audio"], "format=duration"))
    gate = duration_gate(audio_duration, end_padding_seconds, max_final_duration)
    if not gate["passed"]:
        raise RuntimeError(
            f"预检时长超限: 音频 {audio_duration:.2f}s + 缓冲 {end_padding_seconds:.2f}s = "
            f"{gate['estimated_final_duration']:.2f}s > 上限 {max_final_duration:.2f}s"
        )

    # 3. 校验 Whisper + F0
    verify_res = run_checked("verify-audio", [SYS_PY, ROOT / "scripts" / "whisper_f0.py", "--audio", paths["audio"]])
    try:
        raw_out = verify_res["output_tail"].strip().splitlines()[-1]
        whisper_data = json.loads(raw_out)
        whisper_text = str(whisper_data["text"]).strip()
        f0 = float(whisper_data["f0"])
    except Exception as exc:
        raise RuntimeError(f"Whisper + F0 校验输出解析失败: {exc}")
    if len(whisper_text) < 20:
        raise RuntimeError(f"Whisper 可识别文本过短 ({len(whisper_text)} 字 < 20 字)")
    if not (80 <= f0 <= 180):
        raise RuntimeError(f"F0={f0:.1f}Hz 不在 80-180Hz 合同范围内")

    # 4. 校验 TTS 报告（若存在）
    report_file = None
    for rf in [run_dir / "tts-synthesis-report.json", project / "tts-synthesis-report.json"]:
        if rf.is_file():
            report_file = rf
            break
    synth_report = None
    if not report_file:
        raise RuntimeError("缺少 tts-synthesis-report.json，无法证明当前音频通过逐段 TTS 质量门禁，禁止生成 validated")
    try:
        with open(report_file, encoding="utf-8") as handle:
            synth_report = json.load(handle)
        audit = synth_report.get("f0_audit", {})
        content = synth_report.get("content_acceptance", {})
        if not audit or not audit.get("passed", False):
            raise RuntimeError(f"TTS 逐段 F0 审计未通过: fallback={audit.get('fallback_count')}")
        if not content or not content.get("passed", False):
            raise RuntimeError(f"TTS 内容验收未通过: similarity={content.get('similarity')}")
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"TTS 合成报告解析失败: {exc}") from exc

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
            "--min-different",
            "5",
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
    if duration > max_final_duration:
        raise RuntimeError(f"成片视频时长 {duration:.2f}s 超过上限 {max_final_duration:.2f}s")
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
        "policy": {
            "max_final_duration": max_final_duration,
            "end_padding_seconds": end_padding_seconds,
            "audio_speed": audio_speed,
            "voiceover_min_chars": voiceover_min_chars,
            "voiceover_max_chars": voiceover_max_chars,
            "effective_chars": eff_chars,
            "whisper_f0": f0,
            "whisper_text_len": len(whisper_text),
        },
        "steps": steps,
        "outputs": {
            "html": str(paths["html"]),
            "text": str(text_path),
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
    if synth_report:
        manifest["tts_synthesis_report"] = synth_report
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
