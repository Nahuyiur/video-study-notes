# Video Study Notes · 视频学习笔记

把视频中的**讲解、关键画面和时间轴**整理成 HTML、Markdown 或飞书学习笔记。

这是一个 Codex skill：它提取视频材料，由当前 Codex 实际阅读转写与关键帧，再保存一份可复用的笔记，按需要导出或交付。适合课程、讲座、教程和研究解读视频。

目前支持 **Bilibili、YouTube 单个视频与本地视频**。默认生成离线 HTML；也可以输出带配图或纯文本 Markdown，并通过本机已有飞书能力创建文档。平台权限、地区或接口变化可能影响素材访问。

## 能得到什么

- 有内容的中文总结：核心结论、概念/机制解释、结果与边界。
- 可回到原视频的时间轴。
- 关键图、公式、代码或演示画面与解释；图片嵌入 HTML。
- 本次处理范围、抽帧数量和用量记录，区分真实计数、估算与未知。

默认用当前 Codex 读图，缺字幕时用本地 faster-whisper 转写。**不会自动调用额外收费的视觉或转写 API。** 默认 HTML 没有 CDN、外部字体或构建系统，图片随文件一起保存。

## 安装与使用

需要 Python 3.11+、FFmpeg/FFprobe；概览联系表与课件候选扫描使用 Pillow，可以通过 `uv run --with pillow` 隔离提供。

按需依赖：本地 ASR 使用 `uv` 和固定版本运行时；YouTube 使用 yt-dlp/EJS 与已有 Deno ≥ 2.3 或 Node ≥ 22；飞书需要已登录的 `lark-cli` user 身份。YouTube 依赖不影响 B站/本地路径，飞书依赖不影响 HTML/MD。详见 [YouTube](references/youtube.md) 与 [飞书交付](references/feishu.md)。真实媒体/转写链路已在 macOS 验证，其他系统需分别检查。

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

可以指定时间范围、学习重点或本地视频，例如“把这个 YouTube 课程的 20–40 分钟整理成 Markdown”，或“把刚才那份笔记放到飞书，保留图和原视频时间点”。完整工作流见 [SKILL.md](SKILL.md)，统一笔记结构见 [references/note-schema.md](references/note-schema.md)。HTML、Markdown 和飞书消费同一份冻结笔记，切换格式不重新读视频。模块边界见 [实现结构](references/architecture.md)。

## 工作流

```text
选择视频 / 分 P / 时间范围
    → 提取字幕，缺失时本地 ASR
    → 概览关键帧 + 有限的局部细读
    → Codex 结合画面解释内容
    → 关闭阅读窗口，保存 usage.json 和共享用量账本
    → 保存版本化 StudyNote / 导出 HTML、Markdown / 按需交付飞书
```

纯脚本不会自行理解视频：创建图片不是看过图片，ASR 文本也不能替代视觉阅读。实际模型读图是 skill 工作流的一部分。

### 默认预算与画面覆盖

| 档位 | 概览上限 | 细读上限 | 阅读包上限 | 展示文字字符上限 |
| --- | ---: | ---: | ---: | ---: |
| economy（默认） | 12 | 6 | 4 | 12,000 |
| standard | 24 | 12 | 8 | 24,000 |

当前自动概览约每两分钟取一个时间点，至少三张，按整个处理区间均匀分布；细读根据已读讲解和画面选取。上限不是每次实际使用量。字幕超过材料预算或 ASR 超过单次 30 分钟时，保存检查点并给出明确的部分总结。`continue` 从检查点创建下一段 run，旧笔记与账目保持原版本。

课件可选 `frames --strategy slides`，先本地扫描稳定视觉变化，再按全段时间分桶选取预算内代表帧。候选扫描与模型阅读分别计量；并不保证每一页 PPT 都被捕获。稀疏采样会漏掉页面、瞬时动作或小字。讲解全段已处理与视觉逐页覆盖是两回事；密集课件应在预算内补充代表帧，并在 HTML 中说明覆盖。

### 用量记录

分别记录本地候选扫描帧、提取帧、已读声明、处理分钟数和命令耗时。有可信回执时记录实际 token；没有时为 `null`。文字材料粗估不包含图片、推理、工具或历史上下文，不能当作总 token。Codex 订阅额度不换算为美元；独立 API 计费只依据实际 usage 和核实的价格。

视频计量窗口是 prepare 到 finish；之后的笔记撰写、对话和发布不会伪装成这个窗口的已测用量。纯导出脚本没有模型调用。详细口径见 [references/metering.md](references/metering.md)。

## 验证与开发

下面的单元测试只使用临时目录和合成夹具，不请求平台媒体、飞书服务，也不调用模型：

```bash
uv run --python 3.12 --with pillow python -m unittest discover -s tests -v
python3 -m compileall -q scripts video_notes
```

100 项测试覆盖来源契约、字幕格式/语言、依赖与隐私边界、采样与预算、续读、证据快照/版本、HTML/MD 一致性、飞书在线核对与未知结果恢复。真实验收另外完成 B站短片段 ASR/读图/笔记、YouTube 字幕/抽帧/读图/笔记及实际音频 ASR 回退、飞书正文/图片字节与重复发布、合成课件变化扫描。fixture 成功不等于所有线上视频都可用。

[GitHub Actions 配置模板](ci/github-actions-tests.yml) 已提供，检查 Python 3.11/3.12，**尚未启用**。发布账号令牌缺少 `workflow` 权限；有相应权限后可将模板放入 `.github/workflows/tests.yml`。当前报告的是本地测试和实际验收。

开发约束见 [AGENTS.md](AGENTS.md)。升级 ASR 或上游提取器时，应另做真实材料检查并更新固定版本与来源校验值。

## 下一阶段

功能适配已形成同一素材/证据/笔记链路。后续重点是长课阅读体验、公式渲染、关键图放大、页面样式及许可明确的可分享示例，再整理小红书图文。当前仓库不自动发布社交内容。

## 来源与许可

- 自有代码与文档采用 [MIT](LICENSE)。
- BiliLens 的提取器与转写助手来自 [AntaresGG/BiliBiliVideoParser](https://github.com/AntaresGG/BiliBiliVideoParser)，固定版本，保留 [上游 MIT 许可证](scripts/upstream/LICENSE)。
- 提取设计参考 [ezbug/bilibili-to-obsidian](https://github.com/ezbug/bilibili-to-obsidian)；没有复制该项目无独立许可证的专用脚本。
- 版本、文件校验值和已知边界见 [references/upstream.json](references/upstream.json) 与 [references/sources.md](references/sources.md)。

视频、字幕和画面的内容权利不由本仓库的软件许可覆盖。真实运行材料、Cookie、音视频、私人用量账本和本机验证记录不随仓库发布。
