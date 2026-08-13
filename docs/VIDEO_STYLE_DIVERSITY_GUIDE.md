# HyperFrames 视频风格多样化规范

每次新视频开始前检查最近 5 条 STYLE_HISTORY 记录。新的 STYLE_PLAN 至少有 5 个维度和最近项目不同，并且 HTML 根节点必须用 `data-style-*` 属性落地同一组 slug。

## 10 个维度

- 视觉流派 / style_family：整体视觉范式，例如新闻播报、产品演示、编辑拼贴。
- 色彩系统 / palette：主背景、强调色、明暗关系和饱和度策略。
- 字体和排版 / typography：标题、正文、标签、数字的字体与层级。
- 画面构图 / composition：主体位置、网格、留白、数字人窗口和安全区。
- 场景结构 / scene_grammar：开场、证据、转折、数据、收束的组织方式。
- 动画语言 / motion_language：进入、强调、停顿、退出的运动语法。
- 转场方式 / transitions：场景间切换方式和节奏。
- 素材处理 / media_treatment：视频、截图、纹理、阴影、颗粒、遮罩和调色策略。
- 镜头节奏 / pacing：段落长度、信息密度和快慢变化。
- 音乐与音效 / audio_direction：人声、音乐、音效和响度方向。

## 执行要求

- STYLE_PLAN 的 10 个维度必须使用规范化 slug。
- `details` 记录颜色、字体、尺寸和实现证据。
- `intentional_differences` 至少覆盖 5 个不同维度。
- 不得只替换文案或颜色来声明新风格。
