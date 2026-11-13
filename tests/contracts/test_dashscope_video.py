"""T10 contract tests for ``dashscope_video`` (Bailian wan3.0-video).

Guards:
- The tool satisfies the BaseTool contract and declares i2v / first_frame /
  ``output_resolutions`` (including 480p) for capability slots (A2/F34).
- The DashScope request shape is right: t2v carries only a prompt, i2v carries
  ``input.img_url`` as the first frame, and ``first_frame`` is an alias.
- M0 execution is mock-only: the default path never imports/opens a socket,
  never calls requests, never needs a key, and returns a mock URL, not media.
- The paid live path is unreachable without explicit ``DASHSCOPE_VIDEO_LIVE=1``
  plus ``DASHSCOPE_API_KEY`` (zero spend, zero real API calls in M0).
- The provider fragment is valid and aggregated by
  ``lib.capability_slots.load_catalog()`` without editing shared bindings.

Run: pytest tests/contracts/test_dashscope_video.py -q
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

import lib.capability_slots as cs
from tools._dashscope.wan_video_client import (
    RESOLUTION_PRICES_CNY_PER_SECOND,
    LiveWanTransport,
    MockWanTransport,
    build_request_payload,
    normalize_operation,
    normalize_ratio,
    normalize_resolution,
)
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ToolRuntime,
    ToolTier,
)
from tools.video.dashscope_video import DashscopeVideo

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAGMENT = REPO_ROOT / "bindings" / "catalog" / "dashscope.yaml"
SCHEMA = REPO_ROOT / "schemas" / "artifacts" / "capability_catalog.schema.json"


def _boom(*_args, **_kwargs):
    raise AssertionError("network access attempted in M0 mock mode")


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if anything tries to open a connection."""
    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    import requests

    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setattr(requests, "get", _boom)


def _run(tool, **inputs):
    return tool.execute(inputs)


# ---------------------------------------------------------------------------
# Tool contract
# ---------------------------------------------------------------------------


def test_identity_and_contract():
    tool = DashscopeVideo()
    assert isinstance(tool, BaseTool)
    assert tool.name == "dashscope_video"
    assert tool.version
    assert tool.provider == "dashscope"
    assert tool.capability == "video_generation"
    assert tool.tier == ToolTier.GENERATE
    assert tool.runtime == ToolRuntime.API
    assert tool.execution_mode == ExecutionMode.ASYNC
    assert tool.determinism == Determinism.SEEDED
    assert tool.agent_skills == ["dashscope"]
    assert tool.fallback
    assert tool.retry_policy.max_retries >= 0
    assert tool.resource_profile.network_required is True


def test_declares_operations_first_frame_and_resolutions():
    tool = DashscopeVideo()
    assert "text_to_video" in tool.capabilities
    assert "image_to_video" in tool.capabilities
    assert "first_frame_conditioning" in tool.capabilities
    resolutions = tool.supports["output_resolutions"]
    assert "480p" in resolutions
    assert resolutions == ["480p", "720p", "1080p"]
    assert tool.supports["first_frame_conditioning"] is True
    assert tool.supports["native_audio"] is False


def test_input_schema_gates_required_prompt_and_resolution():
    schema = DashscopeVideo.input_schema
    assert schema["required"] == ["prompt"]
    props = schema["properties"]
    assert "first_frame" in props["operation"]["enum"]
    assert "480p" in props["resolution"]["enum"]
    assert props["resolution"]["default"] == "480p"


def test_status_is_available_in_mock_mode_without_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_VIDEO_LIVE", raising=False)
    assert DashscopeVideo().get_status().value == "available"


# ---------------------------------------------------------------------------
# Request mapping
# ---------------------------------------------------------------------------


def test_text_to_video_payload_mapping():
    payload = build_request_payload(
        "a rainy Shanghai alley",
        operation="text_to_video",
        resolution="480p",
        duration_seconds=5,
        negative_prompt="blurry",
        seed=7,
    )
    assert payload["model"] == "wan3.0-video"
    assert payload["input"] == {"prompt": "a rainy Shanghai alley"}
    params = payload["parameters"]
    assert params["resolution"] == "480P"  # DashScope's uppercase spelling
    assert params["duration"] == 5
    assert params["negative_prompt"] == "blurry"
    assert params["seed"] == 7
    assert "img_url" not in payload["input"]


def test_image_to_video_payload_carries_first_frame():
    payload = build_request_payload(
        "she turns to camera",
        operation="image_to_video",
        image_url="https://example.test/frame.png",
        resolution="720p",
        duration_seconds=4,
    )
    assert payload["input"]["img_url"] == "https://example.test/frame.png"
    assert payload["parameters"]["resolution"] == "720P"
    assert payload["parameters"]["duration"] == 4


def test_first_frame_aliases_image_to_video():
    assert normalize_operation("first_frame") == "image_to_video"
    payload = build_request_payload(
        "first frame motion",
        operation="first_frame",
        image_url="data:image/png;base64,AAAA",
    )
    assert payload["input"]["img_url"].startswith("data:image/png")


def test_i2v_without_image_is_rejected():
    with pytest.raises(ValueError, match="requires a reference image"):
        build_request_payload("x", operation="image_to_video")
    result = _run(DashscopeVideo(), prompt="x", operation="image_to_video")
    assert result.success is False
    assert "reference image" in result.error


def test_resolution_normalization():
    assert normalize_resolution("480P") == "480p"
    assert normalize_resolution("1080p") == "1080p"
    with pytest.raises(ValueError):
        normalize_resolution("4k")


# ---------------------------------------------------------------------------
# Mock execution: zero network, zero spend
# ---------------------------------------------------------------------------


def test_mock_t2v_executes_without_network(no_network, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    transport = MockWanTransport()
    result = _run(
        DashscopeVideo(transport=transport),
        prompt="neon street, slow push-in",
        resolution="480p",
        duration_seconds=5,
        poll_interval_seconds=0,
    )
    assert result.success is True
    assert result.data["mode"] == "mock"
    assert result.data["verification_status"] == "MOCKED"
    assert result.data["video_url"].startswith("mock://dashscope/")
    assert result.data["task_status"] == "SUCCEEDED"
    assert result.data["operation"] == "text_to_video"
    assert result.artifacts == []  # a mock URL is not a deliverable
    assert result.cost_usd > 0
    assert result.model == "wan3.0-video"
    assert [c["action"] for c in transport.calls] == ["submit", "fetch"]


def test_mock_i2v_sends_the_image(no_network):
    transport = MockWanTransport()
    result = _run(
        DashscopeVideo(transport=transport),
        prompt="she walks forward",
        operation="image_to_video",
        reference_image_url="https://example.test/keyframe.png",
        resolution="480p",
        poll_interval_seconds=0,
    )
    assert result.success is True
    assert transport.submit_payloads[0]["input"]["img_url"] == (
        "https://example.test/keyframe.png"
    )


def test_mock_first_frame_local_path_becomes_data_uri(no_network, tmp_path):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    transport = MockWanTransport()
    result = _run(
        DashscopeVideo(transport=transport),
        prompt="first frame motion",
        operation="first_frame",
        reference_image_path=str(frame),
        poll_interval_seconds=0,
    )
    assert result.success is True
    assert result.data["operation"] == "image_to_video"
    assert transport.submit_payloads[0]["input"]["img_url"].startswith(
        "data:image/png;base64,"
    )


def test_mock_polls_until_succeeded(no_network):
    transport = MockWanTransport(poll_success_after=3)
    result = _run(
        DashscopeVideo(transport=transport),
        prompt="pending task",
        poll_interval_seconds=0,
    )
    assert result.success is True
    assert result.data["polls"] == 3


def test_provider_failure_is_reported(no_network):
    transport = MockWanTransport(fail=True)
    result = _run(
        DashscopeVideo(transport=transport),
        prompt="rejected request",
        poll_interval_seconds=0,
    )
    assert result.success is False
    assert "FAILED" in result.error


def test_network_error_is_reported(no_network):
    transport = MockWanTransport(network_error=True)
    result = _run(
        DashscopeVideo(transport=transport),
        prompt="network blows up",
        poll_interval_seconds=0,
    )
    assert result.success is False
    assert "failed" in result.error.lower()


def test_poll_timeout_is_reported(no_network):
    transport = MockWanTransport(poll_success_after=100)
    result = _run(
        DashscopeVideo(transport=transport),
        prompt="never finishes",
        poll_interval_seconds=0,
        poll_timeout_seconds=0,
    )
    assert result.success is False
    assert "TIMEOUT" in result.error


def test_invalid_input_is_rejected_before_transport(no_network):
    transport = MockWanTransport()
    result = _run(
        DashscopeVideo(transport=transport),
        prompt="x",
        resolution="4k",
    )
    assert result.success is False
    assert "unsupported resolution" in result.error
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Live path is opt-in only
# ---------------------------------------------------------------------------


def test_live_mode_without_key_refuses_and_never_networks(no_network, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_VIDEO_LIVE", "1")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    tool = DashscopeVideo()
    assert tool.get_status().value == "unavailable"
    result = _run(tool, prompt="paid path", poll_interval_seconds=0)
    assert result.success is False
    assert "DASHSCOPE_API_KEY" in result.error


def test_api_key_alone_does_not_enable_live_mode(no_network, monkeypatch):
    """A key in .env must not turn a normal call into a paid one."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-key-for-testing")
    monkeypatch.delenv("DASHSCOPE_VIDEO_LIVE", raising=False)
    result = _run(DashscopeVideo(), prompt="still mock", poll_interval_seconds=0)
    assert result.success is True
    assert result.data["mode"] == "mock"


def test_live_transport_honors_dashscope_base_url(no_network, monkeypatch):
    """Token-plan subscriptions serve the same async API on a dedicated host.

    DASHSCOPE_BASE_URL lets the live transport point there without editing
    code; otherwise the standard DashScope host would reject the token-plan key.
    """
    monkeypatch.setenv("DASHSCOPE_VIDEO_LIVE", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-key-for-testing")
    monkeypatch.setenv(
        "DASHSCOPE_BASE_URL", "https://token-plan.cn-beijing.maas.aliyuncs.com"
    )
    transport = DashscopeVideo()._resolve_transport()
    assert transport.mode == "live"
    assert transport.base_url == "https://token-plan.cn-beijing.maas.aliyuncs.com"


def test_live_transport_defaults_to_official_dashscope(no_network, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_VIDEO_LIVE", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-key-for-testing")
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    transport = DashscopeVideo()._resolve_transport()
    assert transport.base_url == "https://dashscope.aliyuncs.com"


def test_live_fetch_flattens_nested_task_output(monkeypatch):
    """DashScope nests task_status under ``output``; the wait loop reads it flat.

    Without flattening, a RUNNING task looks like UNKNOWN and is treated as a
    terminal failure even though the provider accepted (and billed) the job.
    """
    import requests as _requests

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "request_id": "req-1",
                "output": {"task_id": "task-1", "task_status": "RUNNING"},
            }

    monkeypatch.setattr(_requests, "get", lambda *a, **k: _Resp())
    transport = LiveWanTransport("fake-key", base_url="https://example.invalid")
    task = transport.fetch("task-1")
    assert task["task_status"] == "RUNNING"
    assert task["task_id"] == "task-1"
    assert task["request_id"] == "req-1"


def test_live_flat_succeeded_task_yields_video_url(no_network, monkeypatch):
    """A flattened SUCCEEDED task must still surface ``video_url``.

    The live transport moved video_url from output.video_url to the top level;
    the success branch must accept both shapes or a billed job looks empty.
    """

    class _FlatTransport:
        mode = "live"

        def submit(self, payload):
            return "task-1"

        def fetch(self, task_id):
            return {
                "task_id": task_id,
                "task_status": "SUCCEEDED",
                "video_url": "https://example.invalid/video.mp4",
            }

    tool = DashscopeVideo()
    monkeypatch.setattr(tool, "_transport", _FlatTransport())
    result = tool.execute(
        {
            "prompt": "flat success",
            "operation": "text_to_video",
            "resolution": "480p",
            "duration_seconds": 5,
            "poll_interval_seconds": 0,
        }
    )
    assert result.success is True
    assert result.data["video_url"] == "https://example.invalid/video.mp4"
    assert result.data["task_status"] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Cost model (Bailian price sheet 2026-11-11)
# ---------------------------------------------------------------------------


def test_cost_scales_with_resolution():
    tool = DashscopeVideo()
    assert tool.estimate_cost_cny({"resolution": "480p", "duration_seconds": 5}) == 1.5
    assert tool.estimate_cost_cny({"resolution": "720p", "duration_seconds": 5}) == 3.0
    assert tool.estimate_cost_cny({"resolution": "1080p", "duration_seconds": 5}) == 6.0
    assert (
        tool.estimate_cost({"resolution": "480p", "duration_seconds": 5})
        < tool.estimate_cost({"resolution": "1080p", "duration_seconds": 5})
    )
    assert RESOLUTION_PRICES_CNY_PER_SECOND["480p"] == 0.30


# ---------------------------------------------------------------------------
# Capability catalog fragment
# ---------------------------------------------------------------------------


def _fragment() -> dict:
    return yaml.safe_load(FRAGMENT.read_text(encoding="utf-8"))


def test_fragment_validates_against_catalog_schema():
    schema = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(_fragment())


def test_fragment_declares_verified_i2v_models():
    entries = _fragment()["entries"]
    by_model = {(entry["model"], entry["operation"]): entry for entry in entries}
    assert ("dashscope/wan3.0-video", "text_to_video") in by_model
    assert ("dashscope/wan3.0-video", "image_to_video") in by_model
    assert ("dashscope/wan2.7-i2v", "image_to_video") in by_model

    for entry in entries:
        assert entry["verification_status"] == "VERIFIED"
        assert entry["commercial_rights"]

    assert "480p" in by_model[("dashscope/wan3.0-video", "image_to_video")]["output_resolutions"]
    assert "720p" in by_model[("dashscope/wan2.7-i2v", "image_to_video")]["output_resolutions"]

    assert by_model[("dashscope/wan3.0-video", "image_to_video")]["first_frame_conditioning"] is True
    assert by_model[("dashscope/wan3.0-video", "image_to_video")]["reference_images_max"] == 1
    assert by_model[("dashscope/wan3.0-video", "text_to_video")]["first_frame_conditioning"] is False
    assert by_model[("dashscope/wan2.7-i2v", "image_to_video")]["first_frame_conditioning"] is True


def test_ratio_is_sent_in_payload():
    assert normalize_ratio("9:16") == "9:16"
    payload = build_request_payload("x", resolution="480p", ratio="9:16")
    assert payload["parameters"]["ratio"] == "9:16"
    assert "size" not in payload["parameters"]


def test_invalid_ratio_is_rejected():
    with pytest.raises(ValueError, match="unsupported ratio"):
        build_request_payload("x", ratio="2:1")
    result = _run(DashscopeVideo(), prompt="x", ratio="2:1")
    assert result.success is False
    assert "ratio" in result.error


class _FlatLiveTransport:
    mode = "live"

    def submit(self, payload):
        return "task-1"

    def fetch(self, task_id):
        return {
            "task_id": task_id,
            "task_status": "SUCCEEDED",
            "video_url": "https://example.invalid/video.mp4",
        }


def test_live_success_downloads_clip_to_output_path(tmp_path, monkeypatch):
    import requests as _requests

    class _Resp:
        content = b"fake-mp4-bytes"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(_requests, "get", lambda *a, **k: _Resp())
    out = tmp_path / "clip.mp4"
    tool = DashscopeVideo()
    monkeypatch.setattr(tool, "_transport", _FlatLiveTransport())
    result = tool.execute({
        "prompt": "x", "operation": "text_to_video", "resolution": "480p",
        "ratio": "9:16", "duration_seconds": 2, "output_path": str(out),
        "poll_interval_seconds": 0,
    })
    assert result.success is True
    assert out.read_bytes() == b"fake-mp4-bytes"
    assert result.artifacts == [str(out)]
    assert result.data["video_path"] == str(out)
    assert result.data["ratio"] == "9:16"


def test_live_download_failure_keeps_task_id_for_reconciliation(tmp_path, monkeypatch):
    import requests as _requests

    def boom(*_args, **_kwargs):
        raise RuntimeError("oss down")

    monkeypatch.setattr(_requests, "get", boom)
    out = tmp_path / "clip.mp4"
    tool = DashscopeVideo()
    monkeypatch.setattr(tool, "_transport", _FlatLiveTransport())
    result = tool.execute({
        "prompt": "x", "operation": "text_to_video", "resolution": "480p",
        "duration_seconds": 2, "output_path": str(out),
        "poll_interval_seconds": 0,
    })
    assert result.success is False
    assert "task-1" in result.error
    assert result.data["video_url"].endswith("video.mp4")
    assert result.cost_usd > 0


def test_capability_slots_loader_aggregates_the_fragment():
    catalog = cs.load_catalog()
    schema = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(catalog)

    t2v = cs.catalog_entry("dashscope/wan3.0-video", "text_to_video")
    i2v = cs.catalog_entry("dashscope/wan3.0-video", "image_to_video")
    assert t2v is not None and i2v is not None
    assert "480p" in i2v["output_resolutions"]
    assert i2v["first_frame_conditioning"] is True
    assert i2v["verification_status"] == "VERIFIED"