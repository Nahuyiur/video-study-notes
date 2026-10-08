# 来源与边界

- BiliLens：<https://github.com/AntaresGG/BiliBiliVideoParser>。MIT 授权的提取器和 faster-whisper 转写助手固定在 `scripts/upstream`；许可证原样保留，版本与校验值见 [upstream.json](upstream.json)。上游完整报告、弹幕分析与发布流程不进入默认链路。
- 提取设计参考：<https://github.com/ezbug/bilibili-to-obsidian>，核查版本 `0857894ff055300fb1fdc9070456aaa16c6792f9`。采用 B站 DASH 音视频分离、FFmpeg 按时间截取、概览后细读的结构；没有复制该仓库无独立许可证的项目脚本，也不加载其历史游戏关键词、邮件或外部 GPT 指令。
- 公开 API 与媒体访问受字幕可用性、账号、地区与平台接口状态影响。默认匿名；读取认证环境变量需要本会话的授权。Cookie 不写到 run/ledger，也不传往媒体 CDN。
- 分 P 使用所选 CID，并以所选 P 时长规划。ASR 片段的局部时间戳加回原视频起点。源字幕覆盖以时间区间并集记录，保留沉默/缺字幕间隙，不把首尾跨度当作完整字幕覆盖。
- 默认概览按整段分桶采样，然后由实际视觉观察决定局部细读。未保证每一页 PPT 都被捕获；小字、切页瞬间、快速操作可能漏掉。重要公式、代码若在当前分辨率无法辨认，报告不确定而不猜测。提高密度只在用户要求的预算内。
- 默认提取网络失败最多重试两次，原生模型不做后台无限循环。缓存与 checkpoint 按 run 独立；不并发写同一个 run。共享账本以文件锁和 run ID 去重。
- ASR 固定 `faster-whisper 1.2.1 / PyAV 18.0.0 / CTranslate2 4.8.2`。本机实测发现 PyAV 19 移除 `metadata_errors` 参数，使默认最新组合失败；上游问题与 workaround：<https://github.com/SYSTRAN/faster-whisper/issues/1589>。缓存音频在 ASR 失败后保留，修复/重试无需重新下载。升级此组合应先跑真实音频检查。
