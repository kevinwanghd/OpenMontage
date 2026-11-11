"""Client for Molica's web API (https://web.molica.com.cn).

Molica ships no public API and no API key. Its web app talks to JSON
endpoints under ``/api`` with a session token that the server issues at login
(phone + SMS code, or WeChat). This module speaks that same contract so an
existing Molica membership can drive OpenMontage.

Scope and limits:

* The token belongs to one account. Every generation made with it spends that
  account's credits (the site calls them 灵力). Nothing here bypasses billing,
  quotas, or membership entitlements.
* Only endpoints the web client itself calls are used, with the same headers.
* The token expires. Refresh it with ``python -m tools._molica.client login``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote
from typing import Any, Optional

DEFAULT_BASE_URL = "https://web.molica.com.cn/api"
DEFAULT_API_VERSION = "2.2.13"
DEFAULT_CHANNEL = "pc_web_browser"
DEFAULT_LOCALE = "zh-CN"

# ret codes the web client treats as "log in again".
RET_AUTH_FAILED = frozenset({20000, 20001})

def _load_dotenv() -> None:
    """Load the project .env into os.environ.

    ``tools.base_tool`` does this for the tool package, but this client is
    also driven from scripts and from its own CLI, where that import may not
    happen. Without the token from .env every call answers "session expired",
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

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class MolicaError(RuntimeError):
    """A Molica API call failed.

    `ret` carries Molica's numeric result code when the server returned one.
    """

    def __init__(self, message: str, ret: Optional[int] = None) -> None:
        super().__init__(message)
        self.ret = ret


class MolicaAuthError(MolicaError):
    """The session token is missing, expired, or rejected."""


class MolicaClient:
    """Thin client over the endpoints Molica's web app uses."""

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        api_version: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.token = token if token is not None else (os.environ.get("MOLICA_TOKEN") or "")
        self.base_url = (
            base_url or os.environ.get("MOLICA_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.api_version = (
            api_version or os.environ.get("MOLICA_API_VERSION") or DEFAULT_API_VERSION
        )
        self.timeout = timeout
        self._session = None
        self.last_create: dict = {}

    # -- plumbing ---------------------------------------------------------

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def headers(self, extra: Optional[dict] = None) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _UA,
            "x-common-params": f"channel={os.environ.get('MOLICA_CHANNEL', DEFAULT_CHANNEL)}",
            "plat": "web_browser",
            "x-platform": "web_browser",
            "ver": self.api_version,
            "x-locale": os.environ.get("MOLICA_LOCALE", DEFAULT_LOCALE),
        }
        if self.token:
            headers["x-token-id"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def post(
        self,
        path: str,
        payload: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> dict:
        """POST a JSON body to an API path and return the decoded envelope."""
        response = self.session.post(
            f"{self.base_url}{path}",
            json=payload or {},
            headers=self.headers(headers),
            timeout=self.timeout,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise MolicaError(
                f"{path} returned HTTP {response.status_code} with a non-JSON body: "
                f"{response.text[:200]}"
            ) from exc
        return self._check(path, body)

    @staticmethod
    def _check(path: str, body: dict) -> dict:
        ret = body.get("ret")
        if ret is None:
            status = body.get("status")
            if status in (None, "Success"):
                return body
            if status == "Unauthorized":
                raise MolicaAuthError(f"{path}: session rejected (Unauthorized)")
            raise MolicaError(f"{path} failed: status={status} msg={body.get('msg')}")
        if ret in RET_AUTH_FAILED:
            raise MolicaAuthError(
                f"{path}: Molica session is not valid ({body.get('msg')}). Log in again."
            )
        # Molica reports business failures with positive codes, so only ret=1
        # means success: 10001 is a bad request, 2017 is "not enough credits",
        # 500 is "the model is busy". Treating those as success is what makes a
        # refused generation look like a submitted one.
        if ret != 1:
            raise MolicaError(f"{path} failed: ret={ret} msg={body.get('msg')}", ret=ret)
        return body

    @staticmethod
    def data_of(body: dict) -> dict:
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    # -- account ----------------------------------------------------------

    def account(self) -> dict:
        """Return the signed-in account profile."""
        return self.data_of(self.post("/v1/account/get"))

    def send_sms_code(self, phone: str) -> dict:
        """Ask the server to text a verification code to `phone`."""
        return self.post(
            "/v1/account/binding/send_sms_v2", {"phone_number": phone, "type": 3}
        )

    def phone_login(self, phone: str, verification_code: str) -> str:
        """Exchange a phone number plus SMS code for a session token."""
        body = self.post(
            "/v1/account/phone_login",
            {
                "platform": 6,
                "phone": phone,
                "verification_code": verification_code,
                "invitation_code": "",
            },
        )
        token = self.data_of(body).get("token")
        if not token:
            raise MolicaError(f"phone_login returned no token: {body}")
        return str(token)

    # -- video ------------------------------------------------------------

    def video_params(self) -> list:
        """Return the live model catalog (this endpoint needs no session)."""
        body = self.post("/v1/video/params")
        params = self.data_of(body).get("video_params")
        return params if isinstance(params, list) else []

    def running_tasks(self) -> list:
        """Tasks the account still has in flight (queued or generating).

        Molica runs one video task at a time per account: a submission while
        another is in flight is refused with ret=10033. The fast queue is the
        way around it, but it is billed while the standard queue is not.
        """
        running = []
        for page_no in (1, 2):
            page = self.list_videos(page_no=page_no, page_size=30)
            for item in page.get("items") or []:
                if item.get("waiting") or item.get("status") == 0:
                    running.append(item)
            if not page.get("has_next"):
                break
        return running

    def create_video(self, payload: dict) -> Optional[str]:
        """Submit a generation task.

        The web API answers a submission with ``{"ret": 1, "msg": "success"}``
        and no id, so the caller may get ``None`` back and has to resolve the
        task through :meth:`find_video`.

        Molica answers ret=500 ("service busy") when the selected model's
        upstream compute is congested. That is a per-model condition rather
        than a bad request, so it is reported as such.
        """
        try:
            body = self.post("/v1/video/create", payload, headers={"plat": "iOS"})
        except MolicaError as exc:
            if exc.ret == 10033:
                model = payload.get("model_group_key", "?")
                raise MolicaError(
                    f"Molica already has a video task in flight, so {model!r} "
                    f"was not submitted (ret=10033). The account runs one video "
                    f"task at a time: wait for the running task to finish, or "
                    f"enable the fast queue (fast_model=true), which is billed.",
                    ret=exc.ret,
                ) from exc
            if exc.ret == 500:
                model = payload.get("model_group_key", "?")
                raise MolicaError(
                    f"Molica reports model {model!r} as busy (upstream compute "
                    f"congested). Pick another model or retry later. ({exc})",
                    ret=exc.ret,
                ) from exc
            raise
        self.last_create = body
        data = self.data_of(body)
        for key in ("video_id", "id", "videoId"):
            value = data.get(key)
            if value:
                return str(value)
        message = str(body.get("msg") or "").strip()
        if message and message.lower() not in ("success", "ok", "成功"):
            raise MolicaError(
                f"Molica took the request but created no task: {message}. The model "
                "is usually unavailable for this membership, or the account's credit "
                "balance is too low for it."
            )
        return None

    def find_video(
        self,
        prompt: str,
        model_group_key: str,
        params: Optional[dict] = None,
    ) -> Optional[dict]:
        """Return the newest list entry matching a prompt and model.

        Used to recover the id of a task that was just submitted, because the
        create endpoint does not echo one back.

        The same prompt can appear many times in one account's history, so the
        match is narrowed by the request parameters when they are known
        (duration, resolution, queue, aspect ratio) and the newest match wins.
        Otherwise a fresh submission can be mistaken for an older task with
        the same prompt but a different resolution or duration.
        """
        target = (prompt or "").strip()
        wanted = {key: value for key, value in (params or {}).items() if value is not None}
        matches: list[dict] = []
        loose: list[dict] = []
        for page_no in (1, 2):
            page = self.list_videos(page_no=page_no, page_size=30)
            items = page.get("items") or []
            for item in items:
                request = item.get("req") or {}
                if item.get("model_group_key") != model_group_key:
                    continue
                if target not in {
                    str(request.get("prompt") or "").strip(),
                    str(item.get("prompt") or "").strip(),
                }:
                    continue
                loose.append(item)
                if not self._params_match(request, wanted):
                    continue
                matches.append(item)
            if not page.get("has_next"):
                break
        if not matches:
            # The stored request may not echo every parameter, so fall back to
            # the prompt and model alone rather than never resolving the task.
            matches = loose
        if not matches:
            return None
        return max(matches, key=self._recency)

    @staticmethod
    def _params_match(request: dict, wanted: dict) -> bool:
        """Check the request fields that distinguish two tasks of one prompt."""
        for key, value in wanted.items():
            if key not in request:
                continue
            current = request.get(key)
            if isinstance(value, bool) or isinstance(current, bool):
                if bool(current) != bool(value):
                    return False
            elif str(current) != str(value):
                return False
        return True

    @staticmethod
    def _recency(item: dict) -> str:
        """Sort key for list entries: the most recently updated one wins."""
        return str(item.get("updated_at") or item.get("create_time") or "")

    def get_video(self, video_id: str) -> dict:
        """Return one task's state (status, video_url, failure_reason)."""
        return self.data_of(self.post("/v1/video/getvideoinfo", {"video_id": video_id}))

    def balance(self) -> Optional[int]:
        """Return the account's credit balance (the site calls it 灵力)."""
        try:
            body = self.post("/v1/integral/balance/query")
        except MolicaError:
            return None
        data = self.data_of(body)
        for key in ("balance", "integral", "integral_balance"):
            if key in data:
                try:
                    return int(data[key])
                except (TypeError, ValueError):
                    return None
        return None

    def integral_logs(self, page_no: int = 1, page_size: int = 15) -> list:
        """Return the credit ledger, newest first.

        Each row says what a generation cost (``integral_change_num``) and
        which model produced it (``change_desc``), which is the only reliable
        way to price a model for a given membership.
        """
        data = self.data_of(
            self.post("/v1/integral/log/query", {"page_no": page_no, "page_size": page_size})
        )
        items = data.get("items")
        return items if isinstance(items, list) else []

    def model_names(self) -> dict:
        """Map ``model_key`` -> display name for every video model.

        The ledger names models the way the site labels them ("Wan3.0-高稳版"),
        so the display names are what make a ledger row attributable.
        """
        body = self.post("/v1/models/snapshot/all", {"check_params": ""})
        models = self.data_of(body).get("models") or {}
        names: dict[str, str] = {}
        for entry in models.get("video") or []:
            key = entry.get("model_key")
            name = entry.get("model_name")
            if key and name:
                names[str(key)] = str(name)
        return names


    def list_videos(self, page_no: int = 1, page_size: int = 30) -> dict:
        """Return the account's video list page."""
        return self.data_of(
            self.post(
                "/v1/video/page",
                {"page_no": page_no, "page_size": page_size, "fastModel": False},
            )
        )

    # -- uploads ----------------------------------------------------------

    def upload_image(self, path: Path) -> str:
        """Upload a local image and return its public URL.

        Molica's web app stores user images in its own Aliyun OSS bucket. The
        server hands out short-lived STS credentials for that bucket
        (``/v1/models/snapshot/oss/authorize``); the file is then PUT directly
        to OSS with those credentials. The resulting object is readable at
        ``static.molica.com.cn``, which is the URL the video API expects for
        reference images.
        """
        try:
            import oss2
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise MolicaError(
                "uploading a local image to Molica needs the oss2 package "
                "(pip install oss2, or declare it in requirements.txt). "
                "Public image URLs need no upload."
            ) from exc

        path = Path(path)
        if not path.is_file():
            raise MolicaError(f"cannot upload missing file: {path}")

        config = self.data_of(
            self.post("/v1/models/snapshot/oss/authorize", {"oss_code": "ali-beijing"})
        )
        prefix = str(config.get("object_key_prefix") or "prompt/image").strip("/")
        base_url = str(config.get("base_url") or "").rstrip("/")
        bucket = str(config.get("bucket_name") or "")
        if not bucket or not base_url:
            raise MolicaError(
                "Molica did not return usable OSS upload credentials: "
                f"{config}"
            )

        auth = oss2.StsAuth(
            str(config.get("access_key_id") or ""),
            str(config.get("access_key_secret") or ""),
            str(config.get("security_token") or ""),
        )
        bucket_obj = oss2.Bucket(auth, str(config.get("endpoint") or ""), bucket)

        name = f"{path.stem}_{int(time.time() * 1000)}{path.suffix}"
        key = f"{prefix}/{name}"
        bucket_obj.put_object_from_file(key, str(path))

        return f"{base_url}/{quote(name)}"

    def download(self, url: str, destination: Path | str) -> Path:
        """Download a finished video to `destination`."""
        destination = Path(destination)
        response = self.session.get(url, timeout=300, stream=True)
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                if chunk:
                    handle.write(chunk)
        return destination


# -- environment helpers ---------------------------------------------------

def read_env_value(env_path: Path, key: str) -> str:
    """Read `key` from a .env file (falls back to the process environment)."""
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
    """Set `key` in a .env file, replacing an existing line or appending."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# -- harvesting a token from a logged-in browser ----------------------------

_JWT_PATTERN = re.compile(rb"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")


def _chrome_leveldb_dirs() -> list:
    """Chrome profile directories that keep Local Storage."""
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    if not root.is_dir():
        return []
    leveldb = []
    for profile in sorted(root.iterdir()):
        if not profile.is_dir():
            continue
        store = profile / "Local Storage" / "leveldb"
        if store.is_dir():
            leveldb.append(store)
    return leveldb


def _claims_of(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def browser_tokens() -> list:
    """Return candidate session tokens found in Chrome's local storage.

    Chrome keeps localStorage in a LevelDB store next to the profile. The
    values are readable while the browser runs, so a token saved by the web
    app can be picked up without touching the browser UI.
    """
    found = []
    for store in _chrome_leveldb_dirs():
        for path in sorted(store.glob("*")):
            if path.suffix not in {".ldb", ".log"}:
                continue
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            for match in _JWT_PATTERN.finditer(blob):
                token = match.group(0).decode("ascii", "ignore")
                claims = _claims_of(token)
                if "exp" in claims and "uid" in claims and "username" in claims:
                    found.append((int(claims["exp"]), token))
    found.sort(reverse=True)
    return [token for _, token in found]


def harvest_token(client: "MolicaClient") -> Optional[str]:
    """Return the newest browser token the Molica API accepts."""
    for token in browser_tokens():
        client.token = token
        try:
            client.account()
        except MolicaError:
            continue
        return token
    return None


# -- model catalog cache --------------------------------------------------

CATALOG_PATH = Path(__file__).resolve().parent / "catalog.json"


def build_catalog(params: list) -> dict:
    """Flatten /v1/video/params into a cache-friendly structure."""
    models: dict[str, dict] = {}
    for entry in params or []:
        key = entry.get("model_key")
        if not key:
            continue
        buckets: dict[str, list[str]] = {}
        groups = (entry.get("model_params") or {}).get("base") or []
        for group in groups:
            param_key = str(group.get("param_key") or "")
            values = [
                str(item.get("param_value"))
                for item in (group.get("items") or [])
                if item.get("param_value") is not None
            ]
            if param_key and values:
                buckets[param_key] = values
        models[str(key)] = {
            "durations": buckets.get("duration", []),
            "resolutions": buckets.get("resolution", []),
            "video_scales": buckets.get("video_scale", []),
            "prompt_limit": entry.get("prompt_limit"),
        }
    return {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "/v1/video/params",
        "model_keys": sorted(models),
        "models": models,
    }


def write_catalog(catalog: dict, path: Path = CATALOG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


CREDITS_PATH = Path(__file__).resolve().parent / "credits.json"


# The ledger is not video-only: the same account spends credits on image
# creation ("图片创作", "生成图片"), chat ("文字对话"), web search ("联网搜索"),
# deep reasoning ("深度思考") and image selection ("选择图片"), and the gift
# balance expires ("赠送灵力过期"). Only rows that say a video was generated
# describe a price this tool must respect, so everything else is excluded
# rather than pooled in.
#
# A video row is either "使用"X"的"生成视频"1次" or "使用"X"的"15s+720P生成"1次".
# The image rows use "生成图片" or "图片创作", which is why the action is
# matched to the end of the string rather than searched for "生成".
_VIDEO_ACTION_RE = re.compile(r"(?:生成视频|\d+s\+\d+[Pp]生成)$")

# A price is really a price of one *specification*: "15s+720P生成" and
# "10s+720P生成" cost different amounts for the same model. These patterns
# pull the duration and resolution out of a ledger description.
_SPEC_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s\s*\+\s*(\d{3,4})\s*[Pp]")


def _parse_spec(desc: str) -> Optional[dict]:
    """Return the duration/resolution a ledger row was billed for, if named."""
    match = _SPEC_RE.search(desc)
    if not match:
        return None
    duration = float(match.group(1))
    resolution = f"{int(match.group(2))}P"
    return {"duration": duration, "resolution": resolution}


def _is_video_charge(desc: str) -> bool:
    """True when a ledger row is billed for producing a video."""
    if not desc.startswith("使用"):
        return False  # e.g. "赠送灵力过期" — the gift balance lapsing
    quoted = re.findall(r'"([^"]+)"', desc)
    if len(quoted) >= 2:
        action = quoted[1]
    else:
        # Unquoted form: "使用Wan3.0-高稳版生成视频" / "使用视频模型 V1生成视频".
        action = desc
    return bool(_VIDEO_ACTION_RE.search(action))


def _normalise_label(label: str) -> str:
    """Ledger labels carry stray whitespace ("Sora2 Pro (高清版) ")."""
    return " ".join(str(label).split())


def learn_credit_prices(logs: list, names: dict, aliases: Optional[dict] = None) -> dict:
    """Derive the observed credit prices per video model from the ledger.

    A ledger row charges a negative amount and names the model in its
    description ("使用Wan3.0-高稳版生成视频" or '使用"Veo3.1 Pro"的"生成视频"1次').
    Refunds are positive and are ignored, so a refunded run never lowers the
    learned price. Rows that do not describe producing a video are skipped.

    The result is the cheapest run this account actually paid for, which is
    the number a balance check has to compare against. Because a price belongs
    to a specification, each entry also records the ``duration`` and
    ``resolution`` it was billed at; an entry without them was billed at the
    model's default specification.

    Rows whose label names a model the live snapshot no longer lists are kept
    under ``labels`` — the price is still measured, only the model key is
    unknown. ``aliases`` maps such a label to a model key by hand, which turns
    it into a usable price.
    """
    prices: dict[str, dict] = {}
    labels: dict[str, dict] = {}
    unmatched: list[dict] = []
    by_name = {_normalise_label(name): key for key, name in names.items() if name}
    # Longest name first, so a name that contains another is not shadowed.
    ordered = sorted(by_name.items(), key=lambda item: len(item[0]), reverse=True)
    clean_aliases = {_normalise_label(k): v for k, v in (aliases or {}).items()}

    def record(store: dict, key: str, cost: int, desc: str) -> None:
        spec = _parse_spec(desc)
        known = store.get(key)
        if known is None:
            known = {"credits": cost, "sample": desc, "specs": {}}
            store[key] = known
            if spec:
                known["duration"] = spec["duration"]
                known["resolution"] = spec["resolution"]
        if cost < known["credits"]:
            known["credits"] = cost
            known["sample"] = desc
            if spec:
                known["duration"] = spec["duration"]
                known["resolution"] = spec["resolution"]
        if spec:
            # A price belongs to a specification, so keep every observed one
            # and let the caller pick the one that matches its request.
            spec_key = f'{spec["duration"]:g}s+{spec["resolution"]}'
            previous = known["specs"].get(spec_key)
            if previous is None or cost < previous:
                known["specs"][spec_key] = cost

    for log in logs or []:
        amount = log.get("integral_change_num")
        if not isinstance(amount, (int, float)) or amount >= 0:
            continue  # a refund, not a charge
        desc = str(log.get("change_desc") or "")
        if not _is_video_charge(desc):
            continue  # image, chat, search, or an expiring gift
        cost = -int(amount)
        quoted = re.findall(r'"([^"]+)"', desc)
        if quoted:
            label = _normalise_label(quoted[0])
            key = by_name.get(label) or clean_aliases.get(label)
            if key:
                record(prices, key, cost, desc)
            else:
                record(labels, label, cost, desc)
            continue
        for name, key in ordered:
            if name and name in desc:
                record(prices, key, cost, desc)
                break
        else:
            unmatched.append({"credits": cost, "sample": desc})
    return {"prices": prices, "labels": labels, "unmatched": unmatched}


def read_aliases(path: Path = CREDITS_PATH) -> dict:
    """Return the hand-written label -> model_key aliases kept in the cache."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    aliases = data.get("aliases") if isinstance(data, dict) else None
    return aliases if isinstance(aliases, dict) else {}


def write_prices(learned: dict, path: Path = CREDITS_PATH, aliases: Optional[dict] = None) -> Path:
    payload = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "/v1/integral/log/query",
        "prices": learned.get("prices") or {},
        "labels": learned.get("labels") or {},
        "aliases": aliases or {},
        "unmatched": learned.get("unmatched") or [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# -- CLI -------------------------------------------------------------------

def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools._molica.client",
        description="Log into Molica and store the session token for OpenMontage.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Phone + SMS login, stores MOLICA_TOKEN in .env")
    login.add_argument("--phone", required=True, help="Phone number registered with Molica")
    login.add_argument("--code", help="SMS verification code; omit to only send the code")
    login.add_argument("--env", default=".env", help="env file to update (default: .env)")

    check = sub.add_parser("check", help="Verify the stored token")
    check.add_argument("--env", default=".env", help="env file to read (default: .env)")

    harvest_cmd = sub.add_parser(
        "harvest", help="Take the session token from a logged-in Chrome and store it in .env"
    )
    harvest_cmd.add_argument("--env", default=".env", help="env file to update (default: .env)")

    refresh = sub.add_parser(
        "refresh-catalog",
        help="Cache the live Molica model catalog so new models become selectable",
    )
    refresh.add_argument("--env", default=".env", help="env file to read (default: .env)")
    refresh.add_argument("--out", default=str(CATALOG_PATH), help="cache file to write")

    prices = sub.add_parser(
        "refresh-credits",
        help="Learn the cheapest observed credit price per video model",
    )
    prices.add_argument("--env", default=".env", help="env file to read (default: .env)")
    prices.add_argument("--out", default=str(CREDITS_PATH), help="cache file to write")
    prices.add_argument("--pages", type=int, default=3, help="ledger pages to read")
    prices.add_argument(
        "--alias",
        action="append",
        default=[],
        metavar="LABEL=MODEL_KEY",
        help="Map a ledger model label to a model key (repeatable)",
    )

    args = parser.parse_args(argv)
    env_path = Path(args.env)

    if args.command == "login":
        client = MolicaClient()
        if not args.code:
            client.send_sms_code(args.phone)
            print(f"Verification code sent to {args.phone}. Re-run with --code <code>.")
            return 0
        token = client.phone_login(args.phone, args.code)
        write_env_value(env_path, "MOLICA_TOKEN", token)
        client.token = token
        profile = client.account()
        print(f"Logged in as {profile.get('nick') or profile.get('mid')}. Token written to {env_path}.")
        return 0

    if args.command == "check":
        token = read_env_value(env_path, "MOLICA_TOKEN")
        if not token:
            print(f"No MOLICA_TOKEN found in {env_path} or the environment.")
            return 1
        client = MolicaClient(token=token)
        try:
            profile = client.account()
        except MolicaAuthError as exc:
            print(f"Token rejected: {exc}")
            return 1
        print(f"Token valid for {profile.get('nick') or profile.get('mid')}.")
        return 0

    if args.command == "harvest":
        client = MolicaClient()
        token = harvest_token(client)
        if not token:
            print(
                "No usable Molica session found in Chrome. Log in at "
                "https://web.molica.com.cn/chat/new (WeChat scan), then re-run."
            )
            return 1
        write_env_value(env_path, "MOLICA_TOKEN", token)
        client.token = token
        profile = client.account()
        print(
            f"Harvested a valid session for {profile.get('nick') or profile.get('mid')}. "
            f"Token written to {env_path}."
        )
        return 0

    if args.command == "refresh-catalog":
        client = MolicaClient()
        catalog = build_catalog(client.video_params())
        out = write_catalog(catalog, Path(args.out))
        print(f"Cached {len(catalog['model_keys'])} model keys to {out}.")
        return 0

    if args.command == "refresh-credits":
        client = MolicaClient()
        names = client.model_names()
        aliases = read_aliases(Path(args.out))
        for pair in args.alias or []:
            label, _, key = pair.partition("=")
            if label and key:
                aliases[label.strip()] = key.strip()
        logs: list = []
        for page_no in range(1, max(1, args.pages) + 1):
            rows = client.integral_logs(page_no=page_no, page_size=50)
            if not rows:
                break
            logs.extend(rows)
        learned = learn_credit_prices(logs, names, aliases=aliases)
        out = write_prices(learned, Path(args.out), aliases=aliases)
        print(f"Learned prices for {len(learned['prices'])} models -> {out}.")
        for key, spec in sorted(learned["prices"].items()):
            print(f"  {key}: {spec['credits']} credits")
        for label, spec in sorted(learned["labels"].items()):
            print(f"  ? {label}: {spec['credits']} credits (label only)")
        if learned["unmatched"]:
            print(f"  {len(learned['unmatched'])} ledger rows did not name a known model.")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_main())
