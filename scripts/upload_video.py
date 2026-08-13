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
    from workflow_config import config_path, load_workflow_config
except ModuleNotFoundError:
    from scripts.workflow_config import config_path, load_workflow_config


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


def execute(args):
    project = Path(args.project).expanduser().resolve()
    video = Path(args.video).expanduser().resolve()
    style_plan = Path(args.style_plan).expanduser().resolve()
    style_history = Path(args.style_history).expanduser().resolve()
    if not video.is_file():
        raise RuntimeError(f"视频不存在: {video}")
    manifest_path, manifest = load_manifest(project)
    if manifest.get("status") not in {"validated", "success"}:
        raise RuntimeError(f"manifest 状态不是 validated: {manifest.get('status')}")
    video_hash = sha256_file(video)
    expected_hash = manifest.get("outputs", {}).get("final_video_sha256")
    if expected_hash != video_hash:
        raise RuntimeError("最终视频 SHA256 与 manifest 不一致，禁止上传")

    marker_path = project / "final" / "upload-success.json"
    if marker_path.is_file():
        with open(marker_path, encoding="utf-8") as handle:
            marker = json.load(handle)
        if marker.get("video_sha256") == video_hash and marker.get("status") == "success":
            done_dir = Path(marker.get("done_dir", ""))
            if not done_dir.is_dir():
                raise RuntimeError(f"上传 marker 存在但 done_dir 不存在: {done_dir}")
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
            run_manifest = project / "runs" / str(manifest.get("run_id")) / "run-manifest.json"
            if run_manifest.parent.is_dir():
                atomic_json(run_manifest, manifest)
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
        run_manifest = project / "runs" / str(manifest.get("run_id")) / "run-manifest.json"
        if run_manifest.parent.is_dir():
            atomic_json(run_manifest, manifest)
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
