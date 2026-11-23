# 决策记录

这里记录**用户亲口定下**的长期约定。Agent 动手前先读；定过的事不再重复问。

## 2026-11-15 合并权归 AgentGate

- AgentGate gate 是唯一合并权威：治理检查（mr-validate / risk-scan / secret-scan / test-check）、仓库 CI（`auto_merge.ci_workflows`）、AI 审查（`auto_merge.ai_review`）、人工审批四道门都在这里。
- agent-autoloop 只做外围自动化（早报、巡检、issue 跟进、通知），不再承担合并；其交付规则已 pause。
- critical 路径（`governance.config.yml`、`.github/workflows/**`、`AGENTS.md` 等）由人决定，bot 不自动合并。

## 2026-11-15 AI 审查的边界

- 审查只报会出错的问题（逻辑、安全、数据、契约），不报风格。
- 纯文档改动（`docs/*.md` 与根 README）免 AI 审查，且免审视为满足（`review=pass`），不设「skip 即放行」的旁路。
- 例外：`protected_paths` 里的文档（本文件、`declined.md`、`AGENT_GUIDE.md` 等）虽同样跳过 AI 审查，但必须经过 gate 的人工审批环节，bot 不会自动合并。
- 审查 fail-closed：模型或密钥不可用时不放行，宁可挡住。

## 2026-11-15 草稿与送审

- 草稿 PR 不跑 AI 审查、不等 CI；转正式（ready_for_review）后整轮重跑。
- 何时送审由人或 Agent 判断，不做 push 即开 PR。

## 2026-11-15 no-mistakes 不采用

- 评估过 `no-mistakes`（本地 git 代理门禁：review/test/document 三阶段后放行 push），结论是**不接入流程**。
- 理由三条：本地门可被绕过（push 路径不受控）；职责与 AgentGate 重叠（合并权威已统一到 gate）；成本不划算（一次试跑 45 分钟仍未走完 document 阶段）。
- 二进制保留在本机，随时可再评估；不进 CI、不进推送链路。

## 2026-11-16 公开窗口与安全开关

- 私有仓库 Actions 分钟额度用尽后，OpenMontage 与 UseGEO 临时转为公开仓库（公开库标准 runner 免费），计划两周后调回私有。
- 公开期间开启：Secret scanning、Push protection、CodeQL、Dependabot alerts、私有漏洞上报、fork PR 全量审批；分支保护要求 `agentgate / gate-decision` 必过。
- 上述开关里 Secret scanning、Push protection、CodeQL、分支保护只对公开库免费，**转回私有时会失效**，届时需重新评估是否升级 Pro。

## 2026-11-16 流程改动必须先入 PRD（原则）

- **顺序不可颠倒**：PRD（含全部澄清）→ **用户确认 PRD** → 拆需求卡 → **用户确认卡** → 代码 → 测试验收 → PR。任何流程改动先同步到 PRD；两道用户确认门都不可跳过，**未确认卡不得进入开发**。不允许先写代码再补文档，也不允许把设计只留在对话里。
- **什么算流程改动**（命中任一项即是）：阶段产出物 / 门的判定条件 / 审核点 / 角色分工 / 决策与审计记录格式 / 降级路径。
- **PRD 源头**：`E:\ClaudeCode\01_设计方案\`（短剧平台 = `short-drama-platform-v2.5-director-layer.md`，需求卡 = `short-drama-v2.5-requirement-cards.md`）。仓库内文档是派生物，不是源头。
- **例外**：纯 bug 修复（不改流程语义）可直接修；但若修复暴露了流程缺口，缺口本身仍须回写 PRD 后再拆卡。
- 由来：2026-11-16 讨论"教训进化机制"时用户定下——设计不落 PRD 就会随会话蒸发，下一次又从零争论。

## 2026-11-16 lessons schema 迁移：reason_code 变为可选（CARD-L08）

- **变更**：`lib/lessons.py` 的 `HARD_REQUIRED_FIELDS` 去掉了 `reason_code`；hard 仍强制 `predicate` + `regression_fixture`。
- **原因**：教训的身份是 predicate。`REASON_CODES` 只服务"驳回→怎么重做"的策略表，只有真会出现在审核决策里的失败类才有码。
- **下游影响与迁移**：
  - 读取 lesson 的代码必须把 `reason_code` 当可空字段（`Lesson.reason_code: Optional[str]`，当前实现已如此）。
  - 任何"用 reason_code 索引 `STRATEGIES`"的代码必须先判空；`STRATEGIES` 只覆盖已注册码，缺省不应 KeyError。
  - 已有教训记录不需要改：旧的 `reason_code` 保留生效；新 hard 教训只有在同时登记了审核码时才填。
- **回退**：把 `reason_code` 加回 `HARD_REQUIRED_FIELDS` 即恢复旧约束；既有数据无需迁移。

## 2026-11-16 lesson gate 与「Python 只做工具」边界的裁决

- **冲突**：审查规则要求编排/审核决策留在 Agent 层；PRD v2.6 §4 要求 `_preflight_stage` 求值 hard lesson 并 BLOCKED。
- **裁决（用户确认）**：按 PRD。hard lesson 只有在人工确认（status=confirmed/promoted）后才会进入仓库，代码侧只是执行这条已确认事实，不产生新的策略；与 D19 agent-model、continuity 前置门同构。
- **边界**：阈值/条件必须写在 lesson 记录里（predicate 参数），Python 不得内联新策略；soft lesson 永不阻塞。
- **回退**：删除 `_preflight_stage` 中的 `_lesson_gate` 调用即回到 P0 行为，lesson 数据不受影响。
- 由来：PR #100 的 AI 审查按旧规则判 FAIL，用户裁决保留 driver 门禁并在此记录例外。
## 2026-11-16 lessons schema 迁移：reason_code 变为可选（CARD-L08）

- **变更**：`lib/lessons.py` 的 `HARD_REQUIRED_FIELDS` 去掉了 `reason_code`；hard 仍强制 `predicate` + `regression_fixture`。
- **原因**：教训的身份是 predicate。`REASON_CODES` 只服务"驳回→怎么重做"的策略表，只有真会出现在审核决策里的失败类才有码。
- **下游影响与迁移**：
  - 读取 lesson 的代码必须把 `reason_code` 当可空字段（`Lesson.reason_code: Optional[str]`，当前实现已如此）。
  - 任何"用 reason_code 索引 `STRATEGIES`"的代码必须先判空；`STRATEGIES` 只覆盖已注册码，缺省不应 KeyError。
  - 已有教训记录不需要改：旧的 `reason_code` 保留生效；新 hard 教训只有在同时登记了审核码时才填。
- **回退**：把 `reason_code` 加回 `HARD_REQUIRED_FIELDS` 即恢复旧约束；既有数据无需迁移。

## 2026-11-21 P0 审片阻断与「Python 只做工具」边界的裁决（CARD-G01）

- **冲突**：`ops/review-points.md` 要求审核决策留在 Agent 层；PRD v2.7 族⑱ GATE-01 要求在 `lib/checkpoint.py` 写入时拒绝带 P0 finding 的 `completed`。PR #105 的 AI 审查据此判 FAIL。
- **裁决（用户确认，2026-11-21）**：按 PRD。判定权仍在 Agent/reviewer（`severity: P0|P1` 由审查侧标注），代码侧只拒绝持久化与 P0 矛盾的状态，不产生新策略；与 `human_approved` → GATE VIOLATION 同构，也与 2026-11-16 lesson gate 的裁决同构。
- **边界**：Python 只认 `severity == "P0"` 这一个已标注字段；不得内联质量阈值、维度或判定规则。无 severity 的历史数据一律放行（向后兼容，有测试）。
- **回退**：删除 `write_checkpoint` 中 `_p0_review_findings` 的调用即回到旧行为；无数据迁移。
- 由来：PR #105 ai-review 按旧规则判 FAIL，用户裁决保留该门禁并在此记录例外；处理程序参照 PR #102。

## 2026-11-21 产物边界校验与「Python 只做工具」边界（CARD-G02）

- **冲突**：`ops/review-points.md` 要求审核决策留在 Agent 层；PRD v2.7 GATE-02 要求在 checkpoint 写入时校验 `asset_manifest` 声明的产物。PR #108 的 AI 审查据此判 FAIL。
- **裁决（用户确认，2026-11-22）**：沿用 2026-11-21 GATE-01 的同一裁决。规则全部来自 manifest 与角色卡（provider/model/sha256/voice_id/character_id），Python 只做逐字段比对：文件是否存在、哈希是否一致、`voice_id` 是否等于角色卡登记值。代码不新增策略、不内联阈值、不做质量评价。
- **边界**：只对 `pipeline_type == "short-drama"` 且 `status == "completed"` 的 `keyframe/shot_gen/assembly` 生效；未声明 `provenance_version` 的旧清单只告警。
- **回退**：删除 `write_checkpoint` 中 `_validate_artifacts` 的调用即回到旧行为。
- **确认结论**：本条为用户对 GATE-01 裁决的同构延伸，已于 2026-11-22 经用户追认。理由：Agent 层有 N 个调用入口，且 11-17 事故正是因为绕过 driver 后 QA 整段静默跳过；checkpoint 是阶段推进的唯一必经点，校验放在那里才不可绕过。代价是 Agent 不能再手工放入未登记的产物推进阶段，这是有意的。
- **新增门的准入判据**：答案能从已声明文件（manifest / 角色卡 / review 结论）查出来 → 放在 checkpoint 边界由 Python 执行；答案需要看片、听声或主观判断 → 留在 Agent/reviewer 层。后续门禁按此判据裁决，不再逐条讨论。

## 2026-11-22 G03/G05 接线范围（用户确认）

- **冲突**：CARD-G03 写的是“各 provider 工具提交/轮询处接入”，仓库约 30 个 provider；CARD-G05 写的是“scene_plan 每场声明”，但 schema 字段可选、旧项目没有。
- **裁决（用户确认，2026-11-22）**：
  - G03 只接实际付费且在本项目使用的两个 provider：**PixVerse**（`tools/video/pixverse_video.py`）与 **MiniMax**（`tools/video/minimax_video.py`、`tools/graphics/minimax_image.py`）。其余 provider 未接入，属于已知缺口，不在本轮范围。
  - G05 采取“**新计划必产出，老项目豁免**”：scene-plan 技能要求新 `scene_plan` 的每个 scene 声明 `generation_block_seconds` 与 `edit_beats`；checkpoint 在 `scene_plan` 写 `completed` 时执行 `lib/generation_blocks.validate_scene_plan`，但未声明这两个字段的旧计划只告警、不阻断。
- **边界**：账本以 `output_path` 向上找到最近含 `artifacts/` 的目录作为项目根；无 `output_path` 时不记账（工具仍可用，结果标记 `unscoped`）。超时/断连记为 `unknown`，不自动重提。
- **回退**：删除三个 provider 中的 `ledger_for_artifact/begin_submission` 调用即回到无账本行为；删除 `checkpoint.py` 的 GATE-05 段即回到只告警。

## 2026-11-23 门禁声明化与审片严重度词汇（CARD-D1/D2/D3）

- **冲突**：产物边界门与生成块门写死 `pipeline_type == "short-drama"`；通用 reviewer 用 `critical/suggestion/nitpick`，而 `_p0_review_findings` 只认 `P0`。换流水线做内容时，门禁静默失效。
- **裁决（用户确认 PRD v2.8 + 需求卡，2026-11-23）**：
  - 门是否生效由 manifest 声明：`artifact_gate_stages`（数组）与 `generation_block_gate`（布尔），默认不生效；short-drama 显式声明原行为。
  - 严重度只做词汇映射：`P0`/`critical` 为阻断级，`P1`/`suggestion`/`nitpick` 与非空其它值不阻断，缺失 severity 不阻断。Python 不判断内容轻重。
  - 儿童教育方向（动画角色讲解）首批接入 `animated-explainer` 与 `character-animation` 的 `review_blocking`；两条流水线的产物边界门与生成块门暂不开启。
- **边界**：未知 pipeline 名抛错；清单损坏记 warning 并降级为不触发，与 `_stage_requires_approval` 一致。
- **回退**：删除 manifest 中的三个声明即回到 v2.7 行为；词汇映射回退为只认 `P0` 需同时回退 D3 的两条声明，否则 critical 不再阻断。
