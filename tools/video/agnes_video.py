"""Agnes AI video generation (`agnes-video-2.5-flash` / `agnes-video-v2.0`).

Agnes AI is an OpenAI-compatible gateway. Authentication is a plain bearer
token and the same create endpoint serves every video model:

    POST https://apihub.agnes-ai.com/v1/videos
    GET  https://apihub.agnes-ai.com/agnesapi?video_id=<id>

Two video models are exposed and they do **not** share a request shape:

* ``agnes-video-2.5-flash`` — OpenAI *Videos*-style: ``seconds`` as a string,
  a ``mode`` (text / keyframe / reference), the fixed ``size: "720P"`` and an
  ``aspect_ratio``. It is on a promotional ``$0``/second trial price and is the
  cheaper, simpler target.
* ``agnes-video-v2.0`` — the older task API: explicit ``width``/``height``,
  ``num_frames`` (must be ``8n + 1`` and <= 441) and ``frame_rate``.

Two details of the retrieval contract are easy to get wrong:

* The current polling form is ``GET /agnesapi?video_id=<id>`` — *not* the
  legacy ``GET /v1/videos/{task_id}``. The 2.5 family additionally needs
  ``model_name``; a bare ``video_id`` query is only valid for ``mode: "text"``.
* The finished URL lives in the top-level ``url`` field for 2.5 Flash, while
  the v2.0 result carries it as ``video_url``. Both are read here, plus
  ``remixed_from_video_id``, which older write-ups document instead.

Regional hosts: keys are issued **per site** and are not interchangeable, so the
host must match the site the key came from.

* International — ``https://apihub.agnes-ai.com`` (the documented one).
* Mainland — ``https://api.agnes-ai.cn``. Note this is *not* the
  ``apihub.agnes-ai.cn`` host some write-ups quote; that host rejects mainland
  keys with ``401 Invalid token``. The mainland hub also answers create with
  ``503 {"code": "video_queue_full"}`` when its queue is full, which is what
  the busy retry below is for.

Set ``AGNES_BASE_URL`` or pass ``base_url`` to switch.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any

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

_MODEL_FLASH = "agnes-video-2.5-flash"
_MODEL_V2 = "agnes-video-v2.0"

_DEFAULT_HOST = "https://apihub.agnes-ai.com"
_CREATE_PATH = "/v1/videos"
_RESULT_PATH = "/agnesapi"

_STATUS_DONE = "completed"
_STATUS_FAILED = "failed"

# 2.5 Flash accepts only the literal "720P"; the pixel size comes from
# `aspect_ratio` (16:9 lands on 1280x704, not 1280x720).
_FLASH_SIZE = "720P"
_FLASH_ASPECT_RATIO_TO_SIZE = {
    "21:9": "1680x720",
    "16:9": "1280x704",
    "4:3": "960x720",
    "1:1": "720x720",
    "3:4": "720x960",
    "9:16": "720x1280",
}

# v2.0 defaults: 1152x768 at 121 frames / 24fps is the documented baseline
# (about 5 seconds). num_frames must satisfy 8n + 1 and stay <= 441.
_V2_DEFAULT_WIDTH = 1152
_V2_DEFAULT_HEIGHT = 768
_V2_DEFAULT_NUM_FRAMES = 121
_V2_DEFAULT_FRAME_RATE = 24
_V2_MAX_NUM_FRAMES = 441

# Seconds accepted by the 2.5 family, and its default.
_FLASH_MIN_SECONDS = 4
_FLASH_MAX_SECONDS = 12
_FLASH_DEFAULT_SECONDS = 5

# List prices per output second (USD). Both models are on a free/trial
# promotion at the time of writing, so estimate_cost reports zero.
_LIST_PRICE_PER_SECOND = {_MODEL_FLASH: 0.025, _MODEL_V2: 0.005}

# Transient HTTP statuses: the catalogue tells callers to retry these with
# backoff (503 = "service busy", 429 = rate limit).
_BUSY_HTTP = {429, 500, 502, 503, 520}


class _BusyError(RuntimeError):
    """Transient 'gateway busy / rate limited' error, safe to retry."""


class AgnesVideo(BaseTool):
    """Agnes AI text-to-video and image-to-video through the apihub gateway."""

    name = "agnes_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "agnes"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:AGNES_API_KEY"]
    install_instructions = (
        "Set AGNES_API_KEY to your Agnes AI API key.\n"
        "  Get it at https://platform.agnes-ai.com/\n"
        "  Keys are issued per site and are NOT interchangeable, so also set "
        "AGNES_BASE_URL to match: https://api.agnes-ai.cn for the mainland site, "
        "or https://apihub.agnes-ai.com for the international site "
        "(the default)."
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video", "reference_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "native_audio": False,
        "seed": True,
        "negative_prompt": True,
    }
    best_for = [
        "a zero-cost draft pass while the 2.5 Flash trial price is $0/second",
        "720p 16:9 clips with a Chinese-friendly prompt",
        "keyframe and image-reference conditioning",
    ]
    not_good_for = [
        "final delivery above 720p — 2.5 Flash is capped at 720P",
        "offline or air-gapped environments",
        "fan-out: the free plan allows 1 executable video request per minute",
    ]
    fallback_tools = ["zhipu_video", "kling_video", "minimax_video", "seedance_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Video description. Describe subject, action, scene, camera "
                    "movement, lighting and style."
                ),
            },
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video"],
                "default": "text_to_video",
                "description": "Generation mode.",
            },
            "model": {
                "type": "string",
                "enum": [_MODEL_FLASH, _MODEL_V2],
                "default": _MODEL_FLASH,
                "description": (
                    "agnes-video-2.5-flash is 720P-only and currently on a free "
                    "trial; agnes-video-v2.0 takes explicit width/height/frames."
                ),
            },
            "duration": {
                "type": "integer",
                "description": (
                    "Nominal clip seconds. 2.5 Flash accepts 4-12 (default 5); "
                    "v2.0 converts it to num_frames at the chosen frame_rate."
                ),
            },
            "seconds": {
                "type": "string",
                "description": "2.5 Flash duration as the API expects it: \"4\"-\"12\".",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": list(_FLASH_ASPECT_RATIO_TO_SIZE),
                "default": "16:9",
                "description": "2.5 Flash output shape (size is always 720P).",
            },
            "size": {
                "type": "string",
                "description": "2.5 Flash only accepts the string \"720P\".",
            },
            "width": {
                "type": "integer",
                "description": "v2.0 output width in pixels. Default 1152.",
            },
            "height": {
                "type": "integer",
                "description": "v2.0 output height in pixels. Default 768.",
            },
            "num_frames": {
                "type": "integer",
                "description": (
                    "v2.0 frame count; must be 8n + 1 (81, 121, 161 ...) and <= 441."
                ),
            },
            "frame_rate": {
                "type": "integer",
                "description": "v2.0 frames per second, 1-60. Default 24.",
            },
            "image_url": {
                "type": "string",
                "description": (
                    "Reference image as a public URL. Required for image_to_video; "
                    "sent as `image` for v2.0 and as first_frame for 2.5 Flash."
                ),
            },
            "image_path": {
                "type": "string",
                "description": (
                    "Local reference image; base64-encoded into a data URI and "
                    "sent inline. Mutually exclusive with image_url."
                ),
            },
            "negative_prompt": {
                "type": "string",
                "description": "What to avoid (v2.0 only).",
            },
            "seed": {
                "type": "integer",
                "description": "Random seed for a reproducible result.",
            },
            "output_path": {
                "type": "string",
                "description": "Destination path for the generated MP4.",
            },
            "base_url": {
                "type": "string",
                "description": (
                    "Override the gateway host, e.g. https://api.agnes-ai.cn. "
                    "Defaults to AGNES_BASE_URL or the international host."
                ),
            },
            "poll_interval_seconds": {
                "type": "number",
                "minimum": 1,
                "default": 10.0,
                "description": (
                    "Seconds between status polls. The docs suggest 1-2s, but the "
                    "live gateway rate-limits harder than that ('查询过于频繁'), so "
                    "10s is the safe default."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 60,
                "default": 900,
                "description": "Maximum seconds to wait for generation.",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True,
    )
    retry_policy = RetryPolicy(
        max_retries=2,
        backoff_seconds=10.0,
        retryable_errors=["rate_limit", "timeout", "server_error"],
    )
    idempotency_key_fields = ["prompt", "model", "operation", "aspect_ratio", "duration"]
    side_effects = [
        "writes video file to output_path",
        "calls the Agnes AI apihub gateway (create task + poll + download)",
        "spends account quota; the 2.5 Flash trial price is currently $0/second",
    ]
    user_visible_verification = [
        "Watch the clip for prompt adherence and motion before accepting it",
        "Confirm the download is a playable MP4 (probe reports width/height/duration)",
    ]

    _MODELS = {_MODEL_FLASH: {}, _MODEL_V2: {}}

    # ------------------------------------------------------------------
    # Credential / host helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _api_key() -> str | None:
        val = os.environ.get("AGNES_API_KEY", "")
        if val and not val.strip().startswith("#"):
            return val.strip()
        return None

    @staticmethod
    def _host(inputs: dict[str, Any]) -> str:
        raw = (
            inputs.get("base_url")
            or os.environ.get("AGNES_BASE_URL", "")
            or _DEFAULT_HOST
        )
        return str(raw).rstrip("/")

    # ------------------------------------------------------------------
    # Tool contract methods
    # ------------------------------------------------------------------

    def get_status(self) -> ToolStatus:
        if self._api_key():
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """Return the USD list price. Both models are on a free trial today."""
        model = inputs.get("model") or _MODEL_FLASH
        seconds = self._resolve_seconds(inputs) if model == _MODEL_FLASH else None
        if model == _MODEL_FLASH:
            # Promotional $0/second at the time of writing.
            return 0.0 if seconds else 0.0
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        """Observed gateway latency is roughly 30-120s for a 5s clip."""
        return 120.0

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="AGNES_API_KEY not set. " + self.install_instructions,
            )

        prompt = (inputs.get("prompt") or "").strip()
        operation = inputs.get("operation", "text_to_video")
        has_image = bool(inputs.get("image_url") or inputs.get("image_path"))
        if operation == "image_to_video" and not has_image:
            return ToolResult(
                success=False,
                error="image_to_video requires image_url or image_path.",
            )
        if inputs.get("image_url") and inputs.get("image_path"):
            return ToolResult(
                success=False,
                error="Provide only one of image_url or image_path, not both.",
            )
        if not prompt and not has_image:
            return ToolResult(success=False, error="Provide a prompt, an image, or both.")

        model = inputs.get("model") or _MODEL_FLASH
        if model not in self._MODELS:
            return ToolResult(
                success=False,
                error=f"Unknown model {model!r}; choose {_MODEL_FLASH} or {_MODEL_V2}.",
            )

        try:
            payload = self._build_payload(inputs, model=model)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        start = time.time()
        try:
            result = self._generate(inputs, model=model, payload=payload, api_key=api_key)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"Agnes video generation failed: {self._safe_error(exc)}",
            )

        result.duration_seconds = round(time.time() - start, 2)
        return result

    # ------------------------------------------------------------------
    # Generation pipeline
    # ------------------------------------------------------------------

    def _generate(
        self,
        inputs: dict[str, Any],
        *,
        model: str,
        payload: dict[str, Any],
        api_key: str,
    ) -> ToolResult:
        import requests

        from tools.video._shared import probe_output

        host = self._host(inputs)
        video_id = self._create_task(host, payload, api_key=api_key)
        video_url = self._poll_task(
            host,
            video_id,
            model=model,
            api_key=api_key,
            poll_interval=float(inputs.get("poll_interval_seconds", 10.0)),
            timeout_seconds=int(inputs.get("timeout_seconds", 900)),
        )

        download = requests.get(video_url, timeout=180)
        download.raise_for_status()

        output_path = Path(
            inputs.get("output_path") or f"agnes_{model}_{video_id}.mp4"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(download.content)

        probed = probe_output(output_path)
        return ToolResult(
            success=True,
            data={
                "provider": "agnes",
                "route": "apihub",
                "model": model,
                "prompt": inputs.get("prompt", ""),
                "operation": inputs.get("operation", "text_to_video"),
                "video_id": video_id,
                "video_url": video_url,
                "output": str(output_path),
                "format": "mp4",
                **probed,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            model=model,
        )

    # ------------------------------------------------------------------
    # Payload construction
    # ------------------------------------------------------------------

    def _build_payload(self, inputs: dict[str, Any], *, model: str) -> dict[str, Any]:
        """Build the create-task body for the selected model."""
        prompt = inputs.get("prompt", "")
        image = inputs.get("image_url") or (
            self._encode_image(inputs["image_path"]) if inputs.get("image_path") else None
        )

        if model == _MODEL_FLASH:
            return self._flash_payload(inputs, prompt=prompt, image=image)
        return self._v2_payload(inputs, prompt=prompt, image=image)

    def _flash_payload(
        self, inputs: dict[str, Any], *, prompt: str, image: str | None
    ) -> dict[str, Any]:
        size = str(inputs.get("size") or _FLASH_SIZE)
        if size != _FLASH_SIZE:
            raise ValueError(
                f'agnes-video-2.5-flash only accepts size "{_FLASH_SIZE}", got {size!r}.'
            )

        aspect = str(inputs.get("aspect_ratio") or "16:9")
        if aspect not in _FLASH_ASPECT_RATIO_TO_SIZE:
            raise ValueError(
                f"Unsupported aspect_ratio {aspect!r}; choose one of "
                f"{', '.join(_FLASH_ASPECT_RATIO_TO_SIZE)}."
            )

        payload: dict[str, Any] = {
            "model": _MODEL_FLASH,
            "prompt": prompt,
            "seconds": self._resolve_seconds(inputs),
            "size": _FLASH_SIZE,
            "aspect_ratio": aspect,
            "n": 1,
        }
        # A single reference image rides in keyframe mode as the first frame.
        if image:
            payload["mode"] = "keyframe"
            payload["first_frame"] = image
        else:
            payload["mode"] = "text"

        if inputs.get("seed") is not None:
            payload["seed"] = int(inputs["seed"])
        return payload

    def _v2_payload(
        self, inputs: dict[str, Any], *, prompt: str, image: str | None
    ) -> dict[str, Any]:
        frame_rate = int(inputs.get("frame_rate") or _V2_DEFAULT_FRAME_RATE)
        if not 1 <= frame_rate <= 60:
            raise ValueError(f"frame_rate must be 1-60, got {frame_rate}.")

        num_frames = inputs.get("num_frames")
        if num_frames is None:
            target = inputs.get("duration")
            if target:
                num_frames = self._snap_num_frames(int(target) * frame_rate)
            else:
                num_frames = _V2_DEFAULT_NUM_FRAMES
        num_frames = self._snap_num_frames(int(num_frames))

        payload: dict[str, Any] = {
            "model": _MODEL_V2,
            "prompt": prompt,
            "width": int(inputs.get("width") or _V2_DEFAULT_WIDTH),
            "height": int(inputs.get("height") or _V2_DEFAULT_HEIGHT),
            "num_frames": num_frames,
            "frame_rate": frame_rate,
        }
        if image:
            payload["image"] = image
        if inputs.get("negative_prompt"):
            payload["negative_prompt"] = inputs["negative_prompt"]
        if inputs.get("seed") is not None:
            payload["seed"] = int(inputs["seed"])
        return payload

    def _resolve_seconds(self, inputs: dict[str, Any]) -> str:
        """Return the 2.5-family `seconds` string, clamped to 4-12."""
        raw = inputs.get("seconds")
        if raw is None:
            raw = inputs.get("duration", _FLASH_DEFAULT_SECONDS)
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = _FLASH_DEFAULT_SECONDS
        value = max(_FLASH_MIN_SECONDS, min(_FLASH_MAX_SECONDS, value))
        return str(value)

    @staticmethod
    def _snap_num_frames(value: int) -> int:
        """Snap to the required 8n + 1 form, clamped to the API's 1..441 range."""
        if value <= 1:
            return 1
        steps = round((value - 1) / 8)
        frames = 8 * steps + 1
        return max(1, min(_V2_MAX_NUM_FRAMES, frames))

    @staticmethod
    def _encode_image(path: str) -> str:
        """Read a local image and return a data: URI."""
        image_path = Path(path)
        if not image_path.is_file():
            raise FileNotFoundError(f"Image not found: {path}")
        raw = image_path.read_bytes()
        suffix = image_path.suffix.lower().lstrip(".") or "png"
        return f"data:image/{suffix};base64,{base64.b64encode(raw).decode('ascii')}"

    # ------------------------------------------------------------------
    # API communication
    # ------------------------------------------------------------------

    @staticmethod
    def _auth_headers(api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _create_task(
        self, host: str, payload: dict[str, Any], *, api_key: str
    ) -> str:
        """Create a generation task and return the video_id to poll with."""
        import requests

        attempts = 6
        delay = 10.0
        last_busy: Exception | None = None

        for attempt in range(1, attempts + 1):
            resp = requests.post(
                f"{host}{_CREATE_PATH}",
                json=payload,
                headers=self._auth_headers(api_key),
                timeout=60,
            )
            try:
                data = self._json_or_raise(resp)
                self._check_response(resp, data)
            except _BusyError as exc:
                last_busy = exc
                if attempt == attempts:
                    break
                print(
                    f"[agnes] 网关繁忙（第 {attempt}/{attempts} 次），"
                    f"{delay:.0f}s 后重试"
                )
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue

            video_id = data.get("video_id") or data.get("id") or data.get("task_id")
            if not video_id:
                raise RuntimeError(f"create returned no video_id: {data}")
            return str(video_id)

        raise last_busy if last_busy else RuntimeError("create failed with no payload")

    def _poll_task(
        self,
        host: str,
        video_id: str,
        *,
        model: str,
        api_key: str,
        poll_interval: float,
        timeout_seconds: int,
    ) -> str:
        """Poll the result endpoint until the task yields a video URL."""
        import requests

        params = {"video_id": video_id}
        # Only the 2.5 family needs model_name; v2.0 rejects the extra param.
        if model == _MODEL_FLASH:
            params["model_name"] = model

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            time.sleep(poll_interval)
            resp = requests.get(
                f"{host}{_RESULT_PATH}",
                params=params,
                headers=self._auth_headers(api_key),
                timeout=60,
            )
            try:
                data = self._json_or_raise(resp)
                self._check_response(resp, data)
            except _BusyError as exc:
                # The result endpoint rate-limits hard ("查询过于频繁"). The task
                # is still running upstream, so back off instead of failing it.
                print(f"[agnes] 轮询被限流，退避后继续：{exc}")
                time.sleep(min(poll_interval * 3, 60.0))
                continue

            status = str(data.get("status", "")).lower()
            if status == _STATUS_DONE:
                url = self._extract_url(data)
                if not url:
                    raise RuntimeError(
                        f"task {video_id} completed but returned no video url: {data}"
                    )
                return url
            if status == _STATUS_FAILED:
                raise RuntimeError(
                    f"task {video_id} failed upstream: "
                    f"{data.get('error') or data}"
                )
            # queued / in_progress — keep waiting.

        raise TimeoutError(f"task {video_id} did not finish within {timeout_seconds}s")

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_url(data: dict[str, Any]) -> str | None:
        """Pull the finished video URL out of whichever field this model uses.

        2.5 Flash returns it as ``url``; v2.0 as ``video_url``; older write-ups
        document ``remixed_from_video_id``. Check all three so a contract change
        does not silently break the download.
        """
        for key in ("url", "video_url", "remixed_from_video_id"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        return None

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Redact the API key if it ever reaches an exception message."""
        msg = str(exc)
        val = os.environ.get("AGNES_API_KEY", "")
        if val:
            msg = msg.replace(val, "[redacted]")
        return msg

    @staticmethod
    def _json_or_raise(response: Any) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"non-JSON response from Agnes gateway: HTTP {response.status_code}"
            ) from exc

    @staticmethod
    def _check_response(response: Any, payload: dict[str, Any]) -> None:
        """Raise on the gateway's error envelopes.

        Four shapes appear in the wild: the OpenAI ``{"error": {...}}``
        envelope, the Flash validation ``{"detail": "..."}`` message, the
        mainland hub's ``{"code": ..., "message": ...}`` envelope, and a bare
        HTTP status. Busy statuses raise :class:`_BusyError` so the caller can
        back off and retry; everything else is a hard failure.
        """
        status = getattr(response, "status_code", 200)
        error = payload.get("error")
        detail = payload.get("detail")
        code_field = payload.get("code")
        message_field = payload.get("message")

        if not error and not detail and not (code_field and message_field):
            if status < 400:
                return

        if isinstance(error, dict):
            message = error.get("message", "unknown error")
            code = error.get("code") or error.get("type") or status
        elif error:
            message = str(error)
            code = status
        elif detail:
            message = str(detail)
            code = status
        elif code_field and message_field:
            # Mainland hub envelope, e.g. {"code": "video_queue_full", "message": "..."}.
            message = str(message_field)
            code = code_field
        else:
            message = f"HTTP {status}"
            code = status

        text = f"Agnes API error: code={code}, message={message}"
        if status in _BUSY_HTTP or str(code) in {str(s) for s in _BUSY_HTTP}:
            raise _BusyError(text)
        raise RuntimeError(text)
