"""Contract tests for the Holopix video provider tool.

These tests verify the tool satisfies the BaseTool contract without
requiring a Holopix account or making any API calls.

The contract under test is the modern one: the app submits Seedance runs over
a websocket (``wss://holopix.cn/pino_service_api/websocket/<clientId>``) and
streams status frames back on the same socket.

Run: pytest tests/contracts/test_holopix_video.py -v
"""

import base64
import json
from pathlib import Path

import pytest

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)
from tools.video.holopix_video import HolopixVideo


class TestContract:

    def test_inherits_base_tool(self):
        assert issubclass(HolopixVideo, BaseTool)

    def test_has_required_identity(self):
        tool = HolopixVideo()
        assert tool.name == "holopix_video"
        assert tool.version
        assert tool.provider == "holopix"
        assert tool.capability == "video_generation"
        assert tool.tier == ToolTier.GENERATE
        assert tool.stability == ToolStability.BETA
        assert tool.runtime == ToolRuntime.API

    def test_execution_mode_is_async(self):
        assert HolopixVideo().execution_mode == ExecutionMode.ASYNC

    def test_determinism_is_stochastic(self):
        assert HolopixVideo().determinism == Determinism.STOCHASTIC

    def test_has_input_schema(self):
        schema = HolopixVideo().input_schema
        assert schema.get("type") == "object"
        props = schema.get("properties", {})
        assert "prompt" in props
        assert "duration" in props
        assert "aspect_ratio" in props

    def test_accepts_a_client_id_instead_of_a_prompt(self):
        schema = HolopixVideo().input_schema
        branches = schema.get("anyOf", [])
        assert {"required": ["prompt"]} in branches
        assert {"required": ["client_id"]} in branches

    def test_has_capabilities(self):
        tool = HolopixVideo()
        assert "text_to_video" in tool.capabilities
        assert "image_to_video" in tool.capabilities

    def test_has_agent_skills(self):
        assert "holopix-video" in HolopixVideo().agent_skills

    def test_has_fallbacks(self):
        assert "molica_video" in HolopixVideo().fallback_tools

    def test_has_install_instructions(self):
        assert "HOLOPIX_TOKEN" in HolopixVideo().install_instructions

    def test_get_info_returns_dict(self):
        info = HolopixVideo().get_info()
        assert isinstance(info, dict)
        assert info["name"] == "holopix_video"
        assert info["provider"] == "holopix"

    def test_status_unavailable_without_token(self, monkeypatch):
        monkeypatch.delenv("HOLOPIX_TOKEN", raising=False)
        assert HolopixVideo().get_status() == ToolStatus.UNAVAILABLE

    def test_status_available_with_token(self, monkeypatch):
        monkeypatch.setenv("HOLOPIX_TOKEN", "fake-token")
        assert HolopixVideo().get_status() == ToolStatus.AVAILABLE

    def test_has_resource_profile(self):
        rp = HolopixVideo().resource_profile
        assert isinstance(rp, ResourceProfile)
        assert rp.network_required is True

    def test_has_retry_policy(self):
        assert isinstance(HolopixVideo().retry_policy, RetryPolicy)

    def test_has_side_effects(self):
        side = HolopixVideo().side_effects
        assert len(side) > 0
        assert any("compute" in s.lower() for s in side)

    def test_has_user_visible_verification(self):
        assert len(HolopixVideo().user_visible_verification) > 0

    def test_estimate_cost_is_zero_for_membership_quota(self, monkeypatch):
        monkeypatch.delenv("HOLOPIX_COST_USD_PER_VIDEO", raising=False)
        assert HolopixVideo().estimate_cost({"prompt": "x"}) == 0.0


class TestMessageBuilder:

    def _message(self, **overrides):
        payload = {
            "prompt": "a lighthouse in a storm",
            "user_id": 42,
            "artwork_id": 7,
            "duration": 5,
            "resolution": "720p",
            "ratio": "16:9",
            "image_url": "",
            "camera_fixed": False,
            "model": "seedance-2-0",
        }
        payload.update(overrides)
        return HolopixVideo.build_message(None, **payload)

    def test_text_to_video_uses_the_text_module(self):
        assert self._message()["module"] == 78

    def test_image_to_video_uses_the_image_module(self):
        message = self._message(operation="image_to_video", image_url="https://x/a.png")
        assert message["module"] == 77
        assert message["videoMode"] == "image-to-video"
        assert message["imageList"] == ["https://x/a.png"]
        assert message["originImgUrl"] == "https://x/a.png"
        assert message["videoRatio"] == "adaptive"

    def test_first_and_last_frame_uses_the_start_end_module(self):
        message = self._message(
            operation="first_last_frame_to_video",
            image_url="https://x/a.png",
            last_image_url="https://x/b.png",
        )
        assert message["module"] == 75
        assert message["videoMode"] == "start-end"
        assert message["lastImageUrl"] == "https://x/b.png"

    def test_carries_model_and_mode(self):
        message = self._message()
        assert message["videoModel"] == "seedance-2-0"
        assert message["videoMode"] == "text-to-video"

    def test_generates_a_unique_client_id(self):
        first = self._message()["apiClientId"]
        second = self._message()["apiClientId"]
        assert first != second
        # The web app builds it as "<user id>-<epoch millis><random digits>".
        assert first.startswith("42-")
        assert first.split("-", 1)[1].isdigit()

    def test_refuses_an_unsupported_resolution(self):
        with pytest.raises(ValueError):
            self._message(resolution="768p")

    def test_spells_a_bare_resolution_the_way_the_ui_does(self):
        assert self._message(resolution="720")["videoResolution"] == "720p"

    def test_passes_duration_and_ratio(self):
        message = self._message(duration=10, ratio="9:16")
        assert message["videoDuration"] == 10
        assert message["videoRatio"] == "9:16"

    def test_frame_carries_no_data_wrapper(self):
        # The websocket frame is the object itself; the HTTP endpoints wrap
        # theirs in {"data": ...}.
        assert "data" not in self._message()


class TestModuleMap:

    def test_knows_the_seedance_modules(self):
        from tools._holopix.client import seedance_module_id

        assert seedance_module_id("seedance-2-0", "text-to-video") == 78
        assert seedance_module_id("seedance-2-0", "image-to-video") == 77
        assert seedance_module_id("seedance-2-0", "start-end") == 75
        assert seedance_module_id("seedance-2-0", "reference") == 76
        assert seedance_module_id("seedance-mini", "text-to-video") == 82

    def test_refuses_an_unknown_model(self):
        from tools._holopix.client import seedance_module_id

        with pytest.raises(ValueError):
            seedance_module_id("sora-2", "text-to-video")

    def test_refuses_an_unknown_mode(self):
        from tools._holopix.client import seedance_module_id

        with pytest.raises(ValueError):
            seedance_module_id("seedance-2-0", "text-to-music")


class TestClientCrypto:

    def test_encrypt_decrypt_roundtrip(self):
        from tools._holopix.client import _decrypt, _encrypt

        payload = {"module": 78, "prompt": "灯塔"}
        blob = _encrypt(payload)
        assert base64.b64decode(blob)
        assert _decrypt(blob) == payload

    def test_websocket_frame_is_the_object_itself(self):
        from tools._holopix.client import _decrypt, _encrypt

        payload = {"module": 78, "apiClientId": "28-123", "prompt": "x"}
        decoded = _decrypt(_encrypt(payload))
        assert decoded == payload
        assert "data" not in decoded


class TestSelectorIntegration:

    def test_registry_discovers_the_tool(self, isolated_tool_registry):
        isolated_tool_registry.discover()
        assert isolated_tool_registry.get("holopix_video") is not None


class TestLayer3Skill:

    def test_skill_file_exists_and_names_the_credential(self):
        from tests.contracts.test_atlas_tools import PROJECT_ROOT

        skill = PROJECT_ROOT / ".agents" / "skills" / "holopix-video" / "SKILL.md"
        assert skill.exists()
        text = skill.read_text(encoding="utf-8")
        assert "HOLOPIX_TOKEN" in text
        assert "holopix_video" in text

    def test_tool_advertises_the_skill(self):
        assert "holopix-video" in HolopixVideo().agent_skills


class TestVideoFlow:
    """The two-step contract: an artwork first, then the generation."""

    def _client(self):
        from tools._holopix.client import HolopixClient

        return HolopixClient(token="fake-token")

    def test_create_artwork_reads_an_integer_id(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            client, "post", lambda *args, **kwargs: {"ret": 0, "data": 2349362}
        )
        assert client.create_artwork("demo", module=78, model_list=[]) == 2349362

    def test_seedance_param_carries_the_app_fields(self):
        from tools._holopix.client import seedance_param

        param = seedance_param(
            prompt="a lighthouse",
            user_id=28,
            unique_id="u1",
            artwork_id=42,
            module=78,
            quality="720p",
            ratio="16:9",
            duration=5,
        )
        assert param["module"] == 78
        assert param["artworkId"] == 42
        assert param["userId"] == 28
        assert param["uniqueId"] == "u1"
        assert param["prompt"] == "a lighthouse"
        assert param["videoResolution"] == "720p"
        assert param["videoDuration"] == 5
        assert param["videoRatio"] == "16:9"
        assert param["calcRatio"] == "16:9"
        assert param["source"] == 1
        assert param["vipChannel"] is False
        assert param["imageList"] == []
        assert param["ctime"] > 0
        # The web app builds the client id as "<user id>-<millis><digits>".
        assert param["apiClientId"].startswith("28-")
        assert param["apiClientId"].split("-", 1)[1].isdigit()


class TestStatusMapping:

    def test_finished_frame_carries_the_clip_url(self):
        frame = {
            "status": 3,
            "images": [{"image": "https://genai.holopix.cn/2026-11-08/x.mp4", "size": 1.5}],
        }
        assert HolopixVideo._video_url_of(frame) == "https://genai.holopix.cn/2026-11-08/x.mp4"

    def test_a_frame_without_images_has_no_url(self):
        assert HolopixVideo._video_url_of({"status": 3}) == ""

    def test_busy_status_raises_a_retryable_error(self):
        from tools._holopix import client as client_module

        error = client_module.HolopixClient._ws_error(-200, {})
        assert isinstance(error, client_module.HolopixBusyError)

    def test_expired_session_raises_an_auth_error(self):
        from tools._holopix import client as client_module

        error = client_module.HolopixClient._ws_error(-3, {})
        assert isinstance(error, client_module.HolopixAuthError)

    def test_empty_balance_raises_an_upstream_error(self):
        from tools._holopix import client as client_module

        error = client_module.HolopixClient._ws_error(-100, {})
        assert isinstance(error, client_module.HolopixUpstreamError)


class TestAccountQueue:
    """The account may only have one generation in flight at a time."""

    def test_submit_waits_out_the_busy_account(self, monkeypatch):
        from tools._holopix import client as client_module

        tool = HolopixVideo()
        client = client_module.HolopixClient(token="t0ken")
        calls = {"n": 0}

        def flaky_submit(self, message, *, timeout=0):
            calls["n"] += 1
            if calls["n"] < 3:
                raise client_module.HolopixBusyError("busy")
            return {"status": 3, "images": [{"image": "https://x/a.mp4"}]}

        monkeypatch.setattr(client_module.HolopixClient, "submit_video_ws_sync", flaky_submit)
        monkeypatch.setattr("tools.video.holopix_video.time.sleep", lambda _s: None)
        frame = tool._submit_when_free(client, {"apiClientId": "c1"}, timeout=60)
        assert calls["n"] == 3
        assert frame["status"] == 3

    def test_submit_gives_up_when_the_account_stays_busy(self, monkeypatch):
        from tools._holopix import client as client_module

        tool = HolopixVideo()
        client = client_module.HolopixClient(token="t0ken")

        def always_busy(self, message, *, timeout=0):
            raise client_module.HolopixBusyError("busy")

        monkeypatch.setattr(client_module.HolopixClient, "submit_video_ws_sync", always_busy)
        monkeypatch.setattr("tools.video.holopix_video.time.sleep", lambda _s: None)
        with pytest.raises(client_module.HolopixBusyError):
            tool._submit_when_free(client, {"apiClientId": "c1"}, timeout=0)


class TestUpstreamRefusal:
    """The upstream model can refuse a run; the credits come back."""

    def test_refusal_is_reported_as_refunded_and_not_resumable(self, monkeypatch):
        from tools._holopix import client as client_module

        tool = HolopixVideo()

        def refuse(self, message, *, timeout=0):
            raise client_module.HolopixUpstreamError("Holopix rejected the run (-100)")

        monkeypatch.setenv("HOLOPIX_TOKEN", "t0ken")
        monkeypatch.setattr(client_module.HolopixClient, "submit_video_ws_sync", refuse)
        monkeypatch.setattr(
            client_module.HolopixClient, "user_info", lambda self: {"id": 28, "uniqueId": "u"}
        )
        monkeypatch.setattr(
            client_module.HolopixClient, "create_artwork", lambda self, **kwargs: 7
        )
        result = tool.execute({"prompt": "a lighthouse in a storm"})
        assert result.success is False
        assert result.data["refunded"] is True
        assert result.data["resumable"] is False
        assert "refunded" in result.error


class TestCollect:
    """A run that is already paid for must be collectable without paying again."""

    def test_collect_reads_the_version_list(self, monkeypatch, tmp_path):
        from tools._holopix import client as client_module

        video_url = "https://genai.holopix.cn/2026-11-07/clip.mp4"
        client = client_module.HolopixClient(token="t0ken")
        seen = {}
        monkeypatch.setenv("HOLOPIX_TOKEN", "t0ken")

        def fake_versions(self, artwork_id, page=1, page_size=5):
            seen["artwork_id"] = artwork_id
            return [{"clientId": "28-123", "status": 3, "videoUrl": video_url}]

        monkeypatch.setattr(client_module.HolopixClient, "artwork_versions", fake_versions)
        monkeypatch.setattr(
            client_module.HolopixClient,
            "download",
            lambda self, url, dest: Path(dest).write_bytes(b"mp4"),
        )
        monkeypatch.setattr("tools.video._shared.probe_output", lambda path: {"duration": 5.0})

        tool = HolopixVideo()
        output = tmp_path / "collected.mp4"
        result = tool.execute(
            {
                "client_id": "28-123",
                "artwork_id": 42,
                "output_path": str(output),
            }
        )
        assert result.success is True
        assert seen["artwork_id"] == 42
        assert result.data["video_url"] == video_url

    def test_a_timeout_keeps_the_task_collectable(self, monkeypatch, tmp_path):
        from tools._holopix import client as client_module

        client = client_module.HolopixClient(token="t0ken")
        monkeypatch.setattr(client_module.HolopixClient, "artwork_versions", lambda self, *a, **k: [])

        tool = HolopixVideo()
        with pytest.raises(TimeoutError):
            tool._await_video(
                client,
                artwork_id=0,
                client_id="28-123",
                prompt="x",
                poll_interval=0.01,
                timeout=0.05,
            )


class TestArtworkList:
    def test_artwork_list_sends_an_integer_order_condition(self, monkeypatch):
        from tools._holopix.client import HolopixClient

        client = HolopixClient(token="t0ken")
        captured = {}

        def fake_post(path, payload=None, **kwargs):
            captured["path"] = path
            captured["payload"] = payload
            return {"success": True, "data": {"records": [{"id": 7}]}}

        monkeypatch.setattr(client, "post", fake_post)
        assert client.artworks() == [{"id": 7}]
        assert captured["payload"]["orderCondition"] == 1
        assert isinstance(captured["payload"]["orderCondition"], int)
