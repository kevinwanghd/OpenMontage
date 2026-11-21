# risk:hardcoded-url reason:"模块文档中给出官方端点供读者核对，实际请求地址始终由 PIXVERSE_BASE_URL 覆盖，文档字符串不参与请求构造" owner:@kevinwanghd reviewed:2026-11-21
"""PixVerse (爱诗科技) video generation provider — official OpenAPI v2.

Base URL defaults to ``https://app-api.pixverseai.cn/openapi/v2``, overridable
via ``PIXVERSE_BASE_URL`` (the .com deployment is a separate account pool, so
the endpoint must stay configurable rather than implied).

Contract verified against the live endpoint 2026-11-17 with zero-cost probes
(invalid-field rejections), not copied from docs:
* Auth is the ``API-KEY`` header — **not** ``Authorization: Bearer``. Sending a
  Bearer token yields ``API_KEY_REQUIRED``, which is why the header name is a
  declared constant here instead of an implicit convention.
* Supported ``model`` values observed: v6, v5.6, v5, v4.5, v4, c1.
* Supported ``quality`` values: 360p, 540p, 720p, 1080p. **480p is rejected.**
* ``duration`` accepts 3, 5, 8, 10; ``aspect_ratio`` accepts 9:16, 16:9, 1:1, 3:4.
* Image-to-video needs an ``img_id`` from ``POST /image/upload``; a bare URL is
  not accepted by ``/video/img/generate``.
* Result polling: ``GET /video/result/{id}`` → ``Resp.status`` 1=success,
  5=waiting/processing, 7=moderation failure, 8=generation failure.

Cost is deliberately not guessed: PixVerse bills per account plan, and a wrong
number here would silently undermine the per-episode budget. ``estimate_cost``
returns the configured override when present and 0.0 (documented as unknown)
otherwise.
"""

from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    DependencyError,
    Determinism,
    ExecutionMode,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)
from tools.video._shared import probe_output

# risk:hardcoded-url reason:"国内站端点是该 provider 的官方默认值，可被 PIXVERSE_BASE_URL 完整覆盖；两站账号池不通用，默认值必须存在且可配置" owner:@kevinwanghd reviewed:2026-11-21
DEFAULT_BASE_URL = "https://app-api.pixverseai.cn/openapi/v2"
AUTH_HEADER_ENV = "PIXVERSE_AUTH_HEADER"
DEFAULT_AUTH_HEADER = "API-KEY"

MODELS = ("v6", "v5.6", "v5", "v4.5", "v4", "c1")
QUALITIES = ("360p", "540p", "720p", "1080p")
DURATIONS = (3, 5, 8, 10)
ASPECT_RATIOS = ("9:16", "16:9", "1:1", "3:4")

STATUS_SUCCESS = 1
STATUS_WAITING = 5
STATUS_MODERATION_FAILED = 7
STATUS_FAILED = 8

# Prices are per second of generated video, in CNY. Left empty on purpose:
# the account plan determines the real price and a fabricated number would be
# indistinguishable from a measured one downstream.
PRICE_CNY_PER_SECOND: dict[str, float] = {}


class PixVerseError(RuntimeError):
    """A PixVerse API call returned a non-zero ErrCode."""

    def __init__(self, code: Any, message: str, payload: dict[str, Any] | None = None):
        super().__init__(f"PixVerse ErrCode={code}: {message}")
        self.code = code
        self.payload = payload or {}


class PixVerseClient:
    """Thin transport. Keeps HTTP concerns out of the tool's contract."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        raw_base = (base_url or os.environ.get("PIXVERSE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        # Accept either the bare host or the full API prefix. A bare host is
        # what the dashboard hands out, and dropping the prefix turns every
        # call into an nginx 404 instead of an API-level error.
        if "/openapi/" not in raw_base:
            raw_base = f"{raw_base}/openapi/v2"
        self.base_url = raw_base
        self.api_key = api_key if api_key is not None else os.environ.get("PIXVERSE_API_KEY", "")
        self.auth_header = os.environ.get(AUTH_HEADER_ENV) or DEFAULT_AUTH_HEADER

    def _headers(self, *, json_body: bool = True) -> dict[str, str]:
        headers = {self.auth_header: self.api_key, "Ai-trace-id": str(uuid.uuid4())}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"ErrCode": -1, "ErrMsg": raw[:200].decode("utf-8", "replace")}

    def _request(self, method: str, path: str, *, data: bytes | None = None,
                 headers: dict[str, str] | None = None, timeout: float = 60.0) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        request = urllib.request.Request(url, data=data, headers=headers or self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = self._decode(response.read())
        except urllib.error.HTTPError as error:
            payload = self._decode(error.read())
        except urllib.error.URLError as error:
            raise PixVerseError("transport", str(error)) from error
        code = payload.get("ErrCode")
        if code not in (0, None):
            error_info = payload.get("Error") or {}
            raise PixVerseError(code, str(error_info.get("Message") or payload.get("ErrMsg") or ""), payload)
        return payload

    def text_to_video(self, payload: dict[str, Any]) -> int:
        return self._video_id(self._request("POST", "/video/text/generate", data=json.dumps(payload).encode()))

    def image_to_video(self, payload: dict[str, Any]) -> int:
        return self._video_id(self._request("POST", "/video/img/generate", data=json.dumps(payload).encode()))

    #: The upload endpoint sits behind nginx and rejects large bodies with a
    #: plain 413 (measured 2026-11-17 on a 4.1 MB RGBA PNG). Sending the raw
    #: keyframe is therefore not an option; it is normalised to JPEG first.
    MAX_UPLOAD_BYTES = 2_000_000
    MAX_EDGE_PX = 1600

    @classmethod
    def _prepare_image(cls, image_path: Path) -> bytes:
        raw = image_path.read_bytes()
        if len(raw) <= cls.MAX_UPLOAD_BYTES and image_path.suffix.lower() in (".jpg", ".jpeg"):
            return raw
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover - Pillow ships with the project
            if len(raw) <= cls.MAX_UPLOAD_BYTES:
                return raw
            raise PixVerseError("upload", f"image is {len(raw)} bytes and Pillow is unavailable to shrink it")

        buffer = io.BytesIO()
        with Image.open(io.BytesIO(raw)) as image:
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.split()[-1] if image.mode == "RGBA" else None)
                image = background
            else:
                image = image.convert("RGB")
            if max(image.size) > cls.MAX_EDGE_PX:
                image.thumbnail((cls.MAX_EDGE_PX, cls.MAX_EDGE_PX))
            for quality in (92, 85, 75, 60):
                buffer.seek(0)
                buffer.truncate()
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
                if buffer.tell() <= cls.MAX_UPLOAD_BYTES:
                    break
        return buffer.getvalue()

    def upload_image(self, image_path: Path) -> int:
        boundary = f"----pixverse{uuid.uuid4().hex}"
        content = self._prepare_image(image_path)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{image_path.stem}.jpg"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        headers = self._headers(json_body=False)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        payload = self._request("POST", "/image/upload", data=body, headers=headers, timeout=180.0)
        resp = payload.get("Resp") or {}
        image_id = resp.get("image_id") or resp.get("img_id")
        if not image_id:
            raise PixVerseError("upload", f"upload response carried no image id: {resp}")
        # The generate endpoints type-check img_id as a JSON number; a string id
        # is rejected with 400013 even though upload returned it verbatim.
        return int(image_id)

    @staticmethod
    def _video_id(payload: dict[str, Any]) -> int:
        resp = payload.get("Resp") or {}
        video_id = resp.get("video_id") or resp.get("id")
        if video_id is None:
            raise PixVerseError("submit", f"submit response carried no video id: {resp}", payload)
        return int(video_id)

    def wait_for_video(self, video_id: int, *, timeout_seconds: float, poll_interval: float) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        while True:
            payload = self._request("GET", f"/video/result/{int(video_id)}")
            resp = payload.get("Resp") or {}
            status = resp.get("status")
            if status == STATUS_SUCCESS:
                return resp
            if status == STATUS_MODERATION_FAILED:
                raise PixVerseError("moderation", "content moderation rejected the generation", resp)
            if status == STATUS_FAILED:
                raise PixVerseError("generation", "generation failed", resp)
            if status not in (STATUS_WAITING, None):
                # An unknown code is not "still running": refusing beats
                # polling a terminal state until the timeout looks like a hang.
                raise PixVerseError("status", f"unrecognised status {status!r}", resp)
            if time.time() >= deadline:
                raise TimeoutError(f"PixVerse task {video_id} did not finish within {timeout_seconds}s")
            time.sleep(poll_interval)


class PixVerseVideo(BaseTool):
    name = "pixverse_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "pixverse"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:PIXVERSE_API_KEY"]
    install_instructions = (
        "Set PIXVERSE_API_KEY in .env (official PixVerse key). "
        "PIXVERSE_BASE_URL optionally overrides the default CN endpoint; "
        "auth uses the API-KEY header, not a Bearer token."
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_image": True,
        "aspect_ratio": True,
        "seed": True,
    }
    best_for = [
        "shots that need a native 9:16 vertical cinematic look",
        "image-to-video continuation of an approved keyframe",
        "drama production where the user has chosen PixVerse explicitly",
    ]
    not_good_for = ["offline generation", "native dialogue lipsync", "long-form single-take shots"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {"type": "string", "enum": ["text_to_video", "image_to_video"],
                          "default": "image_to_video"},
            "model": {"type": "string", "enum": list(MODELS), "default": "v6"},
            "duration": {"type": "integer", "enum": list(DURATIONS), "default": 5},
            "quality": {"type": "string", "enum": list(QUALITIES), "default": "540p"},
            "aspect_ratio": {"type": "string", "enum": list(ASPECT_RATIOS), "default": "9:16"},
            "seed": {"type": "integer"},
            "generate_audio_switch": {
                "type": "boolean",
                "default": False,
                "description": "Let the model generate its own ambience/SFX. This is NOT "
                               "audio-driven lipsync: an external dialogue track cannot be "
                               "used to drive mouth movement.",
            },
            "image_path": {"type": "string", "description": "Local keyframe for image_to_video."},
            "output_path": {"type": "string"},
            "timeout_seconds": {"type": "number", "default": 900},
            "poll_interval": {"type": "number", "default": 5.0},
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "output_path": {"type": "string"},
            "video_id": {"type": "integer"},
            "remote_url": {"type": "string"},
            "model": {"type": "string"},
        },
    }

    side_effects = ["paid_api_call", "writes video file to output_path"]
    user_visible_verification = ["Watch the clip for identity drift, hands, and 9:16 framing"]

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        per_second = PRICE_CNY_PER_SECOND.get(str(inputs.get("quality") or "540p"))
        if per_second is None:
            return 0.0
        return round(per_second * float(inputs.get("duration") or 5), 4)

    # risk:command-injection reason:"扫描器把方法名 estimate_runtime 中的 Runtime 误判为命令执行；该函数仅返回预估秒数，不含任何进程调用或外部输入拼接" owner:@kevinwanghd reviewed:2026-11-21
    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 180.0

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        result = super().dry_run(inputs)
        result.update(
            {
                "paid_api": True,
                "cost_estimate_confidence": "unknown",
                "cost_estimate_basis": (
                    "PixVerse prices per account plan; PRICE_CNY_PER_SECOND is empty "
                    "until the plan's real unit price is recorded."
                ),
            }
        )
        return result

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            self.check_dependencies()
        except DependencyError as exc:
            return ToolResult(success=False, error=str(exc))

        start = time.time()
        client = PixVerseClient()
        prompt = str(inputs.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(success=False, error="prompt is required")

        operation = str(inputs.get("operation") or "image_to_video")
        model = str(inputs.get("model") or "v6")
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "duration": int(inputs.get("duration") or 5),
            "quality": str(inputs.get("quality") or "540p"),
            "aspect_ratio": str(inputs.get("aspect_ratio") or "9:16"),
        }
        if inputs.get("seed") is not None:
            payload["seed"] = int(inputs["seed"])
        if inputs.get("generate_audio_switch") is not None:
            # Field name verified against the API: `generate_audio` and `audio`
            # are silently ignored, only `generate_audio_switch` is type-checked.
            payload["generate_audio_switch"] = bool(inputs["generate_audio_switch"])

        try:
            if operation == "image_to_video":
                image_path = inputs.get("image_path")
                if not image_path:
                    return ToolResult(success=False, error="image_path is required for image_to_video")
                payload["img_id"] = client.upload_image(Path(str(image_path)))
                video_id = client.image_to_video(payload)
            else:
                video_id = client.text_to_video(payload)

            resp = client.wait_for_video(
                video_id,
                timeout_seconds=float(inputs.get("timeout_seconds") or 900),
                poll_interval=float(inputs.get("poll_interval") or 5.0),
            )
            remote_url = str(resp.get("url") or "")
            if not remote_url:
                raise PixVerseError("download", "successful task carried no video url", resp)

            output_path = Path(str(inputs.get("output_path") or f"pixverse_{video_id}.mp4"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(remote_url, timeout=300) as response:
                output_path.write_bytes(response.read())
            probed = probe_output(output_path)
        except (PixVerseError, TimeoutError, ValueError, OSError) as exc:
            return ToolResult(
                success=False,
                data={"provider": self.provider, "model": model,
                      "error_code": getattr(exc, "code", None)},
                error=f"PixVerse video generation failed: {exc}",
            )

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "model": model,
                "operation": operation,
                "video_id": video_id,
                "remote_url": remote_url,
                "output": str(output_path),
                "output_path": str(output_path),
                "format": "mp4",
                "cost_estimate_confidence": "unknown",
                **probed,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )