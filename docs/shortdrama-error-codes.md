# Short-drama 错误码目录（agent-ready 错误契约）

本文档从 `shortdrama/errors.py` 提取，覆盖 API、CLI、doctor 共用的一套稳定错误码
（需求 §10.3 / X-2）。契约测试 `tests/contracts/test_shortdrama_docs.py` 会核对
`ErrorCode` 枚举中的每个码都在本文件出现。

- 枚举源：`shortdrama/errors.py:ErrorCode`
- 人类可读目录：`shortdrama/errors.py:ERROR_CATALOG`
- 退出码映射：`shortdrama/errors.py:_EXIT_CODES`
- 决策拒绝分类：`shortdrama/errors.py:classify_review_rejection`

---

## 1. 信封结构

API body 与 CLI stderr 使用同一个信封：

```json
{
  "code": "STALE_REVIEW",
  "what": "decision targets candidate version 'v1', current is 'v2'",
  "fix": "reload the review item and decide on the current candidate",
  "recovery": "retry",
  "side_effects": "none",
  "context": { "review_id": "r_demo_hook", "current_version": "v2" },
  "trace": null
}
```

| 字段 | 类型 | 语义 |
|---|---|---|
| `code` | string | 稳定机器码，来自 `ErrorCode`，永不因文案变化而变 |
| `what` | string | **操作 + 实际值**，例如 `review_id 'x' not found`；不得为空 |
| `fix` | string | 安全的下一步命令或可执行建议 |
| `recovery` | enum | `retry` \| `fix-then-retry` \| `do-X-first` \| `ask-human` |
| `side_effects` | enum | `none` \| `partial` \| `complete` \| `unknown` |
| `context` | object | 结构化上下文（id、stage、版本、路径等）；默认 `{}` |
| `trace` | string \| null | **仅意外失败**（`INTERNAL_ERROR`）携带；正常拒绝不输出 |

### 1.1 `recovery` 取值

| 值 | 含义 |
|---|---|
| `retry` | 原样重试即可（例如重新加载后再决定） |
| `fix-then-retry` | 修正输入/配置后重试 |
| `do-X-first` | 必须先完成前置步骤（例如先组装成片） |
| `ask-human` | 机器无法安全自动解决，需要人工介入 |

### 1.2 `side_effects` 取值

| 值 | 含义 |
|---|---|
| `none` | 未产生任何持久化变更（大多数校验/未找到） |
| `partial` | 部分写入（调用方可重放/补偿） |
| `complete` | 操作已完成，但后续步骤失败 |
| `unknown` | 无法确认状态（内部错误默认值） |

### 1.3 脱敏（redaction）

- 信封只包含 `ApiError` 显式传入的 `context`；**不包含凭据、token、Authorization
  头或环境变量值**。
- 媒体路径拒绝（`MEDIA_PATH_FORBIDDEN`）不回显服务器绝对路径，只回显请求中的相对路径
  （X-12）。
- CLI `review watch` 默认只给 `media_url`（相对 URL），绝对路径仅在全局 `--verbose`
  时输出。
- `trace` 只在意外异常时出现，且只放异常文本，用于人工诊断，不应作为稳定契约解析。

### 1.4 CLI 退出码公式

```python
exit_code = explicit_exit_code or _EXIT_CODES.get(code, 2)
```

即：`NOTHING_PENDING → 3`、`STALE_REVIEW → 4`、`BUDGET_EXCEEDED → 5`、
`INTERNAL_ERROR → 1`，其余默认 `2`。`KeyboardInterrupt` 返回 `130`。
详见 [shortdrama-cli-reference.md](shortdrama-cli-reference.md#3-退出码)。

---

## 2. 完整错误码目录

| code | 人类可读含义（`ERROR_CATALOG`） | 典型触发 | 典型 `fix` | 默认退出码 | 典型 HTTP |
|---|---|---|---|---|---|
| `VALIDATION_ERROR` | Request shape or value failed validation. | 参数/请求体非法、缺 `--yes`、非交互确认缺失、非法分页、非法 `watched-seconds` | `check the command arguments and retry` / `re-run with --yes` | 2 | 400 / 401 / 403 / 422 |
| `INVALID_ACTION` | Action is not one of the six review actions. | `--action` 不属于六动作 | `use one of: accept, edit_prompt, pick_variant, reject, send_back, skip` | 2 | 422 |
| `MISSING_REASON_CODE` | reject requires a reason_code. | `reject` 未带 `--reason` | `retry with --reason CODE (API: reason_code)` | 2 | 422 |
| `UNKNOWN_REASON_CODE` | reason_code is not in the canonical catalogue. | 原因码不在规范目录 | `use a code from the published reason-code catalogue` | 2 | 422 |
| `REASON_CODE_NOT_APPLICABLE` | reason_code does not apply to this stage (REV-03). | 原因码存在但不适用于当前 stage | `use one of: <该阶段允许的原因码>` | 2 | 422 |
| `INVALID_CANDIDATE` | pick_variant requires a candidate from candidates[]. | `pick_variant` 的候选不在 `candidates[]` | `choose one of: <candidates>` | 2 | 422 |
| `OVERRIDE_EVIDENCE_REQUIRED` | Overriding UNKNOWN requires recorded evidence. | `--override` 未带 `--evidence` | `record who/when/what was visibly verified, then retry` | 2 | 422 |
| `STALE_REVIEW` | Decision targets a superseded candidate version. | `candidate_version` 已过期 | `reload the review item and decide on the current candidate` | 4 | 409 |
| `REVIEW_NOT_FOUND` | review_id does not exist. | `review_id` 不存在 | `list pending reviews first` | 2 | 404 |
| `EPISODE_NOT_FOUND` | Episode manifest does not exist. | 找不到 `episode_manifest.json` | `create projects/<project>/<episode>/episode_manifest.json first` | 2 | 404 |
| `NOTHING_PENDING` | No review items are pending. | 队列为空；batch 没有任何待审项 | `nothing to do; inspect the queue with om review list` | 3 | 409 |
| `BATCH_PRECONDITION_FAILED` | Batch preconditions are not satisfied (REV-07). | 批次含其他剧集项；整片 watch 时镜头未组装 | `run the batch for episode '<id>'` / `finish or explicitly skip the remaining shots, then re-run` | 2 | 422 |
| `UNSUPPORTED_LIBRARY_KIND` | Library kind is not supported. | 资产库 kind 非法 | `use one of: character, keyframe, music, reference, shot, voice` | 2 | 400 |
| `LIBRARY_ASSET_NOT_FOUND` | Library asset does not exist. | 资产 id 不存在或 kind 不匹配 | `check asset_id or re-run the library query` | 2 | 404 |
| `MEDIA_NOT_FOUND` | Media file does not exist. | 剧集无媒体根/媒体文件；watch 无成片 | `assemble the episode first, then re-run om review watch --verify` | 2 | 404 |
| `MEDIA_PATH_FORBIDDEN` | Media path escapes its allowed root. | 绝对路径、`..`、非法路径段、跨根解析 | `pass a relative path inside the episode/library root` | 2 | 403 |
| `BUDGET_EXCEEDED` | Budget gate blocked the operation. | 预算门阻断（M0 目录预留，M1 生效） | 先估算成本并取得审批，再重试（NFR-01） | 5 | —（M0 未抛出） |
| `INTERNAL_ERROR` | Unexpected server error. | 未预期异常；manifest 不可读或 schema 违规 | `retry once; if it persists, ask a human to inspect the log` | 1 | 500 |

> 说明：`BUDGET_EXCEEDED` 与 `MEDIA_PATH_FORBIDDEN` 等码属于**冻结目录**的一部分。
> M0 的 `om` 路径会触发 `MEDIA_PATH_FORBIDDEN`（媒体 API）；`BUDGET_EXCEEDED` 目前只
> 由退出码映射预留，真实预算门在 M1 接入。

---

## 3. 决策相关错误码详解

这些码由 `classify_review_rejection(decision, item)` 从
`lib.review.ReviewStore.decide` 的拒绝结果映射而来，CLI 与 HTTP 共用（X-11）。

### 3.1 `INVALID_ACTION`

```json
{
  "code": "INVALID_ACTION",
  "what": "action 'approve' is not a valid review action",
  "fix": "use one of: accept, edit_prompt, pick_variant, reject, send_back, skip",
  "recovery": "fix-then-retry",
  "side_effects": "none",
  "context": { "review_id": "r1", "stage": "shot_gen" }
}
```

### 3.2 `MISSING_REASON_CODE`

`reject` 必须带原因码。CLI 用 `--reason CODE`，API 用 `reason_code`。

### 3.3 `UNKNOWN_REASON_CODE`

原因码不在 `lib/review.py` 的 `REASON_CODES` 集合中。当前规范集合见
[第 5 节](#5-原因码与阶段适用范围)。

### 3.4 `REASON_CODE_NOT_APPLICABLE`

原因码合法，但不适用于该阶段（REV-03）。例如在 `shot_gen` 使用 `PACING`。
`fix` 会列出该阶段允许的集合。

### 3.5 `INVALID_CANDIDATE`

`pick_variant` 必须从 `item.candidates[]` 中选一个；空候选或不存在都拒绝。

### 3.6 `OVERRIDE_EVIDENCE_REQUIRED`

`override=true` 用于人工解决 `UNKNOWN`；必须同时记录证据（谁、何时、看到什么）。
`UNKNOWN` 永远不会被静默当作 `PASS`。

### 3.7 `STALE_REVIEW`

决策指向的 `candidate_version` 与当前项不一致。处理方式：重新读取当前项后决定；
或对旧决策执行 `om review undo`（REV-13）。退出码 `4`，HTTP `409`。

---

## 4. 其他错误码详解

### 4.1 `REVIEW_NOT_FOUND` / `EPISODE_NOT_FOUND`

- `REVIEW_NOT_FOUND`：`review_id` 在磁盘上的 `reviews.jsonl` 中不存在（HTTP 404）。
- `EPISODE_NOT_FOUND`：没有任何 `episode_manifest.json` 的 `episode_id` 匹配
  （`recovery: do-X-first`）。

### 4.2 `NOTHING_PENDING`

`om review list` 的空队列或 `om review batch` 没有任何待办项。退出码 `3`，不是失败。
注意：`review list` 在空队列时直接返回 `3`，不写错误信封；batch 会把
`NOTHING_PENDING` 放进 `errors[]`。

### 4.3 `BATCH_PRECONDITION_FAILED`

| 场景 | `what` | `recovery` |
|---|---|---|
| batch 包含其他剧集的项 | `review_id 'x' belongs to episode 'y'` | `fix-then-retry` |
| 整片 watch 时有镜头未完成 | `cannot full-watch stage 'assembly': shots are not assembled` | `do-X-first` |

### 4.4 `UNSUPPORTED_LIBRARY_KIND` / `LIBRARY_ASSET_NOT_FOUND`

资产库只接受 `shot`、`keyframe`、`character`、`voice`、`music`、`reference`。
资产存在但 kind 不匹配时同样按 `LIBRARY_ASSET_NOT_FOUND`（HTTP 404）拒绝。

### 4.5 `MEDIA_NOT_FOUND` / `MEDIA_PATH_FORBIDDEN`

- `MEDIA_NOT_FOUND`：媒体文件或剧集媒体根不存在；watch 无成片时
  `recovery: do-X-first`。
- `MEDIA_PATH_FORBIDDEN`：路径逃逸（绝对路径、盘符、`..`、空段、非法路径段），
  HTTP 403；不回显服务器绝对路径。

### 4.6 `INTERNAL_ERROR`

仅用于未预期失败：manifest 不可读/违反 schema、review store 未知异常、CLI 兜底
`except Exception`。特征是 `recovery: ask-human`、`side_effects: unknown`，
并且信封带 `trace`。

---

## 5. 原因码与阶段适用范围

`reject` 的 `reason_code` 必须同时满足：在规范集合内，且属于该 stage 的允许集合
（REV-03）。未列出的 stage 使用全量集合。

### 5.1 规范原因码集合（`REASON_CODES`）

```
FACE_MISMATCH        WARDROBE_DRIFT       OFF_POSE             LIP_SYNC_FAIL
MOTION_WRONG         ARTIFACT             FRAMING_BAD          LIGHTING_WRONG
PACING               DIALOGUE_CUT         DIALECT_OFF          MOTION_UNNATURAL
CLIPPING             SUBTITLE_MISALIGN    HOOK_WEAK            NARRATIVE_WEAK
IDENTITY_UNKNOWN     OTHER
```

### 5.2 stage → 允许原因码

| stage | 允许原因码 |
|---|---|
| `brief` | `HOOK_WEAK`, `NARRATIVE_WEAK`, `OTHER` |
| `beat_map` | `PACING`, `HOOK_WEAK`, `NARRATIVE_WEAK`, `OTHER` |
| `script` | `PACING`, `DIALOGUE_CUT`, `DIALECT_OFF`, `NARRATIVE_WEAK`, `HOOK_WEAK`, `OTHER` |
| `character_cast` | `FACE_MISMATCH`, `WARDROBE_DRIFT`, `IDENTITY_UNKNOWN`, `OTHER` |
| `scene_plan` | `FRAMING_BAD`, `PACING`, `NARRATIVE_WEAK`, `OTHER` |
| `keyframe` | `FACE_MISMATCH`, `WARDROBE_DRIFT`, `OFF_POSE`, `FRAMING_BAD`, `LIGHTING_WRONG`, `IDENTITY_UNKNOWN`, `ARTIFACT`, `OTHER` |
| `shot_gen` | `FACE_MISMATCH`, `WARDROBE_DRIFT`, `OFF_POSE`, `LIP_SYNC_FAIL`, `MOTION_WRONG`, `MOTION_UNNATURAL`, `ARTIFACT`, `FRAMING_BAD`, `LIGHTING_WRONG`, `DIALOGUE_CUT`, `IDENTITY_UNKNOWN`, `OTHER` |
| `assembly` | `PACING`, `DIALOGUE_CUT`, `LIP_SYNC_FAIL`, `SUBTITLE_MISALIGN`, `CLIPPING`, `MOTION_UNNATURAL`, `NARRATIVE_WEAK`, `OTHER` |
| `delivery` | `SUBTITLE_MISALIGN`, `CLIPPING`, `FRAMING_BAD`, `ARTIFACT`, `OTHER` |

---

## 6. `UNKNOWN` 与人工覆盖

- `face_similarity: null` 或质检无法判定的项表示 `UNKNOWN`，**不是通过**。
- 人工确认后可提交：

```powershell
om review decide <review_id> --action accept --override `
  --evidence "owner 2026-11-12 12:00 目视确认面部一致" --yes
```

- 缺少 `--evidence` 时返回 `OVERRIDE_EVIDENCE_REQUIRED`，不会腐蚀机器状态。

---

## 7. 稳定性和版本策略

- `code` 字符串是 API 契约的一部分；新增码是 additive，已有码不改名、不删除。
- `/api/v1` 内只做 additive 变更（X-16）；破坏性变更需要 major 版本。
- CLI 退出码同样稳定：新增错误码默认落到 `2`，除非显式加入 `_EXIT_CODES`。