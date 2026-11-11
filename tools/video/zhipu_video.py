"""Zhipu (智谱AI) CogVideoX video generation via the BigModel open API.

Speaks the same async contract as every other video model on the platform:

    POST https://open.bigmodel.cn/api/paas/v4/videos/generations
    GET  https://open.bigmodel.cn/api/paas/v4/async-result/{id}

Authentication is a plain bearer token from
https://bigmodel.cn/usercenter/proj-mgmt/apikeys — no signing step.

``cogvideox-flash`` is the free tier of this endpoint: same request shape as the
paid ``cogvideox-2`` / ``cogvideox-3`` models, priced at zero, with no cap on
total calls. Two consequences shape this adapter:

* The free tier allows **one in-flight task per account**. Callers that fan out
  concurrently will get refusals, so ``execute`` waits for each task to reach a
  terminal state before returning and the episode drivers stay sequential.
* ``cogvideox-flash`` is a speed-first model. It is fine for motion tests and
  draft passes; it is not a drop-in for the paid models on identity or period
  fidelity, so treat its output as a candidate to be reviewed, not as final.

Resolution: the free tier's ``size`` enum is ``720x480``, ``1024x1024``,
``1280x960``, ``960x1280``, ``1920x1080``, ``1080x1920``, ``2048x1080``,
``3840x2160``. There is no 16:9 option below 1080p, so a ``resolution`` of
480p/720p/1080p all map to ``1920x1080`` (generation is free, and the episodic
pipelines downscale to 1280x720 at render time). Pass ``size`` to override.
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

_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
_SUBMIT_PATH = "/videos/generations"
_QUERY_PATH = "/async-result/{id}"

# Terminal task states reported by the async-result endpoint.
_STATUS_DONE = "SUCCESS"
_STATUS_FAILED = "FAIL"

# Codes the free tier returns when its shared queue is saturated. The message
# ("该模型当前访问量过大，请您稍后再试") literally instructs a retry, so these are
# treated as transient and re-submitted with backoff instead of failing the run.
_BUSY_CODES = {"1305"}


class _BusyError(RuntimeError):
    """Transient 'model is busy' platform error, safe to retry."""


# The size enum the free tier actually accepts. Advertised in the schema so the
# video selector can route by exact model/size instead of guessing.
_SIZES = (
    "720x480",
    "1024x1024",
    "1280x960",
    "960x1280",
    "1920x1080",
    "1080x1920",
    "2048x1080",
    "3840x2160",
)

# The free tier has no 16:9 step below 1080p, so every nominal tier lands on it.
_RESOLUTION_TO_SIZE = {
    "480p": "1920x1080",
    "720p": "1920x1080",
    "1080p": "1920x1080",
}

# Published list prices (RMB per generation). Flash is the free tier.
_MODEL_PRICE_RMB = {
    "cogvideox-flash": 0.0,
    "cogvideox-2": 0.5,
    "cogvideox-3": 1.0,
}
_RMB_PER_USD = 7.2


class ZhipuVideo(BaseTool):
    """Zhipu CogVideoX text/image-to-video through the BigModel API."""

    name = "zhipu_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "zhipu"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:ZHIPU_API_KEY"]
    install_instructions = (
        "Set ZHIPU_API_KEY to your 智谱 BigModel API key.\n"
        "  Get it at https://bigmodel.cn/usercenter/proj-mgmt/apikeys\n"
        "  Real-name verification is required before the key can call any model, "
        "and cogvideox-flash needs no separate activation."
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "native_audio": False,
        "seed": False,
    }
    best_for = [
        "zero-cost draft passes and motion tests",
        "text-to-video and image-to-video with Chinese prompt understanding",
        "a free tier on the same endpoint as the paid CogVideoX models",
    ]
    not_good_for = [
        "final delivery where period accuracy or identity consistency is graded",
        "concurrent fan-out — the free tier allows one in-flight task per account",
        "offline or air-gapped environments",
    ]
    fallback_tools = ["kling_video", "minimax_video", "seedance_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "maxLength": 512,
                "description": (
                    "Video description. Max 512 characters. Chinese is well "
                    "understood. Positive description only — the endpoint has no "
                    "negative-prompt field."
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
                "enum": ["cogvideox-flash", "cogvideox-2", "cogvideox-3"],
                "default": "cogvideox-flash",
                "description": (
                    "BigModel model code. cogvideox-flash is the free tier; "
                    "cogvideox-2 / cogvideox-3 are the paid models."
                ),
            },
            "image_url": {
                "type": "string",
                "description": (
                    "Reference image as a public URL or a data: base64 URI. "
                    "png/jpeg/jpg, max 5MB. Required for image_to_video."
                ),
            },
            "image_path": {
                "type": "string",
                "description": (
                    "Local reference image; base64-encoded and sent inline. "
                    "Mutually exclusive with image_url."
                ),
            },
            "size": {
                "type": "string",
                "enum": list(_SIZES),
                "description": "Exact output size. Overrides `resolution`.",
            },
            "resolution": {
                "type": "string",
                "enum": ["480p", "720p", "1080p"],
                "description": (
                    "Nominal tier. The free tier has no 16:9 size below 1080p, so "
                    "every tier maps to 1920x1080 unless `size` is given."
                ),
            },
            "fps": {
                "type": "integer",
                "enum": [30, 60],
                "default": 30,
                "description": "Frame rate.",
            },
            "duration": {
                "type": "integer",
                "enum": [5, 10],
                "description": (
                    "Seconds. Only forwarded to the paid models; the free tier "
                    "uses the service default (5s)."
                ),
            },
            "output_path": {
                "type": "string",
                "description": "Destination path for the generated MP4.",
            },
            "poll_interval_seconds": {
                "type": "number",
                "minimum": 2,
                "default": 5.0,
                "description": "Seconds between status polls.",
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
        max_retries=1,
        backoff_seconds=5.0,
        retryable_errors=["rate_limit", "timeout"],
    )
    idempotency_key_fields = ["prompt", "operation", "model", "image_url", "size", "fps"]
    side_effects = [
        "writes video file to output_path",
        "calls the Zhipu BigModel API (bearer-token submit + poll + download)",
        "spends the account's free-tier quota (no monetary cost on cogvideox-flash)",
    ]
    user_visible_verification = [
        "Watch the clip for prompt adherence — the free tier is speed-first, so "
        "check period props, faces and motion before accepting it",
        "Confirm the download is a playable MP4 (probe reports width/height/duration)",
    ]

    _MODELS = {key: {} for key in _MODEL_PRICE_RMB}

    # ------------------------------------------------------------------
    # Credential helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _api_key() -> str | None:
        val = os.environ.get("ZHIPU_API_KEY", "")
        if val and not val.strip().startswith("#"):
            return val.strip()
        return None

    # ------------------------------------------------------------------
    # Tool contract methods
    # ------------------------------------------------------------------

    def get_status(self) -> ToolStatus:
        if self._api_key():
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """Return list price in USD. The free tier is genuinely zero."""
        model = inputs.get("model") or "cogvideox-flash"
        rmb = _MODEL_PRICE_RMB.get(model, 0.0)
        return round(rmb / _RMB_PER_USD, 4)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        """Flash is speed-first; observed cloud queues run 60-180s."""
        return 180.0

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="ZHIPU_API_KEY not set. " + self.install_instructions,
            )

        prompt = (inputs.get("prompt") or "").strip()
        operation = inputs.get("operation", "text_to_video")
        if operation == "image_to_video" and not (
            inputs.get("image_url") or inputs.get("image_path")
        ):
            return ToolResult(
                success=False,
                error="image_to_video requires image_url or image_path.",
            )
        if inputs.get("image_url") and inputs.get("image_path"):
            return ToolResult(
                success=False,
                error="Provide only one of image_url or image_path, not both.",
            )
        if not prompt and not inputs.get("image_url") and not inputs.get("image_path"):
            return ToolResult(
                success=False,
                error="Provide a prompt, an image, or both.",
            )
        if len(prompt) > 512:
            return ToolResult(
                success=False,
                error=f"prompt is {len(prompt)} characters; the endpoint allows 512.",
            )

        start = time.time()
        try:
            result = self._generate(inputs, api_key=api_key)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"Zhipu video generation failed: {self._safe_error(exc)}",
            )

        result.duration_seconds = round(time.time() - start, 2)
        return result

    # ------------------------------------------------------------------
    # Generation pipeline
    # ------------------------------------------------------------------

    def _generate(self, inputs: dict[str, Any], *, api_key: str) -> ToolResult:
        import requests

        from tools.video._shared import probe_output

        payload = self._build_payload(inputs)
        model = payload["model"]
        task_id = self._submit_task(payload, api_key=api_key)
        video_url = self._poll_task(
            task_id,
            api_key=api_key,
            poll_interval=float(inputs.get("poll_interval_seconds", 5.0)),
            timeout_seconds=int(inputs.get("timeout_seconds", 900)),
        )

        download = requests.get(video_url, timeout=180)
        download.raise_for_status()

        output_path = Path(
            inputs.get("output_path") or f"zhipu_{model}_{task_id}.mp4"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(download.content)

        probed = probe_output(output_path)
        return ToolResult(
            success=True,
            data={
                "provider": "zhipu",
                "route": "bigmodel",
                "model": model,
                "prompt": inputs.get("prompt", ""),
                "operation": inputs.get("operation", "text_to_video"),
                "size": payload.get("size"),
                "fps": payload.get("fps"),
                "task_id": task_id,
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

    def _build_payload(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Build the request body.

        Only fields the free tier documents are sent: ``model``, ``prompt``,
        ``image_url``, ``size``, ``fps``. ``duration`` is forwarded for the paid
        models, whose schemas list it; sending it to the free tier risks a
        validation error for no benefit, since 5s is the service default anyway.
        """
        model = inputs.get("model") or "cogvideox-flash"

        payload: dict[str, Any] = {"model": model, "prompt": inputs.get("prompt", "")}
        payload["size"] = self._resolve_size(inputs)
        payload["fps"] = int(inputs.get("fps", 30))

        if inputs.get("image_url"):
            payload["image_url"] = inputs["image_url"]
        elif inputs.get("image_path"):
            payload["image_url"] = self._encode_image(inputs["image_path"])

        if model != "cogvideox-flash" and inputs.get("duration"):
            payload["duration"] = int(inputs["duration"])

        return payload

    @staticmethod
    def _resolve_size(inputs: dict[str, Any]) -> str:
        explicit = inputs.get("size")
        if explicit:
            if explicit not in _SIZES:
                raise ValueError(
                    f"Unsupported size {explicit!r}; choose one of {', '.join(_SIZES)}."
                )
            return explicit
        return _RESOLUTION_TO_SIZE.get(str(inputs.get("resolution") or ""), "1920x1080")

    @staticmethod
    def _encode_image(path: str) -> str:
        """Read a local image and return a data: URI ready for ``image_url``."""
        image_path = Path(path)
        if not image_path.is_file():
            raise FileNotFoundError(f"Image not found: {path}")

        raw = image_path.read_bytes()
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError(
                f"Image too large ({len(raw)} bytes). The endpoint caps images at 5MB."
            )
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

    def _submit_task(self, payload: dict[str, Any], *, api_key: str) -> str:
        """Submit a generation task and return its id.

        The free tier shares one queue with every other flash caller, so a busy
        code is expected rather than exceptional: re-submit with backoff a few
        times before giving up.
        """
        import requests

        attempts = 6
        delay = 10.0
        last_busy: Exception | None = None

        for attempt in range(1, attempts + 1):
            resp = requests.post(
                f"{_BASE_URL}{_SUBMIT_PATH}",
                json=payload,
                headers=self._auth_headers(api_key),
                timeout=60,
            )
            data = self._json_or_raise(resp)
            try:
                self._check_response(data)
            except _BusyError as exc:
                last_busy = exc
                if attempt == attempts:
                    break
                print(
                    f"[zhipu] 模型繁忙（第 {attempt}/{attempts} 次），"
                    f"{delay:.0f}s 后重试"
                )
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue

            task_id = data.get("request_id") or data.get("id")
            if not task_id:
                raise RuntimeError(f"submit returned no task id: {data}")
            return str(task_id)

        raise last_busy if last_busy else RuntimeError("submit failed with no payload")

    def _poll_task(
        self,
        task_id: str,
        *,
        api_key: str,
        poll_interval: float,
        timeout_seconds: int,
    ) -> str:
        """Poll the async-result endpoint until the task yields a video URL."""
        import requests

        url = f"{_BASE_URL}{_QUERY_PATH.format(id=task_id)}"
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            time.sleep(poll_interval)

            resp = requests.get(url, headers=self._auth_headers(api_key), timeout=60)
            data = self._json_or_raise(resp)
            self._check_response(data)

            status = str(data.get("task_status", "")).upper()
            if status == _STATUS_DONE:
                results = data.get("video_result") or []
                video_url = results[0].get("url") if results else None
                if not video_url:
                    raise RuntimeError(
                        f"task {task_id} succeeded but returned no video url: {data}"
                    )
                return str(video_url)
            if status == _STATUS_FAILED:
                raise RuntimeError(f"task {task_id} failed upstream: {data}")

            # PROCESSING (or an empty status while the row is still being written)
            if status and status != "PROCESSING":
                raise RuntimeError(f"task {task_id} returned unknown status: {status}")

        raise TimeoutError(
            f"task {task_id} did not finish within {timeout_seconds}s"
        )

    # ------------------------------------------------------------------
    # Error handling helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Redact the API key if it ever reaches an exception message."""
        msg = str(exc)
        val = os.environ.get("ZHIPU_API_KEY", "")
        if val:
            msg = msg.replace(val, "[redacted]")
        return msg

    @staticmethod
    def _json_or_raise(response: Any) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"non-JSON response from BigModel API: HTTP {response.status_code}"
            ) from exc

    @staticmethod
    def _check_response(payload: dict[str, Any]) -> None:
        """Raise on the platform's top-level ``error`` envelope.

        Busy codes raise :class:`_BusyError` so the caller can back off and
        retry; every other code is a hard failure.
        """
        error = payload.get("error")
        if error:
            code = error.get("code", error.get("type", "unknown"))
            message = error.get("message", "unknown error")
            text = f"BigModel API error: code={code}, message={message}"
            if str(code) in _BUSY_CODES:
                raise _BusyError(text)
            raise RuntimeError(text)
