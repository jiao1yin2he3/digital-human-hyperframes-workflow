#!/usr/bin/env python3
"""对已验证视频执行幂等 Bilibili 上传并完成风格历史归档。"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from workflow_config import config_path, get_config_value, load_workflow_config
except ModuleNotFoundError:
    from scripts.workflow_config import config_path, get_config_value, load_workflow_config


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_CONFIG = load_workflow_config(ROOT)
BILIUP_ROOT = config_path(ROOT, WORKFLOW_CONFIG, "paths.biliup_root")
PIPELINE_ROOT = BILIUP_ROOT / "pipeline"
SYS_PY = config_path(ROOT, WORKFLOW_CONFIG, "paths.sys_python")


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


def run_checked(command, timeout, cwd=None):
    result = subprocess.run(
        [str(item) for item in command],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd or ROOT),
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"命令失败({result.returncode}): {output[-1200:]}")
    return result


def find_job(directory, job_name):
    if not directory.exists():
        return None
    matches = [path for path in directory.iterdir() if path.is_dir() and path.name.endswith(job_name)]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def load_manifest(project):
    path = project / "run-manifest.json"
    if not path.is_file():
        raise RuntimeError(f"缺少运行 manifest: {path}")
    with open(path, encoding="utf-8") as handle:
        return path, json.load(handle)


def ffprobe_value(path, entries, stream=None):
    command = ["ffprobe", "-v", "error"]
    if stream:
        command.extend(["-select_streams", stream])
    command.extend(["-show_entries", entries, "-of", "csv=p=0", path])
    result = subprocess.run([str(item) for item in command], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def execute(args):
    project = Path(args.project).expanduser().resolve()
    video = Path(args.video).expanduser().resolve()
    style_plan = Path(args.style_plan).expanduser().resolve()
    style_history = Path(args.style_history).expanduser().resolve()

    # 1. 路径安全性与边界校验
    if not video.is_file():
        raise RuntimeError(f"视频不存在: {video}")
    try:
        video.relative_to(project)
    except ValueError:
        raise RuntimeError(f"非法视频路径: {video} 不在项目目录 {project} 内，拒绝上传")

    if not style_plan.is_file():
        raise RuntimeError(f"style plan 不存在: {style_plan}")
    if not style_history.is_file():
        raise RuntimeError(f"style history 不存在: {style_history}")

    # 2. Manifest 一致性校验
    manifest_path, manifest = load_manifest(project)
    if manifest.get("status") not in {"validated", "success"}:
        raise RuntimeError(f"manifest 状态不是 validated/success: {manifest.get('status')}")
    if manifest.get("project") != args.name:
        raise RuntimeError(f"manifest 中的 project ({manifest.get('project')}) 与 --name ({args.name}) 不一致")

    run_id = manifest.get("run_id")
    if not run_id:
        raise RuntimeError("manifest 缺少有效 run_id")
    run_manifest_path = project / "runs" / str(run_id) / "run-manifest.json"
    if not run_manifest_path.is_file():
        raise RuntimeError(f"子 run-manifest 不存在: {run_manifest_path}")
    with open(run_manifest_path, encoding="utf-8") as handle:
        run_manifest = json.load(handle)
    if run_manifest.get("run_id") != run_id or run_manifest.get("project") != args.name:
        raise RuntimeError("run-manifest 与顶层 manifest 关键字段不匹配")

    # 3. 校验产物路径合法性
    outputs = manifest.get("outputs", {})
    html_path = Path(outputs.get("html", project / "index.html")).expanduser().resolve()
    audio_path = Path(outputs.get("final_audio", project / "audio" / "voiceover_130.wav")).expanduser().resolve()
    for label, path in (("HTML", html_path), ("定稿音频", audio_path)):
        try:
            path.relative_to(project)
        except ValueError:
            raise RuntimeError(f"manifest {label} 路径不在项目目录内: {path}")
    if not html_path.is_file():
        raise RuntimeError(f"manifest HTML 产物不存在: {html_path}")
    if not audio_path.is_file() or audio_path.name != "voiceover_130.wav":
        raise RuntimeError(f"manifest 音频产物非法: {audio_path}")
    policy = manifest.get("policy") or manifest.get("duration_policy")
    if not isinstance(policy, dict):
        raise RuntimeError("manifest 缺少当前 duration policy，禁止上传")
    configured_limit = float(get_config_value(WORKFLOW_CONFIG, "pipeline.max_final_duration", 59.5))
    policy_limit = policy.get("max_final_duration")
    policy_speed = policy.get("audio_speed")
    policy_min_chars = policy.get("voiceover_min_chars")
    policy_max_chars = policy.get("voiceover_max_chars")
    if policy_limit is None or float(policy_limit) != configured_limit:
        raise RuntimeError("manifest duration policy 与当前配置不一致，禁止上传")
    configured_speed = float(str(get_config_value(WORKFLOW_CONFIG, "pipeline.audio_speed", 1.30)))
    configured_min_chars = int(str(get_config_value(WORKFLOW_CONFIG, "pipeline.voiceover_min_chars", 260) or 260))
    configured_max_chars = int(str(get_config_value(WORKFLOW_CONFIG, "pipeline.voiceover_max_chars", 290) or 290))
    if policy_speed is None or float(policy_speed) != configured_speed:
        raise RuntimeError("manifest audio_speed 与当前配置不一致，禁止上传")
    if policy_min_chars is None or int(policy_min_chars) != configured_min_chars:
        raise RuntimeError("manifest voiceover_min_chars 与当前配置不一致，禁止上传")
    if policy_max_chars is None or int(policy_max_chars) != configured_max_chars:
        raise RuntimeError("manifest voiceover_max_chars 与当前配置不一致，禁止上传")

    # 4. 重新执行成片媒体参数校验
    video_hash = sha256_file(video)
    expected_hash = outputs.get("final_video_sha256")
    if expected_hash != video_hash:
        raise RuntimeError("最终视频 SHA256 与 manifest 不一致，禁止上传")

    max_final_duration = float(get_config_value(WORKFLOW_CONFIG, "pipeline.max_final_duration", 59.5))
    duration = float(ffprobe_value(video, "format=duration"))
    if duration > max_final_duration:
        raise RuntimeError(f"成片视频时长 {duration:.2f}s 超过上限 {max_final_duration:.2f}s")

    resolution = ffprobe_value(video, "stream=width,height", stream="v:0").replace(",", "x")
    if resolution != "1920x1080":
        raise RuntimeError(f"成片分辨率必须为 1920x1080，当前为 {resolution}")

    audio_streams = [
        line for line in ffprobe_value(video, "stream=index", stream="a").splitlines() if line.strip()
    ]
    if len(audio_streams) != 1:
        raise RuntimeError(f"成片必须恰好包含一条音频流，当前包含 {len(audio_streams)} 条")

    # 5. 重新执行 Style Gate 校验 (显式传 --min-different 5)
    style_guide = config_path(ROOT, WORKFLOW_CONFIG, "paths.style_guide")
    run_checked(
        [
            SYS_PY,
            ROOT / "scripts" / "validate_style.py",
            "--plan",
            style_plan,
            "--history",
            style_history,
            "--guide",
            style_guide,
            "--project",
            args.name,
            "--html",
            html_path,
            "--min-different",
            "5",
        ],
        timeout=60,
    )

    # 6. 重新执行 Timeline Full Gate 校验
    dh_path = outputs.get("digital_human")
    mv_path = outputs.get("main_video")
    if not dh_path or not mv_path:
        raise RuntimeError("manifest 缺少 digital_human/main_video 输出路径，禁止上传")
    for label, raw_path in (("数字人", dh_path), ("主视频", mv_path)):
        path = Path(raw_path).expanduser().resolve()
        try:
            path.relative_to(project)
        except ValueError:
            raise RuntimeError(f"manifest {label} 路径不在项目目录内: {path}")
        if not path.is_file():
            raise RuntimeError(f"manifest {label} 产物不存在: {path}")
    run_checked(
        [
            SYS_PY,
            ROOT / "scripts" / "pre_composite_check.py",
            "--project",
            project,
            "--full",
            "--audio",
            audio_path,
            "--digital-human",
            dh_path,
            "--main-video",
            mv_path,
            "--html",
            html_path,
        ],
        timeout=60,
    )

    # 7. 重新校验口播稿及音频 F0
    try:
        from pipeline_daily import validate_voiceover
    except ModuleNotFoundError:
        from scripts.pipeline_daily import validate_voiceover
    text_path_str = outputs.get("text") or str(project / "口播稿.txt")
    text_file = Path(text_path_str).expanduser().resolve()
    if not text_file.is_file():
        raise RuntimeError(f"缺少有效口播稿: {text_file}")
    validate_voiceover(text_file.read_text(encoding="utf-8").strip())

    verify_res = run_checked([SYS_PY, ROOT / "scripts" / "whisper_f0.py", "--audio", audio_path], timeout=300)
    try:
        raw_out = verify_res.stdout.strip().splitlines()[-1]
        whisper_data = json.loads(raw_out)
        f0 = float(whisper_data["f0"])
        text_len = len(str(whisper_data["text"]).strip())
    except Exception as exc:
        raise RuntimeError(f"Whisper + F0 校验输出解析失败: {exc}")
    if text_len < 20 or not (80 <= f0 <= 180):
        raise RuntimeError(f"音频门禁未通过: text_len={text_len}, f0={f0:.1f}Hz")
    tts_report = manifest.get("tts_synthesis_report")
    if not isinstance(tts_report, dict):
        raise RuntimeError("manifest 缺少 tts_synthesis_report，禁止上传")
    audit = tts_report.get("f0_audit")
    content = tts_report.get("content_acceptance")
    if not isinstance(audit, dict) or not audit.get("passed", False):
        raise RuntimeError("TTS 逐段 F0 审计未通过或缺失，禁止上传")
    if not isinstance(content, dict) or not content.get("passed", False):
        raise RuntimeError("TTS 内容验收未通过或缺失，禁止上传")

    # 8. 幂等 Marker 检查
    marker_path = project / "final" / "upload-success.json"
    if marker_path.is_file():
        with open(marker_path, encoding="utf-8") as handle:
            marker = json.load(handle)
        if marker.get("video_sha256") == video_hash and marker.get("status") == "success":
            done_dir = Path(marker.get("done_dir", ""))
            if not done_dir.is_dir():
                raise RuntimeError(
                    f"上传 marker 存在但 done_dir 不存在: {done_dir}；禁止标记为 success，拒绝半成功状态"
                )
            append_result = run_checked(
                [
                    SYS_PY,
                    ROOT / "scripts" / "append_style_history.py",
                    "--plan",
                    style_plan,
                    "--history",
                    style_history,
                ],
                timeout=60,
            )
            print(append_result.stdout.strip())
            marker["run_id"] = manifest.get("run_id")
            marker["idempotent_reuse"] = True
            manifest["status"] = "success"
            manifest["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            manifest["upload"] = marker
            atomic_json(manifest_path, manifest)
            if run_manifest_path.parent.is_dir():
                atomic_json(run_manifest_path, manifest)
            print(f"幂等命中：该视频已上传，已同步当前 manifest ({marker_path})")
            return 0

    lock_handle = None
    if not args.reuse_pipeline_lock:
        lock_path = ROOT / "runtime" / "pipeline.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("另一个流水线或上传任务正在运行") from exc

    try:
        job_name = f"hf-{args.name}-{video_hash[:16]}"
        done_job = find_job(PIPELINE_ROOT / "done", job_name)
        failed_job = find_job(PIPELINE_ROOT / "failed", job_name)
        queued_job = find_job(PIPELINE_ROOT / "jobs", job_name)
        running_job = find_job(PIPELINE_ROOT / "in_progress", job_name)
        if failed_job:
            raise RuntimeError(f"相同视频存在失败任务，请先处理: {failed_job}")
        if not done_job and not queued_job and not running_job:
            enqueue = PIPELINE_ROOT / "bin" / "enqueue-video.sh"
            command = [
                enqueue,
                "--video",
                video,
                "--title",
                args.title,
                "--desc",
                args.desc,
                "--dynamic",
                args.dynamic,
                "--tag",
                args.tag,
                "--job-name",
                job_name,
            ]
            if args.public:
                command.append("--public")
            run_checked(command, timeout=300)

        if not done_job:
            publisher = PIPELINE_ROOT / "bin" / "publish-queue.sh"
            publish_result = run_checked([publisher], timeout=1800, cwd=BILIUP_ROOT)
            status_lines = [
                line for line in publish_result.stdout.splitlines() if any(key in line for key in ("START", "DONE", "FAILED"))
            ]
            for line in status_lines:
                print(line)
            done_job = find_job(PIPELINE_ROOT / "done", job_name)
        if not done_job:
            failed_job = find_job(PIPELINE_ROOT / "failed", job_name)
            detail = f"，失败目录: {failed_job}" if failed_job else ""
            raise RuntimeError(f"上传命令结束后未找到 done marker{detail}")

        append_result = run_checked(
            [
                SYS_PY,
                ROOT / "scripts" / "append_style_history.py",
                "--plan",
                style_plan,
                "--history",
                style_history,
            ],
            timeout=60,
        )
        print(append_result.stdout.strip())
        marker = {
            "schema_version": 1,
            "status": "success",
            "project": args.name,
            "run_id": manifest.get("run_id"),
            "video": str(video),
            "video_sha256": video_hash,
            "job_name": job_name,
            "done_dir": str(done_job),
            "visibility": "public" if args.public else "self_only",
            "uploaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        atomic_json(marker_path, marker)
        manifest["status"] = "success"
        manifest["finished_at"] = marker["uploaded_at"]
        manifest["upload"] = marker
        atomic_json(manifest_path, manifest)
        if run_manifest_path.parent.is_dir():
            atomic_json(run_manifest_path, manifest)
        print(f"✅ 上传完成且已写入幂等 marker: {marker_path}")
        return 0
    finally:
        if lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--style-plan", required=True)
    parser.add_argument("--style-history", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--desc", default="")
    parser.add_argument("--dynamic", default="")
    parser.add_argument("--tag", default="AI生成,自动化")
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--reuse-pipeline-lock", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(execute(parse_args()))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"❌ 上传失败: {exc}", file=sys.stderr)
        sys.exit(1)
