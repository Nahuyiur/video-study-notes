# 在本机生成视频笔记

这份指南用于独立网页入口。每位使用者填写自己的视频链接、模型服务地址、模型名和密钥。生成的笔记和历史保存在本机，网页不需要 Codex 会话。

## 启动网页

下载或克隆仓库，进入仓库目录。准备 Python 3.11+、FFmpeg 和 FFprobe。推荐使用已有的 uv 提供隔离 Python 与 Pillow：

```text
uv run --python 3.12 --with pillow python scripts/video_notes.py serve --open
```

终端显示本地网址，浏览器打开操作页。`--open` 只负责打开浏览器；省略它可自行打开显示的地址。服务只监听本机回环地址。保持启动窗口运行，关闭页面不会取消正在进行的分析。

macOS 或 Linux 可运行 `scripts/start-local.sh`，Windows 可双击 `scripts/start-local.cmd`。启动脚本优先使用已有 uv，不替你安装系统工具。启动后查看页面的依赖状态；缺少 FFmpeg 时，先将 FFmpeg 与 FFprobe 放入系统 PATH。

已有 Python、没有 uv 时，可以创建独立环境。macOS 或 Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install Pillow
.venv/bin/python scripts/video_notes.py serve --open
```

Windows：

```text
python -m venv .venv
.venv\Scripts\python.exe -m pip install Pillow
.venv\Scripts\python.exe scripts/video_notes.py serve --open
```

本地语音转写仍需要 uv 和已有的固定 ASR 依赖，首次使用会下载转写模型。YouTube 需要其按需提取器和 JavaScript 运行时，见 [YouTube 配置](youtube.md)。缺少可选依赖不影响重新打开已生成的笔记。

## 生成和查看笔记

1. 粘贴 Bilibili 或 YouTube 单个视频链接。
2. 填写自己的 `base_url`、模型名和 API key。基础地址通常包含服务的 API 路径；程序追加 `/chat/completions`。通用示例为 `https://provider.example/v1`，需要自行替换。
3. 选择视频区间、学习重点和调用预算。模型需要支持图片输入并按要求输出 JSON。协议设置见 [独立模型调用](standalone-api.md)。
4. 开始分析，查看实际阶段和处理范围。
5. 打开笔记，下载离线 HTML、Markdown 或包含配图的 Markdown ZIP。

同一时刻处理一个任务。重复点击同一次提交不会创建第二份分析。重新打开已完成的历史、查看或下载结果不调用模型。

每次理解最多处理 30 分钟，并受文字与画面预算限制。长课可能只完成所选范围的前一部分，结果会标出已处理区间和剩余区间。抽样画面不能保证每一页课件都被看到；通过原图和时间点核对重要公式、图示和结论。

实际 token 取自模型响应；缺少 usage 或可靠价格时显示未知。次数和输出上限约束调用量，无法替任意服务保证美元账单。API 故障不会自动更换服务或重新收费尝试。

## 中断后查看历史

网页重启后读取已有任务与笔记，不自动恢复收费调用。安全可继续的任务需要主动选择继续并重新输入密钥；结果未知的请求停止自动重发，先核对服务侧记录。原素材与已收到响应仍保留。

如果只是换输出格式，使用历史中的下载入口，不重新提交分析。Markdown 配图版需要将文件与对应图片目录一起移动；ZIP 已将两者放在同一个包内。

## 配置与隐私

密钥通过当前任务的内存管道进入分析进程，不写入任务元数据、命令参数、环境变量、浏览器存储或导出。任务结束后不保留可用于下一次分析的密钥。Python 内存中的字符串不能保证物理清零。

非密钥任务设置与视频材料存放在你选择的本地数据目录中，用于历史和恢复，不上传到项目仓库。`--data-dir` 可改变保存位置，`--port` 可指定端口。默认数据目录位于使用者目录下，独立于代码检出。

远程模型会收到本次选中的字幕和 JPEG。网页没有分析统计、远程字体或 CDN。页面读取历史时只获取受控任务视图和笔记文件，不公开原始模型回执或本机路径。

已完成的旧笔记可以通过本地操作导入，无需密钥：

```text
python scripts/video_notes.py serve --import-run PATH_TO_FINISHED_RUN --open
```

导入复制经过校验的冻结笔记和配图，浏览器不能指定任意本地路径。CLI 仍保留本地视频、原生 Codex 工作流及授权的飞书交付。

## 当前验证范围

真实素材、API 和本地启动链路在 macOS 验证。跨平台锁覆盖 POSIX 实际进程争用及 Windows 分支模拟测试；Windows 启动指南与代码适配已经提供，尚未在 Windows 真机验证。缺少可选工具或平台视频权限时，页面保留结果并提示所缺条件。
