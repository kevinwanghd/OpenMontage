# OpenMontage 审查要求

完整评审框架见 `docs/PR_REVIEW_GUIDE.md`；本文件只列交付流水线必须拦住的项。

## 审查要点

只报会导致错误、回归、数据/密钥泄露或契约破坏的问题，不报风格。以下每条都要指出具体文件和触发条件。

- 架构边界：Python 只做工具与持久化；编排、创作、审核和 checkpoint 决策必须留在 Agent/技能层。在 Python 里新增编排逻辑属于阻断项。
  - 例外（PRD v2.6 §4，2026-11-16 用户确认）：由**人工确认的 hard lesson** 驱动的 preflight 校验属于"执行已确认事实"，不是新增决策权，允许留在 `_preflight_stage`，与 D19 agent-model、continuity 门同构。判定标准：规则来自 `governance/lessons/repository.yml` 且 status=confirmed/promoted；代码不得内联新的策略或阈值。
  - 例外（PRD v2.7 §族⑱ GATE-01，2026-11-21 用户确认）：**checkpoint 写入时拒绝带 P0 finding 的 `completed`** 属于同一类"执行已确认事实"。判定由 Agent/reviewer 完成（`severity: P0|P1` 由审查侧标注），Python 只拒绝持久化一个与 P0 矛盾的状态，不新增策略、不内联阈值、不做质量判断；与既有 `human_approved` → GATE VIOLATION 同构（`lib/checkpoint.py`）。无 severity 的旧格式一律放行，向后兼容。回退方式：删除 `_p0_review_findings` 的调用即回到旧行为。参照 PR #102 的处理程序。
- 强制流程：不得绕过 preflight、stage checkpoint 或 review 门；跳过 human approval 的改动属于阻断项。
- 决策记录：涉及渲染运行时的选择必须同时评估本地与云两条路径并写入 `decision_log`；只评估一个运行时是 CRITICAL。
- 契约破坏：改变 schema、pipeline stage 产物、默认 provider/回退行为或 setup 默认项时，必须有迁移与文档；静默 fallback 必须改为显式报错或告知用户。
- 安全：不把密钥、token、账号写入代码、示例配置、日志或测试夹具；新增外部请求默认 HTTPS。
- 错误处理：不得静默吞异常；报错要带足够上下文，便于定位。
- 测试：行为变化要有对应测试；删除测试或标记 skip 必须说明理由。
- 范围卫生：无关文件、生成物、截图、大二进制不应混入；lockfile 只在依赖真实变化时更新。
- 可运行性：依赖或入口变化后 `make lint` 与 `make test` 能在干净环境跑通；Windows 路径与命令要兼容。
- 文档：面向用户的选项、默认值或配置键变化必须同步 README 或 docs。