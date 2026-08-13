# HyperFrames Workflow

简体中文 | [English](README.md)

这是一个可审计的短视频生产工作流，围绕 HyperFrames、IndexTTS2、
SadTalker、Whisper 对齐、ffmpeg 合成、视觉风格门禁和可选 Bilibili
上传适配器构建。

仓库只包含编排代码、模板、测试和脱敏示例；不包含模型权重、真实媒体素材、
参考音频、头像、生成视频、Cookie 或发布队列数据。

## 功能

- 根据脚本文案生成配音，并执行 TTS 质量门禁。
- 用 Whisper 对齐字幕，再注入 HyperFrames HTML。
- 渲染主动画和小尺寸数字人叠加层。
- 给成片增加尾部定帧和音视频淡出，避免结尾突然中断。
- 默认目标 58.0-59.0 秒，最终视频硬上限 59.5 秒；口播默认 260-290 个有效字符，定稿音频默认 1.30x。
- 校验风格差异、时间轴一致性、音频可识别性、分辨率、单音轨和视觉安全区。
- 写入机器可读的 run manifest，用于恢复、修复和幂等上传。
- 编排层支持多语言脚本：脚本文件使用 UTF-8，`pipeline.whisper_language`
  可设为 `zh`、`en`、`ja`、`ko`、`es`、`fr`、`de` 等 Whisper 语言码。
  实际语音质量取决于本地 TTS 引擎、参考音频和模型对目标语言的支持。

## 环境要求

- macOS 或 Linux，`PATH` 中可用 `ffmpeg` 和 `ffprobe`。
- 与 `package-lock.json` 兼容的 Node.js。
- 系统工作流、IndexTTS2、SadTalker 各自的 Python 环境。
- 本地 IndexTTS2 和 SadTalker checkpoints。
- 可选：符合 `scripts/upload_video.py` 契约的本地 Bilibili 上传队列。

大模型、第三方引擎和 checkpoints 都被 Git 忽略。用 `workflow.local.yaml`
或 `HFW_CONFIG` 指向你的本机路径。

## 安装

```bash
npm install
cp workflow.example.yaml workflow.local.yaml
cp config_daily.example.yaml config_daily.local.yaml
```

编辑 `workflow.local.yaml`，配置本地 Python 环境、TTS checkpoints、
SadTalker 目录和可选上传队列。

然后运行：

```bash
npm run doctor
npm run check
```

新 clone 在安装重型引擎和模型前，`npm run doctor` 可能失败，这是预期行为。

## 运行项目

先复制脱敏示例：

```bash
cp -R examples/demo-project projects/my-topic
cp STYLE_HISTORY.example.md STYLE_HISTORY.md
```

修改：

- `projects/my-topic/index.html`
- `projects/my-topic/STYLE_PLAN.yaml`
- `projects/my-topic/口播稿.txt`，或其他 UTF-8 脚本文件，例如 `script.en.txt`
- `projects/my-topic/素材.md`，或其他调研记录文件
- `config_daily.local.yaml`

运行：

```bash
python3 scripts/pipeline_daily.py --config config_daily.local.yaml
```

默认不上传。验证通过后，如需发布：

```bash
python3 scripts/pipeline_daily.py --config config_daily.local.yaml --upload --public
```

不传 `--public` 时，如果上传适配器支持可见性控制，任务默认保持仅自己可见。

## 多语言配置

在 `workflow.local.yaml` 中设置：

```yaml
pipeline:
  whisper_language: en
```

`whisper_language` 会用于字幕对齐、音频识别门禁、TTS 内容验收和上传前复核。
如果脚本、配音和 Whisper 语言码不一致，恢复和上传门禁会拒绝复用旧产物。

中文项目可以继续使用 `口播稿.txt`、`素材.md`。多语言项目可以在
`config_daily.local.yaml` 中把 `text` 指向 `script.en.txt`、`script.ja.txt`
等文件名。

## 安全边界

不要提交：

- `workflow.local.yaml`
- 模型 checkpoints 和第三方引擎 checkout
- 参考音频、头像、生成的音视频图片
- 真实 `projects/` 内容或上传 marker
- Cookie、Token、队列数据或账号自动化说明

公开仓库完整覆盖工作流编排层。第三方引擎和模型权重应单独安装，并通过
`workflow.local.yaml` 接入。

## 许可证

MIT。见 `LICENSE`。
