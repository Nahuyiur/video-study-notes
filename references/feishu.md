# 飞书交付

飞书交付消费已保存的 StudyNote 和证据快照，不重新获取、转写或阅读视频。需要本机已有 `lark-cli` 和可用的 user 身份；核心理解及 HTML/MD 导出不依赖它。不重建 OAuth，也不自动追加授权、改变共享权限或发送消息。

## 使用

```bash
python3 scripts/video_notes.py publish --run RUN --destination feishu
python3 scripts/video_notes.py publish --run RUN --destination feishu --target folder:FOLDER_TOKEN
```

实际子命令由顶层 CLI 提供。模块也提供 `publish_run(run_dir, revision=None, target="new")` 和 `python3 -m video_notes.delivery.feishu --run RUN`。`new` 创建独立的 skill 管理文档；`folder:TOKEN` 在明确给定的文件夹内新建。现有 Docx/Wiki URL 或 token 不能作为覆盖目标。当前版本不修改用户已有正文，也不覆盖后来添加的内容。

按序映射段落、列表、代码、行内 LaTeX、表格、时间轴和来源链接；已声明阅读的 JPEG 作为正文媒体插入图注之后。图注和原视频时间链接保留。讲者内容维持原文归属；Agent 解读、尚不确定的内容显式标注，不能变成讲者结论。核心总结、章节、正文块、时间轴和图示的 evidence_refs 都映射为可读的讲解/画面时间链接。范围与用量使用笔记模块的统一展示事实。正文留一行来源/版本标记，用于恢复时确认是同一份管理笔记。

使用飞书能力之前，Agent 应读取当前安装版本的 `lark-cli skills read lark-doc`、`lark-shared` 及所需 XML/create/fetch/media-insert/media-download 文档。首次未登录时应按 lark-shared 的最小授权流程处理；这个模块只报告缺失，不自行启动授权。

## 回执与恢复

`RUN/deliveries/feishu-<key>.json` 的 key 由 note_id、revision、content_hash、target 决定。回执冻结实际交付 XML、图片计划和 hash，记录 create、媒体 block/file token 和最终回读结果。记录包含私人笔记与资源 token，只留本地，不提交 Git。

任何外部写入前，先原子保存并同步 inflight 回执。结果未知时：

- 创建未知：停止，不再次创建。用户/Agent 找到可能已生成的文档后，传 `--reconcile-document DOCUMENT_ID`；先在线核对版本标记、正文、块数量和链接，再继续原文档。
- 图片插入未知：先读取已知文档，按唯一图注找到媒体，下载并比对冻结 JPEG 的 SHA-256。确认则恢复；缺失、不唯一或下载失败则停止，不盲目重插。
- 本地崩溃遗留的 inflight 等同未知。图片的四步 CLI 操作可能部分完成或自动回滚；无法确认的空块需要人工检查，不自动删块或重写整篇。

`+fetch` 在部分版本不返回 URL，恢复时按标题执行最多四页只读搜索，要求 document token 一致；未找到仍保留回执。相同笔记/目标再次发布始终在线核对，并返回已有 URL，不追加正文或图片。代码排版升级复用回执冻结的旧计划；想采用新排版可保存新的笔记版本，不能伪装原版本覆盖用户内容。

## 验收与限制

完成状态要求正文语义内容、标题/列表/代码/公式/表格数量、来源/时间链接、图片数量均通过回读；每张远端图再下载并与本地冻结字节匹配。只有这些检查通过才记为 verified。缓存回执本身不算远端可用性证明。

已用 `lark-cli 1.0.77` user 身份实际验证原生段落、列表、Python 代码、LaTeX、表格、图注定位、时间链接、图片字节和重复发布复用。模拟测试另覆盖创建/插图超时、崩溃状态、错误目标、回执内容变更、正文丢失、资源变更。真实验收采用自制图片及测试正文，不代表某个视频已被理解。

集成验收还通过顶层 `publish` 命令交付了一份实际阅读过的 19 秒 YouTube 笔记，保留六段字幕证据、三帧阅读声明、两张正文配图和 Agent 解读标注。正文及两张远端图片通过在线回读与字节校验；重复命令返回同一文档。文档地址及交付回执仅保存在本地验收目录，不随仓库发布。

不同飞书/CLI 版本可能降级复杂公式、语言标注或表格样式。模块优先保留原生基础结构，若回读发现内容或必需结构丢失，交付保持未完成；已有 HTML/MD 可以继续阅读。当前没有自动将失败的公式/表格改写成另一篇文档的隐式降级，避免重复写入和偷偷改变内容。单篇交付 XML 上限 160 KB；超长课程按章节保存笔记后分别交付。

XML 回读需要能正常加载标准库 XML parser 的 Python 3.11+。本机默认 Homebrew Python 3.14 曾出现 libexpat 链接异常，已在写入前预检并给出错误；应使用可用的 Python 3.12/3.13 或 Codex 随附运行时，不为此修改系统依赖。

发布脚本不调用模型 API。飞书操作耗时和后续对话不计入笔记冻结的历史视频用量；未知 token/美元费用仍为未知。
