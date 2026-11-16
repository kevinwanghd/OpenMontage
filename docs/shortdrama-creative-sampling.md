# 创意门人工抽检（v2.5 DIR-04）

导演 agent 自动审创意门之后，按比例抽一部分决策让人工复核。选取由**确定性代码**
完成并落审计，agent 不能自选样本（自选等于自己证明自己清白）。

## 配置（每集 `review_policy.json`，或 `ReviewPolicy` 默认值）

| 键 | 默认 | 说明 |
|---|---|---|
| `creative_ratio` | `0.2` | 创意门人工抽检比例，0..1；与技术抽检 `detail_ratio`（0.35）分队列、互不影响 |
| `creative_seed` | `null` | 固定种子；为空时首次抽样自动生成并记录，可重放 |

## 行为

```python
from lib import creative_sampling as cs

draw = cs.select(review_ids, ratio=0.2)      # 自动种子，随结果返回
cs.replay(draw)                               # True：同种子重放一致
cs.write_audit(episode_dir, draw)             # 落 creative_samples.jsonl
```

- 抽样数 = `ceil(n * ratio)`，去重排序后按种子抽取。
- 抽检比例越界（<0 或 >1）直接报错。
- 审计行：`{created_at, ratio, seed, candidates, chosen}`，见
  [shortdrama-agent-artifacts.md](shortdrama-agent-artifacts.md)。

## 与 REV-10 的关系

REV-10 的每集技术抽检（`detail_ratio` 默认 0.35）保持不变；创意抽检是独立队列，
两个比例分别可配。