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

## 2026-11-16 审批颗粒度：教训数据免人工审批

- **原则**：人工审批只用于"改变门的行为"的变更，不用于"记录事实"的变更。
- `governance/lessons/**`（教训记录）从 protected 降为 **high**：仍需 test-check + AI 审查 + CI，但不再要求人工审批、可自动合并。
- 仍然 critical（必须人工审批）：`governance.config.yml`、`governance/*.yml|*.yaml|*.md`、`governance/scripts/**`、`AGENT_GUIDE.md`、`docs/decisions.md` 等规则文件。
- 判据：文件是否改变门的判定条件 / 权限 / 路径清单。教训是数据，改它不改变任何门。
- 边界：教训晋升为 hard（绑定可执行检查）时，检查代码本身仍是生产代码，走正常门禁。
- 由来：2026-11-16 用户问"如何减少我的审批"——教训会频繁增长，每条都人工审会把进化机制拖死。
- 后续（需 AgentGate 中央支持，未落地）：`docs/decisions.md` 的**纯追加** diff 可考虑自动合并；当前无机器判据，维持 critical。
