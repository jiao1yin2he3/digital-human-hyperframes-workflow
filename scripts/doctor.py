#!/usr/bin/env python3
"""Check local dependencies required by the HyperFrames workflow."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from workflow_config import config_path, get_config_value, load_workflow_config
except ModuleNotFoundError:
    from scripts.workflow_config import config_path, get_config_value, load_workflow_config


ROOT = Path(__file__).resolve().parents[1]


def run(command, timeout=20):
    return subprocess.run([str(item) for item in command], capture_output=True, text=True, timeout=timeout)


def check_file(label, path, executable=False):
    path = Path(path)
    ok = path.is_file() and (not executable or path.stat().st_mode & 0o111)
    return ok, label, str(path), "ok" if ok else "missing"


def check_dir(label, path):
    path = Path(path)
    ok = path.is_dir()
    return ok, label, str(path), "ok" if ok else "missing"


def check_command(name):
    found = shutil.which(name)
    return bool(found), name, found or name, "ok" if found else "missing from PATH"


def check_python_import(label, python, module):
    if not Path(python).is_file():
        return False, label, str(python), "python missing"
    result = run([python, "-c", f"import {module}"])
    ok = result.returncode == 0
    detail = "ok" if ok else (result.stderr or result.stdout).strip()[-200:]
    return ok, label, f"{python} import {module}", detail


def check_pillow_python(config):
    env_python = os.environ.get("HFW_PIL_PY")
    sys_python = config_path(ROOT, config, "paths.sys_python")
    sadtalker_python = config_path(ROOT, config, "paths.sadtalker_python")
    indextts_python = config_path(ROOT, config, "paths.indextts_python")
    candidates = [
        Path(env_python).expanduser() if env_python else None,
        sys_python,
        Path(sys.executable),
        Path(shutil.which("python3")) if shutil.which("python3") else None,
        Path("/usr/bin/python3"),
        Path("/Library/Developer/CommandLineTools/usr/bin/python3"),
        sadtalker_python,
        indextts_python,
    ]
    seen = set()
    for python in candidates:
        if not python:
            continue
        python = Path(python)
        if str(python) in seen:
            continue
        seen.add(str(python))
        if python.is_file():
            result = run([python, "-c", "import PIL"])
            if result.returncode == 0:
                return True, "visual python PIL", str(python), "ok"
    return False, "visual python PIL", "Pillow-capable python", "missing"


def check_safetensors_header(label, python, path):
    if not Path(path).is_file():
        return False, label, str(path), "missing"
    code = (
        "from safetensors import safe_open\n"
        f"path = {str(path)!r}\n"
        "with safe_open(path, framework='pt') as handle:\n"
        "    keys = list(handle.keys())\n"
        "print(len(keys))\n"
    )
    result = run([python, "-c", code], timeout=60)
    ok = result.returncode == 0
    detail = "ok" if ok else (result.stderr or result.stdout).strip()[-240:]
    return ok, label, str(path), detail


def check_hyperframes_version(config):
    package_path = ROOT / "package.json"
    if not package_path.is_file():
        return False, "hyperframes package", str(package_path), "package.json missing"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    expected = package.get("dependencies", {}).get("hyperframes")
    expected_version = str(expected).lstrip("^~")
    binary = config_path(ROOT, config, "paths.hyperframes_bin")
    if not binary.is_file():
        return False, "hyperframes version", str(binary), "binary missing"
    installed_path = ROOT / "node_modules" / "hyperframes" / "package.json"
    installed_version = ""
    if installed_path.is_file():
        try:
            installed_version = str(
                json.loads(installed_path.read_text(encoding="utf-8")).get("version", "")
            )
        except (OSError, json.JSONDecodeError):
            installed_version = ""
    result = run([binary, "--version"])
    output = "\n".join(
        value.strip() for value in (result.stdout, result.stderr) if value and value.strip()
    )
    cli_version = ""
    for line in output.splitlines():
        stripped = line.strip()
        named = re.search(r"\bhyperframes\s+v?(\d+\.\d+\.\d+)\b", stripped, re.IGNORECASE)
        exact = not installed_version and re.fullmatch(r"v?(\d+\.\d+\.\d+)", stripped)
        if named or exact:
            cli_version = (named or exact).group(1)
            break
    version_ok = installed_version == expected_version
    cli_version_ok = not cli_version or cli_version == expected_version
    cli_ok = result.returncode == 0
    ok = cli_ok and version_ok and cli_version_ok
    detail = (
        f"package={expected}, installed={installed_version or 'unknown'}, "
        f"binary={cli_version or 'unknown'}"
    )
    return ok, "hyperframes version", str(binary), detail


def check_config_allow_resume(config):
    allow_resume = get_config_value(config, "pipeline.allow_resume", False)
    ok = (allow_resume is False)
    detail = "ok (false)" if ok else "FAIL: pipeline.allow_resume must be false in production contract"
    return ok, "config allow_resume", "pipeline.allow_resume", detail


def main():
    try:
        config = load_workflow_config(ROOT)
    except Exception as exc:
        print(f"FAIL workflow config: {exc}")
        return 1

    sys_python = config_path(ROOT, config, "paths.sys_python")
    indextts_python = config_path(ROOT, config, "paths.indextts_python")
    sadtalker_python = config_path(ROOT, config, "paths.sadtalker_python")
    indextts_checkpoints = config_path(ROOT, config, "paths.indextts_checkpoints")
    biliup_root = config_path(ROOT, config, "paths.biliup_root")
    biliup_pipeline = biliup_root / "pipeline"

    checks = [
        check_file("sys python", sys_python, executable=True),
        check_file("indextts python", indextts_python, executable=True),
        check_file("sadtalker python", sadtalker_python, executable=True),
        check_dir("sadtalker dir", config_path(ROOT, config, "paths.sadtalker_dir")),
        check_dir("sadtalker checkpoints", config_path(ROOT, config, "paths.sadtalker_checkpoints")),
        check_dir("indextts checkpoints", indextts_checkpoints),
        check_file("style guide", config_path(ROOT, config, "paths.style_guide")),
        check_file("hyperframes bin", config_path(ROOT, config, "paths.hyperframes_bin"), executable=True),
        check_dir("projects dir", config_path(ROOT, config, "paths.projects_dir")),
        check_command("ffmpeg"),
        check_command("ffprobe"),
        check_python_import("sys python yaml", sys_python, "yaml"),
        check_python_import("sys python faster_whisper", sys_python, "faster_whisper"),
        check_python_import("indextts python torch", indextts_python, "torch"),
        check_python_import("indextts python safetensors", indextts_python, "safetensors"),
        check_python_import("sadtalker python PIL", sadtalker_python, "PIL"),
        check_pillow_python(config),
        check_safetensors_header(
            "indextts qwen safetensors",
            indextts_python,
            indextts_checkpoints / "qwen0.6bemo4-merge" / "model.safetensors",
        ),
        check_hyperframes_version(config),
        check_config_allow_resume(config),
    ]

    if biliup_root.is_dir():
        checks.extend(
            [
                check_dir("biliup root", biliup_root),
                check_dir("biliup pipeline", biliup_pipeline),
                check_file("biliup enqueue", biliup_pipeline / "bin" / "enqueue-video.sh", executable=True),
                check_file("biliup publish", biliup_pipeline / "bin" / "publish-queue.sh", executable=True),
            ]
        )
    else:
        print(f"SKIP biliup: optional upload adapter not found at {biliup_root}")

    if get_config_value(config, "_config_file"):
        print(f"config: {config['_config_file']}")
    else:
        print("config: built-in defaults")
    failed = 0
    for ok, label, target, detail in checks:
        print(f"{'OK  ' if ok else 'FAIL'} {label}: {target} ({detail})")
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
