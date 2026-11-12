# Short-drama 审核 Runbook

面向实际负责审核的操作员（owner）。覆盖：

- L1/L2/L3 分层审核；
- 抽检比例模式 `all` / `sampled`；
- `batch` 前置条件；
- `undo` / supersede 补偿；
- `stale`（过期决策）处理；
- 整片 `watch --verify`；
- 审核相关磁盘格式与故障排查。

前置阅读：[quickstart](shortdrama-quickstart.md)、
[CLI reference](shortdrama-cli-reference.md)、
[错误码](shortdrama-error-codes.md)。

M0 全程本地文件，无外部 API 调用；审核计时数据只留在本地（X-15）。

---

## 1. 角色与入口

| 入口 | 用途 |
|---|---|
| `om review list` | 看待审队列 |
| `om review show` | 看单项详情与当前决策 |
| `om review decide` | 记录决策（唯一变更入口） |
| `om review policy` | 看/改审核策略 |
| `om review batch` | 批量前置条件检查 |
| `om review undo` | 补偿一次决策 |
| `om review watch` | 整片观看 / 验证 |
| `om review stats` | 吞吐、动作分布、首过通过率 |

所有写操作都经过 `lib.review.ReviewStore.decide` / `undo`，日志 append-only，
CLI 与 HTTP 行为一致（X-11）。

---

## 2. L1 / L2 / L3 分层审核

分层由审核项的 `priority` 投影而来（`lib/review.py:_layer`）：

| 层 | `priority` | 语义 | 是否默认进入队列 |
|---|---|---|---|
| **L1** | `0` | must-view（必看：hook、角色一致性等） | **永久进入**，不受抽检比例影响 |
| **L2** | `1` | recommended（建议看） | `sampled` 模式按比例抽取；`all` 全看 |
| **L3** | `2` 及以上 | skippable（可跳过） | 同上 |

规则：

1. `priority == 0` 的项**永远**进入待审队列，无论 `mode` 与 `detail_ratio`。
2. 只有 `priority > 0` 的项参与抽检。
3. `review stats` 的 timing 部分按层统计 `l1_items` / `l1_minutes`、`l2_*`、`l3_*`，
   便于发现“时间都花在哪一层”。

```powershell
# 只看 must-view
om review list --episode ep01 --priority 0
```

> 故事板与 L1 的区别：故事板是只读一致性视图（含未生成镜头）；L1 队列只包含
> **待审项**且可操作。

---

## 3. 抽检比例模式 `all` / `sampled`

策略字段（`episode_manifest.json` 的 `review_policy`）：

| 字段 | 默认 | 含义 |
|---|---|---|
| `mode` | `sampled` | `all` = 全量；`sampled` = 抽检 |
| `detail_ratio` | `0.35` | 抽检目标比例，`0..1` |
| `face_sim_threshold` | `0.75` | 人脸相似度阈值（低于阈值进入关注） |
| `sampling_salt` | `"default"`（demo 为 `"ep01"`） | 抽样盐，保证同一集结果稳定 |

### 3.1 查看与修改

```powershell
om review policy ep01
om review policy ep01 --mode all
om review policy ep01 --mode sampled --ratio 0.35
```

### 3.2 抽样是确定性的（REV-02/10）

`sampled` 模式下：

1. 未决项分成 `must`（priority 0）与 `rest`（priority > 0）；
2. 对 `rest` 计算 `sha256(f"{salt}:{review_id}") % 100`；
3. 桶值 `< detail_ratio * 100` 的项入选；
4. 以 `ceil(未决总数 × detail_ratio)` 为目标，不足时按 `(priority, slot)` 顺序补齐；
5. `must` 始终全部保留。

因此：

- 同一个 `salt` + 同一个 `review_id` → 同一结果，跨进程稳定；
- **改比例只改变队列长度，不重新生成任何素材**（REV-10）；
- 策略写入不会触碰 `reviews.jsonl`、历史决策或候选。

### 3.3 推荐用法

| 场景 | 建议 |
|---|---|
| 首集 / 新角色 / 新供应商 | `--mode all`，先建立基线 |
| 稳定批量生产 | `--mode sampled --ratio 0.3~0.5` |
| 事故复盘 | 临时 `--mode all`，审完再调回 |

---

## 4. 日常审核闭环

```powershell
# 1. 看队列
om review list --episode ep01

# 2. 逐项查看
om review show r_demo_hook --json

# 3. 决策
om review decide r_demo_hook --action accept --yes
om review decide r_demo_reaction --action reject --reason MOTION_WRONG --yes

# 4. 看进度
om review stats --episode ep01 --json
```

决策动作语义：

| 动作 | 语义 | 有代价 |
|---|---|---|
| `accept` | 通过当前候选 | 否 |
| `skip` | 跳过（**skip ≠ pass**） | 否 |
| `reject` | 驳回并触发返工 | 是（需 `--yes`） |
| `send_back` | 退回上一环节 | 是（需 `--yes`） |
| `edit_prompt` | 改提示词后重生成 | 是（需 `--yes`） |
| `pick_variant` | 从候选中选一个 | 否 |

`reject` 必须给阶段适用的原因码；完整矩阵见
[错误码文档第 5 节](shortdrama-error-codes.md#5-原因码与阶段适用范围)。

### 4.1 先预检再落盘

```powershell
om review decide r_demo_reaction --action reject --reason MOTION_WRONG --dry-run --json
```

`--dry-run` 使用真实 `ReviewStore.decide` 校验，但不写日志。适合批量操作前的语法预检。

### 4.2 非交互

管道、CI、脚本中执行有代价动作**必须** `--yes`；否则返回
`VALIDATION_ERROR` / 退出码 `2`。CLI 从不默认同意。

---

## 5. `batch` 前置条件

`om review batch` 是**只读**前置条件检查（REV-07），不会写决策。

```powershell
om review batch --episode ep01 --dry-run --json
```

返回字段：

| 字段 | 含义 | 处理 |
|---|---|---|
| `ok` | 是否全部通过 | `false` 时看 `errors[0].code` |
| `checked` | 本次检查的 `review_id` 列表 | — |
| `pending` | 仍未决策的项 | 可继续审核 |
| `stale` | 决策基于旧 `candidate_version` | 见第 7 节 |
| `missing` | `review_id` 不存在 | 检查 id |
| `not_in_episode` | 项属于别的剧集 | 用正确的 `--episode` |
| `errors` | 每项一个 §10.3 信封 | 按 `fix` 处理 |

判定规则（`ShortdramaService.batch`）：

1. 显式传入的 `review_ids` 逐项检查；否则取该集所有未决项；
2. `missing` / `not_in_episode` / `stale` 任一非空 → `ok=false`；
3. 没有任何待办项（且上面都为空）→ 追加 `NOTHING_PENDING`，退出码 `3`；
4. `ok=true` → 退出码 `0`；
5. `ok=false` → 退出码取**第一条错误**的映射（`NOTHING_PENDING=3`、
   `STALE_REVIEW=4`、其余通常 `2`）。

batch 永不写 `reviews.jsonl`；`--dry-run` 只是把 `dry_run` 回显为 `true`。

---

## 6. `undo` / supersede

审核日志是 append-only：**没有“修改”或“删除”决策**，只有补偿事件。

```powershell
om review undo r_demo_hook --reason "picked wrong candidate" --json
```

行为：

1. 追加 `event_type: "undo"` 记录，`supersedes` 指向原 `review_id`；
2. 该项的**当前有效决策变为空**，重新回到待审队列；
3. 原决策记录原样保留，审计链完整（REV-13）；
4. `--reason` 必填，写入 `metadata.reason`。

适用场景：

- 选错候选 / 误点 accept；
- 决策后发现质量问题；
- 处理过期决策（见下一节）；
- 需要重新走一次 `decide` 前，先清空旧的有效决策。

> 不要手工编辑 `reviews.jsonl`。手工改历史会破坏有效决策折叠与审计链。

---

## 7. `stale`：过期决策

### 7.1 判定

一条决策记录带有 `candidate_version`（决策时的候选版本）。当审核项的当前
`candidate_version` 与之不同，该决策即为 **stale**（目标候选已被替换）。

### 7.2 查看

```powershell
om review list --stale --json
om review list --stale --episode ep01
```

`--stale` 列出的是**带过期决策的项**（`item`），不是待审项。

### 7.3 处理

```powershell
# 1. 看当前候选
om review show <review_id> --json

# 2. 补偿旧决策
om review undo <review_id> --reason "candidate version superseded"

# 3. 对当前候选重新决策
om review decide <review_id> --action accept --candidate-version <current> --yes
```

只读 `list --stale` 不会自动修复；必须显式 undo。

### 7.4 batch 中的 stale

`batch` 会把 stale 项放进 `stale[]` 并生成 `STALE_REVIEW` 错误，整体 `ok=false`，
退出码 `4`。先处理 stale，再重跑 batch。

---

## 8. 整片 `watch --verify`（REV-11）

逐镜审核不能替代整片观看；`assembly` 与 `delivery` 两个阶段必须整片过一遍。

### 8.1 先看状态

```powershell
om review watch ep01 --stage assembly --json
```

输出：

```json
{
  "episode_id": "ep01",
  "stage": "assembly",
  "state": "not-generated",
  "duration_s": 30,
  "shots": {}
}
```

- `state: "ready"` 且有 `media_url` / `version`（sha256）表示可以验证；
- `state: "not-generated"` → 退出码 `3`，先完成组装；
- `shots` 是每个镜头的当前状态（来自 `events.jsonl`）。

### 8.2 记录整片验证

```powershell
om review watch ep01 --stage assembly --verify --watched-seconds 30
```

前置条件（任何一条不满足都会拒绝）：

| 条件 | 失败错误码 | `recovery` |
|---|---|---|
| 存在成片媒体 | `MEDIA_NOT_FOUND` | `do-X-first` |
| 所有镜头为 `accepted` / `assembled` / `skipped` | `BATCH_PRECONDITION_FAILED` | `do-X-first` |
| `--watched-seconds` ≥ `duration_s` | `VALIDATION_ERROR` | `fix-then-retry` |

成功后：

- 写入 `review_id = fw:<episode>:<stage>`、`slot = full_watch` 的审核项；
- 记录一条 `accept` 决策，`evidence` 包含 `full_watch sha256:<version> stage:<stage>`；
- 同一媒体版本重复执行返回 `already_verified: true`，不重复写决策；
- 媒体版本变化会产生新的 `review_version`。

`--stage` 只接受 `assembly` 或 `delivery`。`media_path` 仅在全局 `--verbose` 下输出。

---

## 9. 磁盘格式

### 9.1 `projects/<project>/<episode>/reviews.jsonl`

追加式 JSONL，每行一条记录，schema 见
`schemas/artifacts/review_decision.schema.json`。`event_type` 三种：

**item（审核项快照）**

```json
{
  "version": "1.0",
  "project": "demo",
  "episode": "ep01",
  "review_id": "r_demo_hook",
  "stage": "shot_gen",
  "slot": "sh_001",
  "event_type": "item",
  "candidates": ["sh_001_v1.mp4", "sh_001_v2.mp4"],
  "criteria": ["face_similarity", "motion"],
  "priority": 0,
  "priority_reasons": ["hook"],
  "candidate_version": "v1",
  "review_version": "1",
  "created_at": "2026-11-12T05:01:33+00:00"
}
```

**decision（决策）**

```json
{
  "version": "1.0",
  "review_id": "r_demo_hook",
  "project": "demo",
  "episode": "ep01",
  "stage": "shot_gen",
  "event_type": "decision",
  "candidate_version": "v1",
  "review_version": "1",
  "decision": {
    "action": "accept",
    "chosen_candidate": null,
    "reason_code": null,
    "note": "",
    "prompt_patch": null,
    "override": false,
    "evidence": "",
    "decided_at": "2026-11-12T05:01:49+00:00",
    "decided_by": "owner"
  },
  "created_at": "2026-11-12T05:01:49+00:00"
}
```

**undo（补偿）**

```json
{
  "version": "1.0",
  "review_id": "r_demo_hook",
  "stage": "shot_gen",
  "slot": "sh_001",
  "event_type": "undo",
  "supersedes": "r_demo_hook",
  "metadata": { "reason": "picked wrong candidate", "compensation": true },
  "created_at": "2026-11-12T05:10:00+00:00"
}
```

有效决策折叠规则：按行顺序，`decision` 覆盖当前值，`undo` 把目标置空。

### 9.2 `review_policy.json`（可选）

`lib.review.ReviewStore.set_policy` 直接驱动 review 层时写出的策略文件：

```json
{
  "mode": "sampled",
  "detail_ratio": 0.35,
  "face_sim_threshold": 0.75,
  "always_detail": ["hook", "climax"],
  "sampling_salt": "default"
}
```

CLI / HTTP 的 `om review policy` 走的是 `episode_manifest.json` 的 `review_policy`，
不写这个文件；两者不要混用。默认没有该文件时使用 `ReviewPolicy()` 默认值。

### 9.3 `episode_manifest.json` → `review_policy`

```json
{
  "review_policy": {
    "mode": "sampled",
    "detail_ratio": 0.35,
    "face_sim_threshold": 0.75,
    "sampling_salt": "ep01"
  }
}
```

### 9.4 `projects/<project>/<episode>/events.jsonl`

镜头状态事件（只追加），`shot_states()` 回放得到状态投影。合法状态：

```
planned → keyframing → keyframe_approved → generating → in_review → accepted → assembled
                    ↘ rejected ↗            ↘ skipped
```

`watch` / `stats` 读取该投影判断 `shots` 与 `total_shots`。

### 9.5 `bindings/decision_log.json`（profile 切换审计）

`om profile use` 追加：

```json
{
  "decision_id": "dec_profile_ab12cd34ef56",
  "category": "provider_selection",
  "subject": "active_profile",
  "options_considered": ["tiktok-draft", "seedance"],
  "chosen": "seedance",
  "reason": "om profile use (operator-confirmed binding switch)",
  "decided_at": "2026-11-12T05:20:00+00:00",
  "decided_by": "owner"
}
```

append-only：与 `reviews.jsonl` 一样，不改写历史。

---

## 10. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `list` 退出码 3 | 没有待审项，或全部已决策 | `--stale` 检查过期项；确认 `--episode` |
| `decide` 报 `MISSING_REASON_CODE` | reject 缺原因码 | 加 `--reason CODE` |
| `decide` 报 `REASON_CODE_NOT_APPLICABLE` | 原因码不适用于该 stage | 按错误里的 `fix` 列表换码 |
| `decide` 报 `STALE_REVIEW` | 候选版本已更新 | `show` 看当前版本，`undo` 后重审 |
| `decide` 退出码 2 且提示确认 | 非交互模式少了 `--yes` | 加 `--yes` |
| `batch` 报 `NOTHING_PENDING` | 队列已清空 | 正常，退出码 3 |
| `watch` 退出码 3 | 还没有成片 | 先完成组装 |
| `watch --verify` 报 `BATCH_PRECONDITION_FAILED` | 有镜头未 accepted/assembled/skipped | 完成或显式 skip 剩余镜头 |
| `watch --verify` 报 `VALIDATION_ERROR` | `--watched-seconds` 小于 `duration_s` | 看完或补足秒数 |
| `undo` 报 `REVIEW_NOT_FOUND` | id 不存在 | `list`/`show` 确认 id |
| `stats` 中 `first_pass_accept_rate` 为 `null` | 还没有已决策项 | 正常 |

---

## 11. 操作纪律

1. **只追加，不改写**：所有修正走 `undo`。
2. **有代价动作显式确认**：脚本里写 `--yes`。
3. **先 dry-run 再落盘**：批量/不确定时先 `--dry-run`。
4. **抽检改动只影响队列**：不要为了“看看效果”删除审核项。
5. **整片必须看**：逐镜通过不等于整片通过；`assembly`/`delivery` 走 `watch --verify`。
6. **计时数据不外传**：审核时间戳只留在本地 episode log（X-15）。