# Bailian (DashScope) free-tier text models

Use this skill before calling `bailian_chat`. It covers the credential split,
task routing, and the failure modes that matter for batch text work.

## What this is

Alibaba Bailian's OpenAI-compatible endpoint
(`https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`) gives
**1M free tokens per model**. The free quota exists only on this endpoint.
The Token Plan / MaaS key (`sk-sp-...`) authenticates a different gateway and
gets HTTP 401 here — a missing or wrong key must surface as BLOCKED, never as
silent paid spill-over.

Set `DASHSCOPE_API_KEY` to a key created at
<https://bailian.console.aliyun.com/#/api-key>. Keep the console's
"stop when exhausted" switch on: when a model's quota is gone the call fails
loudly.

## Task routes

| task | model | default max_tokens | use for |
|---|---|---|---|
| `translate` | qwen-mt-lite | 512 | subtitle/localization batches |
| `classify` | tongyi-intent-detect-v3 | 256 | labels, intent, routing |
| `summarize` | qwen3.7-plus | 1024 | condensing notes and scripts |
| `extract` | qwen3.8-max-0902 | 2048 | structured JSON extraction |
| `vision` | qwen3-vl-plus | 1024 | image captioning, shot understanding |
| `draft` | qwen3.7-plus | 4096 | English outline / dialogue / shot-script drafting |

`model` can override the route, but only with a model that is free on this
endpoint; verify the quota before using an override in production.

## Parameters that bite

- **Reasoning floor**: qwen3.8-max, qwen3.7-plus and kimi-k3 emit hidden
  reasoning tokens first. `max_tokens` is floored at 256; below that they spend
  the whole budget thinking and return `finish_reason=length` with empty text.
  Raise `max_tokens` instead of retrying blindly. Measured on 2026-11-12: a
  three-line draft used ~2.4K reasoning tokens, so `draft` defaults to 8192 and
  long-form work should be drafted per section.
- **Translation**: `qwen-mt-*` requires `target_lang`. Without it the model
  echoes the source unchanged — the tool rejects that call explicitly.
- **Vision**: pass `image_urls` as public http(s) URLs or data URIs.
- **Determinism**: `temperature` defaults to 0. Creative `draft` calls should
  set a higher temperature only when variety is wanted, and the exact prompt
  must be recorded in the artifact for reproducibility.

## Drafting English short drama

For `draft` calls, put the creative contract in `system` and the concrete
task in `prompt`:

1. State the genre, target audience, platform, and episode duration.
2. Demand spoken English, one idea per line, and a visible action per beat.
3. Ask for a JSON object when the result feeds an artifact; otherwise ask for
   plain text and parse it in the agent, not in this tool.
4. Long scripts: draft per act/section instead of one giant call. Each call is
   independently recorded, so a failed act does not invalidate the others.

The draft is creative input for the `idea` / `script` / `scene_plan` stages.
It does **not** replace the human review gate: the checkpoint must stay
`awaiting_human` until the operator approves the script.

## Failure modes

- `DASHSCOPE_API_KEY not set` -> tool UNAVAILABLE; `om doctor` shows BLOCKED.
- HTTP 401 with an `sk-sp-` key -> Token Plan key on the wrong endpoint.
- Empty content -> reported as failure with `finish_reason`; raise max_tokens.
- Quota exhausted -> calls fail; do not route to a paid model without explicit
  budget approval (NFR-01).