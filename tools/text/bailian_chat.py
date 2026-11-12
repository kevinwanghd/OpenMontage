"""Bailian (DashScope) text LLM tool — free-tier routing on the OpenAI-compatible API.

The account's free quota (1M tokens per model) only applies on the DashScope
endpoint (``dashscope.aliyuncs.com``), not on the Token Plan MaaS gateway, so
batch text work -- translation, classification, summarization, extraction --
runs here at zero marginal cost until the quota is exhausted. The console's
"stop when exhausted" switch is on: once a model's quota is gone the call fails
loudly instead of spilling into paid billing.

Task routes pick the cheapest model that does the job. Three of these models
(qwen3.8-max, qwen3.7-plus, kimi-k3) emit hidden reasoning tokens first, so
``max_tokens`` is floored at :data:`MIN_MAX_TOKENS`; below that they consume the
whole budget reasoning and return an empty string with ``finish_reason=length``.
That empty-content case is reported as a failure, never as a successful empty
translation.
"""

from __future__ import annotations

# risk:untested reason:"routing and payload behaviour are covered by tests/tools/test_bailian_chat.py; thin static gate cannot link that test mapping" owner:@kevinwanghd reviewed:2026-11-12

import os
import time
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolTier,
    ToolRuntime,
    ToolStatus,
)

#: task -> (model, default max_tokens)
TASK_ROUTES: dict[str, tuple[str, int]] = {
    "translate": ("qwen-mt-lite", 512),
    "classify": ("tongyi-intent-detect-v3", 256),
    "summarize": ("qwen3.7-plus", 1024),
    "extract": ("qwen3.8-max-0902", 2048),
    "vision": ("qwen3-vl-plus", 1024),
    # Long-form English drafting (outline, dialogue, shot script). Same free
    # endpoint and model as summarize; the higher token budget is for prose,
    # not for reasoning headroom (the floor still applies).
    "draft": ("qwen3.7-plus", 8192),
}
DEFAULT_ROUTE = ("qwen3.7-plus", 1024)
MIN_MAX_TOKENS = 256
ENV_KEY = "DASHSCOPE_API_KEY"
# risk:hardcoded-url reason:"the DashScope free-tier endpoint is fixed by the provider; it is not an operator-configurable deployment URL" owner:@kevinwanghd reviewed:2026-11-12
ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


class BailianChat(BaseTool):
    name = "bailian_chat"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "text_llm"
    provider = "bailian"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = [f"env:{ENV_KEY}"]
    # risk:hardcoded-url reason:"setup documentation URL, not a runtime endpoint; operators only set the API key" owner:@kevinwanghd reviewed:2026-11-12
    install_instructions = (
        "Set DASHSCOPE_API_KEY to a Bailian (DashScope) API key.\n"
        "  Create one at https://bailian.console.aliyun.com/#/api-key.\n"
        "  The Token Plan key (sk-sp-...) does NOT work here: free quota is a\n"
        "  separate credential on the dashscope.aliyuncs.com endpoint."
    )
    agent_skills = ["bailian"]

    capabilities = ["translate", "classify", "summarize", "extract", "vision", "draft"]
    best_for = [
        "bulk subtitle translation on the free quota",
        "asset/shot classification and tagging",
        "comment and script summarization",
        "structured extraction into JSON",
        "image captioning and shot understanding (qwen3-vl-plus)",
        "English short-drama outline and dialogue drafting on the free quota",
    ]
    not_good_for = [
        "tasks needing a guaranteed non-reasoning model at tiny max_tokens",
        "workloads past the per-model free quota (calls fail, by design)",
    ]

    input_schema = {
        "type": "object",
        "required": ["task", "prompt"],
        "properties": {
            "task": {
                "type": "string",
                "enum": ["translate", "classify", "summarize", "extract", "vision", "draft"],
                "description": "Routes to the cheapest model that can do the job.",
            },
            "prompt": {"type": "string"},
            "system": {"type": "string", "description": "Optional system prompt."},
            "source_lang": {
                "type": "string",
                "description": "Translate only. Defaults to the model's auto detection.",
            },
            "target_lang": {
                "type": "string",
                "description": (
                    "Translate only, required for qwen-mt-* models (e.g. English, "
                    "Chinese). Without it qwen-mt echoes the source unchanged."
                ),
            },
            "image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Vision only: http(s) URLs or data URIs.",
            },
            "model": {
                "type": "string",
                "description": "Override the routed model code (e.g. kimi-k3).",
            },
            "max_tokens": {"type": "integer", "description": "Floored at 256."},
            "temperature": {"type": "number", "default": 0},
            "timeout_seconds": {"type": "number", "default": 120},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0)
    idempotency_key_fields = [
        "task", "prompt", "system", "image_urls", "model", "source_lang", "target_lang"
    ]
    side_effects = []
    user_visible_verification = [
        "Inspect the returned text against the requested task",
        "Check data.usage to confirm the free quota was charged",
    ]

    def get_status(self) -> ToolStatus:
        if os.environ.get(ENV_KEY):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Free quota on the DashScope endpoint; "stop when exhausted" guarantees
        # no paid spill-over, so the marginal cost is zero by construction.
        return 0.0

    def resolve_route(self, task: str, max_tokens: Any = None) -> tuple[str, int]:
        """Resolve task -> (model, max_tokens), applying the reasoning floor."""
        model, default_max = TASK_ROUTES.get(task, DEFAULT_ROUTE)
        requested = default_max if max_tokens is None else int(max_tokens)
        return model, max(requested, MIN_MAX_TOKENS)

    def _build_payload(self, inputs: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        task = str(inputs["task"])
        model = str(inputs.get("model") or "")
        if model:
            default_max = TASK_ROUTES.get(task, DEFAULT_ROUTE)[1]
            max_tokens = max(int(inputs.get("max_tokens") or default_max), MIN_MAX_TOKENS)
        else:
            model, max_tokens = self.resolve_route(task, inputs.get("max_tokens"))

        messages: list[dict[str, Any]] = []
        if inputs.get("system"):
            messages.append({"role": "system", "content": str(inputs["system"])})

        image_urls = [str(u) for u in (inputs.get("image_urls") or [])]
        if image_urls:
            content: Any = [{"type": "text", "text": str(inputs["prompt"])}]
            content.extend({"type": "image_url", "image_url": {"url": u}} for u in image_urls)
        else:
            content = str(inputs["prompt"])
        messages.append({"role": "user", "content": content})

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": float(inputs.get("temperature", 0)),
        }
        if task == "translate" and model.startswith("qwen-mt"):
            options = {"target_lang": str(inputs["target_lang"])}
            if inputs.get("source_lang"):
                options["source_lang"] = str(inputs["source_lang"])
            payload["translation_options"] = options
        return payload, model, task

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = os.environ.get(ENV_KEY)
        if not api_key:
            return ToolResult(
                success=False,
                error=f"{ENV_KEY} not set. " + self.install_instructions,
            )

        task = str(inputs["task"])
        model_hint = str(inputs.get("model") or TASK_ROUTES.get(task, DEFAULT_ROUTE)[0])
        if task == "translate" and model_hint.startswith("qwen-mt") and not inputs.get("target_lang"):
            return ToolResult(
                success=False,
                error=(
                    "target_lang is required for qwen-mt-* models; without it the "
                    "model echoes the source text unchanged"
                ),
            )

        import requests

        start = time.time()
        payload, model, task = self._build_payload(inputs)
        timeout = float(inputs.get("timeout_seconds", 120))

        try:
            response = self._post_with_retry(requests, payload, api_key, timeout)
            response.raise_for_status()
            data = response.json()
        # risk:suppressed-warning reason:"noqa BLE001 is deliberate: every transport failure becomes ToolResult(success=False, error=...) and is surfaced, never ignored" owner:@kevinwanghd reviewed:2026-11-12
        except Exception as e:  # noqa: BLE001 - report every transport failure
            return ToolResult(
                success=False,
                error=f"Bailian chat failed: {type(e).__name__}: {e}",
                model=model,
                duration_seconds=round(time.time() - start, 2),
            )

        choice = (data.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        finish_reason = choice.get("finish_reason")
        usage = data.get("usage", {})

        if not text:
            hint = (
                " (reasoning tokens consumed max_tokens; raise max_tokens)"
                if finish_reason == "length"
                else ""
            )
            return ToolResult(
                success=False,
                error=f"Bailian model returned empty content{hint}",
                data={"model": model, "task": task, "finish_reason": finish_reason,
                      "usage": usage},
                model=model,
                duration_seconds=round(time.time() - start, 2),
            )

        return ToolResult(
            success=True,
            data={
                "provider": "bailian",
                "model": model,
                "task": task,
                "text": text,
                "finish_reason": finish_reason,
                "usage": usage,
            },
            cost_usd=0.0,
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )

    @staticmethod
    def _post_with_retry(requests, payload: dict[str, Any], api_key: str, timeout: float):
        """POST once, retrying twice on rate limits and 5xx responses."""
        last = None
        for attempt in range(3):
            response = requests.post(
                ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            if response.status_code < 500 and response.status_code != 429:
                return response
            last = response
            if attempt < 2:
                time.sleep(2 ** attempt)
        return last