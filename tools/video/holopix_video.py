"""Generate video through a Holopix membership (holopix.cn).

Holopix exposes no public API: the web app talks to ``/pino_service_api``
with a session token issued at login. The modern app submits its Seedance
runs over a websocket - ``wss://holopix.cn/pino_service_api/websocket/<clientId>``
- sending one encrypted frame and streaming status frames back on the same
socket. This tool speaks that contract with the account's own token, so an
existing membership can supply video generation to OpenMontage pipelines.

Every call spends the account's compute points (算力). The token expires;
refresh it with ``python -m tools._holopix.client login`` or ``harvest``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ResumeSupport,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

# Model keys the web app offers for the AI-video workspace.
KNOWN_MODEL_KEYS = (
    "seedance-2-0",
    "seedance-mini",
)

# Aliases other OpenMontage tools use for the same family. They are advertised
# so the routing layer can match a requested model to this tool; the server
# only ever receives the canonical key.
MODEL_ALIASES = {
    "seedance": "seedance-2-0",
    "seedance2": "seedance-2-0",
    "seedance-2": "seedance-2-0",
}

_DEFAULT_MODEL = "seedance-2-0"
_DEFAULT_DURATION = 5
_DEFAULT_RESOLUTION = "480p"
_DEFAULT_RATIO = "16:9"

# Ratios the app's own parameter builder uses.
_ALLOWED_RATIOS = ["adaptive", "16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]

# Mode -> the app's mode name, per operation.
_MODE_BY_OPERATION = {
    "text_to_video": "text-to-video",
    "image_to_video": "image-to-video",
    "first_last_frame_to_video": "start-end",
}

# How long to wait between attempts while the account is busy with another
# generation. The web app shows a toast and makes the user wait.
_BUSY_POLL_SECONDS = 20.0


class HolopixVideo(BaseTool):
    """Generate video with a Holopix membership through Holopix's web API."""

    name = "holopix_video"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "holopix"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:HOLOPIX_TOKEN"]
    install_instructions = (
        "Holopix has no public API; it authenticates with a session token that "
        "belongs to one account. Get one with:\n"
        "  python -m tools._holopix.client login --phone <your phone>\n"
        "then re-run with --code <sms code> to store HOLOPIX_TOKEN in .env. "
        "Every generation spends that account's compute points."
    )
    agent_skills = ["holopix-video"]

    capabilities = [
        "text_to_video",
        "image_to_video",
        "first_last_frame_to_video",
        "task_create",
        "task_query",
    ]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "first_last_frame_to_video": True,
        "multiple_reference_images": False,
        "native_audio": True,
        "aspect_ratio": True,
        "resolution": True,
        "duration": True,
    }
    best_for = [
        "a 永久会员 (permanent membership) that already covers Holopix's video models",
        "Chinese-language prompts through Holopix's Seedance-backed video models",
        "short inserts that OpenMontage can stitch and compose",
    ]
    not_good_for = [
        "offline generation",
        "workloads that need a documented, stable public API contract",
        "accounts without compute points",
    ]
    fallback_tools = ["molica_video", "seedance_ark", "wan_video", "veo_video"]

    input_schema = {
        "type": "object",
        "anyOf": [{"required": ["prompt"]}, {"required": ["client_id"]}],
        "properties": {
            "prompt": {"type": "string", "description": "Video prompt."},
            "client_id": {
                "type": "string",
                "description": (
                    "Collect a run that was already submitted and paid for. "
                    "Pass the client_id from a previous (timed out) result; "
                    "no new generation is submitted and nothing is charged."
                ),
            },
            "task_id": {"type": "string", "description": "Alias for client_id."},
            "artwork_id": {
                "type": "integer",
                "description": "Artwork that holds the task, used as a fallback when collecting.",
            },
            "operation": {
                "type": "string",
                "enum": list(_MODE_BY_OPERATION),
                "default": "text_to_video",
            },
            "model": {
                "type": "string",
                "enum": list(KNOWN_MODEL_KEYS) + list(MODEL_ALIASES),
                "default": _DEFAULT_MODEL,
                "description": "Holopix video model key.",
            },
            "duration": {"type": "integer", "default": _DEFAULT_DURATION},
            "aspect_ratio": {"type": "string", "enum": _ALLOWED_RATIOS, "default": _DEFAULT_RATIO},
            "resolution": {"type": "string", "default": _DEFAULT_RESOLUTION},
            "image_url": {"type": "string", "description": "Public URL of the first frame."},
            "last_image_url": {"type": "string", "description": "Public URL of the last frame."},
            "camera_fixed": {"type": "boolean", "default": False},
            "output_path": {"type": "string"},
            "poll_interval_seconds": {"type": "number", "default": 8.0},
            "timeout_seconds": {"type": "number", "default": 1200.0},
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "output_path": {"type": "string"},
            "video_url": {"type": "string"},
            "client_id": {"type": "string"},
            "duration": {"type": "number"},
            "model": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(network_required=True)

    retry_policy = RetryPolicy(max_retries=1, backoff_seconds=10.0, retryable_errors=["服务繁忙"])

    def get_status(self) -> ToolStatus:
        from tools._holopix.client import current_token

        if not current_token():
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    side_effects = [
        "Spends compute points (算力) on the Holopix account tied to HOLOPIX_TOKEN",
        "Submits a billable generation task to the Holopix server",
    ]
    user_visible_verification = [
        "The finished mp4 lands at output_path",
        "The account's compute balance drops by the run's cost",
    ]

    # -- payload ----------------------------------------------------------

    @staticmethod
    def build_message(
        client,
        *,
        prompt: str,
        user_id: Any,
        artwork_id: Any,
        duration: int,
        resolution: str,
        ratio: str,
        image_url: str,
        camera_fixed: bool,
        model: str,
        operation: str = "text_to_video",
        last_image_url: str = "",
        unique_id: str = "",
        client_id: Optional[str] = None,
    ) -> dict:
        """Build the submit frame for a video run.

        This mirrors the app's own ``mJe()`` builder: the frame carries the
        model, the mode and the module id the server dispatches on.
        """
        from tools._holopix.client import seedance_module_id, seedance_param

        mode = _MODE_BY_OPERATION.get(operation, "text-to-video")
        module = seedance_module_id(model, mode)
        if mode == "start-end":
            image_list = [url for url in (image_url, last_image_url) if url]
        else:
            image_list = [image_url] if image_url else []
        return seedance_param(
            prompt=prompt,
            user_id=int(user_id or 0),
            unique_id=unique_id,
            artwork_id=int(artwork_id or 0),
            module=module,
            model=model,
            mode=mode,
            quality=str(resolution),
            ratio=str(ratio),
            duration=int(duration),
            image_list=image_list,
            first_image_url=image_url,
            last_image_url=last_image_url,
            client_id=client_id,
        )

    # -- execution --------------------------------------------------------

    def execute(self, inputs: dict) -> ToolResult:
        from tools._holopix.client import (
            HolopixAuthError,
            HolopixBusyError,
            HolopixClient,
            HolopixError,
            HolopixUpstreamError,
        )

        prompt = str(inputs.get("prompt") or "").strip()
        resume_client_id = str(inputs.get("client_id") or inputs.get("task_id") or "").strip()
        if not prompt and not resume_client_id:
            return ToolResult(success=False, error="prompt is required")

        client = HolopixClient()
        if not client.token:
            return ToolResult(
                success=False,
                error="HOLOPIX_TOKEN is not set. " + self.install_instructions,
            )

        duration = int(inputs.get("duration") or _DEFAULT_DURATION)
        resolution = str(inputs.get("resolution") or _DEFAULT_RESOLUTION)
        ratio = str(inputs.get("aspect_ratio") or _DEFAULT_RATIO)
        model = str(inputs.get("model") or _DEFAULT_MODEL)
        from tools._holopix.client import resolve_seedance_model

        model = resolve_seedance_model(model)
        image_url = str(inputs.get("image_url") or "")
        last_image_url = str(inputs.get("last_image_url") or "")
        operation = str(
            inputs.get("operation")
            or ("image_to_video" if image_url else "text_to_video")
        )
        poll_interval = float(inputs.get("poll_interval_seconds", 8.0))
        timeout = float(inputs.get("timeout_seconds", 1200.0))
        output_path = Path(inputs.get("output_path") or "holopix_video.mp4")

        started = time.time()
        artwork_id = int(inputs.get("artwork_id") or 0)
        client_id = resume_client_id
        try:
            if resume_client_id:
                # Collect a run that has already been paid for. Nothing is
                # submitted, so nothing is charged.
                result = self._await_video(
                    client,
                    artwork_id=int(inputs.get("artwork_id") or 0),
                    client_id=resume_client_id,
                    prompt=prompt,
                    poll_interval=poll_interval,
                    timeout=timeout,
                )
            else:
                profile = client.user_info()
                user_id = profile.get("uid") or profile.get("id")
                if not user_id:
                    raise HolopixError("the session does not carry a user id")
                unique_id = str(profile.get("uniqueId") or "")

                from tools._holopix.client import seedance_module_id

                module = seedance_module_id(
                    model, _MODE_BY_OPERATION.get(operation, "text-to-video")
                )
                artwork_id = client.create_artwork(
                    name=str(inputs.get("artwork_name") or prompt[:40] or "OpenMontage"),
                    module=module,
                    model_list=[],
                )
                # A first frame can be given as a public url or as a local
                # file; a local file is uploaded to the account's own bucket.
                image_url = self._resolve_image(client, image_url, artwork_id)
                message = self.build_message(
                    client,
                    prompt=prompt,
                    user_id=user_id,
                    artwork_id=artwork_id,
                    duration=duration,
                    resolution=resolution,
                    ratio=ratio,
                    image_url=image_url,
                    camera_fixed=bool(inputs.get("camera_fixed", False)),
                    model=model,
                    operation=operation,
                    last_image_url=last_image_url,
                    unique_id=unique_id,
                )
                client_id = str(message["apiClientId"])
                frame = self._submit_when_free(client, message, timeout=timeout)
                video_url = self._video_url_of(frame)
                result = {
                    "video_url": video_url,
                    "status": frame.get("status"),
                    "message": frame.get("errMsg"),
                }
                if not video_url:
                    # The stream finished without a clip; the version list is
                    # the fallback that still knows where the file is.
                    video_url = self._collect_url(client, artwork_id, client_id)
                    result["video_url"] = video_url
            video_url = str(result.get("video_url") or "")
            if not video_url:
                raise HolopixError(
                    f"task {client_id} finished without a video url "
                    f"(status={result.get('status')}, message={result.get('message')})"
                )
            client.download(video_url, output_path)
        except HolopixAuthError as exc:
            return ToolResult(
                success=False,
                error=f"Holopix session rejected: {exc}. " + self.install_instructions,
            )
        except HolopixBusyError as exc:
            return ToolResult(
                success=False,
                error=(
                    f"Holopix account is busy with another generation: {exc}. "
                    "Retry later or collect the running task by client_id."
                ),
                data={"client_id": client_id, "artwork_id": artwork_id, "resumable": True},
            )
        except HolopixUpstreamError as exc:
            return ToolResult(
                success=False,
                error=(
                    f"Holopix's upstream video model refused the run: {exc}. "
                    "The compute points were refunded, so this attempt cost "
                    "nothing, and the refusal comes from the model side - "
                    "resending the same request will not help."
                ),
                data={
                    "client_id": client_id,
                    "artwork_id": artwork_id,
                    "resumable": False,
                    "refunded": True,
                },
            )
        except HolopixError as exc:
            return ToolResult(
                success=False,
                error=f"Holopix video generation failed: {exc}",
                data={"client_id": client_id, "artwork_id": artwork_id, "resumable": bool(client_id)},
            )
        except TimeoutError as exc:
            return ToolResult(
                success=False,
                error=(
                    f"{exc}. The run is already paid for - collect it with "
                    f"client_id={client_id}."
                ),
                data={"client_id": client_id, "artwork_id": artwork_id, "resumable": True},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                success=False,
                error=f"Holopix video generation failed: {exc}",
                data={"client_id": client_id, "artwork_id": artwork_id, "resumable": bool(client_id)},
            )

        from tools.video._shared import probe_output

        probed = probe_output(output_path)
        return ToolResult(
            success=True,
            data={
                "provider": "holopix",
                "model": model,
                "prompt": prompt,
                "operation": operation,
                "client_id": client_id,
                "artwork_id": artwork_id,
                "video_url": video_url,
                "duration": duration,
                "resolution": resolution,
                "aspect_ratio": ratio,
                "output": str(output_path),
                "output_path": str(output_path),
                "format": output_path.suffix.lstrip(".") or "mp4",
                **probed,
            },
            artifacts=[str(output_path)],
            cost_usd=0.0,
            duration_seconds=time.time() - started,
        )

    def _submit_when_free(self, client, message: dict, *, timeout: float) -> dict:
        """Submit the task, waiting out the account's one-task-at-a-time lock.

        Holopix refuses a second generation while the account already has one
        running (status -200). The web app makes the user wait, so the tool
        does the same instead of failing on the first refusal.
        """
        from tools._holopix.client import HolopixBusyError

        deadline = time.time() + timeout
        while True:
            try:
                return client.submit_video_ws_sync(message, timeout=timeout)
            except HolopixBusyError:
                if time.time() >= deadline:
                    raise
                time.sleep(_BUSY_POLL_SECONDS)

    @staticmethod
    def _resolve_image(client, image_url: str, artwork_id: int) -> str:
        """Return a public url for the first frame.

        A url is used as it is; a local path is uploaded to the account's own
        storage so the server can fetch it.
        """
        if not image_url:
            return ""
        if image_url.startswith(("http://", "https://")):
            return image_url
        from tools._holopix.client import HolopixError

        path = Path(image_url)
        if not path.is_file():
            raise HolopixError(f"first frame not found: {image_url}")
        return client.upload_image(path, artwork_id=artwork_id)

    @staticmethod
    def _video_url_of(frame: dict) -> str:
        """Return the clip url from a finished status frame.

        A finished frame carries ``images``: every entry's ``image`` field is
        a downloadable mp4 url.
        """
        for image in frame.get("images") or []:
            if isinstance(image, dict):
                url = image.get("image") or image.get("url") or ""
                if url:
                    return str(url)
        return ""

    @staticmethod
    def _collect_url(client, artwork_id: int, client_id: str) -> str:
        """Find a finished clip in the artwork's version list."""
        if not artwork_id or not client_id:
            return ""
        try:
            row = client.version_by_client_id(artwork_id, client_id)
        except Exception:  # noqa: BLE001 - the list may not be readable yet
            return ""
        return str(row.get("videoUrl") or row.get("video_url") or "")

    def _await_video(
        self,
        client,
        *,
        artwork_id: int,
        client_id: str,
        prompt: str,
        poll_interval: float,
        timeout: float,
    ) -> dict:
        """Wait for a task to finish and return its video url.

        The websocket stream is closed once the run is submitted, so a task
        that is collected later is read back from the artwork's version list,
        which is where the finished clip shows up.
        """
        deadline = time.time() + timeout
        while True:
            url = self._collect_url(client, artwork_id, client_id)
            if url:
                return {"status": 3, "video_url": url, "message": None}
            if time.time() >= deadline:
                raise TimeoutError(
                    f"Holopix task {client_id} did not finish within {int(timeout)}s"
                )
            time.sleep(poll_interval)
