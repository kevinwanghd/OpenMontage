# 短剧双 agent 产物（v2.5）

以下文件按**每集**写在 `projects/<project>/<episode>/` 下，均为追加式 JSONL，
与 `reviews.jsonl` 同级。消费方按行解析，不要假设顺序。

## rejections.jsonl —— 否决库（LIB-06 / DUAL-06）

每行一条 critic REJECT：

| 字段 | 说明 |
|---|---|
| `created_at` | UTC ISO 时间 |
| `role` | 被驳回的角色（script / visual / shot / edit_sound / director） |
| `reason_code` | REV-03 原因码（须为 `lib/review.py` 已注册码） |
| `findings` | `[{title, text, severity}]`，severity ∈ blocking/warning |
| `model` | 产出该作品的模型 |
| `prompt` | 当时的任务文本（便于复盘） |
| `media` | 被审媒体路径列表 |

查询：`lib.dual_rejections.query_rejections(episode_dir, reason_code=..., role=...)`。

## agent_cost.jsonl —— 双 agent 成本账（DUAL-08）

每次 dual-agent 运行写**两行**（producer、critic 各一行）：

| 字段 | 说明 |
|---|---|
| `pair_id` | 同一次运行的两行共享，用于去重/关联 |
| `role` / `actor` / `model` | 角色、producer|critic、具体模型 |
| `tokens` | 该侧 token 数 |
| `cost_usd` | **该侧成本**（不是总额；对行求和即真实总成本） |

## 其他 episode 产物

- `creative_samples.jsonl`（创意门抽检，DIR-04）：见
  [shortdrama-creative-sampling.md](shortdrama-creative-sampling.md)。
- `director_reviews.jsonl`（DirectorCritic 复核，DUAL-07）：见
  [shortdrama-review-gates.md](shortdrama-review-gates.md) 的导演侧说明。