# 模板使用规则

- 默认从 `blank_template.html` 开始，先按 STYLE_PLAN 重写构图、场景和动画。
- `bigtext_template.html` 只作为大字标题类选题的技术骨架，不得连续两期使用。
- 模板中的全部 `data-style-*="replace-me"` 必须替换成 STYLE_PLAN 的 10 个规范化 slug。
- HTML 固定使用 `vendor/gsap.min.js`；pipeline 会从锁定版本的 `node_modules` 同步到项目目录。
- 不得只换颜色和文字；至少落实 5 个结构化差异，并通过 `scripts/validate_style.py --html ...`。
- 成片还会经过 `scripts/validate_video_visuals.py`，与最近项目过度相似会失败。
