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
## CLI

```powershell
# 技术门 PASS（创意门的前置）
om review decide r1 --action accept --gate-type technical
# 创意门批准（须已有技术门 PASS，undo 后重开）
om review decide r1 --action accept --gate-type creative

# delivery 终审：三判定齐备才记录 DIR-05 创意 full_watch（绑定当前版本哈希）
om review watch ep01 --stage delivery --verify `
  --hook "opens on the unanswered call" `
  --emotion "guilt lands on the last line" `
  --pacing "two-beat hold before the cut"
```

缺任一判定即拒；版本变更后旧终审自动失效（`full_watch_ok` 判定）。

## driver 预检门（迁移说明）

v2.5 起 `Driver` 在每个阶段花钱前多了三道强制检查，存量项目可能被拦：

| 检查 | 触发条件 | 解锁方式 |
|---|---|---|
| D19 启动门 | `bindings/agent_models.yaml` 任一角色 producer/critic 同家族 | 改配置使家族不同，自检 `python -c "from lib.agent_models import ensure_valid; ensure_valid()"` |
| 导演门 | shot_gen 前无 script 门创意批准；delivery 前无 assembly 门创意批准 | `om review decide <id> --action accept --gate-type technical` 后 `--gate-type creative` |
| 音乐许可门 | edit_decisions 引用了未登记/非商用/缺署名曲目 | 补 `music_library/licenses.json`（见 shortdrama-music-library.md） |

plan() 的 `director_gates` 字段随时可查四门状态。
