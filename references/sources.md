# 来源与边界

- BiliLens：<https://github.com/AntaresGG/BiliBiliVideoParser>。MIT 授权的提取器和 faster-whisper 转写助手固定在 `scripts/upstream`；许可证原样保留，版本与校验值见 [upstream.json](upstream.json)。上游完整报告、弹幕分析与发布流程不进入默认链路。
- 提取设计参考：<https://github.com/ezbug/bilibili-to-obsidian>，核查版本 `0857894ff055300fb1fdc9070456aaa16c6792f9`。采用 B站 DASH 音视频分离、FFmpeg 按时间截取、概览后细读的结构；没有复制该仓库无独立许可证的项目脚本，也不加载其历史游戏关键词、邮件或外部 GPT 指令。
- 公开 API 与媒体访问受字幕可用性、账号、地区与平台接口状态影响。默认匿名；读取认证环境变量需要本会话的授权。Cookie 不写到 run/ledger，也不传往媒体 CDN。
- 分 P 使用所选 CID，并以所选 P 时长规划。ASR 片段的局部时间戳加回原视频起点。源字幕覆盖以时间区间并集记录，保留沉默/缺字幕间隙，不把首尾跨度当作完整字幕覆盖。
- 默认概览按整段分桶采样；课件可选稳定变化候选扫描（最多 30 分钟/180 张低分辨率候选，至少 5 秒间隔），过滤短暂变化并和上一张稳定画面比较，再跨整段分桶选取，然后由实际视觉观察决定局部细读。未保证每一页 PPT 都被捕获；小字、切页瞬间、快速操作可能漏掉。重要公式、代码若在当前分辨率无法辨认，报告不确定而不猜测。提高密度只在用户要求的预算内。
- Agent 对暂时提取失败最多再尝试一次；各平台的内部 socket/retry 上限另有明确边界（YouTube 见专用说明），原生模型不做后台无限循环。缓存与 checkpoint 按 run 独立；不并发写同一个 run。共享账本以文件锁和 run ID 去重。
- ASR 固定 `faster-whisper 1.2.1 / PyAV 18.0.0 / CTranslate2 4.8.2`。本机实测发现 PyAV 19 移除 `metadata_errors` 参数，使默认最新组合失败；上游问题与 workaround：<https://github.com/SYSTRAN/faster-whisper/issues/1589>。缓存音频在 ASR 失败后保留，修复/重试无需重新下载。升级此组合应先跑真实音频检查。

- YouTube：按需使用固定版本 yt-dlp/EJS 与已有受支持的 JavaScript runtime；支持人工/自动字幕和共享本地 ASR，不展开课程播放列表或持续直播。完整依赖、匿名访问、代理与真实验收范围见 [youtube.md](youtube.md)。
- 本地视频：FFprobe 获取时长，接受时间戳字幕，缺失时共用 ASR；本地路径留在 run，不进入笔记证据快照或公开视频输出。
- 统一来源、证据与交付责任见 [architecture.md](architecture.md)。HTML/MD 没有媒体/模型调用；飞书交付需要现有 user 身份和真实在线回读，详见 [feishu.md](feishu.md)。
