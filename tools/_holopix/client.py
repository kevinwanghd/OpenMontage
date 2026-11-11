"""Client for Holopix's web API (https://holopix.cn).

Holopix ships no public API and no API key. Its web app talks to JSON
endpoints under ``/pino_service_api`` with a session token that the server
issues at login, and every request body is encrypted with a static AES key
that ships in the app bundle. This module speaks that same contract so an
existing Holopix membership can drive OpenMontage.

Scope and limits:

* The token belongs to one account. Every generation made with it spends that
  account's compute points (算力). Nothing here bypasses billing, quotas, or
  membership entitlements.
* Only endpoints the web client itself calls are used, with the same headers.
* The token expires. Refresh it with ``python -m tools._holopix.client login``
  or ``python -m tools._holopix.client harvest``.

Protocol notes (verified against the live service):

* base URL ``https://holopix.cn/pino_service_api``
* auth: request header ``token: <session token>``
* request body: base64(AES-128-CBC(JSON({"data": {...}}), key=iv=static key))
* responses are encrypted the same way; plain JSON is the fallback for errors
  that never reached the payload parser
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

DEFAULT_BASE_URL = "https://holopix.cn/pino_service_api"


def _load_dotenv() -> None:
    """Load the project .env into os.environ.

    ``tools.base_tool`` does this for the tool package, but this client is
    also driven from scripts and from its own CLI, where that import may not
    happen. Without the token from .env every call answers "登录已过期",
    which looks like a broken login rather than a missing environment file.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip("'\"")


_load_dotenv()


def current_token() -> str:
    """Return the session token this account uses, or "" when unset."""
    return (os.environ.get("HOLOPIX_TOKEN") or "").strip()

# Module id of the AI-video workspace in the web app.
AI_VIDEO_MODULE = 25

# Module id the server stamps on video *task* records (``version.module``).
# The workspace and the task use different ids; the task id is what shows up
# on a finished version row.
VIDEO_TASK_MODULE = 107

# The AI video workbench offers exactly these resolutions for the Seedance
# models. The board ("\u81ea\u7531\u753b\u5e03") flow also accepts "768p", but the
# Seedance endpoints reject it: the run fails upstream and the points come
# straight back, so an unsupported value is refused here instead.
VIDEO_QUALITIES = ("480p", "720p", "1080p")

# Model names other tools use for the same Seedance family. The routing layer
# matches a requested model against a tool's advertised enum, so the aliases
# have to be advertised too - but the server only knows the canonical keys.
SEEDANCE_MODEL_ALIASES = {
    "seedance": "seedance-2-0",
    "seedance2": "seedance-2-0",
    "seedance-2": "seedance-2-0",
}

# Server reply when the account already has a generation in flight. The web
# app only shows a toast and makes the user wait, so the adapter has to do
# the same instead of failing on the first refusal.
BUSY_MESSAGE = "已存在生成任务"

# Server reply when the upstream model refused a run. The credits come back,
# so it is neither a busy account nor a malformed request.
UPSTREAM_REFUSED_MESSAGE = "视频生成失败"

# The web app encrypts every request body with AES-128-CBC using this value
# as both key and IV (see assets/public-*.js in the app bundle).
AES_KEY = b"igC9gJ5tXyxS76Ne"

# The app keeps its session in a cookie named token_<env> on .holopix.cn.
TOKEN_COOKIE_PREFIX = "token_"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)


# Alphabet the web app draws its request ids from.
_REQUEST_ID_ALPHABET = "useandom-26T198340PX75pxJACKVERYMINDBUSHWOLF_GQZbfghjklqvwyzrict"


def request_id(length: int = 21) -> str:
    """Return a request id in the shape the web app generates.

    Its HTTP interceptor stamps one of these on every call, so the server
    sees them on every request it handles from the browser.
    """
    return "".join(random.choice(_REQUEST_ID_ALPHABET) for _ in range(length))


def _normalise_quality(quality: str) -> str:
    """Return the resolution the workbench expects.

    The UI offers "480p"/"720p"/"1080p" while the app's own fallback reads
    "720", so a bare number is accepted and spelled the same way the UI
    spells it.
    """
    text = str(quality).strip().lower()
    if text.isdigit():
        text = f"{text}p"
    if text not in VIDEO_QUALITIES:
        raise ValueError(
            f"unsupported video resolution {quality!r}; "
            f"the workbench offers {', '.join(VIDEO_QUALITIES)}"
        )
    return text


def client_id_for(user_id: int) -> str:
    """Return a client id in the shape the web app generates.

    The app builds it as ``<user id>-<epoch millis><random digits>`` and the
    server indexes every task row by that string, so a random uuid is not
    interchangeable with it.
    """
    return f"{user_id}-{int(time.time() * 1000)}{random.randrange(100000)}"


# The modern app's Seedance flow runs over a websocket, not over the legacy
# HTTP submit. Each (model, mode) pair has its own module id; the app's route
# table maps them and the server dispatches on the module id.
SEEDANCE_MODULES = {
    "seedance-2-0": {
        "reference": 76,
        "start-end": 75,
        "image-to-video": 77,
        "text-to-video": 78,
        "multimodal": 76,
    },
    "seedance-mini": {
        "reference": 80,
        "start-end": 79,
        "image-to-video": 81,
        "text-to-video": 82,
        "multimodal": 80,
    },
}

# The websocket endpoint the app opens for one task.
WS_BASE_URL = "wss://holopix.cn/pino_service_api/websocket"

# The server's greeting frame (base64 of a fixed string) and its heartbeat.
WS_GREETING = "FO3V6+YWekojs02VRpkoLg=="
WS_HEARTBEAT = "heartBeat"

# Task status codes the websocket reports.
WS_STATUS_QUEUED = 1
WS_STATUS_GENERATING = 2
WS_STATUS_DONE = 3

# Negative status codes and what they mean. The account's compute points
# (-100), the one-task-at-a-time lock (-200) and the membership level
# (-202/-203) are the ones that decide whether a retry can help.
WS_STATUS_ERRORS = {
    -1: "general server error",
    -2: "the prompt was rejected by the content filter",
    -3: "the session token expired",
    -100: "the account has no compute points left",
    -200: "the account already has a generation in flight",
    -201: "the membership's package limit is reached",
    -202: "this model needs a higher membership level",
    -203: "this membership may not use that model",
    -300: "the model does not support this mode",
    90711: "the team's quota is exceeded",
}


def resolve_seedance_model(model: str) -> str:
    """Return the canonical Holopix model key for a requested model name.

    Other OpenMontage tools advertise Seedance under names like "seedance";
    the Holopix server only knows its own keys, so an alias is mapped here.
    """
    key = str(model).strip()
    return SEEDANCE_MODEL_ALIASES.get(key, key)


def seedance_module_id(model: str, mode: str) -> int:
    """Return the module id the server dispatches (model, mode) to."""
    modes = SEEDANCE_MODULES.get(resolve_seedance_model(model))
    if not modes:
        raise ValueError(
            f"unknown Holopix video model {model!r}; "
            f"known models: {', '.join(sorted(SEEDANCE_MODULES))}"
        )
    module_id = modes.get(str(mode))
    if module_id is None:
        raise ValueError(
            f"unknown video mode {mode!r} for {model!r}; "
            f"known modes: {', '.join(sorted(modes))}"
        )
    return module_id


def calc_ratio(width: int, height: int) -> str:
    """Return the aspect ratio the app would pick for a still frame.

    The app measures the first frame and snaps it to the closest ratio the
    model supports; the server uses that value to size the output.
    """
    if width <= 0 or height <= 0:
        return "16:9"
    ratios = ("1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16", "21:9")
    target = width / height
    return min(ratios, key=lambda r: abs(target - (int(r.split(":")[0]) / int(r.split(":")[1]))))


def seedance_param(
    *,
    prompt: str,
    user_id: int,
    unique_id: str = "",
    artwork_id: int = 0,
    module: int = 78,
    model: str = "seedance-2-0",
    mode: str = "text-to-video",
    quality: str = "480p",
    ratio: str = "16:9",
    duration: int = 5,
    image_list: Optional[list] = None,
    first_image_url: str = "",
    last_image_url: str = "",
    calc: str = "",
    vip_channel: bool = False,
    client_id: Optional[str] = None,
) -> dict:
    """Build the submit frame the app sends for a Seedance run.

    Mirrors the app's own ``mJe()`` builder: the frame is a plain object (no
    ``{"data": ...}`` wrapper), it carries the model and the mode explicitly,
    and the client id is the app's ``<user id>-<millis><random digits>`` shape
    that the server indexes tasks by.

    The image modes differ from the text mode in two ways: the ratio travels
    as "adaptive" (the real ratio comes from the frame) and the frame is
    named in ``originImgUrl`` / ``firstImageUrl`` / ``lastImageUrl``.
    """
    quality = _normalise_quality(quality)
    model = resolve_seedance_model(model)
    now = int(time.time() * 1000)
    payload = {
        "module": module,
        "userId": user_id,
        "uniqueId": unique_id,
        "artworkId": artwork_id,
        "apiClientId": client_id or client_id_for(user_id),
        "source": 1,
        "startTime": now,
        "vipChannel": vip_channel,
        "videoModel": model,
        "videoMode": mode,
        "prompt": prompt,
        "videoResolution": quality,
        "videoDuration": int(duration),
        "imageList": list(image_list or []),
        "ctime": now,
    }
    if mode == "text-to-video":
        payload["videoRatio"] = ratio
        payload["calcRatio"] = calc or ratio
        return payload

    payload["videoRatio"] = "adaptive"
    payload["calcRatio"] = calc or ratio
    if mode == "start-end":
        payload["firstImageUrl"] = first_image_url
        payload["lastImageUrl"] = last_image_url
        payload["lastFrameUrl"] = last_image_url
        payload["originImgUrl"] = first_image_url
    elif mode == "image-to-video":
        payload["originImgUrl"] = first_image_url
    return payload


def video_param(
    *,
    prompt: str,
    artwork_id: int,
    user_id: int = 28,
    module: int = VIDEO_TASK_MODULE,
    quality: str = "480p",
    ratio: str = "16:9",
    duration: int = 5,
    vip_channel: bool = False,
    client_id: Optional[str] = None,
    image_url: str = "",
    camera_fixed: bool = False,
) -> dict:
    """Build the parameter object the web app posts for a video run.

    The web app normalises its workspace state into this shape before it
    reaches the server. This mirrors the app's own ``QRe()`` builder field for
    field. The model is deliberately absent: the app stores the chosen model
    on the artwork record, not in this message, and the server reads it from
    there.

    ``promptImgId`` is left out on purpose - the server parses it as a number
    and refuses an empty value ("NumberFormatException: empty String").

    ``quality`` must be one of :data:`VIDEO_QUALITIES`. The app only offers
    those three for this module; anything else fails upstream and is
    refunded, which is a wasted round trip.
    """
    quality = _normalise_quality(quality)
    return {
        "module": module,
        "apiClientId": client_id or client_id_for(user_id),
        "userId": user_id,
        "artworkId": artwork_id,
        "source": 1,
        "startTime": int(time.time() * 1000),
        "vipChannel": vip_channel,
        "prompt": prompt,
        "videoText": prompt,
        "videoResolution": quality,
        "videoImgUrl": image_url,
        "videoDuration": duration,
        "videoRatio": ratio,
        "videoCamerafixed": camera_fixed,
    }


class HolopixError(RuntimeError):
    """A Holopix API call failed."""


class HolopixAuthError(HolopixError):
    """The session token is missing, expired, or rejected."""


class HolopixBusyError(HolopixError):
    """The account already has a generation task in flight.

    Holopix allows one generation at a time per account. This is a wait
    condition, not a failure: the caller should retry later.
    """


class HolopixUpstreamError(HolopixError):
    """The upstream video model refused the run.

    The server charges the run, hands it on, the upstream refuses and the
    server refunds the credits, so the round trip is free. No task is
    created, so there is nothing to poll. Retrying the same request does
    not help: the refusal comes from the model side, not from the request.
    """


def _aes_key() -> bytes:
    """Return the AES key, overridable for tests and future app versions."""
    return os.environ.get("HOLOPIX_AES_KEY", "").encode() or AES_KEY


def _encrypt(payload: Any) -> str:
    """Encrypt a payload the way the web app does: base64(AES-128-CBC)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sympad

    key = _aes_key()
    padder = sympad.PKCS7(128).padder()
    plain = padder.update(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return base64.b64encode(encryptor.update(plain) + encryptor.finalize()).decode()


def _decrypt(blob: str) -> Any:
    """Decrypt a response body; raises ValueError when it is not ciphertext."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sympad

    key = _aes_key()
    raw = base64.b64decode(blob.strip().strip('"'))
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    plain = decryptor.update(raw) + decryptor.finalize()
    unpadder = sympad.PKCS7(128).unpadder()
    text = (unpadder.update(plain) + unpadder.finalize()).decode("utf-8", "replace")
    return json.loads(text)


class HolopixClient:
    """Thin client over the endpoints the Holopix web app uses."""

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.token = token if token is not None else current_token()
        self.base_url = (base_url or os.environ.get("HOLOPIX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._session = None

    # -- plumbing ---------------------------------------------------------

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def headers(self, *, auth: bool = True) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _UA,
            "Origin": "https://holopix.cn",
            "Referer": "https://holopix.cn/",
        }
        if auth and self.token:
            headers["token"] = self.token
        return headers

    def post(self, path: str, payload: Optional[dict] = None, *, auth: bool = True) -> dict:
        """POST an encrypted body and return the decoded envelope."""
        body = _encrypt({"data": payload or {}})
        response = self.session.post(
            f"{self.base_url}{path}",
            data=body,
            headers=self.headers(auth=auth),
            timeout=self.timeout,
        )
        return self._decode(path, response)

    def get(self, path: str, *, auth: bool = True) -> dict:
        """GET a plain endpoint and return the decoded envelope."""
        response = self.session.get(
            f"{self.base_url}{path}",
            headers=self.headers(auth=auth),
            timeout=self.timeout,
        )
        return self._decode(path, response)

    def _decode(self, path: str, response) -> dict:
        text = (response.text or "").strip()
        payload: Any
        try:
            payload = _decrypt(text)
        except Exception:  # noqa: BLE001 - error envelopes are plain JSON
            try:
                payload = response.json()
            except ValueError as exc:
                raise HolopixError(
                    f"{path} returned HTTP {response.status_code} with an unreadable body: {text[:200]}"
                ) from exc
        if not isinstance(payload, dict):
            raise HolopixError(f"{path} returned an unexpected payload: {payload!r}")
        if payload.get("success") is False:
            message = str(payload.get("msg") or "request failed")
            if "登录已过期" in message or "登录状态无效" in message or "请重新登录" in message:
                raise HolopixAuthError(f"{path}: {message}")
            if BUSY_MESSAGE in message:
                raise HolopixBusyError(f"{path}: {message}")
            if UPSTREAM_REFUSED_MESSAGE in message:
                raise HolopixUpstreamError(f"{path}: {message}")
            raise HolopixError(f"{path}: {message}")
        return payload

    @staticmethod
    def data_of(payload: dict) -> dict:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    # -- account ----------------------------------------------------------

    def user_info(self) -> dict:
        """Return the signed-in user's profile (uid, nickname, membership)."""
        return self.data_of(self.post("/api/user/userInfo"))

    def rights(self) -> dict:
        """Return the account's compute entitlements (算力)."""
        return self.data_of(self.post("/api/rights/getRights"))

    def points_record(self, page: int = 1, page_size: int = 15) -> list:
        """Return the compute ledger, newest first."""
        data = self.data_of(self.post("/api/rights/pointsRecord", {"page": page, "pageSize": page_size}))
        items = data.get("records") or data.get("list") or data.get("items") or []
        return items if isinstance(items, list) else []

    def artworks(self, page: int = 1, page_size: int = 20, order_condition: int = 1) -> list:
        """Return the account's recent works (generated videos included).

        ``orderCondition`` is mandatory and must be an integer: the server
        answers "orderCondition:must not be null" otherwise, and a string
        value fails deserialisation.
        """
        data = self.data_of(
            self.post(
                "/api/artwork/list",
                {
                    "orderCondition": int(order_condition),
                    "page": page,
                    "pageSize": page_size,
                },
            )
        )
        items = data.get("records") or data.get("list") or data.get("items") or []
        return items if isinstance(items, list) else []

    def models(self, module: str = "aivideo", page: int = 1, page_size: int = 50) -> list:
        """Return the published models for a module (aivideo for text-to-video)."""
        data = self.data_of(
            self.post(
                "/api/model/modelPublishList",
                {"module": module, "page": page, "pageSize": page_size, "onlyUserModel": False},
            )
        )
        items = data.get("list") or data.get("records") or data.get("items") or []
        return items if isinstance(items, list) else []

    # -- video ------------------------------------------------------------

    def create_artwork(self, name: str, module: int = AI_VIDEO_MODULE, model_list: Optional[list] = None) -> int:
        """Create the artwork a generation hangs on and return its id.

        The web app creates an artwork first and then points every generation
        at it; the server refuses a second task on the same artwork.
        """
        payload = {
            "artworkName": name,
            "modelList": json.dumps(model_list or [], ensure_ascii=False),
            "lastModule": module,
        }
        body = self.post("/api/artwork/add", payload)
        data = body.get("data")
        if isinstance(data, (int, str)):
            artwork_id = data
        else:
            artwork_id = (data or {}).get("id")
        if not artwork_id:
            raise HolopixError(f"artwork was not created: {json.dumps(data, ensure_ascii=False)[:200]}")
        return int(artwork_id)

    def artwork_versions(self, artwork_id: int, page: int = 1, page_size: int = 5) -> list:
        """Return the finished versions of an artwork (each carries a video url)."""
        data = self.data_of(
            self.post("/api/artwork/versionList", {"artworkId": artwork_id, "page": page, "pageSize": page_size})
        )
        records = data.get("records")
        return records if isinstance(records, list) else []

    # -- websocket transport ---------------------------------------------

    def ws_headers(self) -> dict:
        """Return the headers the app sends with its websocket handshake."""
        return {
            "Origin": "https://holopix.cn",
            "Cookie": f"token_production={self.token}",
            "User-Agent": _UA,
        }

    async def submit_video_ws(self, payload: dict, *, timeout: float = 300.0) -> dict:
        """Submit a Seedance task over the app's websocket and wait for it.

        The app opens ``wss://holopix.cn/pino_service_api/websocket/<clientId>``
        and sends one frame: base64(AES-128-CBC(JSON)) of the submit object
        itself - no ``{"data": ...}`` wrapper. The server streams status frames
        back on the same socket (1 queued, 2 generating, 3 done).

        Returns the final status frame. Raises :class:`HolopixBusyError` for
        the one-task-at-a-time lock, :class:`HolopixAuthError` for an expired
        session and :class:`HolopixUpstreamError` for a refusal or an
        exhausted balance.
        """
        import asyncio

        import websockets

        client_id = str(payload.get("apiClientId") or "")
        url = f"{WS_BASE_URL}/{client_id}"

        async with websockets.connect(
            url,
            additional_headers=self.ws_headers(),
            max_size=64 * 1024 * 1024,
        ) as socket:
            # The server greets first; the greeting is a fixed base64 blob
            # that is not ciphertext, so it is skipped like the heartbeat.
            try:
                await asyncio.wait_for(socket.recv(), timeout=10)
            except asyncio.TimeoutError:
                pass

            await socket.send(_encrypt(payload))

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=25)
                except asyncio.TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                if raw in (WS_GREETING, WS_HEARTBEAT):
                    continue
                try:
                    frame = _decrypt(raw)
                except Exception:  # noqa: BLE001 - not every frame is ciphertext
                    continue
                if not isinstance(frame, dict):
                    continue
                status = frame.get("status")
                if status == WS_STATUS_DONE:
                    return frame
                if isinstance(status, int) and status < 0:
                    raise self._ws_error(status, frame)
        raise TimeoutError(
            f"Holopix task {client_id} did not finish within {int(timeout)}s"
        )

    @staticmethod
    def _ws_error(status: int, frame: dict) -> HolopixError:
        """Turn a negative status code into the matching error."""
        reason = WS_STATUS_ERRORS.get(status, "unknown error")
        detail = frame.get("errMsg") or frame.get("message") or ""
        message = f"Holopix rejected the run ({status}, {reason})"
        if detail:
            message = f"{message}: {detail}"
        if status == -3:
            return HolopixAuthError(message)
        if status == -200:
            return HolopixBusyError(message)
        return HolopixUpstreamError(message)

    def submit_video_ws_sync(self, payload: dict, *, timeout: float = 300.0) -> dict:
        """Blocking wrapper around :meth:`submit_video_ws`."""
        import asyncio

        return asyncio.run(self.submit_video_ws(payload, timeout=timeout))

    def version_by_client_id(self, artwork_id: int, client_id: str) -> dict:
        """Find a finished version of an artwork by its client id.

        ``versionInfoByClientId`` reports the status but not the video url;
        the artwork's version list carries the url, so a run that was paid for
        can be collected from there after a timeout.
        """
        for row in self.artwork_versions(artwork_id, page_size=20):
            if not isinstance(row, dict):
                continue
            if str(row.get("clientId") or row.get("apiClientId") or "") == str(client_id):
                return row
        return {}

    def submit_video(self, message: dict) -> dict:
        """Submit a video generation task.

        The endpoint takes the app's parameter object as a JSON *string* inside
        ``data.message``; the server parses that string again. See
        :func:`video_param` for the fields it expects.

        The request carries a ``requestId`` query parameter, exactly as the
        web app's HTTP interceptor attaches one to every call.

        Raises :class:`HolopixBusyError` when the account already has a
        generation in flight.
        """
        payload = {"data": {"message": json.dumps(message, ensure_ascii=False, separators=(",", ":"))}}
        response = self.session.post(
            f"{self.base_url}/aiApi/api/generateVolceVideo?requestId={request_id()}",
            data=_encrypt(payload),
            headers=self.headers(),
            timeout=self.timeout,
        )
        return self._decode("/aiApi/api/generateVolceVideo", response)

    def video_version(self, client_id: str) -> dict:
        """Return the task record for one client id.

        The record carries the task's status and, once it is finished, the
        video url. It is the cheapest way to check on a run that has already
        been paid for.
        """
        data = self.data_of(self.post("/api/artwork/versionInfoByClientId", {"clientId": client_id}))
        return data if data else {}

    def task_status(self, client_ids: list[str]) -> dict:
        """Poll one or more task ids for status and result URLs."""
        return self.data_of(self.post("/api/task/waitClientIds", {"clientIds": client_ids}))

    def upload_image(self, path: Path | str, *, width: int = 0, height: int = 0, artwork_id: int = 0) -> str:
        """Upload a still frame and return its public url.

        The web app stores first frames in Aliyun OSS: it asks the server for
        a one-shot upload policy, posts the file to the OSS bucket, then
        registers the upload so the asset list can show it. The generated
        image-to-video runs need that url in ``imageList``.
        """
        path = Path(path)
        if not path.is_file():
            raise HolopixError(f"image not found: {path}")

        policy = self.data_of(self.post("/aliyunOss/getAliyunOssToken", {}))
        host = policy.get("host") or ""
        domain = policy.get("domain") or ""
        directory = policy.get("dir") or "uploads/"
        if not host:
            raise HolopixError(f"no upload policy returned: {json.dumps(policy, ensure_ascii=False)[:200]}")

        name = f"{int(time.time() * 1000)}-{random.randrange(100000)}.{path.suffix.lstrip('.') or 'png'}"
        key = f"{directory}{time.strftime('%Y%m%d')}/{name}"

        with path.open("rb") as handle:
            response = self.session.post(
                host,
                data={
                    "key": key,
                    "OSSAccessKeyId": policy.get("accessid") or "",
                    "policy": policy.get("policy") or "",
                    "signature": policy.get("signature") or "",
                    "callback": policy.get("callback") or "",
                    "success_action_status": "200",
                },
                files={"file": (path.name, handle)},
                timeout=300,
            )
        if response.status_code != 200:
            raise HolopixError(f"upload to {host} failed: {response.status_code}")
        url = f"{domain}/{key}"

        # Register the upload so it shows up in the account's asset list.
        try:
            self.post(
                "/api/localUpload/localUpload",
                {
                    "type": 4,
                    "artworkId": artwork_id,
                    "image": [
                        {
                            "width": width,
                            "height": height,
                            "image": url,
                            "size": path.stat().st_size,
                        }
                    ],
                },
            )
        except HolopixError:
            # The url is what the generation needs; registration is bookkeeping.
            pass
        return url

    def download(self, url: str, destination: Path | str) -> Path:
        """Download a finished video to ``destination``."""
        destination = Path(destination)
        response = self.session.get(url, timeout=600, stream=True)
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                if chunk:
                    handle.write(chunk)
        return destination

    # -- login ------------------------------------------------------------

    def send_sms_code(self, phone: str, kind: int = 1) -> dict:
        """Text a verification code to ``phone`` (the app's /user/sendCode).

        The code the app asks for is four digits long.
        """
        return self.post("/user/sendCode", {"account": phone, "type": kind})

    @staticmethod
    def _login_payload(**overrides) -> dict:
        """Build the payload the web app posts to /user/login."""
        payload = {
            "account": "",
            "code": "",
            "loginType": 3,
            "password": "",
            "source": "pc",
            "anonymousId": "",
            "recommendUniqueId": "",
            "recommendModelId": "",
            "inviteCode": "",
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _token_of(data: dict) -> str:
        token = (
            data.get("token")
            or (data.get("userInfo") or {}).get("token")
            or (data.get("user") or {}).get("token")
        )
        if not token:
            raise HolopixError(f"login returned no token: {json.dumps(data, ensure_ascii=False)[:300]}")
        return str(token)

    def wechat_login(self, open_id: str, union_id: str, *, account: str = "", code: str = "") -> str:
        """Exchange a WeChat identity for a session token.

        The web app logs in through WeChat with ``loginType=3``. When the
        WeChat account is not bound to a phone yet, pass ``account`` and the
        SMS ``code`` to bind it in the same call.
        """
        data = self.data_of(
            self.post(
                "/user/login",
                self._login_payload(
                    account=account,
                    code=code,
                    loginType=3,
                    wechatOpenId=open_id,
                    wechatUnionId=union_id,
                ),
            )
        )
        return self._token_of(data)

    def qr_login_request(self, event: str = "login") -> dict:
        """Ask the WeChat public account for a login QR code.

        Returns ``{"ticket": ..., "url": ...}``; pass the ticket to
        :meth:`qr_login_result` until the scan completes.
        """
        data = self.data_of(self.post("/wechatOfficialAccount/getQrCodeByLogin", {"event": event}))
        if not data.get("ticket") or not data.get("url"):
            raise HolopixError(f"no QR ticket returned: {json.dumps(data, ensure_ascii=False)[:200]}")
        return {"ticket": str(data["ticket"]), "url": str(data["url"])}

    def qr_login_result(self, ticket: str) -> dict:
        """Return the scan result for a QR ticket (empty while pending).

        The endpoint answers "当前状态尚未更新" until the code is scanned;
        that is a pending state, not a failure.
        """
        try:
            return self.data_of(
                self.post("/wechatOfficialAccount/getLoginResultByCircle", {"ticket": ticket})
            )
        except HolopixError as exc:
            if "尚未更新" in str(exc) or "未更新" in str(exc):
                return {}
            raise

    def qr_wait(self, ticket: str, timeout: float = 180.0, interval: float = 3.0) -> str:
        """Wait for a QR ticket to be scanned and return the session token."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.qr_login_result(ticket)
            token = result.get("token") or (result.get("user") or {}).get("token")
            if token:
                return str(token)
            time.sleep(interval)
        raise HolopixError("QR login timed out without a scan")

    def qr_login_loop(
        self,
        event: str = "login",
        timeout: float = 1800.0,
        interval: float = 3.0,
        on_qr=None,
    ) -> str:
        """Keep a QR login alive until it is scanned.

        WeChat login codes expire after roughly two minutes, so the client
        re-requests one when it expires and hands the new URL to ``on_qr``.
        """
        deadline = time.time() + timeout
        request = self.qr_login_request(event)
        if on_qr:
            on_qr(request)
        while time.time() < deadline:
            try:
                result = self.qr_login_result(request["ticket"])
            except HolopixError as exc:
                if "过期" not in str(exc) and "expire" not in str(exc).lower():
                    raise
                request = self.qr_login_request(event)
                if on_qr:
                    on_qr(request)
                continue
            open_id = result.get("openId")
            if open_id:
                if str(result.get("eventStatus") or "") == "waitingBandingPhone":
                    raise HolopixError(
                        "this WeChat account is not bound to a phone in Holopix yet; "
                        "bind it in the app first, then scan again"
                    )
                return self.wechat_login(open_id, str(result.get("unionId") or ""))
            time.sleep(interval)
        raise HolopixError("QR login timed out without a scan")

    def qr_login(self, event: str = "login", timeout: float = 180.0, interval: float = 3.0) -> str:
        """Run the whole WeChat QR login and return the session token."""
        request = self.qr_login_request(event)
        return self.qr_wait(request["ticket"], timeout=timeout, interval=interval)

    def phone_login(self, phone: str, code: str) -> str:
        """Exchange a phone number and SMS code for a session token."""
        data = self.data_of(
            self.post(
                "/user/login",
                self._login_payload(account=phone, code=code.strip(), loginType=1),
            )
        )
        return self._token_of(data)


# -- environment helpers ---------------------------------------------------


def read_env_value(env_path: Path, key: str) -> str:
    """Read ``key`` from a .env file (falls back to the process environment)."""
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip("'\"")
    return os.environ.get(key, "")


def write_env_value(env_path: Path, key: str, value: str) -> None:
    """Set ``key`` in a .env file, replacing an existing entry."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    replaced = False
    for index, line in enumerate(lines):
        if "=" not in line or line.strip().startswith("#"):
            continue
        if line.split("=", 1)[0].strip() == key:
            lines[index] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def browser_token(cdp_url: str = "http://127.0.0.1:9333") -> Optional[str]:
    """Read the Holopix session token out of a browser over the DevTools protocol.

    The web app keeps the token in an encrypted cookie, so the only place it
    can be read is inside a live browser. Point this at a Chrome started with
    ``--remote-debugging-port`` and the profile you logged in with.
    """
    import asyncio

    import websockets

    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=5) as response:
            info = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - the browser may simply not be running
        return None
    endpoint = info.get("webSocketDebuggerUrl")
    if not endpoint:
        return None

    # The web app keeps the session in a cookie named token_<env>; the store it
    # writes on login (pinoUserStore.userInfo.token) carries the same value.
    read_cookie_js = (
        "(() => { for (const c of document.cookie.split('; ')) { "
        "const i = c.indexOf('='); if (i > 0 && c.slice(0, i).startsWith('%s')) "
        "return c.slice(i + 1); } return null })()" % TOKEN_COOKIE_PREFIX
    )
    read_store_js = (
        "(() => { try { const raw = localStorage.getItem('pinoUserStore'); "
        "if (!raw) return null; const parsed = JSON.parse(raw); "
        "return (parsed.userInfo && parsed.userInfo.token) || parsed.token || null; "
        "} catch (e) { return null } })()"
    )

    async def _read() -> Optional[str]:
        async with websockets.connect(endpoint, max_size=16 * 1024 * 1024) as socket:
            # The session cookie is httpOnly, so it can only be read through the
            # DevTools cookie domain of an attached page target - the browser
            # level Network.getCookies does not return it.
            await socket.send(json.dumps({"id": 2, "method": "Target.getTargets"}))
            targets = []
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                if message.get("id") == 2:
                    targets = message.get("result", {}).get("targetInfos", [])
                    break
            page = next(
                (
                    t
                    for t in targets
                    if t.get("type") == "page" and "holopix" in str(t.get("url", ""))
                ),
                None,
            )
            if not page:
                return None
            await socket.send(
                json.dumps(
                    {
                        "id": 3,
                        "method": "Target.attachToTarget",
                        "params": {"targetId": page["targetId"], "flatten": True},
                    }
                )
            )
            session_id = None
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                if message.get("id") == 3:
                    session_id = message.get("result", {}).get("sessionId")
                    break

            await socket.send(
                json.dumps(
                    {
                        "id": 6,
                        "method": "Network.getCookies",
                        "params": {"urls": ["https://holopix.cn/", "https://www.holopix.cn/"]},
                        **({"sessionId": session_id} if session_id else {}),
                    }
                )
            )
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                if message.get("id") != 6:
                    continue
                for cookie in message.get("result", {}).get("cookies", []):
                    name = str(cookie.get("name") or "")
                    if name.startswith(TOKEN_COOKIE_PREFIX):
                        return str(cookie.get("value") or "")
                break

            for call_id, expression in ((4, read_cookie_js), (5, read_store_js)):
                payload = {
                    "id": call_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True},
                }
                if session_id:
                    payload["sessionId"] = session_id
                await socket.send(json.dumps(payload))
                while True:
                    message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                    if message.get("id") != call_id:
                        continue
                    value = (
                        message.get("result", {})
                        .get("result", {})
                        .get("result", {})
                        .get("value")
                    )
                    if value:
                        return str(value)
                    break
            return None

    try:
        return asyncio.run(_read())
    except Exception:  # noqa: BLE001
        return None


# -- CLI -------------------------------------------------------------------


def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools._holopix.client",
        description="Log into Holopix and store the session token for OpenMontage.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Phone + SMS login, stores HOLOPIX_TOKEN in .env")
    login.add_argument("--phone", required=True)
    login.add_argument("--code", help="SMS verification code; omit to only send the code")
    login.add_argument("--env", default=".env")

    qr = sub.add_parser("qr", help="WeChat QR login, stores HOLOPIX_TOKEN in .env")
    qr.add_argument("--event", default="login")
    qr.add_argument("--timeout", type=float, default=180.0)
    qr.add_argument("--env", default=".env")

    check = sub.add_parser("check", help="Verify the stored token")
    check.add_argument("--env", default=".env")

    harvest_cmd = sub.add_parser("harvest", help="Take the token from a logged-in browser")
    harvest_cmd.add_argument("--cdp", default="http://127.0.0.1:9333")
    harvest_cmd.add_argument("--env", default=".env")

    args = parser.parse_args(argv)
    env_path = Path(args.env)

    if args.command == "login":
        client = HolopixClient()
        if not args.code:
            client.send_sms_code(args.phone)
            print(f"Verification code sent to {args.phone}. Re-run with --code <code>.")
            return 0
        token = client.phone_login(args.phone, args.code)
        write_env_value(env_path, "HOLOPIX_TOKEN", token)
        client.token = token
        profile = client.user_info()
        print(f"Logged in as {profile.get('nickname') or profile.get('uid')}. Token written to {env_path}.")
        return 0

    if args.command == "qr":
        client = HolopixClient()

        def announce(request: dict) -> None:
            print(f"Scan this QR code with WeChat to sign in: {request['url']}", flush=True)

        token = client.qr_login_loop(args.event, timeout=args.timeout, on_qr=announce)
        write_env_value(env_path, "HOLOPIX_TOKEN", token)
        client.token = token
        profile = client.user_info()
        print(f"Logged in as {profile.get('nickname') or profile.get('uid')}. Token written to {env_path}.")
        return 0

    if args.command == "check":
        token = read_env_value(env_path, "HOLOPIX_TOKEN")
        if not token:
            print(f"No HOLOPIX_TOKEN found in {env_path} or the environment.")
            return 1
        client = HolopixClient(token=token)
        try:
            profile = client.user_info()
        except HolopixAuthError as exc:
            print(f"Token rejected: {exc}")
            return 1
        print(f"Token valid for {profile.get('nickname') or profile.get('uid')}.")
        return 0

    if args.command == "harvest":
        token = browser_token(args.cdp)
        if not token:
            print(
                "No Holopix session found. Log in at https://holopix.cn/ in a Chrome started "
                f"with --remote-debugging-port (this client polls {args.cdp}), then re-run."
            )
            return 1
        write_env_value(env_path, "HOLOPIX_TOKEN", token)
        client = HolopixClient(token=token)
        profile = client.user_info()
        print(
            f"Harvested a valid session for {profile.get('nickname') or profile.get('uid')}. "
            f"Token written to {env_path}."
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_main())
