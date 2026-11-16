# 短剧审核门：技术门与创意门（v2.5 DIR-02）

每个审核点分两道门，决策都写入每集的 `reviews.jsonl`：

| 门 | 谁判 | 判什么 | `gate_type` |
|---|---|---|---|
| 技术门 | 角色 critic + 确定性机检（ffprobe/loudnorm/schema） | 口型、身份、运动、构图、响度、字幕 | `technical` |
| 创意门 | 导演 agent（+ critic） | 钩子、节奏、情绪弧线、戏剧成立 | `creative` |

## 顺序约束

- **技术门 PASS 是创意门的前置**：`ReviewDecision` 里 `gate_type="creative"` 的
  `accept` / `pick_variant` 必须已有同一 review item 的有效技术门 PASS，否则
  `ReviewStore.decide()` 抛 `ReviewValidationError`。
- 创意门 `reject` 不需要技术门（提前否决是安全的）。
- `undo` 掉技术门 PASS 会重新封住创意门（按有效决策链判定）。
- 不传 `gate_type` 的旧调用保持 `null`，行为与 v2.4 一致。

## 示例

```python
from lib.review import ReviewDecision, ReviewStore

store.decide("r1", ReviewDecision(action="accept", gate_type="technical"))
store.decide("r1", ReviewDecision(action="accept", gate_type="creative"))
```

```jsonl
{"review_id":"r1","event_type":"decision","decision":{"action":"accept","gate_type":"technical",...}}
{"review_id":"r1","event_type":"decision","decision":{"action":"accept","gate_type":"creative",...}}
```

字段定义见 `schemas/artifacts/review_decision.schema.json`（`decision.gate_type`，
枚举 `creative|technical|null`）。