# 双 agent 运行器（v2.5 DUAL-03/05）

`lib/dual_agent.py` 是**工具层**：调用模型、校验 critic 的 JSON 契约；不决定阶段是否通过。
producer/critic 的指令模板在技能层
`skills/pipelines/short-drama/dual-agent-prompts.yaml`。

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `ANTHROPIC_BASE_URL` | 是 | 网关地址，必须以 `https://` 开头（明文 http 会被拒绝） |
| `ANTHROPIC_AUTH_TOKEN` | 二选一 | 网关 token，以 `Authorization: Bearer` 发送 |
| `ANTHROPIC_API_KEY` | 二选一 | Anthropic 风格 API key，以 `x-api-key` 发送 |
| `DUAL_AGENT_PROMPTS_PATH` | 否 | 覆盖提示模板文件路径（默认技能层文件） |

两者都设置时 token 优先；都没有则调用前直接报错。

## 契约

critic 只返回 JSON：

```json
{"verdict": "PASS", "findings": [{"title": "...", "text": "...", "severity": "blocking"}]}
```

- REJECT 必须至少一条 `blocking`；PASS 不得含 `blocking`，否则按非法输出处理。
- 非法 JSON 重试一次（追加更严格的提示），仍失败则抛 `DualAgentError`（fail-closed）。
- 响应 `stop_reason` 为 `max_tokens`/`length` 时抛错，截断输出不会被当作完整作品。
- 输出契约与 API 见 [shortdrama-agent-artifacts.md](shortdrama-agent-artifacts.md)。