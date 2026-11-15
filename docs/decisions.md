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
