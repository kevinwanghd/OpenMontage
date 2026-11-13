"""DashScope (Alibaba Cloud Bailian) wan3.0-video generation -- mock-first.

M0 exposes the Bailian wan3.0-video contract without spending money: by default
every execution runs through :class:`MockWanTransport`, which implements the
DashScope async task lifecycle in memory and opens no sockets. The live HTTP
path is reachable only with ``DASHSCOPE_VIDEO_LIVE=1`` plus
``DASHSCOPE_API_KEY`` (M1 measurement, behind NFR-01 approval).

Capability declarations matter as much as execution here (A2/F34): the draft
binding falls back to wan3.0-video at **480P** (¥0.30/s), and the capability
catalog fragment records i2v / first_frame conditioning and
``output_resolutions`` (480p/720p/1080p) with ``verification_status: MOCKED``
until M1 measures the real API.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Optional

from tools._dashscope.wan_video_client import (
    CNY_PER_USD,
    CONSOLE_URL,
    MODEL_ID,
    RESOLUTION_PRICES_CNY_PER_SECOND,
    SUPPORTED_RESOLUTIONS,
    LiveWanTransport,
    MockWanTransport,
    build_request_payload,
    cny_price,
    normalize_operation,
    normalize_ratio,
    normalize_resolution,
)
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

#: Explicit opt-in for the paid M1 path. Unset means mock, always.
LIVE_ENV = "DASHSCOPE_VIDEO_LIVE"
FAILED_TASK_STATES = {"FAILED", "CANCELED", "UNKNOWN", "NOT_FOUND"}


class DashscopeVideo(BaseTool):
    name = "dashscope_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "dashscope"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.SEEDED
    runtime = ToolRuntime.API

    dependencies = ["env:DASHSCOPE_API_KEY"]
    # risk:untested reason:"能力声明与 mock 执行路径由 tests/contracts/test_dashscope_video.py 的 23 条契约用例覆盖；thin 模式的静态 test-check 无证据文件通路，按仓库规定登记豁免" owner:@kevinwanghd reviewed:2026-11-11
    install_instructions = (
        "Set DASHSCOPE_API_KEY to your Alibaba Cloud Bailian API key.\n"
        f"  Get one at {CONSOLE_URL}\n"
        "M0 runs mock-only; paid calls require DASHSCOPE_VIDEO_LIVE=1 "
        "explicitly (M1 measurement, after approval)."
    )
    fallback = "molica_video"
    fallback_tools = ["molica_video", "holopix_video", "wan_fal_video"]
    agent_skills = ["dashscope"]

    capabilities = ["text_to_video", "image_to_video", "first_frame_conditioning"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "first_frame_conditioning": True,
        "output_resolutions": list(SUPPORTED_RESOLUTIONS),
        "negative_prompt": True,
        "seed": True,
        "native_audio": False,
        "offline": False,
    }
    best_for = [
        "paid fallback draft shots at 480P (¥0.30/s, cheapest Bailian tier)",
        "keyframe-driven image-to-video with first-frame conditioning",
    ]
    not_good_for = [
        "offline generation",
        "native dialogue audio (silent clips; TTS + lipsync are separate stages)",
        "M0 production output -- the transport is a contract mock, not a renderer",
    ]
    provider_matrix = {
        "wan3.0-video": {
            "tool": "dashscope_video",
            "name": "Bailian Wan 3.0 Video",
            "quality": "draft",
            "operations": ["text_to_video", "image_to_video"],
            "resolutions": list(SUPPORTED_RESOLUTIONS),
            "cny_per_second": dict(RESOLUTION_PRICES_CNY_PER_SECOND),
            "verification_status": "MOCKED",
        }
    }

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video", "first_frame"],
                "default": "text_to_video",
                "description": "first_frame is an alias for image_to_video with a first-frame image.",
            },
            "model": {"type": "string", "enum": [MODEL_ID, "wan2.7-i2v", "wan2.7-t2v"],
                      "default": MODEL_ID},
            "resolution": {
                "type": "string",
                "enum": list(SUPPORTED_RESOLUTIONS),
                "default": "480p",
                "description": "Draft shots use 480p; 720p/1080p cost 2x/4x more.",
            },
            "duration_seconds": {
                "type": "number",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
            },
            "reference_image_url": {
                "type": "string",
                "description": "Public image URL (or data: URI) for image_to_video/first_frame.",
            },
            "reference_image_path": {
                "type": "string",
                "description": "Local image path, sent as a base64 data URI.",
            },
            "ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1", "4:3", "3:4"],
                "description": (
                    "Aspect ratio. wan3.0 replaced the deprecated size field with "
                    "resolution + ratio; 480P + 9:16 yields a 480x854 portrait clip."
                ),
            },
            "negative_prompt": {"type": "string"},
            "seed": {"type": "integer", "minimum": 0},
            "prompt_extend": {"type": "boolean", "default": True},
            "watermark": {"type": "boolean", "default": False},
            "output_path": {"type": "string"},
            "poll_interval_seconds": {
                "type": "number",
                "default": 2.0,
                "minimum": 0,
                "description": "Delay between task polls; 0 is used by contract tests.",
            },
            "poll_timeout_seconds": {"type": "number", "default": 600.0, "minimum": 1},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=100, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=1, retryable_errors=["rate_limit", "timeout"]
    )
    idempotency_key_fields = [
        "prompt",
        "operation",
        "model",
        "resolution",
        "ratio",
        "duration_seconds",
        "reference_image_url",
        "reference_image_path",
        "negative_prompt",
        "seed",
        "prompt_extend",
        "watermark",
    ]
    side_effects = [
        "M0 mock mode: writes nothing and calls no network",
        "live mode (DASHSCOPE_VIDEO_LIVE=1) calls the DashScope video API and bills per output second",
    ]
    user_visible_verification = [
        "Inspect the returned clip for prompt adherence and first-frame consistency",
        "Confirm duration/resolution before accepting a paid (live) result",
    ]

    def __init__(self, transport: Optional[Any] = None) -> None:
        # Tests inject a mock; production never passes one.
        self._transport = transport

    def get_status(self) -> ToolStatus:
        # Mock mode needs no credential and is always usable. Only the opt-in
        # live mode depends on DASHSCOPE_API_KEY.
        if _live_requested() and not os.environ.get("DASHSCOPE_API_KEY"):
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """Estimated USD cost for the requested clip (Bailian CNY sheet)."""
        try:
            resolution = normalize_resolution(inputs.get("resolution"))
            duration = float(inputs.get("duration_seconds", 5))
        except (TypeError, ValueError):
            return 0.0
        return round(cny_price(resolution, duration) / CNY_PER_USD, 4)

    def estimate_cost_cny(self, inputs: dict[str, Any]) -> float:
        try:
            resolution = normalize_resolution(inputs.get("resolution"))
            duration = float(inputs.get("duration_seconds", 5))
        except (TypeError, ValueError):
            return 0.0
        return round(cny_price(resolution, duration), 4)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        try:
            prompt = inputs["prompt"]
            operation = normalize_operation(inputs.get("operation"))
            resolution = normalize_resolution(inputs.get("resolution"))
            ratio = normalize_ratio(inputs.get("ratio"))
            duration = float(inputs.get("duration_seconds", 5))
            if duration <= 0:
                raise ValueError("duration_seconds must be positive")

            image_url = self._image_reference(inputs)
            payload = build_request_payload(
                prompt,
                operation=operation,
                model=inputs.get("model") or MODEL_ID,
                resolution=resolution,
                duration_seconds=duration,
                image_url=image_url,
                negative_prompt=inputs.get("negative_prompt"),
                seed=inputs.get("seed"),
                prompt_extend=inputs.get("prompt_extend", True),
                watermark=inputs.get("watermark", False),
                ratio=ratio,
            )
        except KeyError:
            return ToolResult(success=False, error="prompt is required")
        except (TypeError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))

        try:
            transport = self._resolve_transport()
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        try:
            task_id = transport.submit(payload)
            task = self._wait_for_task(transport, task_id, inputs)
        except Exception as exc:  # network/provider failure becomes a ToolResult
            return ToolResult(
                success=False,
                error=f"DashScope video generation failed: {self._safe_error(exc)}",
                duration_seconds=round(time.time() - start, 2),
            )

        status = str(task.get("task_status") or "UNKNOWN").upper()
        if status != "SUCCEEDED":
            detail = task.get("message") or task.get("code") or ""
            return ToolResult(
                success=False,
                error=f"DashScope task {status}: {detail}".rstrip(": "),
                data={"task_id": task_id, "task_status": status},
                duration_seconds=round(time.time() - start, 2),
            )

        # The live transport flattens DashScope's nested output; keep accepting
        # the raw nested shape too so mock/other transports stay compatible.
        video_url = (
            task.get("video_url")
            or ((task.get("output") or {}).get("video_url"))
            or ""
        )
        if not video_url:
            return ToolResult(
                success=False,
                error="DashScope task succeeded without a video_url",
                data={"task_id": task_id, "task_status": status},
                duration_seconds=round(time.time() - start, 2),
            )

        mode = getattr(transport, "mode", "mock")
        output_path = inputs.get("output_path")
        artifacts: list[str] = []
        if mode == "live" and output_path:
            # The paid clip exists only as a signed URL until it is downloaded;
            # losing it here would bill the operator for an unusable asset.
            try:
                import requests

                target = Path(output_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                response = requests.get(video_url, timeout=180)
                response.raise_for_status()
                target.write_bytes(response.content)
                if target.stat().st_size == 0:
                    raise IOError("downloaded clip is empty")
                artifacts = [str(target)]
            # risk:swallowed-exception reason:"下载失败不抛异常而是返回 success=False 并保留 task_id/video_url 供对账，避免已计费任务被当成崩溃丢失" owner:@kevinwanghd reviewed:2026-11-13
            except Exception as exc:
                return ToolResult(
                    success=False,
                    error=(
                        "DashScope clip generated but download failed: "
                        f"{self._safe_error(exc)}; task_id={task_id}"
                    ),
                    data={
                        "task_id": task_id,
                        "task_status": status,
                        "video_url": video_url,
                        "output_path": output_path,
                    },
                    cost_usd=self.estimate_cost(inputs),
                    duration_seconds=round(time.time() - start, 2),
                    model=payload["model"],
                )
        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "model": payload["model"],
                "operation": operation,
                "resolution": resolution,
                "ratio": ratio,
                "duration_seconds": duration,
                "mode": mode,
                "verification_status": "MOCKED" if mode == "mock" else "UNVERIFIED",
                "task_id": task_id,
                "task_status": status,
                "video_url": video_url,
                "usage": task.get("usage") or {},
                "polls": sum(
                    1
                    for call in getattr(transport, "calls", [])
                    if call.get("action") == "fetch"
                ),
                "estimate_cny": self.estimate_cost_cny(inputs),
                "output_path": output_path,
                "video_path": artifacts[0] if artifacts else None,
            },
            # Mock URLs are not downloadable media; live clips are downloaded
            # to output_path and returned as a local artifact.
            artifacts=artifacts,
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            seed=inputs.get("seed"),
            model=payload["model"],
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _resolve_transport(self) -> Any:
        if self._transport is not None:
            return self._transport
        if _live_requested():
            api_key = os.environ.get("DASHSCOPE_API_KEY")
            if not api_key:
                raise ValueError(
                    "DASHSCOPE_VIDEO_LIVE=1 requires DASHSCOPE_API_KEY; "
                    "unset DASHSCOPE_VIDEO_LIVE for the M0 mock transport"
                )
            return LiveWanTransport(api_key)
        return MockWanTransport()

    @staticmethod
    def _wait_for_task(
        transport: Any, task_id: str, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        # The mock resolves in-process; sleeping between polls would only make
        # contract tests and dry runs slow.
        interval = 0.0 if getattr(transport, "mode", "mock") == "mock" else max(
            0.0, float(inputs.get("poll_interval_seconds", 2.0))
        )
        timeout = max(0.0, float(inputs.get("poll_timeout_seconds", 600.0)))
        deadline = time.monotonic() + timeout
        while True:
            task = transport.fetch(task_id)
            status = str(task.get("task_status") or "UNKNOWN").upper()
            if status == "SUCCEEDED" or status in FAILED_TASK_STATES:
                return task
            if time.monotonic() >= deadline:
                return {
                    "task_id": task_id,
                    "task_status": "TIMEOUT",
                    "message": f"no terminal status within {timeout:g}s",
                }
            if interval:
                time.sleep(interval)
            else:
                # yield so a misbehaving transport cannot spin forever
                time.sleep(0)

    @staticmethod
    def _image_reference(inputs: dict[str, Any]) -> Optional[str]:
        url = inputs.get("reference_image_url")
        if url:
            return str(url)
        path_value = inputs.get("reference_image_path")
        if not path_value:
            return None
        path = Path(path_value)
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return str(exc).replace(os.environ.get("DASHSCOPE_API_KEY", ""), "[redacted]")


def _live_requested() -> bool:
    return os.environ.get(LIVE_ENV) == "1"