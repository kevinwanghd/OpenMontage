"""Generate video through a Molica membership (web.molica.com.cn).

Molica exposes no public API: its web app posts JSON to endpoints under
``/api`` with a session token issued at login. This tool speaks that same
contract with the account's own token, so an existing membership can supply
video generation to OpenMontage pipelines.

Every call spends the account's credits (the site calls them 灵力). The token
expires; refresh it with ``python -m tools._molica.client login``.

Reference images can be given as public URLs or as local file paths. Local
files are uploaded to Molica's own object storage first (the web app does the
same), which is what the video API expects for `image_url`.
"""

from __future__ import annotations

import json
import os
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
    ToolTier,
)
from tools.provider_pricing import PriceQuoteRequired

# Snapshot of /api/v1/video/params taken 2026-11-06. The live catalog is
# authoritative; this list only powers discovery and model routing.
KNOWN_MODEL_KEYS = (
    "veo31pro",
    "veo31",
    "veo3pro",
    "sora2pro",
    "sora2prohd",
    "sora2",
    "luma",
    "mj_video",
    "wanx",
    "wanx26",
    "wanx27",
    "wan30video",
    "wan30prime",
    "molicas2",
    "veo",
    "jimeng",
    "jimeng3Lite",
    "jimeng3pro",
    "jimeng35pro",
    "seedance2",
    "minimaxh3",
    "minimaxh3open",
    "seedance25",
    "happyHorse1",
    "happyHorse11",
    "zhipu",
)

_DEFAULT_MODEL = "wan30video"
_STATUS_FAILED = 4

# Live model catalog cache, written by
# ``python -m tools._molica.client refresh-catalog``. It lets models that
# appeared after the static snapshot above be selected by the routing layer.
_CATALOG_PATH = Path(__file__).resolve().parents[1] / "_molica" / "catalog.json"


def load_cached_catalog(path: Optional[Path] = None) -> dict:
    """Return the cached live catalog, or an empty dict if there is none."""
    target = Path(path) if path is not None else _CATALOG_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Credit prices learned from the account's own ledger, written by
# ``python -m tools._molica.client refresh-credits``. They are measured, so
# they outrank the hand-written table below.
_CREDITS_PATH = Path(__file__).resolve().parents[1] / "_molica" / "credits.json"
_PRICE_CACHE: dict = {"path": None, "mtime": None, "prices": {}}


def load_learned_prices(path: Optional[Path] = None) -> dict:
    """Return {model_key: cheapest observed credit price} from the ledger cache."""
    return {
        key: entry["credits"]
        for key, entry in load_learned_price_entries(path).items()
        if entry.get("credits") is not None
    }


def load_learned_price_entries(path: Optional[Path] = None) -> dict:
    """Return {model_key: price entry} from the ledger cache.

    An entry keeps the specification it was billed at, because a price belongs
    to a duration/resolution pair rather than to the model as a whole.
    """
    target = Path(path) if path is not None else _CREDITS_PATH
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return {}
    if _PRICE_CACHE["path"] == target and _PRICE_CACHE["mtime"] == mtime:
        return _PRICE_CACHE["prices"]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    prices: dict[str, dict] = {}
    for key, spec in (data.get("prices") or {}).items():
        if not isinstance(spec, dict) or spec.get("credits") is None:
            continue
        try:
            credits = int(spec["credits"])
        except (TypeError, ValueError):
            continue
        entry = {"credits": credits}
        if spec.get("duration") is not None:
            entry["duration"] = float(spec["duration"])
        if spec.get("resolution"):
            entry["resolution"] = str(spec["resolution"])
        observed = spec.get("specs")
        if isinstance(observed, dict) and observed:
            entry["specs"] = {
                str(k): int(v) for k, v in observed.items() if isinstance(v, (int, float))
            }
        prices[str(key)] = entry
    _PRICE_CACHE.update(path=target, mtime=mtime, prices=prices)
    return prices


# Cheapest observed price, in credits (灵力), of one generation per model.
# Measured on this account from /v1/integral/log/query on 2026-11-07; the site
# publishes no tariff, so these are the only ground truth available. They are
# used as a lower bound: a balance below the cheapest run means no run of that
# model can succeed. Set MOLICA_CREDIT_COST_<MODEL> to correct them.
#
# Only prices this account actually paid are listed. A model that was never
# run has no known price, and an unknown price never blocks a run.
_MEASURED_MIN_CREDITS = {
    "wan30video": 105,
    "minimaxh3open": 157,
    "molicas2": 171,
}


# How long to wait for a submitted task to produce its video.
#
# Measured on this account on 2026-11-07: a 5s 480P wan30video run took about
# 62 minutes from submission to a playable video_url. The default therefore has
# to be generous — a timeout that fires while the task is still healthy throws
# away a run the account already paid for. The site's own frontend waits on a
# websocket rather than polling, so it publishes no matching deadline.
_DEFAULT_TIMEOUT_SECONDS = 7200.0

# Models observed serving while other models were congested. A busy model
# answers ret=500, so a run can fail over to one of these instead of dying.
# A failover must not turn into a sweep of the whole catalog: each attempt can
# create a real task, and the account pays for the ones that finish.
_MAX_FAILOVER_ATTEMPTS = 3

_FAILOVER_MODELS = (
    "molicas2",
    "wan30video",
    "wan30prime",
    "wanx26",
    "wanx27",
)


def _credit_cost(model: str, duration=None, resolution=None, fast_model: bool = True) -> Optional[int]:
    """Known credit price of one run of ``model``.

    Only the fast (priority) queue is billed on this membership: standard-queue
    runs produced no charge at all in the account ledger, so they cost nothing
    and must never be blocked by a balance check.

    A price belongs to a specification, so when the request names a duration
    and a resolution that the ledger also recorded, that observation wins.
    Otherwise the cheapest observed run of the model is used, which is the
    floor a balance check needs.

    The learned ledger prices win over the hand-written table, because they
    are what this account actually paid.
    """
    if not fast_model:
        return 0
    override = os.environ.get(f"MOLICA_CREDIT_COST_{model.upper()}")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    entry = load_learned_price_entries().get(model)
    if entry:
        if duration is not None and resolution is not None:
            key = f"{float(duration):g}s+{str(resolution).upper()}"
            observed = (entry.get("specs") or {}).get(key)
            if observed is not None:
                return int(observed)
        if entry.get("credits") is not None:
            return int(entry["credits"])
    return _MEASURED_MIN_CREDITS.get(model)


def _credit_cost_source(model: str, duration=None, resolution=None) -> str:
    """Where the price came from: env, the ledger, the static table, or nowhere."""
    if os.environ.get(f"MOLICA_CREDIT_COST_{model.upper()}"):
        return "env"
    entry = load_learned_price_entries().get(model)
    if entry:
        if duration is not None and resolution is not None:
            key = f"{float(duration):g}s+{str(resolution).upper()}"
            if key in (entry.get("specs") or {}):
                return "ledger_spec"
        return "ledger"
    if model in _MEASURED_MIN_CREDITS:
        return "static"
    return "unknown"


class MolicaVideo(BaseTool):
    """Generate video with a Molica membership through Molica's web API."""

    name = "molica_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "molica"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:MOLICA_TOKEN"]
    install_instructions = (
        "Molica has no public API; it authenticates with a session token that "
        "belongs to one account. Get one with:\n"
        "  python -m tools._molica.client login --phone <your phone>\n"
        "then re-run with --code <sms code> to store MOLICA_TOKEN in .env. "
        "Every generation spends that account's credits."
    )
    agent_skills = ["molica-video", "ai-video-gen"]

    capabilities = [
        "text_to_video",
        "image_to_video",
        "reference_to_video",
        "task_create",
        "task_query",
    ]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_to_video": True,
        "first_last_frame_to_video": True,
        "multiple_reference_images": True,
        "native_audio": True,
        "public_image_urls_only": False,
        "local_image_upload": True,
        "aspect_ratio": True,
        "resolution": True,
        "duration": True,
    }
    best_for = [
        "one paid membership covering many video models (Veo, Sora, Seedance, "
        "Jimeng, Wan, MiniMax) in one place",
        "Chinese-language prompts and models such as jimeng3pro or seedance2",
    ]
    not_good_for = [
        "offline generation",
        "workloads that need a documented, stable public API contract",
    ]
    fallback_tools = ["veo_video", "kling_video", "seedance_ark", "wan_video"]

    input_schema = {
        "type": "object",
        # Either a prompt to generate from, or the id of a task that was
        # already submitted: collecting such a task needs no prompt, because
        # the account has already paid for it.
        "anyOf": [
            {"required": ["prompt"]},
            {"required": ["video_id"]},
            {"required": ["task_id"]},
        ],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Video prompt. Model-specific character limits apply.",
            },
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video", "reference_to_video"],
                "description": (
                    "Derived from the image inputs when omitted. "
                    "reference_to_video needs reference_image_urls."
                ),
            },
            "model": {
                "type": "string",
                "enum": list(KNOWN_MODEL_KEYS),
                "default": _DEFAULT_MODEL,
                "description": "Molica model_key. The live catalog is authoritative.",
            },
            "duration": {
                "type": "integer",
                "description": "Seconds. Must be offered by the selected model.",
            },
            "aspect_ratio": {
                "type": "string",
                "description": "Video scale, e.g. 16:9, 9:16, 1:1.",
            },
            "resolution": {
                "type": "string",
                "description": "Resolution offered by the selected model, e.g. 1080P.",
            },
            "image_url": {
                "type": "string",
                "description": "Public URL of the first frame or single reference image.",
            },
            "image_end_url": {
                "type": "string",
                "description": "Public URL of the last frame (first/last frame mode).",
            },
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Public URLs of multiple reference images.",
            },
            "image_path": {
                "type": "string",
                "description": (
                    "Local image file used as first frame or single reference. "
                    "Uploaded to Molica's object storage before the request."
                ),
            },
            "image_end_path": {
                "type": "string",
                "description": "Local image file used as last frame (first/last frame mode).",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Local image files used as multiple reference images.",
            },
            "fast_model": {
                "type": "boolean",
                "default": False,
                "description": "Fast queue. Off uses the standard queue and costs less.",
            },
            "expand_prompt": {
                "type": "boolean",
                "default": True,
                "description": "Let Molica rewrite the prompt before generation.",
            },
            "loop": {
                "type": "boolean",
                "default": False,
                "description": "Loop the clip (models that offer it).",
            },
            "output_path": {
                "type": "string",
                "description": "Where to save the finished mp4.",
            },
            "video_id": {
                "type": "string",
                "description": (
                    "Collect an already-submitted task instead of creating one. "
                    "A run that timed out while waiting keeps this id, and the "
                    "account has already paid for it, so no new task is created "
                    "and no further credits are spent. No prompt is needed: the "
                    "task record carries the request that produced it."
                ),
            },
            "disable_failover": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Fail instead of trying another model when the requested "
                    "model's upstream is congested."
                ),
            },
            "task_id": {
                "type": "string",
                "description": (
                    "Alias for video_id: resume an existing Molica task instead "
                    "of creating one. Use it when a queued task outlived the "
                    "process that submitted it."
                ),
            },
            "poll_interval_seconds": {"type": "number", "default": 8.0},
            "timeout_seconds": {"type": "number", "default": _DEFAULT_TIMEOUT_SECONDS},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["timeout", "rate_limit"])
    resume_support = ResumeSupport.FROM_CHECKPOINT
    idempotency_key_fields = [
        "prompt",
        "video_id",
        "model",
        "operation",
        "duration",
        "resolution",
        "aspect_ratio",
        "image_url",
        "image_end_url",
        "reference_image_urls",
        "fast_model",
        "expand_prompt",
        "loop",
    ]
    side_effects = [
        "Spends credits on the Molica account tied to MOLICA_TOKEN",
        "Submits a billable generation task to the Molica server",
    ]
    user_visible_verification = [
        "The finished mp4 lands at output_path",
        "Its duration and resolution match the requested model parameters",
    ]

    # -- contract helpers --------------------------------------------------

    def __init__(self) -> None:
        super().__init__()
        self._catalog = load_cached_catalog()
        models = self._catalog.get("models") or {}
        extra = sorted(set(models) - set(KNOWN_MODEL_KEYS))
        if extra:
            schema = dict(self.input_schema)
            properties = dict(schema.get("properties") or {})
            model_prop = dict(properties.get("model") or {})
            model_prop["enum"] = list(KNOWN_MODEL_KEYS) + extra
            properties["model"] = model_prop
            schema["properties"] = properties
            self.input_schema = schema

    def get_info(self) -> dict:
        info = super().get_info()
        models = self._catalog.get("models") or {}
        catalog = {
            key: {"family": "molica", "operation": "text_to_video"} for key in KNOWN_MODEL_KEYS
        }
        for key, spec in models.items():
            entry = dict(catalog.get(key) or {"family": "molica", "operation": "text_to_video"})
            entry["durations"] = spec.get("durations") or []
            entry["resolutions"] = spec.get("resolutions") or []
            entry["from_live_catalog"] = True
            catalog[key] = entry
        info["model_catalog"] = catalog
        info["model_catalog_updated_at"] = self._catalog.get("updated_at")
        prices = load_learned_prices()
        if prices:
            info["credit_prices"] = prices
        return info

    def estimate_cost(self, inputs: dict) -> float:
        """Cash cost of one video, when the account's credit price is known.

        Molica bills against prepaid 灵力 credits, so a run carries no separate
        cash charge. Reporting ``0.0`` would tell the selector that this
        provider is free and outrank every paid one, so an unknown credit price
        is reported as "needs a quote" instead: the selector treats an unpriced
        provider as unknown, not as free.
        """
        override = os.environ.get("MOLICA_COST_USD_PER_VIDEO")
        if override:
            return float(override)
        rate = os.environ.get("MOLICA_USD_PER_CREDIT")
        if rate:
            required = _credit_cost(
                str(inputs.get("model") or _DEFAULT_MODEL),
                inputs.get("duration"),
                inputs.get("resolution"),
            )
            if required is not None:
                return round(required * float(rate), 6)
        raise PriceQuoteRequired(
            "Molica spends prepaid 灵力 credits. Set MOLICA_COST_USD_PER_VIDEO for "
            "a flat price, or MOLICA_USD_PER_CREDIT to convert credits into USD."
        )

    def dry_run(self, inputs: dict) -> dict:
        """Preflight with an honest cost status for a membership-backed tool."""
        info = super().dry_run(inputs)
        if not (
            os.environ.get("MOLICA_COST_USD_PER_VIDEO")
            or os.environ.get("MOLICA_USD_PER_CREDIT")
        ):
            info["cost_status"] = "membership_quota"
            info["quote_reason"] = (
                "Molica spends the account's prepaid 灵力 credits; no separate "
                "cash charge per video. Set MOLICA_COST_USD_PER_VIDEO to price "
                "it against a USD budget."
            )
        model = str(inputs.get("model") or _DEFAULT_MODEL)
        fast = bool(inputs.get("fast_model"))
        required = _credit_cost(
            model, inputs.get("duration"), inputs.get("resolution"), fast
        )
        if required is not None:
            info["credits_required"] = required
            info["credits_queue"] = "fast" if fast else "standard"
            if not fast:
                info["credits_required_fast_queue"] = _credit_cost(
                    model, inputs.get("duration"), inputs.get("resolution"), True
                )
            info["credits_source"] = _credit_cost_source(
                model, inputs.get("duration"), inputs.get("resolution")
            )
        catalog_models = self._catalog.get("models") or {}
        if catalog_models:
            info["model_catalog_updated_at"] = self._catalog.get("updated_at")
            info["model_in_live_catalog"] = model in catalog_models
        if os.environ.get("MOLICA_TOKEN"):
            from tools._molica.client import MolicaClient

            balance = self._balance(MolicaClient())
            if balance is not None:
                info["credits_balance"] = balance
                info["credits_sufficient"] = (
                    required is None or balance >= required
                )
        return info

    def estimate_runtime(self, inputs: dict) -> float:
        duration = float(inputs.get("duration") or 8)
        return 60.0 + duration * 15.0

    # -- execution ---------------------------------------------------------

    def execute(self, inputs: dict) -> ToolResult:
        from tools._molica.client import MolicaAuthError, MolicaClient, MolicaError

        prompt = str(inputs.get("prompt") or "").strip()
        # Collecting a task the account has already submitted needs no prompt:
        # the account has paid for that task, and all that is left is to pick
        # the finished video up.
        existing_task = str(
            inputs.get("video_id") or inputs.get("task_id") or ""
        ).strip()
        if not prompt and not existing_task:
            return ToolResult(success=False, error="prompt is required")

        client = MolicaClient()
        if not client.token:
            return ToolResult(
                success=False,
                error="MOLICA_TOKEN is not set. " + self.install_instructions,
            )

        started = time.time()
        poll_interval = float(inputs.get("poll_interval_seconds", 8.0))
        try:
            if existing_task:
                return self._collect(
                    client,
                    inputs,
                    video_id=existing_task,
                    prompt=prompt,
                    poll_interval=poll_interval,
                    timeout=float(
                        inputs.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS * 3)
                    ),
                    started=started,
                )
            return self._create(
                client,
                inputs,
                prompt,
                poll_interval=poll_interval,
                started=started,
            )
        except MolicaAuthError as exc:
            return ToolResult(
                success=False,
                error=f"Molica session rejected: {exc} " + self.install_instructions,
            )
        except MolicaError as exc:
            return ToolResult(
                success=False,
                error=(
                    f"Molica video generation failed: {exc}. "
                    f"Credit state: {self._diagnose(client)}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"Molica video generation failed: {exc}")

    # -- execution: create -------------------------------------------------

    def _create(
        self,
        client,
        inputs: dict,
        prompt: str,
        *,
        poll_interval: float,
        started: float,
    ) -> ToolResult:
        """Submit a new task and wait for its video."""
        from tools._molica.client import MolicaError

        catalog = client.video_params()
        spec = self._find_model(catalog, str(inputs.get("model") or _DEFAULT_MODEL))
        if spec is None:
            available = [entry.get("model_key") for entry in catalog]
            raise MolicaError(
                f"Molica model {inputs.get('model')!r} is not offered. "
                f"Available: {available}"
            )
        resolved = self._resolve_images(client, inputs)
        operation = self._operation(resolved)
        # The standard queue is free but shared, so a task can wait a long
        # time before it starts generating. The billed fast queue runs
        # immediately, so it needs a shorter patience.
        default_timeout = (
            _DEFAULT_TIMEOUT_SECONDS
            if bool(resolved.get("fast_model"))
            else _DEFAULT_TIMEOUT_SECONDS * 3
        )
        timeout = float(inputs.get("timeout_seconds", default_timeout))

        # A model whose upstream is congested answers ret=500 and creates
        # no task. Rather than failing the whole run, try the next model
        # the account can reach.
        refused: list[str] = []
        adapted_params: list[str] = []
        account_balance = self._balance(client)
        # The floor is the cheapest run this account has ever paid for,
        # learned from its own ledger where that is available.
        known_prices = [p for p in _MEASURED_MIN_CREDITS.values() if p]
        known_prices += [
            entry["credits"]
            for entry in load_learned_price_entries().values()
            if entry.get("credits")
        ]
        cheapest_known = min(known_prices) if known_prices else None
        if (
            bool(resolved.get("fast_model"))
            and account_balance is not None
            and cheapest_known is not None
            and account_balance < cheapest_known
        ):
            return ToolResult(
                success=False,
                error=(
                    f"Molica credits are too low: the account holds "
                    f"{account_balance} credits, the cheapest known run costs "
                    f"{cheapest_known}. No task was created."
                ),
                data={
                    "credits_balance": account_balance,
                    "credits_required": cheapest_known,
                    "provider": "molica",
                },
            )

        output_path = Path(inputs.get("output_path") or "molica_video.mp4")
        candidates = self._candidate_models(catalog, str(spec.get("model_key")), inputs)
        attempted = 0
        for model_key in candidates:
            candidate = self._find_model(catalog, model_key)
            if candidate is None:
                continue
            if attempted >= _MAX_FAILOVER_ATTEMPTS:
                refused.append(f"stopped after {_MAX_FAILOVER_ATTEMPTS} models")
                break
            attempted += 1
            overrides, adapted = self._adapt_params(candidate, resolved)
            try:
                payload = self._build_payload(resolved, candidate, prompt, overrides)
            except ValueError:
                # This model cannot do this combination; try the next one.
                refused.append(f"{model_key}: does not support these parameters")
                continue
            if adapted:
                adapted_params.append(
                    f"{model_key}: {overrides.get('duration')}s / {overrides.get('resolution')} / "
                    f"{overrides.get('aspect_ratio')}"
                )

            required = _credit_cost(
                model_key,
                overrides.get("duration") or resolved.get("duration"),
                overrides.get("resolution") or resolved.get("resolution"),
                bool(payload.get("fast_model")),
            )
            if (
                account_balance is not None
                and required is not None
                and account_balance < required
            ):
                refused.append(
                    f"{model_key}: needs {required} credits, balance is {account_balance}"
                )
                continue

            try:
                video_id = client.create_video(payload)
            except MolicaError as exc:
                if exc.ret == 500:
                    refused.append(f"{model_key}: upstream busy")
                    continue
                if exc.ret == 2017:
                    # The account cannot pay for this model. Every other
                    # candidate is at least as expensive, so stop instead of
                    # creating tasks that will only fail and refund.
                    refused.append(f"{model_key}: {exc}")
                    break
                raise
            if not video_id:
                try:
                    video_id = self._resolve_video_id(
                        client,
                        prompt,
                        model_key,
                        poll_interval,
                        timeout,
                        payload,
                    )
                except TimeoutError as exc:
                    raise MolicaError(f"{exc} ({self._diagnose(client)})") from exc
            try:
                info = self._poll(
                    client,
                    video_id,
                    poll_interval=poll_interval,
                    timeout=timeout,
                )
            except TimeoutError as exc:
                # The task is still alive and the account has already paid for
                # it. Dropping its id would throw a paid run away, so it is
                # reported back and can be collected with `video_id`.
                return self._still_running(client, video_id, str(exc), model=model_key)
            video_url = str(info.get("video_url") or "")
            if not video_url:
                raise MolicaError(f"task {video_id} finished without a video_url")
            client.download(video_url, output_path)
            model = str(payload["model_group_key"])
            return self._success(
                client,
                inputs,
                output_path=output_path,
                payload=payload,
                prompt=prompt,
                operation=operation,
                video_id=video_id,
                video_url=video_url,
                info=info,
                adapted_params=adapted_params,
                started=started,
                credits_required=_credit_cost(
                    model,
                    payload.get("duration"),
                    payload.get("resolution"),
                    bool(payload.get("fast_model")),
                ),
                credits_source=_credit_cost_source(
                    model,
                    payload.get("duration"),
                    payload.get("resolution"),
                ),
            )
        return ToolResult(
            success=False,
            error=(
                "No Molica model could serve this run. "
                + "; ".join(refused or ["no candidate model offered"])
                + ". No task was created."
            ),
            data={
                "provider": "molica",
                "refused": refused,
                "credits_balance": self._balance(client),
            },
        )

    # -- execution: collect ------------------------------------------------

    def _collect(
        self,
        client,
        inputs: dict,
        *,
        video_id: str,
        prompt: str,
        poll_interval: float,
        timeout: float,
        started: float,
    ) -> ToolResult:
        """Pick up a task the account has already submitted and paid for.

        Nothing is created and no credits are spent. The account runs one
        video task at a time and the free queue can wait for hours, so a
        queued task may outlive the process that submitted it; the wait here
        is therefore generous.
        """
        from tools._molica.client import MolicaError

        output_path = Path(inputs.get("output_path") or "molica_video.mp4")
        try:
            info = self._poll(
                client, video_id, poll_interval=poll_interval, timeout=timeout
            )
        except TimeoutError as exc:
            return self._still_running(client, video_id, str(exc))
        video_url = str(info.get("video_url") or "")
        if not video_url:
            raise MolicaError(f"task {video_id} finished without a video_url")
        client.download(video_url, output_path)

        # The task record carries the request that produced it, so the result
        # describes the run as it was actually made rather than as it was
        # requested now.
        request = info.get("req") or {}
        if isinstance(request, str):
            try:
                request = json.loads(request)
            except ValueError:
                request = {}
        model = str(
            request.get("model_group_key")
            or info.get("model_group_key")
            or inputs.get("model")
            or _DEFAULT_MODEL
        )
        payload = {
            "prompt": str(request.get("prompt") or info.get("prompt") or prompt),
            "model_group_key": model,
            "duration": request.get("duration"),
            "resolution": request.get("resolution"),
            "video_scale": request.get("video_scale"),
            "fast_model": bool(request.get("fast_model", False)),
            "video_id": video_id,
            "resumed": True,
        }
        record_inputs = {
            "image_url": request.get("image_url") or request.get("video_single_image") or "",
            "image_end_url": request.get("image_end_url") or "",
            "reference_image_urls": list(request.get("video_multiple_image") or []),
        }
        return self._success(
            client,
            inputs,
            output_path=output_path,
            payload=payload,
            prompt=payload["prompt"],
            operation=self._operation(record_inputs),
            video_id=video_id,
            video_url=video_url,
            info=info,
            adapted_params=[],
            started=started,
            # The task was paid for when it was submitted; collecting it
            # spends nothing more.
            credits_required=0,
            credits_source="resume",
        )

    def _still_running(
        self, client, video_id: str, message: str, model: Optional[str] = None
    ) -> ToolResult:
        """Report a task that is still running, keeping it collectable.

        The account has already paid for the task, so failing the run without
        its id would throw a paid run away.
        """
        return ToolResult(
            success=False,
            error=(
                f"{message}. The task is still running; collect it later with "
                f"video_id={video_id!r} — no further credits are spent."
            ),
            data={
                "provider": "molica",
                "video_id": video_id,
                "resumable": True,
                "model": model,
                "credits_balance": self._balance(client),
            },
        )

    def _success(
        self,
        client,
        inputs: dict,
        *,
        output_path: Path,
        payload: dict,
        prompt: str,
        operation: str,
        video_id: str,
        video_url: str,
        info: dict,
        adapted_params: list,
        started: float,
        credits_required,
        credits_source: str,
    ) -> ToolResult:
        """Render a finished clip as the tool's standard success result."""
        from tools.video._shared import probe_output

        probed = probe_output(output_path)
        resumed = bool(payload.get("resumed"))
        if resumed:
            # Collecting a task that was paid for costs nothing.
            cost_usd = 0.0
        else:
            try:
                cost_usd = self.estimate_cost(inputs)
            except PriceQuoteRequired:
                # The credit price is the account's business; an unpriced call
                # is reported as unknown, not as a failed generation.
                cost_usd = None

        model = str(payload["model_group_key"])
        return ToolResult(
            success=True,
            data={
                "provider": "molica",
                "model": model,
                "prompt": prompt,
                "operation": operation,
                "video_id": video_id,
                "video_url": video_url,
                "thumbnail_url": info.get("thumbnail_url"),
                "duration": payload.get("duration"),
                "resolution": payload.get("resolution"),
                "video_scale": payload.get("video_scale"),
                "request_params": payload,
                "output": str(output_path),
                "output_path": str(output_path),
                "format": output_path.suffix.lstrip(".") or "mp4",
                "credits_balance": self._balance(client),
                "credits_required": credits_required,
                "credits_source": credits_source,
                "resumed": resumed,
                "queued_before_generation": bool(info.get("queued_before_generation")),
                "parameters_adapted": bool(adapted_params),
                "adapted_to": adapted_params[-1] if adapted_params else None,
                **probed,
            },
            artifacts=[str(output_path)],
            cost_usd=cost_usd,
            duration_seconds=round(time.time() - started, 2),
            model=model,
        )

    # -- internals ---------------------------------------------------------

    def _adapt_params(self, spec: dict, inputs: dict) -> tuple:
        """Fit the requested duration/resolution/scale to what this model offers.

        Returns ``(overrides, adapted)``. A failover to a model with different
        capabilities must not silently drop the request, so the closest value
        the model does offer is used and the change is reported back.
        """
        overrides: dict = {}
        adapted = False
        for key, requested in (
            ("duration", inputs.get("duration")),
            ("resolution", inputs.get("resolution")),
            ("aspect_ratio", inputs.get("aspect_ratio")),
        ):
            if requested is None or str(requested) == "":
                continue
            options = self._options(spec, key if key != "aspect_ratio" else "video_scale")
            if not options:
                continue
            values = [str(item.get("param_value")) for item in options]
            if str(requested) in values:
                overrides[key] = str(requested)
                continue
            adapted = True
            default = next(
                (str(item.get("param_value")) for item in options if item.get("default")),
                values[0],
            )
            overrides[key] = default
        return overrides, adapted

    @staticmethod
    def _candidate_models(catalog: list, requested: str, inputs: dict) -> list:
        """Model order to try: the requested one first, then known-serving ones."""
        if inputs.get("disable_failover"):
            return [requested]
        order = [requested]
        for key in _FAILOVER_MODELS:
            if key != requested:
                order.append(key)
        # Any other model the account can reach is a last resort: the known
        # ones go first, but a working model beats a failed run.
        for entry in catalog:
            key = entry.get("model_key")
            if key and key not in order:
                order.append(key)
        return order

    @staticmethod
    def _find_model(catalog: list, model_key: str) -> Optional[dict]:
        for entry in catalog:
            if entry.get("model_key") == model_key:
                return entry
        return None

    @staticmethod
    def _options(spec: dict, key: str) -> list:
        for group in spec.get("model_params", {}).get("base", []) or []:
            if group.get("param_key") == key:
                return list(group.get("items", []) or [])
        return []

    def _pick(self, spec: dict, key: str, requested: Any, label: str) -> Any:
        """Resolve one model parameter, rejecting values the model cannot do."""
        options = self._options(spec, key)
        if not options:
            return None
        values = [str(item.get("param_value")) for item in options]
        if requested is not None and str(requested) != "":
            if str(requested) not in values:
                raise ValueError(
                    f"{label}={requested!r} is not supported by this model; choose one of {values}"
                )
            return str(requested)
        for item in options:
            if item.get("default"):
                return str(item.get("param_value"))
        return values[0]

    @staticmethod
    def _operation(inputs: dict) -> str:
        """Pick the operation, deriving it from the image inputs when absent."""
        operation = str(inputs.get("operation") or "")
        if operation:
            return operation
        if inputs.get("reference_image_urls") or inputs.get("reference_image_paths"):
            return "reference_to_video"
        if (
            inputs.get("image_url")
            or inputs.get("image_end_url")
            or inputs.get("image_path")
            or inputs.get("image_end_path")
        ):
            return "image_to_video"
        return "text_to_video"

    def _resolve_images(self, client, inputs: dict) -> dict:
        """Turn local image files into public URLs, leaving URLs untouched."""
        resolved = dict(inputs)

        from tools._molica.client import MolicaError

        def upload(path: str) -> str:
            try:
                return client.upload_image(Path(path))
            except MolicaError as exc:
                # Molica's own bucket needs the oss2 package. When it is not
                # installed, reuse the repository's existing uploader instead of
                # failing the run — Molica accepts any public image URL.
                if "oss2" not in str(exc):
                    raise
                from tools.video._shared import upload_image_fal

                return upload_image_fal(path)

        if resolved.get("image_path"):
            resolved["image_url"] = upload(str(resolved["image_path"]))
        if resolved.get("image_end_path"):
            resolved["image_end_url"] = upload(str(resolved["image_end_path"]))

        local_references = [
            str(p) for p in (resolved.get("reference_image_paths") or []) if str(p)
        ]
        if local_references:
            uploaded = [upload(p) for p in local_references]
            existing = [
                str(u) for u in (resolved.get("reference_image_urls") or []) if str(u)
            ]
            resolved["reference_image_urls"] = existing + uploaded

        return resolved

    def _build_payload(self, inputs: dict, spec: dict, prompt: str, overrides: Optional[dict] = None) -> dict:
        image_url = str(inputs.get("image_url") or "")
        image_end_url = str(inputs.get("image_end_url") or "")
        reference_urls = [
            str(url) for url in (inputs.get("reference_image_urls") or []) if str(url)
        ]

        if image_end_url and not image_url:
            raise ValueError("image_end_url needs image_url (first frame)")

        operation = self._operation(inputs)

        create_mode = spec.get("create_mode", {}) or {}
        text_mode = create_mode.get("text_to_video", {}) or {}
        image_mode = create_mode.get("image_to_video", {}) or {}

        if operation == "text_to_video":
            if not text_mode.get("enable", True):
                raise ValueError(f"model {spec.get('model_key')!r} has no text-to-video mode")
        elif operation == "image_to_video":
            if not image_mode.get("enable", True):
                raise ValueError(f"model {spec.get('model_key')!r} has no image-to-video mode")
        elif operation == "reference_to_video":
            if not reference_urls:
                raise ValueError("reference_to_video needs reference_image_urls")
            if not image_mode.get("multiple_image", False):
                raise ValueError(
                    f"model {spec.get('model_key')!r} does not accept multiple reference images"
                )

        payload = {
            "prompt": prompt,
            "image_url": image_url,
            "image_end_url": image_end_url,
            "expand_prompt": bool(inputs.get("expand_prompt", True)),
            "loop": bool(inputs.get("loop", False)),
            "fast_model": bool(inputs.get("fast_model", False)),
            "model_group_key": spec.get("model_key"),
        }

        if reference_urls:
            payload["video_multiple_image"] = reference_urls
        elif image_url and not image_end_url and image_mode.get("single_image"):
            payload["video_single_image"] = image_url

        overrides = overrides or {}
        duration = self._pick(spec, "duration", overrides.get("duration", inputs.get("duration")), "duration")
        if duration is not None:
            payload["duration"] = int(float(duration))
        resolution = self._pick(spec, "resolution", overrides.get("resolution", inputs.get("resolution")), "resolution")
        if resolution is not None:
            payload["resolution"] = resolution
        scale = self._pick(spec, "video_scale", overrides.get("aspect_ratio", inputs.get("aspect_ratio")), "aspect_ratio")
        if scale is not None:
            payload["video_scale"] = scale

        return payload

    @staticmethod
    def _balance(client) -> Optional[int]:
        """The account's credit balance, or None when it cannot be read."""
        try:
            return client.balance()
        except Exception:  # noqa: BLE001 - a balance probe must not fail the run
            return None

    @staticmethod
    def _diagnose(client) -> str:
        """Explain a silent submission with the account's credit state.

        Molica answers a submission it cannot run with ``ret=1`` and no task
        id, so the only symptom is a task that never appears in the video
        list. The credit balance and the ledger say whether the run was
        refused for lack of credits or because the model is not available to
        this membership.
        """
        parts = []
        try:
            balance = client.balance()
        except Exception:  # noqa: BLE001 - diagnostics must not mask the error
            balance = None
        if balance is not None:
            parts.append(f"credit balance is {balance}")
        try:
            for row in client.integral_logs(1, 3):
                parts.append(
                    f"{str(row.get('created_at'))[:19]} {row.get('integral_change_num')} "
                    f"{row.get('change_desc')}"
                )
        except Exception:  # noqa: BLE001
            pass
        message = (getattr(client, "last_create", {}) or {}).get("msg")
        if message:
            parts.append(f"create said: {message}")
        return "; ".join(parts) if parts else "no credit information available"

    def _resolve_video_id(
        self,
        client,
        prompt: str,
        model_group_key: str,
        poll_interval: float,
        timeout: float,
        payload: Optional[dict] = None,
    ) -> str:
        """Recover the id of a task the create endpoint accepted.

        ``/v1/video/create`` answers a submission with ``ret=1`` and no id, so
        the task has to be picked back out of the account's video list. The
        request parameters are matched too, because the same prompt can be in
        the list many times with different settings.
        """
        params = {
            "duration": (payload or {}).get("duration"),
            "resolution": (payload or {}).get("resolution"),
            "fast_model": (payload or {}).get("fast_model"),
            "video_scale": (payload or {}).get("video_scale"),
        }
        deadline = time.time() + timeout
        while True:
            item = client.find_video(prompt, model_group_key, params=params)
            if item and item.get("video_id"):
                return str(item["video_id"])
            if time.time() >= deadline:
                raise TimeoutError(
                    f"submitted task for model {model_group_key!r} did not show up in "
                    f"the video list within {int(timeout)}s"
                )
            time.sleep(poll_interval)

    def _poll(self, client, video_id: str, *, poll_interval: float, timeout: float) -> dict:
        deadline = time.time() + timeout
        queued = False
        while True:
            info = client.get_video(video_id)
            status = info.get("status")
            if status == _STATUS_FAILED:
                reason = info.get("failure_reason") or "no reason given"
                raise ValueError(f"generation failed: {reason}")
            if info.get("video_url"):
                info["queued_before_generation"] = queued
                return info
            if info.get("waiting"):
                # The account runs one task at a time; this one is waiting for
                # the free queue rather than generating.
                queued = True
            if time.time() >= deadline:
                raise TimeoutError(
                    f"video task {video_id} did not finish within {int(timeout)}s"
                    + (" (it was still queued)" if queued else "")
                )
            time.sleep(poll_interval)
