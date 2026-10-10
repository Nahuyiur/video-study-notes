# 独立模型调用

同一个视频理解核心有两种入口：Codex 按 skill 实际阅读材料，或独立 Python/CLI 把字幕和实际 JPEG 交给用户配置的视觉模型。两种方式都生成 StudyNote，沿用 HTML、Markdown 和飞书交付。普通 skill 请求仍默认使用当前 Codex；独立 API 模式需要明确配置服务与费用范围。

## 从仓库直接运行

需要现有 Python 3.11+、FFmpeg/FFprobe 和 Pillow。YouTube、本地 ASR 的按需依赖与原流程相同。HTTP 调用使用 Python 标准库，不需要 Codex 会话，也不需要另外启动服务器。

在仓库目录运行；先通过自己的密钥管理方式设置 `VIDEO_NOTES_API_KEY`，不要把密钥写入代码、命令参数或运行目录：

```bash
uv run --python 3.12 --with pillow python scripts/video_notes.py analyze \
  --video 'https://www.bilibili.com/video/BV…/' \
  --out runs/course \
  --base-url 'https://provider.example/v1' \
  --model 'your-vision-model' \
  --api-key-env VIDEO_NOTES_API_KEY \
  --start 0 --end 180 --format html,md
```

示例端点和模型名需要替换为自己的服务。`base_url` 是基础路径，程序会追加 `/chat/completions`。普通远程端点必须使用 HTTPS；HTTP 仅接受本机回环地址。端点不能包含用户名、密码、查询参数或 fragment。

Python 入口在仓库根目录可直接导入：

```python
from video_notes import analyze, analyze_prepared, ProviderConfig, ApiBudget

provider = ProviderConfig(
    base_url="https://provider.example/v1",
    model="your-vision-model",
    api_key_env="VIDEO_NOTES_API_KEY",
)
result = analyze(
    "https://www.youtube.com/watch?v=VIDEO_ID",
    run_dir="runs/course",
    provider=provider,
    start=0,
    end=180,
    focus="解释概念、公式与例子的关系",
    outputs=("html", "md"),
    budget=ApiBudget(),
)
print(result["status"], result["outputs"], result["reason"])
```

Python 也接受运行时 `api_key`；它不进入配置的可显示表示、请求清单或笔记。传密钥时仍应从安全配置读取，不写字面量。已有未完成素材可交给 `analyze_prepared(run_dir, provider=provider, ...)`，不必重新采集。已结束的原生 Codex run 不会自动转换为新的付费理解任务。

CLI 的 `analyze-prepared --run runs/course` 使用同一模型配置参数，沿用已有范围与素材，不接受新的 start/end/transcript/source-json。`--language` 控制笔记语言，`--asr-language` 独立控制转写语言，默认 `auto`。`--no-asr` 可以禁止本地转写。新建 `analyze` 任务时，本地视频可传 `--transcript`；测试或外部系统可以通过 `--source-json` 提供已有来源记录。准确选项可查看 `--help`。

Python 返回 `run_id`、`run_dir`、`status`、`note`、`outputs`、`usage`、`reason` 和 `cached`。成功状态为 `complete`、`partial` 或 `visual_only`；`failed` 或 `outcome_unknown` 带失败原因和当时可知用量，不应当作完成。CLI 只返回笔记身份，不重复打印完整字幕/证据快照，失败退出码为 1。

## 模型协议与内容

目前执行 **Chat Completions 图文 JSON**，没有原生视频上传、自动协议探测或自动改参数重试。模型必须接受实际 `image_url` 数据输入并按要求输出 JSON。`supports_images=True` 是调用者声明，不是服务兼容性检测，也不证明模型看懂了每张图片。

默认输出限制参数是 `max_completion_tokens`，可在 ProviderConfig 明确选择 `token_parameter="max_tokens"`。默认使用 `response_format={"type":"json_object"}`；不支持这个选项的服务可明确设置 `json_mode=False`，返回内容仍必须为符合约定的 JSON。不会自动增加 temperature 或做付费兼容性试探。配置选择参照服务文档；[OpenAI Chat Completions 协议](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)与[图像输入说明](https://developers.openai.com/api/docs/guides/images-vision)只适用于对应服务。

HTTP 超时默认 90 秒。较慢的服务可在 Python 中设置 `ProviderConfig(timeout_seconds=300)`，或在 CLI 使用 `--timeout 300`；支持大于 0、最多 600 秒。这个选项改变传输等待时间，不增加调用次数或输出预算。已经超时且结果未知的 run 仍会阻止重发，延长超时不会解除该保护；经用户同意再次测试时，应新建 run 并保留旧记录。

流程先取字幕，缺失且允许时用本地 ASR，再准备概览图片与同时间轴文字。模型完成一次概览，必要时在预算内选择一组细读时间点，最后生成 StudyNote。标题、简介不能替代讲解，字幕和画面被标明为非指令材料。图文提交回执保存确切材料 ID、哈希和调用身份；最终草稿先校验证据引用与时间范围，再冻结用量、保存和导出。

独立入口默认 `strategy="hybrid"`。先保留全段均匀取帧点，再补充本地扫描出的稳定变化帧；变化不足时补充均匀点。目标概览数为 `min(概览上限, max(6, ceil(处理秒数 / 20)))`，新任务 economy 上限 24 张、standard 上限 48 张。可显式改为 `uniform` 或 `slides`。候选扫描不向模型发送图片，也不代表已读或逐页覆盖。概览每六帧组成一张拼图，细读时间由概览响应选择，最多六个时间点，仍受保存的素材预算与剩余 API 图片、调用、输出预算限制。已有任务不会重写原预算。

引用校验能阻止引用未提交材料，不能证明解释正确。API provenance 表示材料已提交并收到可校验响应；它与 Codex 的人工已读声明分别标记。稀疏采样仍可能漏页、漏动作和小字。部分区间和无语音结果会保留相应状态与限制。

笔记的 `sources` 用于 `{label,url}` 外部参考。若模型把内部材料说明写成 `{type,description,evidence_refs}`，仅接受 `transcript` 或 `validated_visual_observations`，且引用必须分别指向实际提交的字幕或画面；有效说明保留到笔记的限制与材料说明中，不生成网址。原始响应不改写，缺失、错类或不可用引用仍会拒绝保存，也不会触发付费修复。

## 调用预算与账目

素材仍受 economy/standard 档位及每段最多 30 分钟限制。API 默认额外限制为：

| 限制 | 默认值 |
| --- | ---: |
| 调用尝试上限 | 3 |
| 累计展示文字，包括提示与重复输入 | 80,000 字符 |
| 累计提交图片，包括重复展示 | 16 张 |
| 每次请求的输出 token 上限 | 4,096 |
| 累计预留输出 token | 12,288 |
| 单张 JPEG 字节 | 5,000,000 |
| 单次序列化请求字节 | 16,000,000 |
| 单次响应字节 | 2,000,000 |

前五项由 `ApiBudget` 配置，字节限制由 `ProviderConfig` 配置。失败的尝试也占预算，缓存命中不重复预留。图片通常已算入服务返回的输入 token，不另外重复加一笔图片 token。这些限制控制提交量，无法保证任意服务的美元账单硬上限。

CLI 对应 `--max-calls`、`--max-input-chars`、`--max-images`、`--output-tokens`、`--max-output-tokens`、`--max-image-bytes` 和 `--max-request-bytes`。最小流程需要概览和总结两次调用，预算不足会在发送前拒绝；细读是可选的，不够预留下一次总结时会省略并记录限制。可靠价格文件可通过 `--prices` 传入。

实际 token 使用响应的 usage。缺 usage、缺缓存计数或缺可靠价格分别保留未知；不会当作零。只要有未知调用，整体相关总计就保持 `null`，已知小计另列。费用需要精确匹配响应模型的价格文件，格式与限制见 [metering.md](metering.md)。`实际 token × 指定价格` 是估算费用，不是发票。

API 的概览、细读和总结调用都发生在 `finish` 前，因此生成笔记也计入冻结的 API 窗口。HTML/MD 导出、之后的对话和飞书发布没有倒算进这个窗口。原生 Codex 的计量窗口沿用原口径。

## 重复运行和故障恢复

同一个 run 同时只有一个写入者。请求发送前先预留预算并保存 attempt marker；响应落盘后再解析模型内容。相同材料、端点、模型与参数复用可核对的响应。缓存正文、身份或哈希被修改时停止，不重新收费来修补缓存。

网络超时、进程中断或响应超过限制可能留下**结果未知**的请求；它会阻止自动重发及原生阅读/finish。先检查私有回执并与服务侧记录核对，不删除 marker 来重置预算。已落盘响应可以恢复，拒绝、截断、坏 JSON 或坏引用不会触发自动付费修复。

完成后的同配置重复运行和变更 HTML/MD 导出格式复用冻结笔记，不调用模型。改变视频范围、材料或理解设置应使用新的 run；不要让同一个名字覆盖旧证据和账目。飞书继续使用现有 `publish` 流程，不因独立理解模式而自动发布。

升级抽帧与细读提示契约时，未完成 API 任务的旧设置可能与新版不兼容。程序保留原材料和回执并停止，不自动用新版提示重发。旧的冻结笔记仍可直接使用 `export` 查看或换格式，无需模型调用；明确开始新版理解时使用新的 run。

只改表达形式时直接使用导出命令，无需模型凭据或原始媒体：

```bash
python scripts/video_notes.py export --run runs/course --format md --text-only
```

## 借鉴来源与后续边界

[BiliNote](https://github.com/JefferyHcool/BiliNote)已经结合可配置端点、真实图文输入、字幕优先和分块恢复。相关实现可核对[图文调用](https://github.com/JefferyHcool/BiliNote/blob/90940fde4d224d0f69ad99674f4af0a1ea52d3c4/backend/app/gpt/universal_gpt.py#L45-L81)及[分块恢复](https://github.com/JefferyHcool/BiliNote/blob/90940fde4d224d0f69ad99674f4af0a1ea52d3c4/backend/app/gpt/universal_gpt.py#L269-L339)。本项目借鉴这些机制，复用自己的素材、证据、预算和导出结构；没有复制或执行竞品代码。相似功能并非原创性主张，质量与成本需要实际视频比较。

独立 Python/CLI 和[本地网页](local-product.md)共用这个核心。网页管理受控任务与独立分析进程，不复制素材、理解或导出实现。其入口只监听本机，查看历史和下载不调用模型。原生视频接口、多模型自动路由、长课自动连续处理不在当前入口中。
