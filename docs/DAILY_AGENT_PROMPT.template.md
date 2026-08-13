# HyperFrames Daily Agent Prompt

这是一个脱敏模板。将 `{WORKFLOW_ROOT}` 替换为本机工作流目录，并把
`{REFERENCE_AUDIO}`、`{AVATAR_IMAGE}` 和 `{UPLOAD_POLICY}` 设置为本机值。

## 目标

每天选择一个适合不超过 59.5 秒视频表达的科技、AI 视频或数字人主题，
生成口播稿、STYLE_PLAN、HyperFrames HTML，完成本地机器校验，并根据上传策略
决定是否进入 Bilibili 队列。

可配置目标语言。脚本文件必须是 UTF-8；将 `workflow.local.yaml` 的
`pipeline.whisper_language` 设置为目标语言码，例如 `zh`、`en`、`ja`、`ko`、
`es`、`fr`、`de`。脚本文案、TTS 参考音频、TTS 模型能力和 Whisper 语言码必须一致。

## 安全规则

1. 只能修改本期 `projects/<topic>/` 内容和本地运行产物。
2. 不得打印或提交 Cookie、Token、密钥、账号数据、模型权重或真实媒体。
3. 不得修改校验器来绕过失败。
4. 默认不上传；只有 `{UPLOAD_POLICY}` 明确允许时才使用 `--upload`。
5. 输入、STYLE_PLAN、STYLE_HISTORY、HTML、头像、参考音频或策略变化后必须全量重跑；只有 manifest 的 `resume_contract` 和产物指纹匹配时才使用 `--resume`。

## 运行顺序

```bash
cd {WORKFLOW_ROOT}
python3 scripts/doctor.py
python3 scripts/validate_style.py \
  --plan projects/<topic>/STYLE_PLAN.yaml \
  --history STYLE_HISTORY.md \
  --project <topic> \
  --html projects/<topic>/index.html
python3 scripts/pipeline_daily.py \
  --name <topic> \
  --ref {REFERENCE_AUDIO} \
  --photo {AVATAR_IMAGE} \
  --html projects/<topic>/index.html \
  --text projects/<topic>/口播稿.txt \
  --style-plan projects/<topic>/STYLE_PLAN.yaml
```

失败后读取 `projects/<topic>/run-manifest.json`，定位首个失败步骤和
`output_tail`，只修复输入或本期 HTML。手工替换产物后使用
`scripts/repair_run.py` 重新生成 `validated` manifest。

只有 manifest 为 `validated`，最终视频 SHA256 与 manifest 一致，`tts_synthesis_report`
通过，`outputs.text` 存在，成片时长接近 `final_audio_duration + end_padding_seconds`，
且时间轴、音频、分辨率和视觉门禁全部通过时，才可以上传。

## 汇报

汇报必须包含 topic、run_id、最终视频路径、manifest 状态和失败步骤。
不要把 Cookie、Token、队列 JSON 或本机账号信息写入项目文件。
