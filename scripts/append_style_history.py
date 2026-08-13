#!/usr/bin/env python3
"""原子化、去重地把 STYLE_PLAN 追加到 STYLE_HISTORY。"""

import argparse
import fcntl
import os
import re
import tempfile
from pathlib import Path

import yaml


DIMENSIONS = [
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
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*$")


def load_plan(path):
    with open(path, encoding="utf-8") as handle:
        plan = yaml.safe_load(handle)
    if not isinstance(plan, dict):
        raise ValueError("STYLE_PLAN 必须是 YAML 对象")
    project = str(plan.get("project", "")).strip()
    if not project:
        raise ValueError("STYLE_PLAN 缺少 project")
    for dimension in DIMENSIONS:
        value = plan.get(dimension)
        if not isinstance(value, str) or not SLUG.fullmatch(value):
            raise ValueError(f"{dimension} 必须是规范化 slug")
    return plan


def existing_projects(content):
    return set(re.findall(r"(?m)^- project:\s*(\S+)\s*$", content))


def append_history(plan_path, history_path):
    plan = load_plan(plan_path)
    history_path = Path(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(history_path) + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        content = history_path.read_text(encoding="utf-8") if history_path.exists() else "# HyperFrames style history\n"
        if plan["project"] in existing_projects(content):
            print(f"STYLE_HISTORY 已包含 {plan['project']}，跳过重复追加")
            return False
        lines = ["", f"- project: {plan['project']}"]
        lines.extend(f"  {dimension}: {plan[dimension]}" for dimension in DIMENSIONS)
        updated = content.rstrip() + "\n" + "\n".join(lines) + "\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{history_path.name}.", suffix=".tmp", dir=history_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(updated)
            os.replace(temporary, history_path)
        except Exception:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
    print(f"已追加 STYLE_HISTORY: {plan['project']}")
    return True


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--history", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    append_history(arguments.plan, arguments.history)
