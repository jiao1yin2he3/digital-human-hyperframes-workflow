#!/usr/bin/env python3
"""HyperFrames 每日视频生产流水线。

默认只生成并完成机器校验，不自动上传。需要上传时显式传入 ``--upload``，
或在成功后单独运行 ``scripts/upload_video.py``。
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import yaml

try:
    from workflow_config import config_path, get_config_value, load_workflow_config
except ModuleNotFoundError:
    from scripts.workflow_config import config_path, get_config_value, load_workflow_config
try:
    from tts_quality import DEFAULT_NEWS_EMOTION_TEXT
except ModuleNotFoundError:
    from scripts.tts_quality import DEFAULT_NEWS_EMOTION_TEXT


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_CONFIG = load_workflow_config(ROOT)
SYS_PY = config_path(ROOT, WORKFLOW_CONFIG, "paths.sys_python")
INDX_PY = config_path(ROOT, WORKFLOW_CONFIG, "paths.indextts_python")
SAD_PY = config_path(ROOT, WORKFLOW_CONFIG, "paths.sadtalker_python")
SAD_DIR = config_path(ROOT, WORKFLOW_CONFIG, "paths.sadtalker_dir")
CKPT_DIR = config_path(ROOT, WORKFLOW_CONFIG, "paths.sadtalker_checkpoints")
STYLE_GUIDE = config_path(ROOT, WORKFLOW_CONFIG, "paths.style_guide")
PROJ_DIR = config_path(ROOT, WORKFLOW_CONFIG, "paths.projects_dir")
HYPERFRAMES_BIN = config_path(ROOT, WORKFLOW_CONFIG, "paths.hyperframes_bin")
try:
    VOICEOVER_MIN_CHARS = int(get_config_value(WORKFLOW_CONFIG, "pipeline.voiceover_min_chars", 260) or 260)
except (TypeError, ValueError):
    VOICEOVER_MIN_CHARS = 260
try:
    VOICEOVER_MAX_CHARS = int(get_config_value(WORKFLOW_CONFIG, "pipeline.voiceover_max_chars", 290) or 290)
except (TypeError, ValueError):
    VOICEOVER_MAX_CHARS = 290
try:
    AUDIO_SPEED = float(get_config_value(WORKFLOW_CONFIG, "pipeline.audio_speed", 1.30))
except (TypeError, ValueError):
    AUDIO_SPEED = 1.30
try:
    MAX_FINAL_DURATION = float(get_config_value(WORKFLOW_CONFIG, "pipeline.max_final_duration", 59.5))
except (TypeError, ValueError):
    MAX_FINAL_DURATION = 59.5
try:
    TTS_INTERVAL_SILENCE = max(0.0, float(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_interval_silence", 0.2)))
except (TypeError, ValueError):
    TTS_INTERVAL_SILENCE = 0.2
try:
    TTS_REQUIRED_KEYWORDS = [
        item.strip()
        for item in str(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_required_keywords", "")).split(",")
        if item.strip()
    ]
except (TypeError, ValueError, AttributeError):
    TTS_REQUIRED_KEYWORDS = []
try:
    TTS_EMO_ALPHA = float(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_emo_alpha", 0.6))
except (TypeError, ValueError):
    TTS_EMO_ALPHA = 0.6
TTS_EMO_TEXT = str(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_emo_text", "") or "").strip() or None
TTS_EMO_AUDIO_PROMPT = str(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_emo_audio_prompt", "") or "").strip() or None
TTS_USE_EMO_TEXT = bool(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_use_emo_text", True))
TTS_USE_RANDOM = bool(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_use_random", False))
try:
    TTS_TEMPERATURE = float(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_temperature", 0.75))
except (TypeError, ValueError):
    TTS_TEMPERATURE = 0.75
try:
    TTS_TOP_P = float(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_top_p", 0.85))
except (TypeError, ValueError):
    TTS_TOP_P = 0.85
try:
    TTS_TOP_K = int(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_top_k", 30))
except (TypeError, ValueError):
    TTS_TOP_K = 30
try:
    TTS_REPETITION_PENALTY = float(get_config_value(WORKFLOW_CONFIG, "pipeline.tts_repetition_penalty", 10.0))
except (TypeError, ValueError):
    TTS_REPETITION_PENALTY = 10.0
SADTALKER_POSE_STYLE = str(get_config_value(WORKFLOW_CONFIG, "pipeline.sadtalker_pose_style", "12"))
SADTALKER_SOURCE_MAX_HEIGHT = int(get_config_value(WORKFLOW_CONFIG, "pipeline.sadtalker_source_max_height", 720))
END_PADDING_SECONDS = max(0.0, float(get_config_value(WORKFLOW_CONFIG, "pipeline.end_padding_seconds", 1.2)))
END_VIDEO_FADE_SECONDS = max(0.0, float(get_config_value(WORKFLOW_CONFIG, "pipeline.end_video_fade_seconds", 0.45)))
END_AUDIO_FADE_SECONDS = max(0.0, float(get_config_value(WORKFLOW_CONFIG, "pipeline.end_audio_fade_seconds", 0.25)))
TOTAL_STEPS = 10
STYLE_DIMENSIONS = [
    "style_family",
    "palette",
    "typography",
    "composition",
    "scene_grammar",
    "motion_language",
    "transitions",
    "media_treatment",
    "pacing",
    "audio_direction",
]


class PipelineError(RuntimeError):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def resolve_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


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


def atomic_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def replace_output(temporary, destination):
    temporary = Path(temporary)
    destination = Path(destination)
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise PipelineError(f"命令未生成有效产物: {temporary}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, destination)


def is_reusable_file(path):
    path = Path(path)
    return path.is_file() and path.stat().st_size > 0


def mark_reused(pipeline, step, path):
    pipeline.record_step(step, "reused", output=str(path))


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
    raise PipelineError("未找到包含 Pillow 的 Python 解释器")


def validate_name(name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", name):
        raise PipelineError("--name 只允许 2-64 位小写字母、数字、下划线和连字符")


def update_html(html_content, captions, audio_duration, total_duration=None):
    captions_json = json.dumps(captions, ensure_ascii=False, indent=2)
    updated, caption_count = re.subn(
        r"const\s+captions\s*=\s*\[.*?\]\s*;",
        lambda _: f"const captions={captions_json};",
        html_content,
        count=1,
        flags=re.DOTALL,
    )
    if caption_count != 1:
        raise PipelineError("HTML 必须且只能包含一个 const captions=[...]; 占位数组")

    def replace_audio_tag(match):
        tag = match.group(0)
        if not re.search(r'\bid=["\']main-voiceover["\']', tag):
            return tag
        return re.sub(
            r'(\bsrc\s*=\s*["\'])[^"\']+(["\'])',
            r"\1audio/voiceover_130.wav\2",
            tag,
            count=1,
        )

    updated, audio_count = re.subn(
        r'<audio\b(?=[^>]*\bid=["\']main-voiceover["\'])(?=[^>]*\bsrc\s*=)[^>]*>',
        replace_audio_tag,
        updated,
        count=1,
        flags=re.DOTALL,
    )
    if audio_count != 1:
        raise PipelineError('HTML 必须包含 id="main-voiceover" 的 audio 标签')

    total_duration = audio_duration if total_duration is None else total_duration
    audio_duration_value = f"{audio_duration:.2f}"
    total_duration_value = f"{total_duration:.2f}"

    def replace_duration_value(match, duration_value):
        return re.sub(
            r'(\bdata-duration\s*=\s*["\'])[^"\']+(["\'])',
            rf"\g<1>{duration_value}\2",
            match.group(0),
            count=1,
        )

    updated, stage_count = re.subn(
        r'<[^>]+(?=[^>]*\bid=["\']stage["\'])(?=[^>]*\bdata-duration\s*=)[^>]*>',
        lambda match: replace_duration_value(match, total_duration_value),
        updated,
        count=1,
        flags=re.DOTALL,
    )
    updated, audio_duration_count = re.subn(
        r'<audio\b(?=[^>]*\bid=["\']main-voiceover["\'])(?=[^>]*\bdata-duration\s*=)[^>]*>',
        lambda match: replace_duration_value(match, audio_duration_value),
        updated,
        count=1,
        flags=re.DOTALL,
    )
    if stage_count != 1 or audio_duration_count != 1:
        raise PipelineError("HTML stage 和 main-voiceover 必须分别包含 data-duration")

    def strip_scene_timing(match):
        tag = match.group(0)
        tag = re.sub(r'\s+data-start\s*=\s*["\'][^"\']*["\']', "", tag)
        tag = re.sub(r'\s+data-duration\s*=\s*["\'][^"\']*["\']', "", tag)
        return tag

    updated = re.sub(
        r'<section\b(?=[^>]*\bclass=["\'][^"\']*\bscene\b)[^>]*>',
        strip_scene_timing,
        updated,
        flags=re.DOTALL,
    )

    updated = re.sub(
        r"\b(?:const|let|var)\s+totalDuration\s*=\s*[^;]+;",
        (
            "const totalDuration = "
            f"Number(document.getElementById('stage')?.dataset.duration) || {total_duration_value};"
        ),
        updated,
        count=1,
    )
    return updated


def wait_for_stable_mp4(run_dir, timeout=30.0, poll_interval=0.5):
    """Return an MP4 written completely inside this run directory."""
    run_dir = Path(run_dir).resolve()
    deadline = time.monotonic() + timeout
    previous = {}
    while True:
        candidates = []
        for path in run_dir.rglob("*.mp4"):
            try:
                if path.is_file() and path.stat().st_size > 0:
                    candidates.append(path)
            except OSError:
                continue
        stable = []
        for path in candidates:
            key = str(path)
            state = (path.stat().st_size, path.stat().st_mtime_ns)
            if previous.get(key) == state:
                stable.append(path)
            previous[key] = state
        if stable:
            return max(stable, key=lambda path: path.stat().st_mtime_ns)
        if time.monotonic() >= deadline:
            raise PipelineError(f"SadTalker 输出未完成: {run_dir}")
        time.sleep(poll_interval)


class PipelineRun:
    def __init__(self, args):
        validate_name(args.name)
        self.args = args
        self.name = args.name
        self.project_dir = PROJ_DIR / self.name
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.previous_manifest = self.load_previous_manifest()
        self.run_id = f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-{self.name}-{uuid.uuid4().hex[:8]}"
        self.run_dir = self.project_dir / "runs" / self.run_id
        self.lock_path = ROOT / "runtime" / "pipeline.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_handle = None
        self.manifest = {
            "schema_version": 1,
            "run_id": self.run_id,
            "project": self.name,
            "status": "running",
            "started_at": now_iso(),
            "updated_at": now_iso(),
            "steps": {},
            "outputs": {},
            "upload": {"status": "not_requested"},
        }

    def load_previous_manifest(self):
        path = self.project_dir / "run-manifest.json"
        if not path.is_file():
            return {}
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def acquire_lock(self):
        self.lock_handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineError(f"另一个流水线正在运行，锁文件: {self.lock_path}") from exc
        self.lock_handle.seek(0)
        self.lock_handle.truncate()
        self.lock_handle.write(json.dumps({"pid": os.getpid(), "run_id": self.run_id, "started_at": now_iso()}))
        self.lock_handle.flush()
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.write_manifest()

    def close(self):
        if self.lock_handle:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
            self.lock_handle.close()
            self.lock_handle = None

    def write_manifest(self):
        self.manifest["updated_at"] = now_iso()
        atomic_json(self.run_dir / "run-manifest.json", self.manifest)
        atomic_json(self.project_dir / "run-manifest.json", self.manifest)

    def set_status(self, status, error=None):
        self.manifest["status"] = status
        if error:
            self.manifest["error"] = str(error)
        if status in {"validated", "success", "failed"}:
            self.manifest["finished_at"] = now_iso()
        self.write_manifest()

    def record_step(self, step, status, **extra):
        record = self.manifest["steps"].setdefault(step, {})
        record.update({"status": status, "updated_at": now_iso(), **extra})
        self.write_manifest()

    def record_output(self, key, path):
        self.manifest["outputs"][key] = str(path)
        self.write_manifest()

    def run_checked(self, step, command, timeout=600, cwd=None, env=None):
        command = [str(item) for item in command]
        self.record_step(step, "running", command=command, cwd=str(cwd or ROOT))
        try:
            result = subprocess.run(
                command,
                cwd=str(cwd or ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self.record_step(step, "failed", error=f"timeout after {timeout}s")
            raise PipelineError(f"{step} 超时（{timeout}s）") from exc
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            self.record_step(step, "failed", returncode=result.returncode, output_tail=output[-2000:])
            raise PipelineError(f"{step} 失败，退出码 {result.returncode}: {output[-800:]}")
        self.record_step(step, "success", returncode=0, output_tail=output[-2000:])
        return result

    def ffprobe_duration(self, path):
        result = self.run_checked(
            "ffprobe-duration",
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            timeout=60,
        )
        try:
            return float(result.stdout.strip())
        except ValueError as exc:
            raise PipelineError(f"无法读取媒体时长: {path}") from exc

    def verify_audio(self, path, step):
        helper = ROOT / "scripts" / "whisper_f0.py"
        result = self.run_checked(step, [SYS_PY, helper, "--audio", path], timeout=300)
        try:
            data = json.loads(result.stdout.strip())
            text = str(data["text"]).strip()
            f0 = float(data["f0"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise PipelineError(f"音频验证输出不可解析: {result.stdout[-500:]}") from exc
        if len(text) < 20:
            raise PipelineError(f"Whisper 可识别内容过短，仅 {len(text)} 字")
        if not 80 <= f0 <= 180:
            raise PipelineError(f"F0={f0:.1f}Hz，不在 80-180Hz 范围")
        return text, f0


def log_step(step, message):
    print(f"\n{'=' * 60}\n>>> STEP {step}: {message}\n{'=' * 60}")


def load_style_plan(path):
    with open(path, encoding="utf-8") as handle:
        plan = yaml.safe_load(handle)
    return plan if isinstance(plan, dict) else {}


def validate_voiceover(text):
    sentences = [item for item in re.split(r"[。！？\n]", text) if item.strip()]
    exclamation_count = text.count("！") + text.count("!")
    rate = exclamation_count / len(sentences) if sentences else 0
    effective_chars = len(re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", text))
    if rate > 0.15:
        raise PipelineError(
            f"口播稿感叹号过多: {exclamation_count}/{len(sentences)} ({rate * 100:.0f}% > 15%)"
        )
    if "#" in text:
        raise PipelineError("口播稿含 # 标签格式，请改为自然口语")
    if not VOICEOVER_MIN_CHARS <= effective_chars <= VOICEOVER_MAX_CHARS:
        raise PipelineError(
            f"口播稿有效字符数为 {effective_chars}，要求 {VOICEOVER_MIN_CHARS}-{VOICEOVER_MAX_CHARS}（不计空格、换行和标点）"
        )


def create_compatibility_link(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def prepare_sadtalker_source_image(pipeline, source_image, max_height):
    source_image = Path(source_image)
    if max_height <= 0:
        return source_image
    probe = pipeline.run_checked(
        "sadtalker-source-probe",
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=height",
            "-of",
            "csv=p=0",
            source_image,
        ],
        timeout=60,
    )
    try:
        height = int(probe.stdout.strip().splitlines()[0])
    except (IndexError, ValueError) as exc:
        raise PipelineError(f"无法读取 SadTalker 输入图高度: {source_image}") from exc
    if height <= max_height:
        return source_image
    prepared = pipeline.run_dir / f"sadtalker_source_h{max_height}{source_image.suffix.lower() or '.jpg'}"
    pipeline.run_checked(
        "sadtalker-source-image",
        [
            "ffmpeg",
            "-y",
            "-i",
            source_image,
            "-vf",
            f"scale=-2:{max_height}:force_original_aspect_ratio=decrease",
            "-frames:v",
            "1",
            prepared,
        ],
        timeout=120,
    )
    return prepared


def execute(args):
    pipeline = PipelineRun(args)
    try:
        pipeline.acquire_lock()
        reference_audio = resolve_path(args.ref)
        photo = resolve_path(args.photo)
        html_source = resolve_path(args.html)
        text_source = resolve_path(args.text)
        style_plan = resolve_path(args.style_plan)
        style_history = resolve_path(args.style_history)
        for label, path in {
            "参考音频": reference_audio,
            "数字人照片": photo,
            "HTML": html_source,
            "口播稿": text_source,
            "STYLE_PLAN": style_plan,
            "STYLE_HISTORY": style_history,
        }.items():
            if not path.is_file():
                raise PipelineError(f"{label}不存在: {path}")

        gsap_source = ROOT / "node_modules" / "gsap" / "dist" / "gsap.min.js"
        if not gsap_source.is_file():
            raise PipelineError(f"缺少本地 GSAP: {gsap_source}")
        vendor_dir = pipeline.project_dir / "vendor"
        vendor_dir.mkdir(exist_ok=True)
        shutil.copy2(gsap_source, vendor_dir / "gsap.min.js")

        style_validator = ROOT / "scripts" / "validate_style.py"
        pipeline.run_checked(
            "style-gate",
            [
                SYS_PY,
                style_validator,
                "--plan",
                style_plan,
                "--history",
                style_history,
                "--guide",
                STYLE_GUIDE,
                "--project",
                args.name,
                "--html",
                html_source,
            ],
        )
        style_data = load_style_plan(style_plan)
        pipeline.manifest["style"] = {dimension: style_data[dimension] for dimension in STYLE_DIMENSIONS}
        pipeline.manifest["inputs"] = {
            "reference_audio": str(reference_audio),
            "photo": str(photo),
            "html": str(html_source),
            "text": str(text_source),
            "style_plan": str(style_plan),
            "style_history": str(style_history),
        }
        pipeline.write_manifest()

        text_content = text_source.read_text(encoding="utf-8").strip()
        validate_voiceover(text_content)

        log_step(f"1/{TOTAL_STEPS}", "TTS")
        natural_wav = pipeline.project_dir / "voiceover_natural.wav"
        natural_tmp = pipeline.run_dir / "voiceover_natural.wav"
        if args.resume and is_reusable_file(natural_wav):
            mark_reused(pipeline, "tts", natural_wav)
        else:
            environment = os.environ.copy()
            environment["PYTHONPATH"] = ""
            pipeline.run_checked(
                "tts",
                [
                    "/usr/bin/env",
                    "PYTHONPATH=",
                    INDX_PY,
                    ROOT / "scripts" / "tts_male_pipeline.py",
                    "-t",
                    text_content,
                    "-r",
                    reference_audio,
                    "-o",
                    natural_tmp,
                    "--max_len",
                    "25",
                    "--interval_silence",
                    f"{TTS_INTERVAL_SILENCE:g}",
                    "--json-report",
                    str(pipeline.run_dir / "tts-synthesis-report.json"),
                    *(["--emo-text", TTS_EMO_TEXT] if TTS_EMO_TEXT else []),
                    *(["--emo-audio-prompt", TTS_EMO_AUDIO_PROMPT] if TTS_EMO_AUDIO_PROMPT else []),
                    "--emo-alpha",
                    f"{TTS_EMO_ALPHA:g}",
                    "--use-emo-text" if TTS_USE_EMO_TEXT else "--no-use-emo-text",
                    "--use-random" if TTS_USE_RANDOM else "--no-use-random",
                    "--temperature",
                    f"{TTS_TEMPERATURE:g}",
                    "--top-p",
                    f"{TTS_TOP_P:g}",
                    "--top-k",
                    str(TTS_TOP_K),
                    "--repetition-penalty",
                    f"{TTS_REPETITION_PENALTY:g}",
                ],
                timeout=3600,
                env=environment,
            )
            replace_output(natural_tmp, natural_wav)
        pipeline.record_output("natural_audio", natural_wav)
        text_check, f0 = pipeline.verify_audio(natural_wav, "verify-natural-audio")
        print(f"✅ 原速口播: {natural_wav} ({len(text_check)}字, F0={f0:.0f}Hz)")

        # 逐段合成质量门禁：任何分段 fallback 或内容验收失败都应阻断后续昂贵渲染
        report_json = pipeline.run_dir / "tts-synthesis-report.json"
        if report_json.is_file():
            try:
                with open(report_json, encoding="utf-8") as handle:
                    synth_report = json.load(handle)
                audit = synth_report.get("f0_audit", {})
                content = synth_report.get("content_acceptance", {})
                if audit and not audit.get("passed", True):
                    raise PipelineError(
                        f"TTS 逐段 F0 审计未通过: fallback={audit.get('fallback_count')}, "
                        f"male_ratio={audit.get('male_ratio')}"
                    )
                if TTS_REQUIRED_KEYWORDS and content and not content.get("passed", True):
                    raise PipelineError(
                        f"TTS 内容验收未通过: similarity={content.get('similarity')}, "
                        f"missing={content.get('missing_keywords')}"
                    )
                pipeline.manifest["tts_synthesis_report"] = synth_report
            except (json.JSONDecodeError, OSError):
                pipeline.record_step("tts-report-check", "skipped", note="无法解析合成报告")

        log_step(f"2/{TOTAL_STEPS}", f"{AUDIO_SPEED:g}x speed and loudness normalization")
        speed_wav = pipeline.project_dir / "voiceover_130.wav"
        speed_tmp = pipeline.run_dir / "voiceover_130.wav"
        if args.resume and is_reusable_file(speed_wav):
            mark_reused(pipeline, "speed-audio", speed_wav)
        else:
            pipeline.run_checked(
                "speed-audio",
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    natural_wav,
                    "-af",
                    f"atempo={AUDIO_SPEED:g},loudnorm=I=-14:TP=-1.0:LRA=8",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    speed_tmp,
                ],
            )
            replace_output(speed_tmp, speed_wav)
        pipeline.record_output("final_audio", speed_wav)
        pipeline.manifest["audio_speed"] = AUDIO_SPEED
        pipeline.manifest["max_final_duration"] = MAX_FINAL_DURATION
        duration = pipeline.ffprobe_duration(speed_wav)
        render_duration = duration + END_PADDING_SECONDS
        audio_dir = pipeline.project_dir / "audio"
        audio_dir.mkdir(exist_ok=True)
        shutil.copy2(speed_wav, audio_dir / "voiceover_130.wav")
        pipeline.record_output("canonical_final_audio", audio_dir / "voiceover_130.wav")
        try:
            from tts_quality import duration_gate
        except ModuleNotFoundError:
            from scripts.tts_quality import duration_gate
        gate = duration_gate(duration, END_PADDING_SECONDS, MAX_FINAL_DURATION)
        pipeline.manifest["duration_policy"] = gate
        pipeline.record_step("duration-gate", "success" if gate["passed"] else "failed", **gate)
        if not gate["passed"]:
            raise PipelineError(
                f"预检时长超限: 定稿音频 {duration:.2f}s + 尾部缓冲 {END_PADDING_SECONDS:.2f}s = "
                f"{gate['estimated_final_duration']:.2f}s，硬上限 {MAX_FINAL_DURATION:.2f}s；"
                "已在 SadTalker 前停止，不生成视频。"
            )
        print(
            f"✅ 时长门禁通过: 预计成片 {gate['estimated_final_duration']:.2f}s / "
            f"上限 {MAX_FINAL_DURATION:.2f}s"
        )

        log_step(f"3/{TOTAL_STEPS}", "Whisper caption alignment")
        captions_path = pipeline.project_dir / "captions.json"
        captions_tmp = pipeline.run_dir / "captions.json"
        if args.resume and is_reusable_file(captions_path):
            mark_reused(pipeline, "caption-alignment", captions_path)
        else:
            pipeline.run_checked(
                "caption-alignment",
                [
                    SYS_PY,
                    ROOT / "scripts" / "gen_caption_timeline.py",
                    "--audio",
                    speed_wav,
                    "--script",
                    text_source,
                    "--output",
                    captions_tmp,
                    "--model",
                    "base",
                ],
                timeout=900,
            )
            replace_output(captions_tmp, captions_path)
        pipeline.record_output("captions", captions_path)
        with open(captions_path, encoding="utf-8") as handle:
            caption_data = json.load(handle)
        captions = caption_data.get("captions")
        if not isinstance(captions, list) or not captions:
            raise PipelineError("字幕时间轴为空")

        updated_html = pipeline.project_dir / "index.html"
        html_content = update_html(
            html_source.read_text(encoding="utf-8"),
            captions,
            duration,
            total_duration=render_duration,
        )
        atomic_text(updated_html, html_content)
        pipeline.record_output("html", updated_html)
        pipeline.run_checked(
            "style-html-gate",
            [
                SYS_PY,
                style_validator,
                "--plan",
                style_plan,
                "--history",
                style_history,
                "--guide",
                STYLE_GUIDE,
                "--project",
                args.name,
                "--html",
                updated_html,
            ],
        )

        log_step(f"4/{TOTAL_STEPS}", "Pre-SadTalker timeline check")
        timeline_checker = ROOT / "scripts" / "pre_composite_check.py"
        pipeline.run_checked(
            "timeline-quick",
            [
                SYS_PY,
                timeline_checker,
                "--project",
                pipeline.project_dir,
                "--quick",
                "--audio",
                speed_wav,
                "--html",
                updated_html,
            ],
        )

        log_step(f"5/{TOTAL_STEPS}", "SadTalker digital human")
        digital_human_run_dir = pipeline.project_dir / "digital_human" / pipeline.run_id
        reusable_digital_human = None
        if args.resume:
            try:
                candidate = Path(pipeline.previous_manifest.get("outputs", {}).get("digital_human", ""))
                if is_reusable_file(candidate):
                    reusable_digital_human = candidate
            except (OSError, TypeError):
                reusable_digital_human = None
        if reusable_digital_human:
            digital_human = reusable_digital_human
            mark_reused(pipeline, "sadtalker", digital_human)
        else:
            sadtalker_source_image = prepare_sadtalker_source_image(pipeline, photo, SADTALKER_SOURCE_MAX_HEIGHT)
            pipeline.record_output("sadtalker_source_image", sadtalker_source_image)
            digital_human_run_dir.mkdir(parents=True, exist_ok=False)
            pipeline.run_checked(
                "sadtalker",
                [
                    SAD_PY,
                    SAD_DIR / "inference.py",
                    "--driven_audio",
                    speed_wav,
                    "--source_image",
                    sadtalker_source_image,
                    "--result_dir",
                    digital_human_run_dir,
                    "--checkpoint_dir",
                    CKPT_DIR,
                    "--preprocess",
                    "full",
                    "--pose_style",
                    SADTALKER_POSE_STYLE,
                ],
                timeout=3600,
                cwd=SAD_DIR,
            )
            digital_human = wait_for_stable_mp4(digital_human_run_dir)
        pipeline.record_output("digital_human", digital_human)
        digital_human_duration = pipeline.ffprobe_duration(digital_human)
        if abs(digital_human_duration - duration) >= 0.5:
            raise PipelineError(
                f"数字人时长 {digital_human_duration:.3f}s 与音频 {duration:.3f}s 差值超过 0.5s"
            )

        log_step(f"6/{TOTAL_STEPS}", "Digital-human timeline check")
        pipeline.run_checked(
            "timeline-digital-human",
            [
                SYS_PY,
                timeline_checker,
                "--project",
                pipeline.project_dir,
                "--dh-only",
                "--audio",
                speed_wav,
                "--digital-human",
                digital_human,
                "--html",
                updated_html,
            ],
        )

        log_step(f"7/{TOTAL_STEPS}", "HyperFrames render")
        if not HYPERFRAMES_BIN.is_file():
            raise PipelineError(f"HyperFrames 本地 binary 不存在: {HYPERFRAMES_BIN}")
        renders_dir = pipeline.project_dir / "renders"
        renders_dir.mkdir(exist_ok=True)
        main_video = renders_dir / "main_video.mp4"
        main_tmp = pipeline.run_dir / "main_video.mp4"
        if args.resume and is_reusable_file(main_video):
            mark_reused(pipeline, "hyperframes-render", main_video)
        else:
            pipeline.run_checked(
                "hyperframes-render",
                [HYPERFRAMES_BIN, "render", "-c", "index.html", "-o", main_tmp],
                timeout=1800,
                cwd=pipeline.project_dir,
            )
            replace_output(main_tmp, main_video)
        pipeline.record_output("main_video", main_video)

        pipeline.run_checked(
            "timeline-full",
            [
                SYS_PY,
                timeline_checker,
                "--project",
                pipeline.project_dir,
                "--full",
                "--audio",
                speed_wav,
                "--digital-human",
                digital_human,
                "--main-video",
                main_video,
                "--html",
                updated_html,
            ],
        )

        log_step(f"8/{TOTAL_STEPS}", "Projection layer")
        pil_python = find_pil_python()
        projection = pipeline.project_dir / "shadow_layer.png"
        projection_tmp = pipeline.run_dir / "shadow_layer.png"
        if args.resume and is_reusable_file(projection):
            mark_reused(pipeline, "projection-layer", projection)
        else:
            pipeline.run_checked(
                "projection-layer",
                [
                    pil_python,
                    ROOT / "scripts" / "gen_proj_strong.py",
                    "--output",
                    projection_tmp,
                    "--preview-dir",
                    pipeline.run_dir,
                ],
            )
            replace_output(projection_tmp, projection)
        pipeline.record_output("projection", projection)

        log_step(f"9/{TOTAL_STEPS}", "Final composition")
        final_video = pipeline.project_dir / "final_video.mp4"
        final_tmp = pipeline.run_dir / "final_video.mp4"
        video_fade_start = max(0.0, render_duration - END_VIDEO_FADE_SECONDS)
        audio_fade_start = max(0.0, duration - END_AUDIO_FADE_SECONDS)
        filter_complex = (
            f"[1:v]scale=260:347,tpad=stop_mode=clone:stop_duration={END_PADDING_SECONDS:.3f}[inner];"
            "[3:v]format=rgba[proj];"
            "[proj][inner]overlay=14:14[combo];"
            f"[0:v]scale=1920:1080,tpad=stop_mode=clone:stop_duration={END_PADDING_SECONDS:.3f}[bg];"
            "[bg][combo]overlay=W-w-32:H-h-32,"
            f"fade=t=out:st={video_fade_start:.3f}:d={END_VIDEO_FADE_SECONDS:.3f}[v];"
            f"[2:a]afade=t=out:st={audio_fade_start:.3f}:d={END_AUDIO_FADE_SECONDS:.3f},"
            f"apad=pad_dur={END_PADDING_SECONDS:.3f},atrim=0:{render_duration:.3f}[a]"
        )
        if args.resume and is_reusable_file(final_video):
            mark_reused(pipeline, "final-composite", final_video)
        else:
            pipeline.run_checked(
                "final-composite",
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    main_video,
                    "-i",
                    digital_human,
                    "-i",
                    speed_wav,
                    "-i",
                    projection,
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    "[v]",
                    "-map",
                    "[a]",
                    "-t",
                    f"{render_duration:.3f}",
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
                    final_tmp,
                ],
                timeout=1800,
            )
            replace_output(final_tmp, final_video)
        pipeline.record_output("final_video", final_video)
        canonical_final = pipeline.project_dir / "final" / "final_video.mp4"
        create_compatibility_link(final_video, canonical_final)
        pipeline.record_output("canonical_final_video", canonical_final)

        log_step(f"10/{TOTAL_STEPS}", "Final verification")
        final_duration = pipeline.ffprobe_duration(final_video)
        if abs(final_duration - render_duration) >= 0.75:
            raise PipelineError(
                f"最终视频 {final_duration:.3f}s 与目标成片 {render_duration:.3f}s 差值超过 0.75s"
            )
        if final_duration > MAX_FINAL_DURATION:
            raise PipelineError(
                f"最终视频 {final_duration:.3f}s 超过全局硬上限 {MAX_FINAL_DURATION:.3f}s"
            )
        verified_text, verified_f0 = pipeline.verify_audio(final_video, "verify-final-audio")
        stream_result = pipeline.run_checked(
            "verify-streams",
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                final_video,
            ],
        )
        audio_streams = [line for line in stream_result.stdout.splitlines() if line.strip()]
        if len(audio_streams) != 1:
            raise PipelineError(f"最终视频音频轨数量为 {len(audio_streams)}，要求恰好 1 条")
        resolution_result = pipeline.run_checked(
            "verify-resolution",
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                final_video,
            ],
        )
        if resolution_result.stdout.strip() != "1920x1080":
            raise PipelineError(f"最终分辨率错误: {resolution_result.stdout.strip()}")

        visual_report = pipeline.run_dir / "visual-report.json"
        pipeline.run_checked(
            "visual-gate",
            [
                pil_python,
                ROOT / "scripts" / "validate_video_visuals.py",
                "--video",
                final_video,
                "--style-video",
                main_video,
                "--project",
                pipeline.project_dir,
                "--history-root",
                PROJ_DIR,
                "--report",
                visual_report,
            ],
            timeout=300,
        )

        pipeline.manifest["outputs"] = {
            "natural_audio": str(natural_wav),
            "final_audio": str(speed_wav),
            "captions": str(captions_path),
            "html": str(updated_html),
            "digital_human": str(digital_human),
            "main_video": str(main_video),
            "final_video": str(final_video),
            "canonical_final_video": str(canonical_final),
            "visual_report": str(visual_report),
            "final_video_sha256": sha256_file(final_video),
            "duration": final_duration,
            "resolution": "1920x1080",
            "audio_streams": 1,
            "whisper_characters": len(verified_text),
            "f0_hz": verified_f0,
        }
        pipeline.set_status("validated")
        print(f"\n✅ 视频已生成并通过全部机器校验: {final_video}")
        print(f"✅ run_id: {pipeline.run_id}")
        print(f"✅ manifest: {pipeline.project_dir / 'run-manifest.json'}")

        if args.upload:
            upload_command = [
                    SYS_PY,
                    ROOT / "scripts" / "upload_video.py",
                    "--project",
                    pipeline.project_dir,
                    "--video",
                    final_video,
                    "--name",
                    args.name,
                    "--style-plan",
                    style_plan,
                    "--style-history",
                    style_history,
                    "--title",
                    args.bili_title or args.name,
                    "--desc",
                    args.bili_desc or "",
                    "--dynamic",
                    args.bili_dynamic or "",
                    "--tag",
                    args.bili_tag or "AI生成,自动化",
                    "--reuse-pipeline-lock",
                    *(["--public"] if args.public else []),
                ]
            pipeline.record_step("upload", "running", command=[str(item) for item in upload_command])
            try:
                upload_result = subprocess.run(
                    [str(item) for item in upload_command],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=2400,
                )
            except subprocess.TimeoutExpired as exc:
                raise PipelineError("上传超时（2400s）") from exc
            upload_output = (upload_result.stdout or "") + (upload_result.stderr or "")
            print(upload_output.strip())
            if upload_result.returncode != 0:
                raise PipelineError(f"上传失败，退出码 {upload_result.returncode}: {upload_output[-800:]}")
            with open(pipeline.project_dir / "run-manifest.json", encoding="utf-8") as handle:
                pipeline.manifest = json.load(handle)
            if pipeline.manifest.get("status") != "success":
                raise PipelineError("上传脚本退出 0，但 manifest 未变为 success")
            pipeline.record_step("upload", "success", returncode=0, output_tail=upload_output[-2000:])
        return 0
    except (PipelineError, OSError, ValueError, KeyError, json.JSONDecodeError, yaml.YAMLError) as exc:
        pipeline.set_status("failed", exc)
        print(f"\n❌ 流水线失败: {exc}", file=sys.stderr)
        return 1
    finally:
        pipeline.close()


def parse_args():
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config")
    pre_args, _ = pre_parser.parse_known_args()
    defaults = {}
    if pre_args.config:
        config_path = resolve_path(pre_args.config)
        if not config_path.is_file():
            pre_parser.error(f"配置文件不存在: {config_path}")
        with open(config_path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        if not isinstance(loaded, dict):
            pre_parser.error("配置文件必须是 YAML 对象")
        defaults = loaded
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="YAML 配置文件；命令行参数优先")
    parser.add_argument("--name", default=defaults.get("name"))
    parser.add_argument("--ref", default=defaults.get("ref"))
    parser.add_argument("--photo", default=defaults.get("photo"))
    parser.add_argument("--html", default=defaults.get("html"))
    parser.add_argument("--text", default=defaults.get("text"))
    parser.add_argument("--style-plan", default=defaults.get("style_plan"))
    parser.add_argument("--style-history", default=defaults.get("style_history", str(ROOT / "STYLE_HISTORY.md")))
    parser.add_argument("--upload", action="store_true", default=bool(defaults.get("upload", False)), help="验证完成后执行幂等上传")
    parser.add_argument("--no-upload", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--public", action="store_true", help="显式公开；默认仅自己可见")
    parser.add_argument("--bili-title", default=defaults.get("bili_title"))
    parser.add_argument("--bili-desc", default=defaults.get("bili_desc"))
    parser.add_argument("--bili-dynamic", default=defaults.get("bili_dynamic"))
    parser.add_argument("--bili-tag", default=defaults.get("bili_tag"))
    parser.add_argument(
        "--resume",
        action="store_true",
        default=bool(defaults.get("resume", get_config_value(WORKFLOW_CONFIG, "pipeline.allow_resume", False))),
        help="显式复用项目目录中已存在且会被后续校验的产物；默认关闭",
    )
    args = parser.parse_args()
    missing = [key for key in ("name", "ref", "photo", "html", "text", "style_plan") if not getattr(args, key)]
    if missing:
        parser.error(f"缺少必要参数/配置: {', '.join(missing)}")
    if args.no_upload:
        args.upload = False
    if args.public and not args.upload:
        parser.error("--public 必须与 --upload 同时使用")
    return args


if __name__ == "__main__":
    sys.exit(execute(parse_args()))
