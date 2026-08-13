#!/usr/bin/env python3
"""验证 STYLE_PLAN schema、历史差异和 HTML 实现标记。"""

import argparse
import html as html_module
import json
import re
import sys
from pathlib import Path

import yaml

try:
    from workflow_config import config_path, load_workflow_config
except ModuleNotFoundError:
    from scripts.workflow_config import config_path, load_workflow_config


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_CONFIG = load_workflow_config(ROOT)
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
DEFAULT_GUIDE = config_path(ROOT, WORKFLOW_CONFIG, "paths.style_guide")
GUIDE_MARKERS = {
    "style_family": ("视觉流派", "style_family"),
    "palette": ("色彩系统", "palette"),
    "typography": ("字体和排版", "typography"),
    "composition": ("画面构图", "composition"),
    "scene_grammar": ("场景结构", "scene_grammar"),
    "motion_language": ("动画语言", "motion_language"),
    "transitions": ("转场方式", "transitions"),
    "media_treatment": ("素材处理", "media_treatment"),
    "pacing": ("镜头节奏", "pacing"),
    "audio_direction": ("音乐与音效", "audio_direction"),
}
PLACEHOLDER = re.compile(
    r"\b(?:replace[\s_-]*me|todo|tbd|placeholder|your[\s_-]*(?:project|value)|example(?:[\s_-]*value)?)\b",
    re.IGNORECASE,
)
REQUIRED = ["project", "creative_seed", "content_goal", "audience", "emotion"] + DIMENSIONS + [
    "details",
    "intentional_differences",
]
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def load_yaml(path):
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("STYLE_PLAN 必须是 YAML 对象")
    return data


def load_history(path):
    entries = []
    current = None
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip()
            if line.startswith("- project:"):
                if current:
                    entries.append(current)
                current = {"project": line.split(":", 1)[1].strip()}
            elif current and line.startswith("  ") and ":" in line:
                key, value = line.strip().split(":", 1)
                current[key.strip()] = value.strip()
    if current:
        entries.append(current)
    return entries


def validate_guide(path=DEFAULT_GUIDE):
    """Ensure the external diversity guide is present and describes every dimension."""
    guide_path = Path(path)
    if not guide_path.is_file():
        return [f"风格多样化规范不存在: {guide_path}"]
    text = guide_path.read_text(encoding="utf-8")
    errors = []
    for dimension, markers in GUIDE_MARKERS.items():
        if not any(marker in text for marker in markers):
            errors.append(f"风格规范缺少维度说明: {dimension}")
    if not re.search(r"最近\s*5|latest\s+5", text, re.IGNORECASE):
        errors.append("风格规范必须要求检查最近 5 条历史记录")
    if not re.search(r"至少[^\n]{0,20}5|至少有[^\n]{0,20}5", text):
        errors.append("风格规范必须要求至少 5 个维度差异")
    return errors


def _contains_placeholder(value):
    if isinstance(value, dict):
        return any(_contains_placeholder(key) or _contains_placeholder(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_placeholder(item) for item in value)
    return isinstance(value, str) and bool(PLACEHOLDER.search(value))


def validate_schema(plan, project=None):
    errors = []
    missing = [key for key in REQUIRED if key not in plan]
    if missing:
        errors.append(f"缺少字段: {', '.join(missing)}")
        return errors
    if _contains_placeholder(plan):
        errors.append("STYLE_PLAN 含有占位值，请替换为真实内容")
    if project and plan.get("project") != project:
        errors.append(f"plan.project={plan.get('project')} 与 --project={project} 不一致")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", str(plan.get("project", ""))):
        errors.append("project 必须是 2-64 位小写 slug")
    for key in ("creative_seed", "content_goal", "audience", "emotion"):
        if not isinstance(plan.get(key), (str, int)) or not str(plan.get(key)).strip():
            errors.append(f"{key} 不能为空")
    for dimension in DIMENSIONS:
        value = plan.get(dimension)
        if not isinstance(value, str) or not SLUG.fullmatch(value):
            errors.append(f"{dimension} 必须是规范化 slug，例如 asymmetric-editorial")
    details = plan.get("details")
    if not isinstance(details, dict) or not details:
        errors.append("details 必须是非空对象，用于记录颜色、字体和实现细节")
    differences = plan.get("intentional_differences")
    if not isinstance(differences, list) or len(differences) < 5:
        errors.append("intentional_differences 至少包含 5 个结构化差异")
        return errors
    seen = set()
    for index, difference in enumerate(differences, start=1):
        if not isinstance(difference, dict):
            errors.append(f"intentional_differences[{index}] 必须是对象")
            continue
        required = {"dimension", "from", "to", "evidence"}
        if not required.issubset(difference):
            errors.append(f"intentional_differences[{index}] 缺少 {sorted(required - set(difference))}")
            continue
        dimension = difference["dimension"]
        if dimension not in DIMENSIONS:
            errors.append(f"intentional_differences[{index}].dimension 无效: {dimension}")
            continue
        if dimension in seen:
            errors.append(f"intentional_differences 重复维度: {dimension}")
        seen.add(dimension)
        if difference["to"] != plan.get(dimension):
            errors.append(f"intentional_differences[{index}].to 必须等于 plan.{dimension}")
        if not str(difference["from"]).strip() or not str(difference["evidence"]).strip():
            errors.append(f"intentional_differences[{index}] 的 from/evidence 不能为空")
    if len(seen) < 5:
        errors.append("intentional_differences 必须覆盖至少 5 个不同维度")
    return errors


def compare_history(plan, history, minimum):
    relevant = [
        entry
        for entry in history
        if entry.get("project") != plan.get("project")
        and all(str(entry.get(dimension, "")).strip() for dimension in DIMENSIONS)
    ]
    recent = relevant[-5:]
    comparisons = []
    for old in recent:
        different = [dimension for dimension in DIMENSIONS if plan[dimension] != old.get(dimension)]
        comparisons.append({"project": old.get("project", "unknown"), "different": different})
    if comparisons:
        weakest = min(comparisons, key=lambda item: len(item["different"]))
        if len(weakest["different"]) < minimum:
            return comparisons, (
                f"与 {weakest['project']} 仅 {len(weakest['different'])}/10 个维度不同，要求至少 {minimum}/10"
            )
    return comparisons, None


def parse_stage_attributes(html):
    stage_match = re.search(r"<[^>]+\bid=[\"']stage[\"'][^>]*>", html, flags=re.DOTALL)
    if not stage_match:
        return None
    tag = stage_match.group(0)
    return {
        key: html_module.unescape(value)
        for key, _, value in re.findall(r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", tag, flags=re.DOTALL)
    }


def validate_html(plan, html_path):
    html = Path(html_path).read_text(encoding="utf-8")
    attributes = parse_stage_attributes(html)
    if attributes is None:
        return ["HTML 缺少 id=stage 的 composition 根节点"]
    errors = []
    for dimension in DIMENSIONS:
        attribute = f"data-style-{dimension.replace('_', '-')}"
        if attributes.get(attribute) != plan[dimension]:
            errors.append(f"HTML {attribute}={attributes.get(attribute)!r}，应为 {plan[dimension]!r}")
    if "const captions=" not in html and not re.search(r"const\s+captions\s*=", html):
        errors.append("HTML 缺少 const captions=[...] 时间轴入口")
    if 'id="main-voiceover"' not in html and "id='main-voiceover'" not in html:
        errors.append("HTML 缺少 id=main-voiceover 的音频标签")
    if re.search(r'<script[^>]+src=["\'](?:\.\./|node_modules/)', html):
        errors.append("项目 HTML 不得跨越项目根目录加载脚本；请使用 vendor/gsap.min.js")
    if 'src="vendor/gsap.min.js"' not in html and "src='vendor/gsap.min.js'" not in html:
        errors.append("HTML 必须从项目内 vendor/gsap.min.js 加载 GSAP")
    if len(re.findall(r'class=["\'][^"\']*\bscene\b', html)) < 3:
        errors.append("HTML 至少需要 3 个独立 scene")

    scripts_content = "".join(re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.DOTALL))
    if not re.search(r"scene|scenes|sceneTimes|\.scene|#scene", scripts_content, flags=re.IGNORECASE):
        errors.append("HTML 缺少 JS 场景切换逻辑（仅依赖 CSS，后续场景无法正常显示）")

    return errors


def execute(args):
    plan = load_yaml(args.plan)
    errors = validate_schema(plan, args.project)
    if not errors:
        errors.extend(validate_guide(getattr(args, "guide", DEFAULT_GUIDE)))
    if not errors:
        history = load_history(args.history)
        comparisons, comparison_error = compare_history(plan, history, args.min_different)
        if comparison_error:
            errors.append(comparison_error)
    else:
        comparisons = []
    if args.html:
        errors.extend(validate_html(plan, args.html))

    report = {
        "project": plan.get("project"),
        "status": "failed" if errors else "passed",
        "comparisons": [
            {
                "project": item["project"],
                "different_count": len(item["different"]),
                "different_dimensions": item["different"],
            }
            for item in comparisons
        ],
        "errors": errors,
    }
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"STYLE_PLAN: {plan.get('project')}")
    for item in report["comparisons"]:
        print(
            f"  vs {item['project']}: {item['different_count']}/10 different "
            f"({', '.join(item['different_dimensions'])})"
        )
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        return 1
    minimum = min((item["different_count"] for item in report["comparisons"]), default=10)
    print(f"OK: schema、HTML 和历史差异校验通过；最低差异 {minimum}/10")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--history", required=True)
    parser.add_argument("--project")
    parser.add_argument("--html")
    parser.add_argument("--min-different", type=int, default=5)
    parser.add_argument("--guide", default=str(DEFAULT_GUIDE))
    parser.add_argument("--report")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(execute(parse_args()))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
