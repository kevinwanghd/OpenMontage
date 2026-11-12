"""Contract tests for the Bailian free-tier text tool.

These prove the routing, payload, empty-content handling and no-key behaviour
without touching the network. Live quota consumption is verified separately by
the smoke script in the task notes, not here.
"""

from __future__ import annotations

import json

import pytest

from tools.base_tool import BaseTool, ToolStatus
from tools.text.bailian_chat import (
    ENDPOINT,
    MIN_MAX_TOKENS,
    TASK_ROUTES,
    BailianChat,
)


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def _ok_payload(text: str = "收到", finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _FakeResponse(_ok_payload())

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    return calls


def test_is_a_base_tool_with_identity():
    tool = BailianChat()
    assert isinstance(tool, BaseTool)
    assert tool.name == "bailian_chat"
    assert tool.capability == "text_llm"
    assert tool.provider == "bailian"


def test_route_table_covers_every_declared_task():
    assert set(TASK_ROUTES) == set(BailianChat.capabilities)


def test_reasoning_floor_is_applied_to_small_max_tokens():
    tool = BailianChat()
    model, max_tokens = tool.resolve_route("translate", 16)
    assert model == "qwen-mt-lite"
    assert max_tokens == MIN_MAX_TOKENS


def test_status_is_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert BailianChat().get_status() == ToolStatus.UNAVAILABLE


def test_missing_key_fails_without_network(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("network attempted without an API key")

    monkeypatch.setattr("requests.post", boom)
    result = BailianChat().execute({"task": "translate", "prompt": "hello"})
    assert result.success is False
    assert "DASHSCOPE_API_KEY" in result.error


def test_translation_uses_routed_model_and_endpoint(captured):
    result = BailianChat().execute(
        {"task": "translate", "prompt": "Hello", "target_lang": "Chinese"}
    )
    assert result.success is True, result.error
    call = captured[0]
    assert call["url"] == ENDPOINT
    assert call["json"]["model"] == "qwen-mt-lite"
    assert call["json"]["messages"][-1]["content"] == "Hello"
    assert call["json"]["translation_options"] == {"target_lang": "Chinese"}
    assert call["headers"]["Authorization"] == "Bearer sk-test"


def test_translation_without_target_lang_fails_before_network(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")

    def boom(*args, **kwargs):
        raise AssertionError("qwen-mt echo request should never be sent")

    monkeypatch.setattr("requests.post", boom)
    result = BailianChat().execute({"task": "translate", "prompt": "Hello"})
    assert result.success is False
    assert "target_lang" in result.error


def test_usage_is_surfaced_for_quota_accounting(captured):
    result = BailianChat().execute({"task": "classify", "prompt": "这是哪一类？"})
    assert result.data["usage"]["total_tokens"] == 12
    assert result.cost_usd == 0.0


def test_vision_puts_images_in_content_parts(captured):
    BailianChat().execute(
        {
            "task": "vision",
            "prompt": "描述这张图",
            "image_urls": ["https://example.com/a.png"],
        }
    )
    parts = captured[0]["json"]["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "描述这张图"}
    assert parts[1]["type"] == "image_url"


def test_empty_content_with_length_is_a_loud_failure(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: _FakeResponse(_ok_payload("", finish_reason="length")),
    )
    result = BailianChat().execute({"task": "summarize", "prompt": "x"})
    assert result.success is False
    assert "empty content" in result.error
    assert "max_tokens" in result.error


def test_http_error_is_reported(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: _FakeResponse({}, status_code=401)
    )
    result = BailianChat().execute({"task": "extract", "prompt": "x"})
    assert result.success is False
    assert "401" in result.error


def test_idempotency_key_is_stable_for_same_inputs():
    tool = BailianChat()
    inputs = {"task": "translate", "prompt": "你好", "system": None, "image_urls": None,
              "model": None}
    assert tool.idempotency_key(inputs) == tool.idempotency_key(dict(inputs))
    assert tool.idempotency_key(inputs) != tool.idempotency_key({**inputs, "prompt": "再见"})