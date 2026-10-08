# Video Study Notes · 视频学习笔记

把视频中的**讲解、关键画面和时间轴**整理成清楚的 HTML 学习笔记。

这是一个 Codex skill：它提取视频材料，由当前 Codex 实际阅读转写与关键帧，再生成可离线保存的 HTML。适合课程、讲座、教程和研究解读视频。

目前支持 **Bilibili 与本地视频**；YouTube 支持列在后续计划中。名称不绑定某个平台，当前能力也不会提前标成“支持所有视频网站”。

## 能得到什么

- 有内容的中文总结：核心结论、概念/机制解释、结果与边界。
- 可回到原视频的时间轴。
- 关键图、公式、代码或演示画面与解释；图片嵌入 HTML。
- 本次处理范围、抽帧数量和用量记录，区分真实计数、估算与未知。

默认用当前 Codex 读图，缺字幕时用本地 faster-whisper 转写。**不会自动调用额外收费的视觉或转写 API。** 默认 HTML 没有 CDN、外部字体或构建系统，图片随文件一起保存。

## 安装与使用

需要 Python 3.11+、FFmpeg/FFprobe；使用本地 ASR 时还需要 `uv`。概览联系表使用 Pillow，skill 通过 `uv run --with pillow` 提供隔离运行环境。当前真实音频链路在 macOS 上验证过；其他系统的 ASR/媒体访问需分别验证。

新安装到 Codex 的全局 skill 目录：

```bash
git clone https://github.com/Nahuyiur/video-study-notes.git \
  "$HOME/.codex/skills/video-study-notes"
```

如果该目录已存在，先核对是否为本仓库的检出；不要用克隆或复制命令覆盖自己的修改。刷新技能列表后调用：

```text
用 $video-study-notes 总结这个 B站视频：<链接>
重点解释方法和图示，生成 HTML 学习笔记。
```

也可以指定时间范围、学习重点或本地视频。完整工作流见 [SKILL.md](SKILL.md)，HTML 内容结构见 [references/html-summary.md](references/html-summary.md)。

## 工作流

```text
选择视频 / 分 P / 时间范围
    → 提取字幕，缺失时本地 ASR
    → 概览关键帧 + 有限的局部细读
    → Codex 结合画面解释内容
    → 生成 summary.json / summary.html
    → 保存 usage.json 和共享用量账本
```

纯脚本不会自行理解视频：创建图片不是看过图片，ASR 文本也不能替代视觉阅读。实际模型读图是 skill 工作流的一部分。

### 默认预算与画面覆盖

| 档位 | 概览上限 | 细读上限 | 阅读包上限 | 展示文字字符上限 |
| --- | ---: | ---: | ---: | ---: |
| economy（默认） | 12 | 6 | 4 | 12,000 |
| standard | 24 | 12 | 8 | 24,000 |

当前自动概览约每两分钟取一个时间点，至少三张，按整个处理区间均匀分布；细读根据已读讲解和画面选取。上限不是每次实际使用量。字幕超过材料预算或 ASR 超过单次 30 分钟时，保存检查点并给出明确的部分总结。

**当前没有 PPT 切页检测。** 稀疏采样会漏掉页面、瞬时动作或小字。讲解全段已处理与视觉逐页覆盖是两回事；密集课件应在预算内补充代表帧，并在 HTML 中说明覆盖。

### 用量记录

有可信回执时记录实际 token；没有时为 `null`。文字材料粗估不包含图片、推理、工具或历史上下文，不能当作总 token。Codex 订阅额度不换算为美元；独立 API 计费只依据实际 usage 和核实的价格。

详细口径见 [references/metering.md](references/metering.md)。

## 验证与开发

下面的单元测试只使用临时目录和合成夹具，不请求真实 B站媒体，也不调用模型：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts
```

39 项测试覆盖时间轴/分 P、预算与续读、失败和完成状态、用量计数与去重、HTML 转义、嵌入图片和部分覆盖。当前检查在本地运行；[GitHub Actions 配置模板](ci/github-actions-tests.yml) 已提供，计划检查 Python 3.11/3.12，**尚未启用**。发布账号当前令牌缺少 `workflow` 权限，待具有相应权限后把模板放入 `.github/workflows/tests.yml`。单元测试/CI 通过也不等于 B站接口、真实 ASR 或所有视频都可用。

开发约束见 [AGENTS.md](AGENTS.md)。升级 ASR 或上游提取器时，应另做真实材料检查并更新固定版本与来源校验值。

## 后续计划

- **改善画面覆盖**：本地识别 PPT 切页或明显画面变化，先去重再挑代表帧；比较均匀采样与变化采样的覆盖和阅读用量。
- **加入 YouTube**：获取字幕/音视频，沿用统一时间轴、预算与 HTML 工作流；处理平台权限和字幕缺失。
- **完善 HTML 阅读体验**：公式/代码展示、长课章节导航、关键图放大，以及适合展示的页面样式。
- **形成可分享演示**：用自制或许可明确的材料制作示例，再整理小红书图文。当前仓库不自动发布内容。

这些是计划，不是已实现的功能。

## 来源与许可

- 自有代码与文档采用 [MIT](LICENSE)。
- BiliLens 的提取器与转写助手来自 [AntaresGG/BiliBiliVideoParser](https://github.com/AntaresGG/BiliBiliVideoParser)，固定版本，保留 [上游 MIT 许可证](scripts/upstream/LICENSE)。
- 提取设计参考 [ezbug/bilibili-to-obsidian](https://github.com/ezbug/bilibili-to-obsidian)；没有复制该项目无独立许可证的专用脚本。
- 版本、文件校验值和已知边界见 [references/upstream.json](references/upstream.json) 与 [references/sources.md](references/sources.md)。

视频、字幕和画面的内容权利不由本仓库的软件许可覆盖。真实运行材料、Cookie、音视频、私人用量账本和本机验证记录不随仓库发布。
