"""Contract tests for the Molica video provider tool.

These tests verify that the tool satisfies the BaseTool contract without
requiring a Molica account or making any API calls.

Run: pytest tests/contracts/test_molica_video.py -v
"""

import pytest
from pathlib import Path

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
from tools.provider_pricing import PriceQuoteRequired
from tools.video.molica_video import KNOWN_MODEL_KEYS, MolicaVideo


class TestContract:

    def test_inherits_base_tool(self):
        assert issubclass(MolicaVideo, BaseTool)

    def test_has_required_identity(self):
        tool = MolicaVideo()
        assert tool.name == "molica_video"
        assert tool.version
        assert tool.provider == "molica"
        assert tool.capability == "video_generation"
        assert tool.tier == ToolTier.GENERATE
        assert tool.stability == ToolStability.BETA
        assert tool.runtime == ToolRuntime.API

    def test_execution_mode_is_async(self):
        assert MolicaVideo().execution_mode == ExecutionMode.ASYNC

    def test_determinism_is_stochastic(self):
        assert MolicaVideo().determinism == Determinism.STOCHASTIC

    def test_has_input_schema(self):
        schema = MolicaVideo().input_schema
        assert schema.get("type") == "object"
        props = schema.get("properties", {})
        # A call either generates from a prompt, or collects a task that was
        # already submitted — the second needs no prompt.
        alternatives = schema.get("anyOf", [])
        required = [field for alt in alternatives for field in alt.get("required", [])]
        assert "prompt" in required
        assert "video_id" in required
        for field in required:
            assert field in props

    def test_has_capabilities(self):
        tool = MolicaVideo()
        assert "text_to_video" in tool.capabilities
        assert "image_to_video" in tool.capabilities
        assert "reference_to_video" in tool.capabilities

    def test_has_agent_skills(self):
        assert "ai-video-gen" in MolicaVideo().agent_skills

    def test_has_fallbacks(self):
        assert "veo_video" in MolicaVideo().fallback_tools

    def test_has_install_instructions(self):
        assert "MOLICA_TOKEN" in MolicaVideo().install_instructions

    def test_get_info_returns_dict(self):
        info = MolicaVideo().get_info()
        assert isinstance(info, dict)
        assert info["name"] == "molica_video"
        assert info["provider"] == "molica"
        assert info["runtime"] == "api"

    def test_model_catalog_exposes_known_keys(self):
        catalog = MolicaVideo().get_info()["model_catalog"]
        for key in KNOWN_MODEL_KEYS:
            assert key in catalog

    def test_status_unavailable_without_token(self, monkeypatch):
        monkeypatch.delenv("MOLICA_TOKEN", raising=False)
        assert MolicaVideo().get_status() == ToolStatus.UNAVAILABLE

    def test_status_available_with_token(self, monkeypatch):
        monkeypatch.setenv("MOLICA_TOKEN", "fake-token")
        assert MolicaVideo().get_status() == ToolStatus.AVAILABLE

    def test_has_resource_profile(self):
        rp = MolicaVideo().resource_profile
        assert rp.network_required is True
        assert rp.vram_mb == 0

    def test_has_retry_policy(self):
        assert MolicaVideo().retry_policy.max_retries >= 0

    def test_has_side_effects(self):
        side = MolicaVideo().side_effects
        assert len(side) > 0
        assert any("credit" in s.lower() for s in side)

    def test_has_user_visible_verification(self):
        assert len(MolicaVideo().user_visible_verification) > 0

    def test_estimate_cost_is_unknown_without_a_credit_price(self, monkeypatch):
        """An unpriced membership call must read as unknown, not as free.

        Reporting 0.0 hands the selector a perfect cost-efficiency score, so a
        prepaid membership would outrank every paid provider on that axis
        alone. "Unknown" keeps the ranking honest.
        """
        monkeypatch.delenv("MOLICA_COST_USD_PER_VIDEO", raising=False)
        monkeypatch.delenv("MOLICA_USD_PER_CREDIT", raising=False)
        with pytest.raises(PriceQuoteRequired):
            MolicaVideo().estimate_cost({"prompt": "x"})

    def test_estimate_cost_converts_credits_into_usd(self, monkeypatch):
        monkeypatch.delenv("MOLICA_COST_USD_PER_VIDEO", raising=False)
        monkeypatch.setenv("MOLICA_USD_PER_CREDIT", "0.01")
        # wan30video's measured cheapest run costs 105 credits.
        assert MolicaVideo().estimate_cost({"prompt": "x", "model": "wan30video"}) == 1.05

    def test_estimate_cost_uses_override(self, monkeypatch):
        monkeypatch.setenv("MOLICA_COST_USD_PER_VIDEO", "0.5")
        assert MolicaVideo().estimate_cost({"prompt": "x"}) == 0.5

    def test_dry_run_reports_membership_quota(self, monkeypatch):
        monkeypatch.delenv("MOLICA_COST_USD_PER_VIDEO", raising=False)
        result = MolicaVideo().dry_run({"prompt": "test"})
        assert isinstance(result, dict)
        assert result["tool"] == "molica_video"
        assert result["cost_status"] == "membership_quota"

    def test_dry_run_reports_estimated_when_priced(self, monkeypatch):
        monkeypatch.setenv("MOLICA_COST_USD_PER_VIDEO", "0.25")
        result = MolicaVideo().dry_run({"prompt": "test"})
        assert result["cost_status"] == "estimated"

    def test_estimate_runtime_scales_with_duration(self):
        short = MolicaVideo().estimate_runtime({"duration": 5})
        long = MolicaVideo().estimate_runtime({"duration": 15})
        assert long > short


class TestIdempotencyKeys:

    def test_includes_all_output_affecting_fields(self):
        fields = MolicaVideo().idempotency_key_fields
        for field in ("prompt", "model", "duration", "resolution", "aspect_ratio"):
            assert field in fields, f"missing idempotency field: {field}"

    def test_excludes_execution_only_fields(self):
        fields = MolicaVideo().idempotency_key_fields
        for field in ("output_path", "poll_interval_seconds", "timeout_seconds"):
            assert field not in fields


class TestBusyModel:

    def test_error_carries_ret_code(self):
        from tools._molica.client import MolicaError

        error = MolicaError("boom", ret=500)
        assert error.ret == 500
        assert str(error) == "boom"

    def test_create_video_reports_a_busy_model(self, monkeypatch):
        from tools._molica import client as client_module

        client = client_module.MolicaClient(token="fake")

        def fail(*args, **kwargs):
            raise client_module.MolicaError("服务繁忙，请稍后再试", ret=500)

        monkeypatch.setattr(client, "post", fail)
        with pytest.raises(client_module.MolicaError) as excinfo:
            client.create_video({"model_group_key": "veo31pro"})
        assert "busy" in str(excinfo.value)
        assert "veo31pro" in str(excinfo.value)


class TestPayloadBuilding:

    def _spec(self):
        return {
            "model_key": "veo31pro",
            "create_mode": {
                "text_to_video": {"enable": True},
                "image_to_video": {
                    "enable": True,
                    "single_image": False,
                    "multiple_image": True,
                    "begin_image": True,
                    "end_image": True,
                },
            },
            "model_params": {
                "base": [
                    {
                        "param_key": "video_scale",
                        "items": [
                            {"param_value": "16:9", "default": True},
                            {"param_value": "9:16", "default": False},
                        ],
                    },
                    {
                        "param_key": "resolution",
                        "items": [{"param_value": "1080P", "default": True}],
                    },
                    {
                        "param_key": "duration",
                        "items": [{"param_value": "8", "default": True}],
                    },
                ]
            },
        }

    def test_text_to_video_payload(self):
        payload = MolicaVideo()._build_payload({"prompt": "a cat"}, self._spec(), "a cat")
        assert payload["model_group_key"] == "veo31pro"
        assert MolicaVideo()._operation({"prompt": "a cat"}) == "text_to_video"
        assert "operation" not in payload, "operation is not part of Molica's request body"
        assert payload["video_scale"] == "16:9"
        assert payload["duration"] == 8
        assert payload["resolution"] == "1080P"

    def test_rejects_unsupported_aspect_ratio(self):
        with pytest.raises(ValueError):
            MolicaVideo()._build_payload(
                {"prompt": "x", "aspect_ratio": "4:3"}, self._spec(), "x"
            )

    def test_reference_images_require_model_support(self):
        spec = self._spec()
        payload = MolicaVideo()._build_payload(
            {"prompt": "x", "reference_image_urls": ["https://example.com/a.png"]},
            spec,
            "x",
        )
        assert MolicaVideo()._operation(
            {"prompt": "x", "reference_image_urls": ["https://example.com/a.png"]}
        ) == "reference_to_video"
        assert payload["video_multiple_image"] == ["https://example.com/a.png"]

    def test_last_frame_needs_first_frame(self):
        with pytest.raises(ValueError):
            MolicaVideo()._build_payload(
                {"prompt": "x", "image_end_url": "https://example.com/b.png"},
                self._spec(),
                "x",
            )


class TestLocalImageUpload:
    """Local image files are uploaded to Molica's object storage first."""

    def test_operation_derives_from_local_paths(self):
        assert MolicaVideo()._operation({"image_path": "a.png"}) == "image_to_video"
        assert (
            MolicaVideo()._operation({"reference_image_paths": ["a.png"]})
            == "reference_to_video"
        )

    def test_resolve_images_uploads_local_files(self):
        class FakeClient:
            def __init__(self):
                self.uploaded = []

            def upload_image(self, path):
                self.uploaded.append(str(path))
                return f"https://static.molica.com.cn/{path.name}"

        client = FakeClient()
        resolved = MolicaVideo()._resolve_images(
            client,
            {
                "image_path": "first.png",
                "image_end_path": "last.png",
                "reference_image_paths": ["ref1.png", "ref2.png"],
                "reference_image_urls": ["https://example.com/keep.png"],
            },
        )
        assert client.uploaded == ["first.png", "last.png", "ref1.png", "ref2.png"]
        assert resolved["image_url"] == "https://static.molica.com.cn/first.png"
        assert resolved["image_end_url"] == "https://static.molica.com.cn/last.png"
        assert resolved["reference_image_urls"] == [
            "https://example.com/keep.png",
            "https://static.molica.com.cn/ref1.png",
            "https://static.molica.com.cn/ref2.png",
        ]

    def test_resolve_images_leaves_urls_untouched(self):
        class FakeClient:
            def upload_image(self, path):
                raise AssertionError("should not upload")

        inputs = {
            "image_url": "https://example.com/a.png",
            "reference_image_urls": ["https://example.com/b.png"],
        }
        assert MolicaVideo()._resolve_images(FakeClient(), inputs) == inputs

    def test_client_upload_image_builds_public_url(self, monkeypatch, tmp_path):
        import tools._molica.client as client_module

        image = tmp_path / "logo.png"
        image.write_bytes(b"fake-png")

        client = client_module.MolicaClient(token="fake")
        monkeypatch.setattr(
            client,
            "post",
            lambda *args, **kwargs: {
                "ret": 1,
                "data": {
                    "type": "ali",
                    "access_key_id": "ak",
                    "access_key_secret": "sk",
                    "security_token": "st",
                    "bucket_name": "molica",
                    "endpoint": "oss-cn-beijing.aliyuncs.com",
                    "object_key_prefix": "prompt/image",
                    "base_url": "https://static.molica.com.cn/prompt/image",
                },
            },
        )

        put = {}

        class FakeBucket:
            def __init__(self, auth, endpoint, bucket):
                put["auth"] = auth
                put["endpoint"] = endpoint
                put["bucket"] = bucket

            def put_object_from_file(self, key, path):
                put["key"] = key
                put["path"] = path

        fake_oss2 = type("FakeOss2", (), {"Bucket": FakeBucket, "StsAuth": lambda *a: a})
        monkeypatch.setitem(__import__("sys").modules, "oss2", fake_oss2)

        url = client.upload_image(image)
        assert url.startswith("https://static.molica.com.cn/prompt/image/logo_")
        assert url.endswith(".png")
        assert put["bucket"] == "molica"
        assert put["key"].startswith("prompt/image/logo_")
        assert put["path"] == str(image)

    def test_client_upload_image_rejects_missing_file(self, tmp_path):
        from tools._molica.client import MolicaClient, MolicaError

        client = MolicaClient(token="fake")
        with pytest.raises(MolicaError):
            client.upload_image(tmp_path / "nope.png")


class TestTaskIdResolution:
    """The create endpoint answers with ret=1 and no id, so the id is recovered."""

    def _client(self):
        from tools._molica.client import MolicaClient

        return MolicaClient(token="fake-token")

    def test_create_video_returns_none_when_envelope_has_no_id(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            client, "post", lambda *args, **kwargs: {"ret": 1, "msg": "success"}
        )
        assert client.create_video({"prompt": "x"}) is None

    def test_create_video_returns_id_when_present(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            client,
            "post",
            lambda *args, **kwargs: {"ret": 1, "data": {"video_id": "abc123"}},
        )
        assert client.create_video({"prompt": "x"}) == "abc123"

    def test_find_video_matches_prompt_and_model(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            client,
            "list_videos",
            lambda page_no=1, page_size=30: {
                "items": [
                    {
                        "video_id": "other",
                        "model_group_key": "wan30video",
                        "req": {"prompt": "a dog"},
                    },
                    {
                        "video_id": "match",
                        "model_group_key": "wan30video",
                        "req": {"prompt": "a cat"},
                    },
                    {
                        "video_id": "wrong-model",
                        "model_group_key": "veo31pro",
                        "req": {"prompt": "a cat"},
                    },
                ],
                "has_next": False,
            },
        )
        assert client.find_video("a cat", "wan30video")["video_id"] == "match"

    def test_tool_resolves_id_when_create_returns_none(self, monkeypatch):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def find_video(self, prompt, model_group_key, params=None):
                self.calls += 1
                if self.calls < 2:
                    return None
                return {"video_id": "resolved-id"}

        client = FakeClient()
        video_id = MolicaVideo()._resolve_video_id(
            client, "a cat", "wan30video", poll_interval=0, timeout=5
        )
        assert video_id == "resolved-id"
        assert client.calls == 2

    def test_resolution_matches_the_requested_parameters(self):
        client = self._client()
        items = [
            {
                "video_id": "old",
                "model_group_key": "wan30video",
                "updated_at": "2026-11-07T09:00:00+08:00",
                "req": {"prompt": "a cat", "duration": 5, "resolution": "480P"},
            },
            {
                "video_id": "new",
                "model_group_key": "wan30video",
                "updated_at": "2026-11-07T11:00:00+08:00",
                "req": {"prompt": "a cat", "duration": 5, "resolution": "720P"},
            },
        ]
        monkey = lambda *a, **k: {"items": items, "has_next": False}
        client.list_videos = monkey
        found = client.find_video(
            "a cat", "wan30video", params={"resolution": "720P", "duration": 5}
        )
        assert found["video_id"] == "new"

    def test_newest_match_wins_when_parameters_are_unknown(self):
        client = self._client()
        items = [
            {
                "video_id": "old",
                "model_group_key": "wan30video",
                "updated_at": "2026-11-07T09:00:00+08:00",
                "req": {"prompt": "a cat"},
            },
            {
                "video_id": "new",
                "model_group_key": "wan30video",
                "updated_at": "2026-11-07T11:00:00+08:00",
                "req": {"prompt": "a cat"},
            },
        ]
        client.list_videos = lambda *a, **k: {"items": items, "has_next": False}
        found = client.find_video("a cat", "wan30video")
        assert found["video_id"] == "new"

    def test_parameter_match_falls_back_when_nothing_matches(self):
        client = self._client()
        items = [
            {
                "video_id": "only",
                "model_group_key": "wan30video",
                "updated_at": "2026-11-07T09:00:00+08:00",
                "req": {"prompt": "a cat", "duration": 5},
            },
        ]
        client.list_videos = lambda *a, **k: {"items": items, "has_next": False}
        found = client.find_video(
            "a cat", "wan30video", params={"resolution": "1080P"}
        )
        assert found["video_id"] == "only"

    def test_tool_raises_when_task_never_appears(self):
        class FakeClient:
            def find_video(self, prompt, model_group_key, params=None):
                return None

        with pytest.raises(TimeoutError):
            MolicaVideo()._resolve_video_id(
                FakeClient(), "a cat", "wan30video", poll_interval=0, timeout=0
            )


# ------------------------------------------------------------------
# OpenMontage integration: selector routing and agent-facing skill
# ------------------------------------------------------------------

class TestCreditPreflight:
    """A run that cannot be paid for must fail before a task is created."""

    def _tool(self):
        from tools.video.molica_video import MolicaVideo

        return MolicaVideo()

    def test_the_standard_queue_is_not_billed_and_not_blocked(self, monkeypatch, tmp_path):
        """Only the fast queue is charged, so only it can be refused.

        Verified against the account ledger: standard-queue runs produced no
        charge, fast-queue runs were billed the learned price every time.
        """
        from tools._molica import client as client_module

        created = []

        class FakeClient:
            token = "fake"

            def video_params(self):
                return [
                    {
                        "model_key": "wan30video",
                        "create_mode": {"text_to_video": {"enable": True}},
                        "model_params": {
                            "base": [
                                {
                                    "param_key": "duration",
                                    "items": [{"param_value": "5", "default": True}],
                                }
                            ]
                        },
                    }
                ]

            def balance(self):
                return 102

            def create_video(self, payload):
                created.append(payload)
                return "task-id"

            def get_video(self, video_id):
                return {"video_url": "https://example.com/clip.mp4"}

            def download(self, url, destination):
                # Mirrors MolicaClient.download: the parent folder is created on demand.
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"video")
                return Path(destination)

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        result = self._tool().execute({
            "prompt": "a cat",
            "model": "wan30video",
            "fast_model": False,
            "output_path": str(tmp_path / "standard_queue_test.mp4"),
        })
        assert result.success, result.error
        assert created, "the standard queue must not be blocked by a balance"
        assert (tmp_path / "standard_queue_test.mp4").exists()
        assert result.data["credits_required"] == 0

    def test_refuses_when_the_balance_is_below_the_cheapest_run(self, monkeypatch):
        from tools._molica import client as client_module

        created = []

        class FakeClient:
            token = "fake"

            def video_params(self):
                return [
                    {
                        "model_key": "wan30video",
                        "create_mode": {"text_to_video": {"enable": True}},
                        "model_params": {
                            "base": [
                                {
                                    "param_key": "duration",
                                    "items": [{"param_value": "5", "default": True}],
                                }
                            ]
                        },
                    }
                ]

            def balance(self):
                return 102

            def create_video(self, payload):
                created.append(payload)
                return "task-id"

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())

        result = self._tool().execute({
            "prompt": "a cat", "model": "wan30video", "fast_model": True
        })
        assert result.success is False
        assert "102" in result.error and "105" in result.error
        assert created == [], "no task may be created when credits are too low"

    def test_credit_cost_table_has_an_override(self, monkeypatch, tmp_path):
        from tools.video import molica_video

        # No learned ledger cache, so the static table is what answers.
        monkeypatch.setattr(molica_video, "_CREDITS_PATH", tmp_path / "absent.json")
        monkeypatch.setattr(molica_video, "_PRICE_CACHE", {"path": None, "mtime": None, "prices": {}})

        monkeypatch.setenv("MOLICA_CREDIT_COST_WAN30VIDEO", "42")
        assert molica_video._credit_cost("wan30video") == 42
        monkeypatch.delenv("MOLICA_CREDIT_COST_WAN30VIDEO")
        assert molica_video._credit_cost("wan30video") == 105
        assert molica_video._credit_cost("unknown-model") is None

    def test_dry_run_reports_the_credit_budget(self, monkeypatch):
        from tools._molica import client as client_module

        class FakeClient:
            token = "fake"

            def balance(self):
                return 102

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        monkeypatch.setenv("MOLICA_TOKEN", "fake-token")

        result = self._tool().dry_run({"prompt": "a cat", "model": "wan30video"})
        assert result["credits_required"] == 0, "the standard queue is not billed"
        assert result["credits_queue"] == "standard"
        assert result["credits_required_fast_queue"] == 105
        assert result["credits_balance"] == 102
        assert result["credits_sufficient"] is True

        fast = self._tool().dry_run({
            "prompt": "a cat", "model": "wan30video", "fast_model": True
        })
        assert fast["credits_required"] == 105
        assert fast["credits_queue"] == "fast"
        assert fast["credits_sufficient"] is False


class TestSelectorIntegration:

    def test_registry_discovers_the_tool(self, isolated_tool_registry):
        isolated_tool_registry.discover()
        assert isolated_tool_registry.get("molica_video") is not None

    def test_video_selector_offers_it_as_a_provider(self, monkeypatch):
        from tools.video.video_selector import VideoSelector

        monkeypatch.setenv("MOLICA_TOKEN", "fake-token")
        providers = VideoSelector()._providers()
        assert "molica_video" in [tool.name for tool in providers]

    def test_an_exact_molica_model_pins_the_candidate(self):
        from tools.video.video_selector import VideoSelector

        unrelated = type("Unrelated", (), {
            "input_schema": {"properties": {"model": {"enum": ["vendor/other"]}}},
            "get_info": lambda self: {},
            "supports": {"text_to_video": True},
            "is_operation_available": lambda self, operation: True,
        })()
        filtered = VideoSelector()._filter_candidates(
            {"model": "molicas2", "operation": "text_to_video"},
            [unrelated, MolicaVideo()],
        )
        assert [tool.name for tool in filtered] == ["molica_video"]

    def test_preferred_tool_routes_to_molica(self):
        from tools.video.video_selector import VideoSelector

        unrelated = type("Unrelated", (), {
            "name": "unrelated_tool",
            "input_schema": {"properties": {"prompt": {}}},
            "get_info": lambda self: {},
            "supports": {"text_to_video": True},
            "is_operation_available": lambda self, operation: True,
        })()
        filtered = VideoSelector()._filter_candidates(
            {"preferred_tool": "molica_video", "operation": "text_to_video"},
            [unrelated, MolicaVideo()],
        )
        assert [tool.name for tool in filtered] == ["molica_video"]


class TestLayer3Skill:

    def test_skill_file_exists_and_names_the_credential(self):
        from tests.contracts.test_atlas_tools import PROJECT_ROOT

        skill = PROJECT_ROOT / ".agents" / "skills" / "molica-video" / "SKILL.md"
        assert skill.exists()
        text = skill.read_text(encoding="utf-8")
        assert "MOLICA_TOKEN" in text
        assert "molica_video" in text

    def test_tool_advertises_the_skill(self):
        assert "molica-video" in MolicaVideo().agent_skills


class TestCreditDiagnostics:
    """Molica refuses a run silently, so the tool must explain why."""

    def _client(self):
        from tools._molica.client import MolicaClient

        return MolicaClient(token="fake-token")

    def test_create_video_raises_when_the_server_refuses_without_an_id(self, monkeypatch):
        from tools._molica.client import MolicaError

        client = self._client()
        monkeypatch.setattr(
            client,
            "post",
            lambda *args, **kwargs: {"ret": 1, "msg": "积分不足"},
        )
        with pytest.raises(MolicaError) as excinfo:
            client.create_video({"prompt": "x"})
        assert "积分不足" in str(excinfo.value)

    def test_create_video_records_the_last_response(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            client, "post", lambda *args, **kwargs: {"ret": 1, "msg": "success"}
        )
        client.create_video({"prompt": "x"})
        assert client.last_create.get("msg") == "success"

    def test_diagnose_reports_balance_and_ledger(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(client, "balance", lambda: 102)
        monkeypatch.setattr(
            client,
            "integral_logs",
            lambda page_no=1, page_size=15: [
                {
                    "created_at": "2026-11-07T10:22:26+08:00",
                    "integral_change_num": -105,
                    "change_desc": "使用Wan3.0-高稳版生成视频",
                }
            ],
        )
        client.last_create = {"ret": 1, "msg": "服务繁忙"}
        text = MolicaVideo()._diagnose(client)
        assert "102" in text
        assert "Wan3.0" in text
        assert "服务繁忙" in text

    def test_business_error_codes_are_failures(self, monkeypatch):
        from tools._molica.client import MolicaError

        client = self._client()
        for ret, msg in ((2017, "灵力值不足"), (10001, "请填写提示词"), (500, "服务繁忙")):
            monkeypatch.setattr(
                client, "post", lambda *args, **kwargs: {"ret": ret, "msg": msg}
            )
            with pytest.raises(MolicaError):
                client.create_video({"prompt": "x"})

    def test_download_accepts_a_string_path(self, monkeypatch, tmp_path):
        client = self._client()

        class FakeResponse:
            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=0):
                yield b"video-bytes"

        monkeypatch.setattr(
            client.session, "get", lambda *args, **kwargs: FakeResponse()
        )
        destination = tmp_path / "out.mp4"
        written = client.download("https://example.invalid/v.mp4", str(destination))
        assert written.exists()
        assert written.read_bytes() == b"video-bytes"


class TestModelCatalogCache:
    """A model released after the static snapshot must still be selectable."""

    def _write_cache(self, tmp_path, models):
        import json

        path = tmp_path / "catalog.json"
        path.write_text(
            json.dumps(
                {
                    "updated_at": "2026-11-07T12:00:00",
                    "model_keys": sorted(models),
                    "models": models,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_build_catalog_flattens_the_live_params(self):
        from tools._molica.client import build_catalog

        catalog = build_catalog(
            [
                {
                    "model_key": "wan30video",
                    "prompt_limit": 1000,
                    "model_params": {
                        "base": [
                            {
                                "param_key": "duration",
                                "items": [{"param_value": "5"}, {"param_value": "10"}],
                            },
                            {
                                "param_key": "resolution",
                                "items": [{"param_value": "480P"}],
                            },
                        ]
                    },
                }
            ]
        )
        assert catalog["model_keys"] == ["wan30video"]
        assert catalog["models"]["wan30video"]["durations"] == ["5", "10"]
        assert catalog["models"]["wan30video"]["resolutions"] == ["480P"]

    def test_a_new_model_becomes_selectable(self, monkeypatch, tmp_path):
        from tools.video import molica_video

        path = self._write_cache(
            tmp_path,
            {
                "wan30video": {"durations": ["5"], "resolutions": ["480P"]},
                "wan31video": {"durations": ["5", "10"], "resolutions": ["720P"]},
            },
        )
        monkeypatch.setattr(molica_video, "_CATALOG_PATH", path)

        tool = molica_video.MolicaVideo()
        enum = tool.input_schema["properties"]["model"]["enum"]
        assert "wan31video" in enum
        assert "wan30video" in enum

    def test_get_info_reports_the_live_catalog(self, monkeypatch, tmp_path):
        from tools.video import molica_video

        path = self._write_cache(
            tmp_path,
            {"wan30video": {"durations": ["5"], "resolutions": ["480P"]}},
        )
        monkeypatch.setattr(molica_video, "_CATALOG_PATH", path)

        info = molica_video.MolicaVideo().get_info()
        assert info["model_catalog_updated_at"] == "2026-11-07T12:00:00"
        assert info["model_catalog"]["wan30video"]["durations"] == ["5"]

    def test_a_missing_or_broken_cache_is_not_fatal(self, monkeypatch, tmp_path):
        from tools.video import molica_video

        broken = tmp_path / "catalog.json"
        broken.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(molica_video, "_CATALOG_PATH", broken)

        tool = molica_video.MolicaVideo()
        assert tool.input_schema["properties"]["model"]["enum"] == list(KNOWN_MODEL_KEYS)

        monkeypatch.setattr(molica_video, "_CATALOG_PATH", tmp_path / "absent.json")
        tool = molica_video.MolicaVideo()
        assert tool.input_schema["properties"]["model"]["enum"] == list(KNOWN_MODEL_KEYS)

    def test_dry_run_flags_a_model_missing_from_the_live_catalog(self, monkeypatch, tmp_path):
        from tools.video import molica_video

        path = self._write_cache(
            tmp_path,
            {"wan30video": {"durations": ["5"], "resolutions": ["480P"]}},
        )
        monkeypatch.setattr(molica_video, "_CATALOG_PATH", path)

        info = molica_video.MolicaVideo().dry_run({"prompt": "a cat", "model": "wan30video"})
        assert info["model_in_live_catalog"] is True
        assert info["model_catalog_updated_at"] == "2026-11-07T12:00:00"


class TestModelFailover:
    """A congested model must not kill the run."""

    def _tool(self):
        return MolicaVideo()

    def test_candidate_order_puts_the_request_first(self):
        order = MolicaVideo()._candidate_models([], "veo31pro", {})
        assert order[0] == "veo31pro"
        assert "molicas2" in order[1:]

    def test_failover_can_be_switched_off(self):
        order = MolicaVideo()._candidate_models([], "veo31pro", {"disable_failover": True})
        assert order == ["veo31pro"]

    def test_a_busy_model_falls_over_to_the_next_one(self, monkeypatch, tmp_path):
        """The first model answers ret=500; the next one serves the run."""
        from tools._molica import client as client_module

        catalog = [
            {
                "model_key": "busy",
                "create_mode": {"text_to_video": {"enable": True}},
                "model_params": {"base": []},
            },
            {
                "model_key": "working",
                "create_mode": {"text_to_video": {"enable": True}},
                "model_params": {"base": []},
            },
        ]
        created = []

        class FakeClient:
            token = "fake"

            def video_params(self):
                return catalog

            def balance(self):
                return 10_000

            def create_video(self, payload):
                model = payload["model_group_key"]
                created.append(model)
                if model == "busy":
                    raise client_module.MolicaError("busy", ret=500)
                return "task-1"

            def get_video(self, video_id):
                return {"video_url": "https://example.com/clip.mp4", "status": 1}

            def download(self, url, destination):
                # Mirrors MolicaClient.download: the parent folder is created on demand.
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"video")
                return Path(destination)

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        result = MolicaVideo().execute({
            "prompt": "a cat",
            "model": "busy",
            "output_path": str(tmp_path / "failover_test.mp4"),
        })
        assert result.success, result.error
        assert created == ["busy", "working"]
        assert result.data["model"] == "working"
        assert (tmp_path / "failover_test.mp4").exists()

    def test_all_models_busy_reports_what_was_tried(self, monkeypatch):
        from tools._molica import client as client_module

        catalog = [
            {
                "model_key": "busy",
                "create_mode": {"text_to_video": {"enable": True}},
                "model_params": {"base": []},
            }
        ]

        class FakeClient:
            token = "fake"

            def video_params(self):
                return catalog

            def balance(self):
                return 10_000

            def create_video(self, payload):
                raise client_module.MolicaError("busy", ret=500)

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        result = MolicaVideo().execute({"prompt": "a cat", "model": "busy"})
        assert not result.success
        assert "busy" in result.error
        assert result.data["refused"]


class TestLocalImageUploadFallback:
    """Local reference images must work without the oss2 package."""

    def test_falls_back_to_the_repository_uploader(self, monkeypatch, tmp_path):
        from tools._molica import client as client_module

        image = tmp_path / "ref.png"
        image.write_bytes(b"not-really-a-png")

        class FakeClient:
            token = "fake"

            def upload_image(self, path):
                raise client_module.MolicaError(
                    "uploading a local image to Molica needs the oss2 package"
                )

        calls = {}

        def fake_upload(image_path):
            calls["path"] = image_path
            return "https://cdn.example.com/ref.png"

        monkeypatch.setattr("tools.video._shared.upload_image_fal", fake_upload)
        resolved = MolicaVideo()._resolve_images(FakeClient(), {"image_path": str(image)})
        assert resolved["image_url"] == "https://cdn.example.com/ref.png"
        assert calls["path"] == str(image)

    def test_a_real_upload_error_is_not_swallowed(self, monkeypatch, tmp_path):
        from tools._molica import client as client_module

        image = tmp_path / "ref.png"
        image.write_bytes(b"not-really-a-png")

        class FakeClient:
            token = "fake"

            def upload_image(self, path):
                raise client_module.MolicaError("cannot upload missing file")

        with pytest.raises(client_module.MolicaError):
            MolicaVideo()._resolve_images(FakeClient(), {"image_path": str(image)})


class TestLearnedCreditPrices:
    """The ledger is the only tariff the site publishes about itself."""

    LOGS = [
        {"integral_change_num": -105, "change_desc": "使用Wan3.0-高稳版生成视频"},
        {"integral_change_num": -105, "change_desc": "使用Wan3.0-高稳版生成视频"},
        {"integral_change_num": 105, "change_desc": "使用Wan3.0-高稳版生成视频失败归还"},
        {"integral_change_num": -720, "change_desc": '使用"Veo3.1 Pro"的"生成视频"1次'},
        {"integral_change_num": -36, "change_desc": '使用"Banana2（4K超清版）"的"图片创作"1次'},
    ]
    NAMES = {"wan30video": "Wan3.0-高稳版", "veo31pro": "Veo3.1 Pro"}

    def test_prices_come_from_the_cheapest_charge(self):
        from tools._molica.client import learn_credit_prices

        learned = learn_credit_prices(self.LOGS, self.NAMES)
        assert learned["prices"]["wan30video"]["credits"] == 105

    def test_a_refund_never_lowers_the_price(self):
        from tools._molica.client import learn_credit_prices

        logs = [
            {"integral_change_num": -105, "change_desc": "使用Wan3.0-高稳版生成视频"},
            {"integral_change_num": 105, "change_desc": "使用Wan3.0-高稳版生成视频失败归还"},
        ]
        learned = learn_credit_prices(logs, self.NAMES)
        assert learned["prices"]["wan30video"]["credits"] == 105

    def test_an_unmapped_label_keeps_its_price_but_has_no_model(self):
        from tools._molica.client import learn_credit_prices

        # "Veo3.1 Pro" is unmapped only when the live names do not list it.
        learned = learn_credit_prices(self.LOGS, {"wan30video": "Wan3.0-高稳版"})
        assert "Veo3.1 Pro" in learned["labels"]
        assert learned["labels"]["Veo3.1 Pro"]["credits"] == 720
        assert "veo31pro" not in learned["prices"]

    def test_an_alias_turns_a_label_into_a_model_price(self):
        from tools._molica.client import learn_credit_prices

        learned = learn_credit_prices(self.LOGS, self.NAMES, aliases={"Veo3.1 Pro": "veo31pro"})
        assert learned["prices"]["veo31pro"]["credits"] == 720
        assert "Veo3.1 Pro" not in learned["labels"]

    def test_the_ledger_outranks_the_static_table(self, monkeypatch, tmp_path):
        import json

        from tools.video import molica_video

        cache = tmp_path / "credits.json"
        cache.write_text(
            json.dumps({"prices": {"wan30video": {"credits": 42}}}), encoding="utf-8"
        )
        monkeypatch.setattr(molica_video, "_CREDITS_PATH", cache)
        monkeypatch.setattr(molica_video, "_PRICE_CACHE", {"path": None, "mtime": None, "prices": {}})

        assert molica_video._credit_cost("wan30video") == 42
        assert molica_video._credit_cost_source("wan30video") == "ledger"

    def test_an_environment_override_outranks_everything(self, monkeypatch, tmp_path):
        import json

        from tools.video import molica_video

        cache = tmp_path / "credits.json"
        cache.write_text(
            json.dumps({"prices": {"wan30video": {"credits": 42}}}), encoding="utf-8"
        )
        monkeypatch.setattr(molica_video, "_CREDITS_PATH", cache)
        monkeypatch.setattr(molica_video, "_PRICE_CACHE", {"path": None, "mtime": None, "prices": {}})
        monkeypatch.setenv("MOLICA_CREDIT_COST_WAN30VIDEO", "7")

        assert molica_video._credit_cost("wan30video") == 7
        assert molica_video._credit_cost_source("wan30video") == "env"

    def test_a_model_without_a_known_price_is_reported_as_unknown(self, monkeypatch, tmp_path):
        from tools.video import molica_video

        monkeypatch.setattr(molica_video, "_CREDITS_PATH", tmp_path / "absent.json")
        monkeypatch.setattr(molica_video, "_PRICE_CACHE", {"path": None, "mtime": None, "prices": {}})

        assert molica_video._credit_cost("some-unrun-model") is None
        assert molica_video._credit_cost_source("some-unrun-model") == "unknown"


class TestSpecAwareCreditPrices:
    """A credit price belongs to a specification, not to a model as a whole."""

    LOGS = [
        {"integral_change_num": -1349, "change_desc": '使用"HappyHorse1.0"的"15s+720P生成"1次'},
        {"integral_change_num": -360, "change_desc": '使用"即梦3.5 Pro"的"10s+720P生成"1次'},
        # Image, chat and search spend the same wallet but are not video runs.
        {"integral_change_num": -36, "change_desc": '使用"Banana2（4K超清版）"的"图片创作"1次'},
        {"integral_change_num": -32, "change_desc": '使用"mj"的"生成图片"1次'},
        {"integral_change_num": -16, "change_desc": '使用"Chat-4o"的"联网搜索"1次'},
        {"integral_change_num": -5, "change_desc": '使用"Chat-5"的"文字对话"1次'},
        # The gift balance lapsing is not a purchase either.
        {"integral_change_num": -30, "change_desc": "赠送灵力过期"},
    ]
    NAMES = {"happyHorse1": "HappyHorse1.0", "jimeng35pro": "即梦3.5 Pro"}

    def test_non_video_spend_is_not_a_video_price(self):
        from tools._molica.client import learn_credit_prices

        learned = learn_credit_prices(self.LOGS, self.NAMES)
        assert set(learned["prices"]) == {"happyHorse1", "jimeng35pro"}
        assert learned["labels"] == {}
        assert learned["unmatched"] == []

    def test_each_specification_keeps_its_own_price(self):
        from tools._molica.client import learn_credit_prices

        learned = learn_credit_prices(self.LOGS, self.NAMES)
        happy = learned["prices"]["happyHorse1"]
        assert happy["credits"] == 1349
        assert happy["duration"] == 15
        assert happy["resolution"] == "720P"
        assert happy["specs"] == {"15s+720P": 1349}

    def test_a_label_with_trailing_space_still_matches(self):
        from tools._molica.client import learn_credit_prices

        # The ledger writes "Sora2 Pro (高清版) " with a trailing space.
        logs = [
            {"integral_change_num": -640, "change_desc": '使用"Sora2 Pro (高清版) "的"生成视频"1次'}
        ]
        learned = learn_credit_prices(
            logs, {}, aliases={"Sora2 Pro (高清版)": "sora2prohd"}
        )
        assert learned["prices"]["sora2prohd"]["credits"] == 640

    def test_the_matching_specification_price_is_used(self, monkeypatch, tmp_path):
        import json

        from tools.video import molica_video

        cache = tmp_path / "credits.json"
        cache.write_text(
            json.dumps(
                {
                    "prices": {
                        "happyHorse1": {
                            "credits": 1349,
                            "duration": 15,
                            "resolution": "720P",
                            "specs": {"15s+720P": 1349, "5s+720P": 300},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(molica_video, "_CREDITS_PATH", cache)
        monkeypatch.setattr(molica_video, "_PRICE_CACHE", {"path": None, "mtime": None, "prices": {}})

        assert molica_video._credit_cost("happyHorse1", 15, "720P") == 1349
        assert molica_video._credit_cost("happyHorse1", 5, "720P") == 300
        # An unobserved specification falls back to the cheapest observed run.
        assert molica_video._credit_cost("happyHorse1", 10, "1080P") == 1349
        assert molica_video._credit_cost_source("happyHorse1", 15, "720P") == "ledger_spec"
        assert molica_video._credit_cost_source("happyHorse1", 10, "1080P") == "ledger"

    def test_the_resolution_is_matched_case_insensitively(self, monkeypatch, tmp_path):
        import json

        from tools.video import molica_video

        cache = tmp_path / "credits.json"
        cache.write_text(
            json.dumps({"prices": {"jimeng35pro": {"credits": 360, "specs": {"10s+720P": 360}}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(molica_video, "_CREDITS_PATH", cache)
        monkeypatch.setattr(molica_video, "_PRICE_CACHE", {"path": None, "mtime": None, "prices": {}})

        # The catalog spells it "720p"; the ledger spells it "720P".
        assert molica_video._credit_cost("jimeng35pro", 10, "720p") == 360


class TestResumeAnExistingTask:
    """A queued task can outlive the process that submitted it."""

    def test_a_given_task_id_is_polled_instead_of_created(self, monkeypatch, tmp_path):
        from tools._molica import client as client_module

        polled = []
        created = []

        class FakeClient:
            token = "fake"

            def video_params(self):
                return [
                    {
                        "model_key": "wan30video",
                        "create_mode": {"text_to_video": {"enable": True}},
                        "model_params": {"base": []},
                    }
                ]

            def balance(self):
                return 102

            def create_video(self, payload):
                created.append(payload)
                return "should-not-happen"

            def get_video(self, video_id):
                polled.append(video_id)
                return {"video_url": "https://example.com/clip.mp4", "status": 1}

            def download(self, url, destination):
                Path(destination).write_bytes(b"video")
                return Path(destination)

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        out = tmp_path / "resumed.mp4"
        result = MolicaVideo().execute({
            "prompt": "a cat",
            "model": "wan30video",
            "task_id": "queued-task-1",
            "output_path": str(out),
        })
        assert result.success, result.error
        assert created == [], "a resumed task must not create a new one"
        assert polled == ["queued-task-1"]
        assert result.data["video_id"] == "queued-task-1"

    def test_a_resume_needs_no_prompt(self, monkeypatch, tmp_path):
        """The account already paid for the task, so no prompt is needed."""
        from tools._molica import client as client_module

        class FakeClient:
            token = "fake"

            def balance(self):
                return 102

            def create_video(self, payload):
                raise AssertionError("a resumed task must not create a new one")

            def get_video(self, video_id):
                return {"video_url": "https://example.com/clip.mp4", "status": 1}

            def download(self, url, destination):
                Path(destination).write_bytes(b"video")
                return Path(destination)

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        out = tmp_path / "resumed.mp4"
        result = MolicaVideo().execute({"video_id": "queued-task-2", "output_path": str(out)})
        assert result.success, result.error
        assert result.data["video_id"] == "queued-task-2"
        assert out.exists()

    def test_a_resume_describes_the_task_record(self, monkeypatch, tmp_path):
        """The result describes the run as it was made, not as it is requested."""
        from tools._molica import client as client_module

        record = {
            "video_id": "task-3",
            "video_url": "https://example.com/clip.mp4",
            "status": 1,
            "model_group_key": "wan30video",
            "prompt": "a red kite over a windy beach",
            "thumbnail_url": "https://example.com/thumb.png",
            "req": {
                "prompt": "a red kite over a windy beach",
                "model_group_key": "wan30video",
                "duration": 5,
                "resolution": "480P",
                "video_scale": "16:9",
                "fast_model": False,
                "image_url": "",
                "image_end_url": "",
                "video_single_image": "",
                "video_multiple_image": [],
            },
        }

        class FakeClient:
            token = "fake"

            def balance(self):
                return 102

            def create_video(self, payload):
                raise AssertionError("a resumed task must not create a new one")

            def get_video(self, video_id):
                return record

            def download(self, url, destination):
                Path(destination).write_bytes(b"video")
                return Path(destination)

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        out = tmp_path / "resumed.mp4"
        result = MolicaVideo().execute({"video_id": "task-3", "output_path": str(out)})
        assert result.success, result.error
        assert result.data["model"] == "wan30video"
        assert result.data["prompt"] == "a red kite over a windy beach"
        assert result.data["duration"] == 5
        assert result.data["resolution"] == "480P"
        assert result.data["thumbnail_url"] == "https://example.com/thumb.png"
        assert result.data["resumed"] is True
        # The task was paid for when it was submitted; collecting it is free.
        assert result.data["credits_required"] == 0
        assert result.data["credits_source"] == "resume"
        assert result.cost_usd == 0.0

    def test_a_resume_is_not_blocked_by_a_low_balance(self, monkeypatch, tmp_path):
        """Collecting a task spends no credits, so a low balance is no obstacle."""
        from tools._molica import client as client_module

        class FakeClient:
            token = "fake"

            def balance(self):
                return 1

            def create_video(self, payload):
                raise AssertionError("a resumed task must not create a new one")

            def get_video(self, video_id):
                return {"video_url": "https://example.com/clip.mp4", "status": 1}

            def download(self, url, destination):
                Path(destination).write_bytes(b"video")
                return Path(destination)

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        out = tmp_path / "resumed.mp4"
        result = MolicaVideo().execute({"video_id": "task-4", "output_path": str(out)})
        assert result.success, result.error

    def test_a_collect_that_times_out_keeps_the_task_id(self, monkeypatch, tmp_path):
        from tools._molica import client as client_module

        class FakeClient:
            token = "fake"

            def balance(self):
                return 102

            def get_video(self, video_id):
                return {"video_id": video_id, "status": 0, "video_url": ""}

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        result = MolicaVideo().execute({
            "video_id": "task-5",
            "output_path": str(tmp_path / "resumed.mp4"),
            "timeout_seconds": 0,
        })
        assert not result.success
        assert result.data["video_id"] == "task-5"
        assert result.data["resumable"] is True
        assert "task-5" in result.error


class TestAPaidRunIsNeverLost:
    """A timeout must not throw away a task the account already paid for."""

    def test_a_timed_out_run_reports_its_task_id(self, monkeypatch, tmp_path):
        from tools._molica import client as client_module

        class FakeClient:
            token = "fake"

            def video_params(self):
                return [
                    {
                        "model_key": "wan30video",
                        "create_mode": {"text_to_video": {"enable": True}},
                        "model_params": {"base": []},
                    }
                ]

            def balance(self):
                return 10_000

            def create_video(self, payload):
                return "paid-task-1"

            def get_video(self, video_id):
                return {"video_id": video_id, "status": 0, "video_url": ""}

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        result = MolicaVideo().execute({
            "prompt": "a cat",
            "model": "wan30video",
            "output_path": str(tmp_path / "clip.mp4"),
            "timeout_seconds": 0,
        })
        assert not result.success
        assert result.data["video_id"] == "paid-task-1"
        assert result.data["resumable"] is True
        assert "paid-task-1" in result.error

    def test_a_prompt_without_a_task_is_still_refused(self):
        result = MolicaVideo().execute({})
        assert not result.success
        assert "prompt" in result.error


class TestTimeout:
    """The wait must cover a generation that really takes about an hour."""

    def test_the_default_outlasts_the_measured_generation(self):
        from tools.video import molica_video

        # Measured on this account: about 62 minutes for a 5s 480P run.
        assert molica_video._DEFAULT_TIMEOUT_SECONDS >= 62 * 60
        assert MolicaVideo().input_schema["properties"]["timeout_seconds"]["default"] == (
            molica_video._DEFAULT_TIMEOUT_SECONDS
        )

    def test_the_free_queue_gets_a_longer_patience(self, monkeypatch, tmp_path):
        """The shared free queue can wait long before it starts generating."""
        from tools._molica import client as client_module
        from tools.video import molica_video

        seen = {}

        def fake_poll(self, client, video_id, *, poll_interval, timeout):
            seen["timeout"] = timeout
            return {"video_url": "https://example.com/clip.mp4", "status": 1}

        class FakeClient:
            token = "fake"

            def video_params(self):
                return [
                    {
                        "model_key": "wan30video",
                        "create_mode": {"text_to_video": {"enable": True}},
                        "model_params": {"base": []},
                    }
                ]

            def balance(self):
                return 10_000

            def create_video(self, payload):
                return "task-6"

            def get_video(self, video_id):
                return {"video_url": "https://example.com/clip.mp4", "status": 1}

            def download(self, url, destination):
                Path(destination).write_bytes(b"video")
                return Path(destination)

        monkeypatch.setattr(client_module, "MolicaClient", lambda *a, **k: FakeClient())
        monkeypatch.setattr(molica_video.MolicaVideo, "_poll", fake_poll)

        MolicaVideo().execute({
            "prompt": "a cat",
            "fast_model": True,
            "output_path": str(tmp_path / "fast.mp4"),
        })
        assert seen["timeout"] == molica_video._DEFAULT_TIMEOUT_SECONDS

        MolicaVideo().execute({
            "prompt": "a cat",
            "fast_model": False,
            "output_path": str(tmp_path / "standard.mp4"),
        })
        assert seen["timeout"] == molica_video._DEFAULT_TIMEOUT_SECONDS * 3
