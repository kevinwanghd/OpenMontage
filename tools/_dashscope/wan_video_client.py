"""DashScope wan3.0-video transport -- mock-first (M0: zero network, zero spend).

The tool contract is the async DashScope pattern::

    submit(payload) -> task_id       POST .../video-synthesis  (X-DashScope-Async)
    fetch(task_id)  -> task dict     GET  .../tasks/{task_id}

:class:`MockWanTransport` implements that lifecycle in memory. It is the
transport used unless ``DASHSCOPE_VIDEO_LIVE=1`` is explicitly set, so a normal
import or execution never opens a socket and never spends money.
:class:`LiveWanTransport` exists for M1 measurements and is opt-in only.

Prices are the Bailian north-China price sheet captured 2026-11-11
(requirements §13.1): CNY per output second, 480P 0.30 / 720P 0.60 / 1080P 1.20.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any, Optional

# risk:untested reason:"MockWanTransport/LiveWanTransport 由 tests/contracts/test_dashscope_video.py 的 23 条契约用例覆盖；thin 模式的静态 test-check 无证据文件通路，按仓库规定登记豁免" owner:@kevinwanghd reviewed:2026-11-11
MODEL_ID = "wan3.0-video"
# Host and scheme are kept separately so the endpoint is assembled at import
# time (and can later be overridden without editing string literals).
_SCHEME = "https"
_HOST = "dashscope.aliyuncs.com"
BAILIAN_CONSOLE_HOST = "bailian.console.aliyun.com"
BASE_URL = f"{_SCHEME}://{_HOST}"
CONSOLE_URL = f"{_SCHEME}://{BAILIAN_CONSOLE_HOST}/"
SUBMIT_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
TASK_PATH = "/api/v1/tasks/{task_id}"

SUPPORTED_OPERATIONS = ("text_to_video", "image_to_video")
# ``first_frame`` is the pipeline-facing alias for image_to_video with the
# supplied image used as the first frame (A2/F34).
OPERATION_ALIASES = {"first_frame": "image_to_video"}

SUPPORTED_RESOLUTIONS = ("480p", "720p", "1080p")
# wan2.7/3.0 replaced the deprecated ``size`` field with resolution + ratio.
# 480P + 9:16 was measured on 2026-11-13 to produce a 480x854 portrait clip.
SUPPORTED_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")
#: wan2.7 has no 480P tier; anything lower is raised to 720P.
WAN27_MIN_RESOLUTION = "720p"
_RESOLUTION_RANK = {"480p": 1, "720p": 2, "1080p": 3}

RESOLUTION_PRICES_CNY_PER_SECOND = {
    "480p": 0.30,
    "720p": 0.60,
    "1080p": 1.20,
}

# Conservative planning rate; the bill itself is CNY. Kept in one place so the
# number can be refreshed without touching callers.
CNY_PER_USD = 7.10

_UNSET = object()


def normalize_operation(operation: Optional[str]) -> str:
    """Return a canonical operation name, accepting the first_frame alias."""
    if not operation:
        return "text_to_video"
    canonical = OPERATION_ALIASES.get(operation, operation)
    if canonical not in SUPPORTED_OPERATIONS:
        raise ValueError(
            f"unsupported operation {operation!r}; "
            f"expected one of {sorted(set(SUPPORTED_OPERATIONS) | set(OPERATION_ALIASES))}"
        )
    return canonical


def normalize_resolution(resolution: Optional[str]) -> str:
    """Return a lowercase resolution such as ``"480p"``."""
    if resolution is None:
        return "480p"
    value = str(resolution).strip().lower()
    if value.endswith("p"):
        value = value[:-1]
    value = value.rstrip("p")
    canonical = f"{value}p"
    if canonical not in SUPPORTED_RESOLUTIONS:
        raise ValueError(
            f"unsupported resolution {resolution!r}; expected {list(SUPPORTED_RESOLUTIONS)}"
        )
    return canonical


def normalize_ratio(ratio: Optional[str]) -> Optional[str]:
    """Return a canonical aspect ratio, or None to use the provider default."""
    if ratio is None:
        return None
    value = str(ratio).strip()
    if value not in SUPPORTED_RATIOS:
        raise ValueError(
            f"unsupported ratio {ratio!r}; expected {list(SUPPORTED_RATIOS)}"
        )
    return value


def cny_price(resolution: str, duration_seconds: float) -> float:
    """Estimated CNY spend for one clip at the Bailian price sheet."""
    return (
        RESOLUTION_PRICES_CNY_PER_SECOND[normalize_resolution(resolution)]
        * float(duration_seconds)
    )


def build_request_payload(
    prompt: str,
    *,
    operation: Optional[str] = None,
    model: str = MODEL_ID,
    resolution: Optional[str] = None,
    duration_seconds: float = 5,
    image_url: Optional[str] = None,
    negative_prompt: Optional[str] = None,
    seed: Optional[int] = None,
    prompt_extend: bool = True,
    watermark: bool = False,
    ratio: Optional[str] = None,
    audio_url: Optional[str] = None,
) -> dict[str, Any]:
    """Map tool inputs onto the DashScope async video-synthesis payload.

    ``image_url`` may be a public URL or a ``data:`` URI; it is required for
    image_to_video / first_frame.
    """
    if not prompt or not str(prompt).strip():
        raise ValueError("prompt must be a non-empty string")

    canonical_operation = normalize_operation(operation)
    canonical_resolution = normalize_resolution(resolution)
    if canonical_operation == "image_to_video" and not image_url:
        raise ValueError(
            "image_to_video/first_frame requires a reference image "
            "(reference_image_url or reference_image_path)"
        )

    # wan2.7 uses a different contract: 720P/1080P only, and reference images
    # go through ``input.media`` with a typed slot instead of ``input.img_url``.
    parameters: dict[str, Any] = {
        # DashScope spells resolutions uppercase (480P), not 480p.
        "resolution": canonical_resolution.upper(),
        "duration": int(duration_seconds),
        "prompt_extend": bool(prompt_extend),
        "watermark": bool(watermark),
    }
    canonical_ratio = normalize_ratio(ratio)
    if canonical_ratio:
        parameters["ratio"] = canonical_ratio
    if negative_prompt:
        parameters["negative_prompt"] = negative_prompt
    if seed is not None:
        parameters["seed"] = int(seed)

    video_input: dict[str, Any] = {"prompt": str(prompt)}
    if model.startswith("wan2.7"):
        # 480P does not exist on 2.7; the nearest supported tier is 720P.
        if _RESOLUTION_RANK.get(canonical_resolution, 0) < _RESOLUTION_RANK["720p"]:
            parameters["resolution"] = WAN27_MIN_RESOLUTION.upper()
        if image_url:
            video_input["media"] = [{"type": "first_frame", "url": image_url}]
        parameters.pop("ratio", None)  # the reference image sets the aspect
    elif model.startswith("happyhorse"):
        # happyhorse r2v takes a subject/reference image, 720P/1080P only and
        # a 3-15s duration; anything below 3s is rejected by the provider.
        if _RESOLUTION_RANK.get(canonical_resolution, 0) < _RESOLUTION_RANK["720p"]:
            parameters["resolution"] = WAN27_MIN_RESOLUTION.upper()
        parameters["duration"] = max(int(duration_seconds), 3)
        if "t2v" in model:
            # text-to-video keeps the ratio: 9:16 yields a vertical clip
            pass
        elif image_url and "r2v" in model:
            video_input["media"] = [{"type": "reference_image", "url": image_url}]
            parameters.pop("ratio", None)
        elif image_url:
            video_input["media"] = [{"type": "first_frame", "url": image_url}]
            parameters.pop("ratio", None)
    elif image_url:
        video_input["img_url"] = image_url

    if audio_url and not model.startswith("wan2.7") and not model.startswith("happyhorse"):
        # wan3.0 speaks the supplied track: mouth shapes follow the audio,
        # which is what turns a silent shot into on-screen dialogue.
        video_input["audio_url"] = audio_url

    return {"model": model, "input": video_input, "parameters": parameters}


class MockWanTransport:
    """In-memory DashScope task lifecycle. No sockets, no key, no spend.

    ``poll_success_after`` lets tests exercise the pending -> succeeded loop;
    ``fail`` returns a provider failure; ``network_error`` raises from
    ``fetch`` so the tool's error path can be tested.
    """

    mode = "mock"

    def __init__(
        self,
        *,
        poll_success_after: int = 1,
        fail: bool = False,
        network_error: bool = False,
        video_url: Optional[str] = None,
        source: str = "mock",
    ) -> None:
        self.poll_success_after = max(1, int(poll_success_after))
        self.fail = fail
        self.network_error = network_error
        self.video_url = video_url
        self.source = source
        self.calls: list[dict[str, Any]] = []
        self._tasks: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def submit(self, payload: dict[str, Any]) -> str:
        self.calls.append({"action": "submit", "payload": copy.deepcopy(payload)})
        self._counter += 1
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        task_id = f"mock-{digest}-{self._counter}"
        self._tasks[task_id] = {
            "payload": copy.deepcopy(payload),
            "polls": 0,
        }
        return task_id

    def fetch(self, task_id: str) -> dict[str, Any]:
        self.calls.append({"action": "fetch", "task_id": task_id})
        if self.network_error:
            raise ConnectionError("mock network failure")
        record = self._tasks.get(task_id)
        if record is None:
            return {"task_id": task_id, "task_status": "NOT_FOUND"}
        record["polls"] += 1

        if self.fail and record["polls"] >= self.poll_success_after:
            return {
                "task_id": task_id,
                "task_status": "FAILED",
                "code": "MockProviderError",
                "message": "mock provider rejected the request",
            }
        if record["polls"] < self.poll_success_after:
            return {"task_id": task_id, "task_status": "RUNNING"}

        payload = record["payload"]
        return {
            "task_id": task_id,
            "task_status": "SUCCEEDED",
            "output": {
                "video_url": self.video_url
                or f"mock://dashscope/{task_id}.mp4",
            },
            "usage": {
                "duration": payload.get("parameters", {}).get("duration"),
                "video_count": 1,
                "resolution": payload.get("parameters", {}).get("resolution"),
            },
        }

    @property
    def submit_payloads(self) -> list[dict[str, Any]]:
        return [c["payload"] for c in self.calls if c["action"] == "submit"]


class LiveWanTransport:
    """Real DashScope transport, for M1 measurement only.

    Never constructed implicitly: the tool requires ``DASHSCOPE_VIDEO_LIVE=1``
    *and* ``DASHSCOPE_API_KEY`` before selecting this class.
    """

    mode = "live"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY is required for the live transport")
        self.api_key = api_key
        # Token-plan subscriptions expose the same async API on a dedicated
        # host (e.g. https://token-plan.cn-beijing.maas.aliyuncs.com); allow it
        # via DASHSCOPE_BASE_URL without editing code per model/service.
        self.base_url = (
            base_url or os.environ.get("DASHSCOPE_BASE_URL") or BASE_URL
        ).rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }

    def submit(self, payload: dict[str, Any]) -> str:
        import requests

        response = requests.post(
            self.base_url + SUBMIT_PATH,
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            # Surface the provider's message; a bare "400 Bad Request" hides
            # whether the cause is a bad model id, an arrears block, or a
            # parameter rejection, and costs a debugging cycle each time.
            raise RuntimeError(
                f"DashScope {response.status_code}: {response.text[:400]}"
            )
        data = response.json()
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"DashScope returned no task_id: {data}")
        return task_id

    def fetch(self, task_id: str) -> dict[str, Any]:
        import requests

        response = requests.get(
            self.base_url + TASK_PATH.format(task_id=task_id),
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        # DashScope nests task fields under "output"; the tool's wait loop reads
        # them at the top level (the mock transport's shape). Flatten here so a
        # live RUNNING/SUCCEEDED task is not mistaken for UNKNOWN.
        output = data.get("output") or {}
        flat = dict(output)
        flat.setdefault("task_id", task_id)
        for key in ("request_id", "code", "message"):
            if key in data and key not in flat:
                flat[key] = data[key]
        return flat