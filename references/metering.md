# 用量记录的口径

每次独立调用一个 run；视频时长、所选分 P 时长、本次处理范围、材料源、图片展示次数与耗时分别记录。`processed_minutes` 是声明的处理区间长度，不是 ASR 计算耗时，也不是观看完整视频的证明。`image_presentations_claimed` 是 Agent 回报已读的图片张数；一张概览联系表可以包含六帧。脚本能限制材料预留，无法拦截 Agent 在工具外重复读图或限制全部原生推理 token。

## 当前 Codex 会话

- 有真实 token 回执时优先使用它；不得从模型名称、图片张数或订阅百分比伪造总 token。
- 可在 prepare 时传 `--session-log <本线程JSONL路径>`。脚本只读取最后 2 MB 的 `event_msg/token_count/info/total_token_usage`，保存 prepare 与 finish 的累计差。当前托管会话可能没有这个文件；不要遍历其他会话来寻找用量，也不要把别的线程数据算到本次。
- 累计差的范围是本线程 prepare 到 finish 的观测区间，包含工具/上下文开销，**不包含 finish 之后的最终回复**。计数器未刷新、重置或字段缺失时为 null，不计为零。日志中的输入包含缓存输入；缓存字段单独保留。
- 若宿主给了可信的实测回执，可用 `finish --native-usage FILE.json`，文件包含 `input_tokens`, `output_tokens`, 可选 `input_tokens_details.cached_tokens`, 必填 `source` 与 `scope`。
- 无真实回执时，`material_text_estimate` 仅以 CJK 字符数加其他 UTF-8 字节数/4 作文字材料粗估。它不包含图片、推理、工具、历史上下文，也不代表实际账单；视觉估算与原生美元费用留 null。用这种记录可以比较采样量和处理范围，不能据此断言每分钟实际成本。
- 原生用量工具提供账号级剩余额度时，可保存调用前/后快照，传入 `finish --quota-before FILE --quota-after FILE`。不同窗口要按 limit ID 与时长区分；多个线程共用额度，快照差不等于该视频的独立消耗。两次快照之间跨越重置时不计算消耗。

## 独立视觉 API（仅用户选择后）

默认 skill 不调用付费 API。若用户选择独立 API，由现有可信工具调用并保存仅含 id/model/usage 的回执。不要把 API key 放进文件或输出。记录命令：

```bash
python3 "$S/scripts/video_notes.py" add-api --run "$R" --response usage-receipt.json --prices verified-prices.json --stage vision
```

支持 Responses 和 Chat Completions 的真实 usage 格式；`id` 防止同一个回执重复计费。缺 usage 直接失败，缺价格保留真实 token 而费用为 null。收费依据真实 API usage，不另加假设的图片 token（图片通常已计入输入）。输出 token 已包含推理计数时也不能重复加 reasoning token。

价格文件必须绑定回执里的精确模型名、币种、来源和核对日期，例如结构：

```json
{
  "model": "exact-model-id-from-the-response",
  "currency": "USD",
  "input_per_million": 0,
  "cached_input_per_million": 0,
  "output_per_million": 0,
  "source_url": "official-pricing-page-verified-for-this-model",
  "checked_at": "YYYY-MM-DD"
}
```

上面的 0 是格式占位，**不是价格**；首次真实计价必须从当前官方价格核实并填写。使用其他收费档位、Batch/Flex 或阶梯长上下文价格时，先改为对应档位，不要套普通单价。脚本计算的是 `实际 token × 指定价格`，不是服务商发票；不包含税费、第三方溢价或套餐折扣。

共享账本默认 `~/.local/share/video-study-notes/usage.jsonl`。只含用量、公开视频链接和本地 run 位置，不含原始转写、图片、密钥或 Cookie。用 `--ledger` 可指定其他路径；测试使用独立账本，避免污染真实视频统计。
