# HTML 输出

当前 HTML 从已验证的 StudyNote v2 生成，内容结构见 [note-schema.md](note-schema.md)。正文由 Agent 在实际读完图文材料后写出；渲染器只排版。

```bash
python3 "$S/scripts/video_notes.py" export --run "$R" --format html
```

默认文件 `$R/exports/note-v001.html` 为离线单文件，图像与样式嵌入，没有 CDN/字体/模型调用。公式显示可读源码；后续排版升级可单独进行。运行范围、已读帧和用量均来自冻结快照，不能让模型编造统计。

旧 summary.json 使用 `note --legacy-summary` 显式导入；原始账本保持不变。原来的直接 html 命令已合并到 export，只有一份当前渲染器。

实际打开页面检查摘要、图注、部分覆盖提示、较窄窗口和时间跳转。无法预览时记录未验证，不声称视觉检查通过。
