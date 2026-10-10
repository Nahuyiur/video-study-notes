# Video Study Notes · 视频学习笔记

把视频中的**讲解、关键画面和时间轴**整理成 HTML、Markdown 或飞书学习笔记。

它提供本地网页、Codex skill 和独立 Python/CLI 入口，调用使用者自己配置的视觉模型。所有入口共用素材、证据与笔记结构，适合课程、讲座、教程和研究解读视频。

目前支持 **Bilibili、YouTube、小红书（RedNote）单个视频与本地视频**。默认生成离线 HTML；也可以输出带配图或纯文本 Markdown，并通过本机已有飞书能力创建文档。平台权限、地区或接口变化可能影响素材访问；小红书本轮验证了跳转和登录停止，真实转写与总结尚未验收。

## 能得到什么

- 有内容的中文总结：核心结论、概念/机制解释、结果与边界。
- 可回到原视频的时间轴。
- 关键图、公式、代码或演示画面与解释；图片嵌入 HTML。
- 本次处理范围、抽帧数量和用量记录，区分真实计数、估算与未知。

普通 skill 请求默认用当前 Codex 读图，缺字幕时用本地 faster-whisper 转写。独立 API 模式由你指定端点、模型、密钥引用和预算。默认 HTML 没有 CDN、外部字体或构建系统，图片随文件一起保存。

## 安装与使用

### 在本机打开网页

下载仓库，准备 Python、FFmpeg/FFprobe 和 uv，在仓库目录运行：

```text
uv run --python 3.12 --with pillow python scripts/video_notes.py serve --open
```

也可运行 macOS/Linux 的 `scripts/start-local.sh` 或 Windows 的 `scripts/start-local.cmd`。网页中粘贴视频链接或小红书分享文字，填写自己的模型地址、模型名和密钥，选择区间后开始分析。可以查看进度、打开历史、阅读 HTML 并下载 HTML/Markdown 配图包。密钥只用于当前任务，不写入浏览器存储或磁盘；完成后的查看和下载不调用模型。

启动、依赖配置、中断恢复和隐私说明见 [本地网页使用指南](references/local-product.md)。真实本地链路在 macOS 验证，Windows 文件锁分支已测试，尚未完成 Windows 真机验收。

### 作为 Codex skill

需要 Python 3.11+、FFmpeg/FFprobe；概览联系表与课件候选扫描使用 Pillow，可以通过 `uv run --with pillow` 隔离提供。

按需依赖：本地 ASR 使用 `uv` 和固定版本运行时；YouTube 使用 yt-dlp/EJS 与已有 Deno ≥ 2.3 或 Node ≥ 22；飞书需要已登录的 `lark-cli` user 身份。YouTube 依赖不影响 B站/本地路径，飞书依赖不影响 HTML/MD。小红书默认匿名提取；需要已有登录会话时可显式连接外部 xiaohongshu-skill。详见 [YouTube](references/youtube.md)、[小红书](references/rednote.md) 与 [飞书交付](references/feishu.md)。真实媒体/转写链路已在 macOS 验证，其他系统需分别检查。

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

### 不依赖 Codex 的入口

配置支持图文 JSON 的 Chat Completions 服务，密钥通过环境变量读取。在仓库目录运行：

```bash
uv run --python 3.12 --with pillow python scripts/video_notes.py analyze \
  --video '<视频链接或本地路径>' --out runs/course \
  --base-url 'https://provider.example/v1' --model 'your-vision-model' \
  --api-key-env VIDEO_NOTES_API_KEY --start 0 --end 180 --format html,md
```

示例端点和模型名需要替换，并由你的密钥管理方式设置环境变量。也可 `from video_notes import analyze, ProviderConfig, ApiBudget` 在 Python 中调用。实际 JPEG 与字幕一起提交给模型；预算覆盖概览、可选细读和总结，重复运行使用可核对的缓存。配置、返回值、协议差异与恢复边界见 [独立 API 说明](references/standalone-api.md)。本地网页调用同一核心。

## 工作流

```text
选择视频 / 分 P / 时间范围
    → 提取字幕，缺失时本地 ASR
    → 概览关键帧 + 有限的局部细读
    → Codex 实际阅读，或配置的视觉 API 结合画面解释内容
    → 关闭阅读窗口，保存 usage.json 和共享用量账本
    → 保存版本化 StudyNote / 导出 HTML、Markdown / 按需交付飞书
```

采集和导出脚本不调用理解模型；独立 `analyze` 核心会提交真实图文并生成笔记。创建图片不等于看过图片，材料引用和响应回执也不证明解释正确，仍需核对重要结论。

### 默认预算与画面覆盖

| 档位 | 概览上限 | 细读上限 | 阅读包上限 | 展示文字字符上限 |
| --- | ---: | ---: | ---: | ---: |
| economy（默认） | 24 | 6 | 4 | 12,000 |
| standard | 48 | 12 | 8 | 24,000 |

新任务默认使用混合抽帧 `hybrid`，目标数量为 `min(概览上限, max(6, ceil(处理秒数 / 20)))`。约一半是全段均匀取帧点，其余优先补充稳定画面变化；变化不足时补充均匀点。10 分钟视频默认目标 24 张概览，详细档目标 30 张；30 分钟目标分别为 24、48 张。上限不是每次实际使用量，完全相同的概览图会去重。已有任务保留原预算与素材，不因升级自动增加调用。

候选扫描在本地进行，间隔为 `max(5 秒, 处理秒数 / 180)`，最多 180 张低清候选；候选不会全部交给模型。概览图每六张组成一张带时间戳拼图，细读使用清晰单图。独立 API 可在预算内追加最多六个细读时间点，仍为概览、可选细读、总结的两至三次模型调用；更多画面可能增加输入 token，费用按实际用量记录。

可显式选择 `uniform` 的稀疏均匀采样，或 `slides` 的变化帧替换策略。默认混合模式保留均匀取帧点，避免变化检测将它们全部替换。像素变化不能识别语义重要性，仍可能漏掉细小文字变化、快速换页或动作；讲解全段处理与视觉逐页覆盖是两回事。字幕超过材料预算或 ASR 超过单次 30 分钟时，保存检查点并给出明确的部分总结。`continue` 从检查点创建下一段 run，旧笔记与账目保持原版本。

### 用量记录

分别记录本地候选扫描帧、提取帧、已读声明、处理分钟数和命令耗时。有可信回执时记录实际 token；没有时为 `null`。文字材料粗估不包含图片、推理、工具或历史上下文，不能当作总 token。Codex 订阅额度不换算为美元；独立 API 计费只依据实际 usage 和核实的价格。

原生视频计量窗口是 prepare 到 finish，不含之后的笔记撰写。独立 API 模式在 finish 前完成总结，计入所有模型调用；未知总计与已知小计分开。后续对话与发布不倒算进旧窗口，纯导出脚本没有模型调用。详细口径见 [references/metering.md](references/metering.md)。

## 验证与开发

下面的测试使用临时目录、合成夹具和本地端点替身，不请求平台媒体、飞书服务，也不调用付费模型：

```bash
uv run --python 3.12 --with pillow python -m unittest discover -s tests -v
python3 -m compileall -q scripts video_notes
```

测试覆盖来源契约、字幕格式/语言、依赖与隐私边界、采样与预算、续读、证据快照/版本、HTML/MD 一致性、API 实际图文请求/回执/故障恢复，以及飞书在线核对与未知结果恢复。此前真实验收完成 B站短片段 ASR/读图/笔记、YouTube 字幕/抽帧/读图/笔记及实际音频 ASR 回退、飞书正文/图片字节与重复发布、合成课件变化扫描。

独立 API 已使用缓存的真实 B站前30秒转写与3张帧做一次线上验收，两轮实际模型响应共返回5,034 token，生成 HTML/MD；已存响应恢复与完成后重复运行均无新模型调用。语义核对发现一处跨帧混写，校对版已修正，原始版本保留。这只证明该短片段与当前服务配置可用；较早一次请求超时且结果未知，全部测试的总 token 和费用仍未知，可靠价格缺失时不计为零。服务配置、凭据和运行材料均不随仓库发布。fixture 或单次线上成功都不等于所有视频和模型可用。

[GitHub Actions 配置模板](ci/github-actions-tests.yml) 已提供，检查 Python 3.11/3.12，**尚未启用**。发布账号令牌缺少 `workflow` 权限；有相应权限后可将模板放入 `.github/workflows/tests.yml`。当前报告的是本地测试和实际验收。

开发约束见 [AGENTS.md](AGENTS.md)。升级 ASR 或上游提取器时，应另做真实材料检查并更新固定版本与来源校验值。

## 下一阶段

后续可改进长课阅读体验、原生视频模型、公式渲染和关键图放大。整理许可明确的可分享示例后再制作小红书图文；当前仓库不自动发布社交内容。

## 来源与许可

- 自有代码与文档采用 [MIT](LICENSE)。
- BiliLens 的提取器与转写助手来自 [AntaresGG/BiliBiliVideoParser](https://github.com/AntaresGG/BiliBiliVideoParser)，固定版本，保留 [上游 MIT 许可证](scripts/upstream/LICENSE)。
- 提取设计参考 [ezbug/bilibili-to-obsidian](https://github.com/ezbug/bilibili-to-obsidian)；没有复制该项目无独立许可证的专用脚本。
- 版本、文件校验值和已知边界见 [references/upstream.json](references/upstream.json) 与 [references/sources.md](references/sources.md)。

视频、字幕和画面的内容权利不由本仓库的软件许可覆盖。真实运行材料、Cookie、音视频、私人用量账本和本机验证记录不随仓库发布。
