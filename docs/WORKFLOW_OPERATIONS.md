# HyperFrames Workflow Operations

## 安全模型

- `pipeline_daily.py` 默认不上传，每次运行使用唯一 `run_id` 和独立临时目录。
- 所有外部命令必须退出 0；产物通过原子替换进入正式路径，旧文件不能掩盖失败。
- 最新机器状态位于 `projects/<topic>/run-manifest.json`。
- `validated` 表示生成和核验完成但尚未上传；只有上传 marker 和风格历史都成功后才会变成 `success`。

## 标准运行

先检查本机依赖：

```bash
npm run doctor
```

```bash
python3 scripts/validate_style.py \
  --plan projects/<topic>/STYLE_PLAN.yaml \
  --history STYLE_HISTORY.md \
  --project <topic> \
  --html projects/<topic>/index.html

python3 scripts/pipeline_daily.py \
  --name <topic> \
  --ref cosyvoice/ref_user5_clean.wav \
  --photo digital-human/user_avatar.jpg \
  --html projects/<topic>/index.html \
  --text projects/<topic>/口播稿.txt \
  --style-plan projects/<topic>/STYLE_PLAN.yaml
```

也可以复制 `config_daily.example.yaml` 后使用 `--config`。

## 配置

全局时长策略：`audio_speed=1.30`，定稿文件为 `voiceover_130.wav`；口播稿有效字符为 260-290；最终视频硬上限 59.5 秒。TTS 加速后先执行 duration-gate，超限在 SadTalker 前停止。最终成片还必须接近 `final_audio_duration + end_padding_seconds`，默认容差 0.75 秒；只低于 59.5 秒但长度和音频合同不一致，不算通过。

- 本机路径默认来自 `workflow.example.yaml` 中的约定。
- 需要覆盖时复制为 `workflow.local.yaml`，或设置 `HFW_CONFIG=/path/to/workflow.yaml`。
- `workflow.local.yaml` 不应进入版本控制；它只记录本机 venv、模型、biliup 队列等路径。
- 公开仓库使用 `STYLE_HISTORY.example.md` 作为起始模板；真实的 `STYLE_HISTORY.md`
  只保留在本机，不要提交其中的生产项目记录。
- 多语言项目可把 `config_daily.local.yaml` 的 `text` 指向任意 UTF-8 脚本文件，
  并在 `workflow.local.yaml` 设置 `pipeline.whisper_language`，例如 `zh`、`en`、
  `ja`、`ko`、`es`、`fr`、`de`。该语言码会进入 resume 合同、字幕对齐、
  音频门禁和上传前复核。

## 恢复运行

```bash
./venv/bin/python scripts/pipeline_daily.py \
  --config config_daily.example.yaml \
  --resume
```

`--resume` 只在显式传入时启用。流水线会在 manifest 中写入：

- `resume_contract`: reference audio、avatar、HTML、script、STYLE_PLAN、STYLE_HISTORY 的 SHA256，加上关键策略参数。
- `input_fingerprints`: 本次输入文件指纹。
- `output_fingerprints`: 已生成产物指纹。
- `tts_synthesis_report`: TTS 分段 F0 与 Whisper 内容验收报告。

只有旧 manifest 的合同、输入指纹、策略和被复用产物指纹都匹配时，才允许复用音频、字幕、数字人、主视频、投影层或最终合成视频。复用产物仍会进入后续时间轴、音频、分辨率、视觉和上传门禁。

适用场景：TTS、SadTalker 或 HyperFrames 已成功，但后续合成或视觉门禁失败。输入文案、头像、参考音频、HTML、STYLE_PLAN、STYLE_HISTORY 或策略参数变化后，应全量重跑。

## 修复运行

手工修复 HTML 或重新渲染某个产物后，用 repair 脚本重新核验并写入标准 manifest：

```bash
python3 scripts/repair_run.py \
  --project projects/<topic> \
  --html projects/<topic>/index.html \
  --audio projects/<topic>/voiceover_130.wav \
  --digital-human projects/<topic>/digital_human/<run_id>/<file>.mp4 \
  --main-video projects/<topic>/renders/main_video.mp4 \
  --final-video projects/<topic>/final_video.mp4 \
  --style-plan projects/<topic>/STYLE_PLAN.yaml
```

repair 会重新校验 style、timeline、visual、口播稿、音频 F0、TTS 报告、最终视频 SHA256、分辨率、单音轨、59.5 秒上限，以及最终视频是否接近 `audio_duration + end_padding_seconds`。repair manifest 的状态为 `validated`，仍需单独上传才会变成 `success`。

## 上传

完整的 Bilibili 适配器契约见 `docs/BILIBILI_UPLOAD.md`。仓库不包含账号、
Cookie、Token 或本地发布队列。

```bash
python3 scripts/upload_video.py \
  --project projects/<topic> \
  --video projects/<topic>/final_video.mp4 \
  --name <topic> \
  --style-plan projects/<topic>/STYLE_PLAN.yaml \
  --style-history STYLE_HISTORY.md \
  --title '<标题>'
```

上传使用项目名和视频 SHA256 生成确定性 job name；相同视频再次运行会直接命中 `final/upload-success.json`。上传前会重新校验 manifest policy、`outputs.text`、TTS 报告、视频 SHA256、`outputs.duration` 和目标时长，避免上传过期或手工截断的产物。

## 风格门禁

- 10 个比较维度必须使用规范化 slug，详细颜色和字体放在 `details`。
- HTML 根节点必须包含匹配的 10 个 `data-style-*` 属性。
- `intentional_differences` 至少覆盖 5 个不同维度。
- 成片检查空白帧、亮色安全区和最近项目视觉相似度。

## 目录与容量

- 源码、模板和配置应进入版本控制。
- 模型、venv、`node_modules` 和项目媒体产物已加入 `.gitignore`。
- 用 `./venv/bin/python scripts/cleanup_artifacts.py` 预览可清理内容；确认后添加 `--execute`。
- 长期建议把模型目录迁移到独立缓存盘，再通过环境变量或软链接接入；定时任务不得自动搬迁或升级模型。

## 版本策略

- `package-lock.json` 固定 HyperFrames 和 GSAP 版本。
- 升级 HyperFrames 后必须运行 `npm run doctor` 和 `npm run check`。
- `.hyperframes/auto-update.log` 中的自动更新跳过记录不作为生产版本依据；生产以本 repo 的 npm 锁文件为准。

## 故障处理

- 不得手工跳过失败步骤或复制历史产物。
- 查看 `run-manifest.json` 中首个 `failed` step 和 `output_tail`。
- 修复本期输入后重新运行；同一时间只能有一个 pipeline/上传任务持有 `runtime/pipeline.lock`。
