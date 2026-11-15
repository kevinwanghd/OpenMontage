# Short-drama CLI Reference（`om`）

本文档由 `shortdrama/cli.py` 的实际 `--help` 输出整理，覆盖 M0 的完整命令文法。
命令、参数与退出码**以运行时代码为准**；契约测试
`tests/contracts/test_shortdrama_docs.py` 会逐项核对本文件与
`shortdrama.cli.build_parser()`、`shortdrama.errors` 是否一致。

- 入口：`om`（`setup.py` 的 `console_scripts`）
- 等价入口：`python -m shortdrama`（未安装 console script 时）
- 入口模块：`shortdrama/cli.py:main`
- M0 承诺：**只读/写本地文件，零外部 API 调用**

---

## 1. 全局选项

```
usage: om [-h] [--projects-dir PROJECTS_DIR] [--bindings-dir BINDINGS_DIR]
          [--verbose] {review,doctor,profile,demo,serve} ...
```

| 选项 | 作用 | 默认 |
|---|---|---|
| `-h`, `--help` | 打印帮助 | — |
| `--projects-dir PROJECTS_DIR` | 项目根目录 | `OPENMONTAGE_PROJECTS_DIR`，否则仓库根 `projects/` |
| `--bindings-dir BINDINGS_DIR` | bindings 根目录（profile/decision_log） | 仓库根 `bindings/` |
| `--verbose` | 输出绝对媒体路径（`review watch` 的 `media_path`） | 关闭 |

注意：

- 全局选项**必须写在子命令之前**：`om --projects-dir ./p review list`。
- `--json` 不是全局选项，每个叶子子命令各自定义，写在子命令之后。
- `--verbose` 只影响 `review watch` 的媒体路径展示；默认不暴露绝对路径。

---

## 2. 命令总览

```
om review list      [--episode EPISODE] [--stage STAGE] [--priority PRIORITY] [--stale] [--json]
om review show      review_id [--json]
om review decide    review_id --action ACTION [--candidate CANDIDATE] [--reason REASON]
                    [--note NOTE] [--prompt-patch PROMPT_PATCH] [--override]
                    [--evidence EVIDENCE] [--candidate-version CANDIDATE_VERSION]
                    [--yes] [--dry-run] [--json]
om review policy    episode [--mode {all,sampled}] [--ratio RATIO] [--json]
om review batch     --episode EPISODE [--dry-run] [--json]
om review undo      review_id --reason REASON [--json]
om review watch     episode --stage {assembly,delivery} [--verify]
                    [--watched-seconds WATCHED_SECONDS] [--json]
om review stats     [--episode EPISODE] [--json]
om doctor           [--profile PROFILE] [--json]
om profile list     [--json]
om profile use      name [--yes] [--json]
om demo             [--mock] [--json]
om demo             --real --approve-live --budget-usd BUDGET [--profile PROFILE]
                    [--stop-after STAGE] [--start-at STAGE] [--without-keyframes]
                    [--max-item-cost-cny CNY] [--dry-run-cost] [--json]
om checkpoint approve --project P --stage STAGE [--note TEXT] [--json]
om ledger reconcile --project P --episode E --entry-id ID --state free|billed [--note TEXT] [--json]
om qa               --project P [--stage S] [--episode E] [--json]
om serve            [--host HOST] [--port PORT] [--allow-remote] [--auth-token TOKEN]
                    [--projects-dir PROJECTS_DIR] [--json]
```

### `om serve`

同源启动 §10 API 与静态审核台（UI-11）：`http://127.0.0.1:8765/review-console/`。
只监听 loopback；`--allow-remote` 必须同时给 `--auth-token`（UI-02）。
`om serve` 是长驻进程，Ctrl+C 退出（退出码 130）。

### `om demo --real` 的人工前置条件

`--real` 只负责生成阶段。创作阶段（idea/beat_map/script/scene_plan）由
director skill 产出 artifact 后，必须已有 **completed + human_approved**
checkpoint；否则驱动在任何付费调用之前暂停（`BATCH_PRECONDITION_FAILED`，
`context.review_required=true`）。这就是"先审剧本和脚本、再生成"的门。

---

## 3. 退出码

`shortdrama/errors.py` 把错误码映射为稳定的 CLI 退出码：

| 退出码 | 含义 | 触发 |
|---|---|---|
| `0` | 成功 | 命令完成且结果正常 |
| `1` | 内部错误 | `INTERNAL_ERROR`（意外异常，带 `trace`） |
| `2` | 用法 / 校验 / 未找到 | 其余错误码（`VALIDATION_ERROR`、`REVIEW_NOT_FOUND`、`BATCH_PRECONDITION_FAILED` 等） |
| `3` | 没有待办 | `NOTHING_PENDING`；`review list` 空队列；`review watch` 无成片 |
| `4` | 过期或被阻塞 | `STALE_REVIEW`；`om doctor` 返回 `BLOCKED` |
| `5` | 预算阻断 | `BUDGET_EXCEEDED`（M0 目录已预留，预算门在 M1 生效） |
| `130` | 操作员中断 | `KeyboardInterrupt` |

`argparse` 自身的参数错误（未知参数、缺少必填参数、非法 `choices`）直接以 `2` 退出。

---

## 4. `--json` 契约

### 4.1 成功

`--json` 把该命令的结果文档写到 **stdout**，结构由命令决定，例如：

- `review list` → `{items, count, policy}`
- `review show` → `{item, decision, stale}`
- `review decide` → `{review_id, project, episode, stage, slot, candidate_version, review_version, decision, created_at, idempotent_replay}`
- `review policy` → `{mode, detail_ratio, face_sim_threshold, sampling_salt}`
- `review batch` → `{ok, dry_run, checked, pending, stale, missing, not_in_episode, errors}`
- `review undo` → `{review_id, undone, event}`
- `review watch` → `{episode_id, stage, state, duration_s, shots, media_url?, version?, verified?}`
- `review stats` → `{scope, episodes, review, total_shots, shot_states}` 或 `{episode_id, review, total_shots, shot_states}`
- `doctor` → `{status, profile, reasons, degradation, cost_comparison}`
- `profile list` → `{active, profiles}`
- `profile use` → `{active, previous?, changed, decision_id?}`
- `demo --mock` → `{mock, project_id, episode_id, episode_dir, created_manifest, seeded_items, external_api_calls, next}`

### 4.2 失败：§10.3 错误信封

失败时 JSON 信封写到 **stderr**（stdout 不输出成功文档），并返回上表的退出码：

```json
{
  "code": "MISSING_REASON_CODE",
  "what": "reject was submitted without a reason_code",
  "fix": "retry with --reason CODE (API: reason_code)",
  "recovery": "fix-then-retry",
  "side_effects": "none",
  "context": { "review_id": "r_demo_reaction", "stage": "shot_gen" },
  "trace": "..."
}
```

- `trace` **只在意外失败（`INTERNAL_ERROR`）时出现**。
- 不带 `--json` 时，同一信封以人类可读的多行文本写到 stderr。
- 字段语义与全部错误码见 [shortdrama-error-codes.md](shortdrama-error-codes.md)。

---

## 5. 非交互模式与确认门

CLI 从不假设同意：

| 场景 | 交互式 TTY | 非交互（管道/CI/`-c`） |
|---|---|---|
| `review decide` 的 `reject` / `send-back` / `edit-prompt` | 询问 `[y/N]` | 必须 `--yes`，否则 `VALIDATION_ERROR` + 退出码 2 |
| `profile use` 切换活动 profile | 询问 `[y/N]` | 必须 `--yes`，否则 `VALIDATION_ERROR` + 退出码 2 |
| 其他只读命令 | 不需要确认 | 不需要确认 |

`--dry-run` 只存在于 `review decide` 与 `review batch`：

- `review decide --dry-run`：在临时 `ReviewStore` 上用真实 `decide()` 校验，不写真实日志；
- `review batch --dry-run`：把 `dry_run` 回显为 `true`。batch 本身只做前置条件检查，不写决策。

---

## 6. `om review list`

```
usage: om review list [-h] [--episode EPISODE] [--stage STAGE]
                      [--priority PRIORITY] [--stale] [--json]
```

列出待审项。默认只列未决项；`--stale` 改为列出“决策落在已被替换的
`candidate_version` 上”的项。

| 参数 | 说明 |
|---|---|
| `--episode EPISODE` | 只看某集 |
| `--stage STAGE` | 只看某阶段（`shot_gen`、`keyframe`、`assembly` 等） |
| `--priority PRIORITY` | 只匹配该优先级（`0` must-view / `1` recommended / `2` skippable） |
| `--stale` | 列过期决策，而不是待审项 |
| `--json` | 机器可读输出 |

退出码：有结果 `0`，空队列 `3`。

```powershell
om review list --episode ep01 --json
om review list --stale --json
```

---

## 7. `om review show`

```
usage: om review show [-h] [--json] review_id
```

返回单项的 `item`、`decision` 与 `stale`。`review_id` 不存在时返回
`REVIEW_NOT_FOUND`（退出码 2）。

```powershell
om review show r_demo_hook --json
```

---

## 8. `om review decide`

```
usage: om review decide [-h] --action ACTION [--candidate CANDIDATE]
                        [--reason REASON] [--note NOTE]
                        [--prompt-patch PROMPT_PATCH] [--override]
                        [--evidence EVIDENCE]
                        [--candidate-version CANDIDATE_VERSION] [--yes]
                        [--dry-run] [--json]
                        review_id
```

六个规范动作：`accept`、`reject`、`pick_variant`、`edit_prompt`、`send_back`、`skip`。
CLI 同时接受连字符拼写（`pick-variant`、`edit-prompt`、`send-back`），内部统一归一化。

| 参数 | 必填 | 说明 |
|---|---|---|
| `review_id` | 是 | 位置参数 |
| `--action ACTION` | 是 | `accept|reject|pick-variant|edit-prompt|send-back|skip` |
| `--candidate CANDIDATE` | `pick_variant` 必填 | 必须是 `item.candidates[]` 中的值 |
| `--reason REASON` | `reject` 必填 | 规范原因码；必须适用于该阶段 |
| `--note NOTE` | 否 | 自由文本备注 |
| `--prompt-patch PROMPT_PATCH` | 否 | `edit_prompt` 的提示词补丁 |
| `--override` | 否 | 人工解决 `UNKNOWN` |
| `--evidence EVIDENCE` | 与 `--override` 同时必填 | 谁/何时/看到什么 |
| `--candidate-version CANDIDATE_VERSION` | 否 | 过期检查的目标版本；不匹配返回 `STALE_REVIEW` |
| `--yes` | 有代价动作必填（非交互） | 确认 `reject`/`send-back`/`edit-prompt` |
| `--dry-run` | 否 | 只校验，不落盘 |
| `--json` | 否 | 机器可读输出 |

校验顺序（与 `lib.review.ReviewStore.decide` 一致）：

1. 动作是否属于六动作 → `INVALID_ACTION`
2. `reject` 是否有原因码 → `MISSING_REASON_CODE`
3. 原因码是否在规范目录 → `UNKNOWN_REASON_CODE`
4. 原因码是否适用于该阶段 → `REASON_CODE_NOT_APPLICABLE`
5. `pick_variant` 的候选是否存在 → `INVALID_CANDIDATE`
6. `override` 是否带证据 → `OVERRIDE_EVIDENCE_REQUIRED`
7. `candidate_version` 是否过期 → `STALE_REVIEW`
8. 有代价动作是否确认 → `VALIDATION_ERROR`（非交互缺 `--yes`）

```powershell
om review decide r_demo_hook --action accept --yes --json
om review decide r_demo_reaction --action reject --reason MOTION_WRONG --yes
om review decide r_demo_hook --action pick-variant --candidate sh_001_v2.mp4 --yes
om review decide r_demo_hook --action accept --dry-run --json
```

---

## 9. `om review policy`

```
usage: om review policy [-h] [--mode {all,sampled}] [--ratio RATIO] [--json]
                        episode
```

读取或更新某集的审核策略。不带 `--mode`/`--ratio` 时只打印当前策略。

| 参数 | 说明 |
|---|---|
| `episode` | 位置参数，剧集 id |
| `--mode {all,sampled}` | `all` 全量审核；`sampled` 按比例抽检 |
| `--ratio RATIO` | 抽检比例，取值 `0..1`（如 `0.35`） |
| `--json` | 机器可读输出 |

策略持久化在 `projects/<project>/<episode>/episode_manifest.json` 的
`review_policy` 字段（`mode`、`detail_ratio`、`face_sim_threshold`、`sampling_salt`）。
改比例只改变队列长度，不重新生成任何素材。

```powershell
om review policy ep01
om review policy ep01 --mode all
om review policy ep01 --mode sampled --ratio 0.5 --json
```

---

## 10. `om review batch`

```
usage: om review batch [-h] --episode EPISODE [--dry-run] [--json]
```

REV-07 批量**前置条件检查**（不写决策）。返回：

```json
{
  "ok": true,
  "dry_run": true,
  "checked": ["r_demo_reaction"],
  "pending": ["r_demo_reaction"],
  "stale": [],
  "missing": [],
  "not_in_episode": [],
  "errors": []
}
```

- `pending`：仍未决策的项；
- `stale`：决策基于旧 `candidate_version`；
- `missing`：`review_id` 不存在；
- `not_in_episode`：项属于别的剧集；
- `errors`：每项一个 §10.3 信封。

退出码：全部通过 `0`；否则取第一个错误的退出码（例如 `NOTHING_PENDING` → `3`、
`STALE_REVIEW` → `4`、`REVIEW_NOT_FOUND` → `2`）。

```powershell
om review batch --episode ep01 --dry-run --json
```

---

## 11. `om review undo`

```
usage: om review undo [-h] --reason REASON [--json] review_id
```

REV-13 补偿：向 `reviews.jsonl` **追加**一条 `event_type: "undo"` 记录（`supersedes`
指向原 `review_id`），当前有效决策变为空，该项回到待审状态；历史决策**不被改写**。

`--reason` 必填。

```powershell
om review undo r_demo_hook --reason "picked wrong candidate" --json
```

---

## 12. `om review watch`

```
usage: om review watch [-h] --stage {assembly,delivery} [--verify]
                       [--watched-seconds WATCHED_SECONDS] [--json]
                       episode
```

REV-11 整片观看面。

| 参数 | 说明 |
|---|---|
| `episode` | 位置参数 |
| `--stage {assembly,delivery}` | 必填；只有这两个阶段有整片观看 |
| `--verify` | 记录一条 `full_watch` 决策（绑定当前媒体 sha256） |
| `--watched-seconds WATCHED_SECONDS` | 操作员声称的观看秒数；`--verify` 时必须 ≥ `duration_s` |
| `--json` | 机器可读输出 |

媒体发现顺序：`media/final.mp4` → `renders/final.mp4` → `media/assembly.mp4` →
`media/delivery.mp4` → `media/*.mp4` / `renders/*.mp4` 中最新者。

不带 `--verify`：只报告 `state`（`ready` / `not-generated`）、`shots`、`version`
（sha256）与 `media_url`；有媒体退出 `0`，无媒体退出 `3`。

带 `--verify` 的前置条件：

1. 必须有成片媒体，否则 `MEDIA_NOT_FOUND`（`do-X-first`）；
2. 所有镜头状态必须是 `accepted` / `assembled` / `skipped`，否则
   `BATCH_PRECONDITION_FAILED`；
3. `--watched-seconds` 若提供，必须 ≥ `duration_s`，否则 `VALIDATION_ERROR`；
4. 成功后写入 `review_id = fw:<episode>:<stage>` 的 `full_watch` 决策；
   同一媒体版本重复执行返回 `already_verified: true`（幂等）。

```powershell
om review watch ep01 --stage assembly --json
om review watch ep01 --stage assembly --verify --watched-seconds 30 --json
```

---

## 13. `om review stats`

```
usage: om review stats [-h] [--episode EPISODE] [--json]
```

- 带 `--episode`：返回该集统计；
- 不带：聚合所有集，`scope: "all"`，并列出 `episodes[]`。

字段包含 `total_items`、`decided`、`pending`、`action_distribution`、
`reason_code_distribution`、`first_pass_accept_rate`；EpisodeLog 有镜头事件时还会
给出 `total_shots` 与 `shot_states`。

```powershell
om review stats --episode ep01 --json
om review stats --json
```

---

## 14. `om doctor`

```
usage: om doctor [-h] [--profile PROFILE] [--json]
```

X-13 三态体检：`GREEN` / `DEGRADED` / `BLOCKED`。

| 参数 | 说明 |
|---|---|
| `--profile PROFILE` | 指定 profile；默认取 `bindings/model_bindings.yaml` 的 `active_profile` |
| `--json` | 机器可读输出 |

`BLOCKED` 时退出码为 `4`，其余 `0`。doctor 只读本地 registry、profile 与
`account_state.yaml`，不发网络请求。

```powershell
om doctor --json
om doctor --profile tiktok-draft --json
```

---

## 15. `om profile list` / `om profile use`

```
usage: om profile list [-h] [--json]
usage: om profile use [-h] [--yes] [--json] name
```

- `profile list`：列出 `bindings/profiles/*.yaml` 与当前 `active`；
- `profile use <name>`：校验 profile 存在后，改写
  `bindings/model_bindings.yaml` 的 `active_profile`，并向
  `bindings/decision_log.json` 追加一条 `provider_selection` 决策。

非交互模式必须 `--yes`；已经是活动 profile 时不写盘，返回 `changed: false`。

```powershell
om profile list --json
om profile use seedance --yes --json
```

---

## 16. `om demo`

```
usage: om demo [-h] [--mock] [--json]
```

NFR-12 零 API hello-world。

| 参数 | 说明 |
|---|---|
| `--mock` | M0 **必填**；真实生成属于 M1 |
| `--json` | 机器可读输出 |

不带 `--mock` 返回 `VALIDATION_ERROR` 与退出码 `2`。成功时写入
`projects/demo/ep01/` 并确保两个 demo 审核项存在，`external_api_calls` 恒为 `0`。

```powershell
om demo --mock --json
```

---

## 17. `om checkpoint approve`

```
usage: om checkpoint approve [-h] --project PROJECT --stage STAGE [--note NOTE] [--json]
```

把生成阶段写下的 `awaiting_human` 检查点推进为 `completed` + `human_approved=True`。
用于自动审片通过后由 agent 推进，不需要人工逐项确认。

## 18. `om ledger reconcile`

```
usage: om ledger reconcile [-h] --project P --episode E --entry-id ID --state free|billed [--note NOTE] [--force] [--json]
```

provider 拒绝或崩溃后账本进入 `reconcile`；结算该条目后才能继续真实运行。只允许结算
`unknown` 条目，非 `unknown` 需 `--force`。

## 19. `om qa`

```
usage: om qa [-h] --project PROJECT [--stage {keyframe,shot_gen,assembly,delivery}] [--episode EPISODE] [--json]
```

自动审片门禁：确定性检查（画幅、时长、对白音轨、字幕时长、连续性引用）加审片 agent
持久化的 `artifacts/qa_visual.json`。`passed=false` 时退出码 `1`，driver 在
`shot_gen` / `assembly` 后自动调用同一套检查。

```powershell
om qa --project the-voicemail-m1 --stage shot_gen --json
```

---

## 20. Windows 说明

- PowerShell 中直接调用 `om` 需要先激活 venv 或把 `Scripts` 加进 PATH；否则用
  `.\.venv\Scripts\python.exe -m shortdrama ...`。
- CLI 在输出边界对无法编码的字符做 `errors="replace"` 降级，GBK 控制台不会崩溃；
  需要稳定输出时优先 `--json`。
- 路径参数用正斜杠或反斜杠均可；本仓库内部统一按 `Path` 处理。