"""M0 HTTP API: review + media minimal set (§10, T7).

Two layers:

- :class:`ShortdramaService` — the shared application layer. Review mutations
  delegate to ``lib.review.ReviewStore.decide`` (the single mutation authority),
  so the HTTP handlers and the future CLI (WP-G) reject the same inputs for the
  same reason. Library access is a **mock interface** until WP-A lands
  ``lib/asset_library.py``.
- :func:`create_app` — FastAPI wiring. Every rejection becomes the shared
  §10.3 error envelope, including framework request-validation failures.

Library endpoints are additive within ``/api/v1`` (X-16); the media surface is
local-only by default and enforces same-origin CSRF, root confinement and
asset-existence checks before serving bytes (UI-02).

Review discovery uses the T2/T3 on-disk layout
(``projects/<project>/<episode>/{reviews,events}.jsonl``). Episode endpoints
read ``episode_manifest.json`` either at the project root or the episode dir.
M0: local files only, zero external API calls.
"""
from __future__ import annotations

import hmac
import json
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from jsonschema import Draft202012Validator

from lib.episode_log import EpisodeLog
from lib.paths import PROJECTS_DIR
from lib.review import (
    ReviewDecision,
    ReviewError,
    ReviewPolicy,
    ReviewStore,
    ReviewValidationError,
    StaleReviewError,
    UnknownReviewError,
)
from shortdrama.errors import ApiError, ErrorCode, classify_review_rejection
from shortdrama.media_locator import MediaLocator
from shortdrama.models import (
    BatchRequest,
    BatchResponse,
    DecideRequest,
    DecisionResponse,
    EpisodeListResponse,
    EpisodeManifestModel,
    EpisodeSummary,
    ErrorEnvelope,
    LibraryAssetResponse,
    LibraryListResponse,
    PendingResponse,
    ReviewDetailResponse,
    ReviewItemModel,
    ReviewPolicyModel,
    ShotsResponse,
    StatsResponse,
    UndoRequest,
    UndoResponse,
    canonical_action,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EPISODE_SCHEMA_PATH = _REPO_ROOT / "schemas" / "artifacts" / "episode_manifest.schema.json"
_EPISODE_VALIDATOR = Draft202012Validator(
    json.loads(_EPISODE_SCHEMA_PATH.read_text(encoding="utf-8"))
)

LIBRARY_KINDS = {"shot", "keyframe", "character", "voice", "music", "reference"}

# UI-02: the API is local-only by default. Requests whose Host is not loopback
# are rejected before routing unless ``allow_remote=True`` (which in turn
# requires a bearer token). "testclient"/"testserver" are the in-process ASGI
# test hosts and are treated as loopback so the contract suite can exercise the
# app without opening a socket.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testclient", "testserver"})

# Defence-in-depth for media responses: never let a browser re-interpret the
# bytes from a different origin (X-12 / UI-02).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}

_ERROR_RESPONSES = {
    400: {"model": ErrorEnvelope, "description": "Request rejected (machine code in body)"},
    401: {"model": ErrorEnvelope, "description": "Authentication required (remote mode)"},
    403: {"model": ErrorEnvelope, "description": "Path escapes its root / cross-origin request"},
    404: {"model": ErrorEnvelope, "description": "Not found"},
    409: {"model": ErrorEnvelope, "description": "Stale review / idempotency conflict"},
    422: {"model": ErrorEnvelope, "description": "Validation failure"},
}


def _request_hostname(request: Request) -> str:
    """Return the Host header's hostname (port stripped), or "" if malformed."""
    raw = request.headers.get("host", "")
    try:
        return (urlsplit(f"//{raw}").hostname or "").lower()
    except ValueError:
        return ""


def _same_origin(origin: str, request: Request) -> bool:
    """True when ``Origin`` names the exact scheme/host/port being served."""
    if origin.strip().lower() == "null":
        return False
    try:
        sent = urlsplit(origin)
        served = urlsplit(str(request.url))
        sent_port = sent.port or (443 if sent.scheme == "https" else 80)
        served_port = served.port or (443 if served.scheme == "https" else 80)
    except ValueError:
        return False
    if sent.scheme not in ("http", "https") or not sent.hostname:
        return False
    return (
        sent.scheme == served.scheme
        and sent.hostname == served.hostname
        and sent_port == served_port
    )


def _access_rejection(
    request: Request,
    *,
    allow_remote: bool,
    auth_token: Optional[str],
) -> Optional[ApiError]:
    """Local-only + auth + same-origin CSRF checks (UI-02).

    Returns the §10.3 rejection to emit, or ``None`` to let the request through.
    Rejections reuse ``VALIDATION_ERROR`` because the frozen error catalogue
    (§10.3) has no dedicated CSRF/auth code yet; what/fix still carry the
    machine-actionable detail.
    """
    host = _request_hostname(request)
    if not allow_remote and host not in _LOOPBACK_HOSTS:
        return ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"request Host '{host or '(missing)'}' is not a loopback address",
            "keep the API bound to localhost; enable remote mode with a bearer token to expose it",
            recovery="fix-then-retry",
            http_status=403,
            context={"host": host},
        )

    if auth_token:
        provided = request.headers.get("authorization", "")
        if not hmac.compare_digest(provided, f"Bearer {auth_token}"):
            return ApiError(
                ErrorCode.VALIDATION_ERROR,
                "missing or invalid bearer token",
                "send Authorization: Bearer <token>",
                recovery="ask-human",
                http_status=401,
            )

    origin = request.headers.get("origin")
    if origin is not None:
        if not _same_origin(origin, request):
            return ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"cross-origin request from '{origin}' rejected",
                "call the API from its own origin",
                recovery="fix-then-retry",
                http_status=403,
                context={"origin": origin},
            )
    elif request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return ApiError(
            ErrorCode.VALIDATION_ERROR,
            "cross-site request rejected (Sec-Fetch-Site: cross-site)",
            "call the API from its own origin",
            recovery="fix-then-retry",
            http_status=403,
            context={"sec_fetch_site": "cross-site"},
        )
    return None


# ---------------------------------------------------------------------------
# Library mock (replaced by lib/asset_library.AssetLibrary when WP-A lands)
# ---------------------------------------------------------------------------


class MockAssetLibrary:
    """Deterministic, in-memory stand-in for the asset library (WP-A).

    Implements the two methods the API needs (``query`` / ``get``) with the
    same shape WP-A's real adapter will expose. No network, no filesystem.
    """

    provider = "mock"

    def __init__(self, assets: Optional[list[dict[str, Any]]] = None) -> None:
        self._assets = [dict(a) for a in (assets if assets is not None else _DEFAULT_ASSETS)]

    def query(
        self,
        kind: str,
        *,
        text: Optional[str] = None,
        image: Optional[str] = None,
        character_id: Optional[str] = None,
        min_sim: float = 0.0,
        reason_code: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if page < 1 or page_size < 1 or page_size > 100:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"invalid pagination page={page} page_size={page_size}",
                "use page >= 1 and 1 <= page_size <= 100",
                http_status=422,
                context={"page": page, "page_size": page_size},
            )
        if text is not None and image is not None:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "text and image filters cannot be combined",
                "pass either text or image, not both",
                http_status=422,
                context={"text": text, "image": image},
            )
        needle = (text or "").strip().lower()
        image_ref = (image or "").strip().lower()
        matches = []
        for asset in self._assets:
            if asset.get("kind") != kind:
                continue
            if character_id and asset.get("character_id") != character_id:
                continue
            if reason_code and asset.get("reason_code") != reason_code:
                continue
            if min_sim > 0:
                similarity = asset.get("similarity")
                if similarity is None or similarity < min_sim:
                    continue
            if needle:
                haystack = " ".join([str(asset.get("text", "")), *asset.get("tags", [])]).lower()
                if needle not in haystack:
                    continue
            if image_ref:
                # Mock stand-in for LIB-05 visual search: match the declared
                # image reference (metadata wins over the on-disk media path).
                metadata = asset.get("metadata") or {}
                reference = str(
                    asset.get("image_ref")
                    or metadata.get("image_ref")
                    or asset.get("media_path")
                    or ""
                ).lower()
                if image_ref not in reference:
                    continue
            matches.append(asset)
        matches.sort(key=lambda a: a["asset_id"])
        total = len(matches)
        start = (page - 1) * page_size
        return {
            "provider": self.provider,
            "kind": kind,
            "items": [dict(a) for a in matches[start : start + page_size]],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get(self, asset_id: str) -> dict[str, Any]:
        for asset in self._assets:
            if asset["asset_id"] == asset_id:
                return dict(asset)
        raise ApiError(
            ErrorCode.LIBRARY_ASSET_NOT_FOUND,
            f"asset '{asset_id}' not found",
            "check asset_id or re-run the library query",
            http_status=404,
            context={"asset_id": asset_id},
        )


_DEFAULT_ASSETS: list[dict[str, Any]] = [
    {
        "asset_id": "ast_shot_001",
        "kind": "shot",
        "character_id": "ch_ling",
        "text": "Ling turns toward the rain-streaked window",
        "reason_code": "FACE_MISMATCH",
        "similarity": 0.92,
        "tags": ["dialogue", "hook"],
        "media_path": "shots/ast_shot_001.mp4",
    },
    {
        "asset_id": "ast_shot_002",
        "kind": "shot",
        "character_id": "ch_wei",
        "text": "Wei steps into the doorway, off-pose",
        "reason_code": "OFF_POSE",
        "similarity": 0.61,
        "tags": ["reaction"],
        "media_path": "shots/ast_shot_002.mp4",
    },
    {
        "asset_id": "ast_key_001",
        "kind": "keyframe",
        "character_id": "ch_ling",
        "text": "Ling close-up, wardrobe reference",
        "reason_code": "WARDROBE_DRIFT",
        "similarity": 0.88,
        "tags": ["reference"],
        "media_path": "keyframes/ast_key_001.png",
    },
    {
        "asset_id": "ast_char_ling",
        "kind": "character",
        "character_id": "ch_ling",
        "text": "Ling canonical front-facing wardrobe",
        "tags": ["canonical"],
        "media_path": "characters/ast_char_ling/front.png",
    },
]


# ---------------------------------------------------------------------------
# Service layer (shared by HTTP and CLI)
# ---------------------------------------------------------------------------


class ShortdramaService:
    """Application logic over the frozen T2/T3 contracts."""

    def __init__(
        self,
        projects_dir: Optional[Path] = None,
        *,
        library: Optional[Any] = None,
        library_dir: Optional[Path] = None,
    ) -> None:
        self.projects_dir = Path(projects_dir) if projects_dir else Path(PROJECTS_DIR)
        self.library = library or MockAssetLibrary()
        self.locator = MediaLocator(self.projects_dir, library_dir)
        self._idempotency: dict[str, dict[str, Any]] = {}

    # --- discovery --------------------------------------------------------

    def _stores(self) -> list[tuple[str, str, ReviewStore]]:
        if not self.projects_dir.is_dir():
            return []
        stores = []
        for path in sorted(self.projects_dir.glob("*/*/reviews.jsonl")):
            project, episode = path.parent.parent.name, path.parent.name
            stores.append((project, episode, ReviewStore(project, episode, self.projects_dir)))
        return stores

    def _lookup(self, review_id: str) -> Optional[tuple[ReviewStore, dict[str, Any]]]:
        for _project, _episode, store in self._stores():
            try:
                return store, store.get(review_id)
            except UnknownReviewError:
                continue
        return None

    def _require(self, review_id: str) -> tuple[ReviewStore, dict[str, Any]]:
        found = self._lookup(review_id)
        if not found:
            raise ApiError(
                ErrorCode.REVIEW_NOT_FOUND,
                f"review_id '{review_id}' not found",
                "list pending reviews (API: GET /api/v1/review/pending)",
                http_status=404,
                context={"review_id": review_id},
            )
        return found

    @staticmethod
    def _decision_record(store: ReviewStore, review_id: str) -> Optional[dict[str, Any]]:
        """Latest decision/undo record for a review (read-only log projection).

        ``ReviewStore._effective`` projects only the inner decision object, so
        the candidate version a decision was made on lives on the append-only
        record. Reading it back keeps stale detection honest without a second
        writer or a schema change.
        """
        if not store.path.exists():
            return None
        last: Optional[dict[str, Any]] = None
        for line in store.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("review_id") == review_id and record.get("event_type") in ("decision", "undo"):
                last = record
        return last if last and last.get("event_type") == "decision" else None

    def _is_stale(self, store: ReviewStore, item: dict[str, Any],
                  decision: Optional[dict[str, Any]]) -> bool:
        if not decision:
            return False
        record = self._decision_record(store, item["review_id"])
        if record is None:
            return False
        current = item.get("candidate_version")
        decided_on = record.get("candidate_version")
        return bool(current and decided_on and current != decided_on)

    # --- manifests --------------------------------------------------------

    def _manifest_paths(self) -> list[Path]:
        if not self.projects_dir.is_dir():
            return []
        paths = sorted(self.projects_dir.glob("*/episode_manifest.json"))
        paths += sorted(self.projects_dir.glob("*/*/episode_manifest.json"))
        seen: set[Path] = set()
        unique = []
        for path in paths:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(path)
        return unique

    def _load_manifest(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                f"episode manifest '{path}' is unreadable: {exc}",
                "fix or restore episode_manifest.json",
                recovery="ask-human",
                side_effects="unknown",
                http_status=500,
                context={"path": str(path)},
            ) from exc
        errors = sorted(_EPISODE_VALIDATOR.iter_errors(data), key=lambda e: list(e.path))
        if errors:
            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                f"episode manifest '{path}' violates its schema",
                "fix episode_manifest.json against schemas/artifacts/episode_manifest.schema.json",
                recovery="ask-human",
                http_status=500,
                context={"path": str(path), "errors": [e.message for e in errors]},
            )
        return data

    def _find_manifest(self, episode_id: str) -> tuple[dict[str, Any], Path]:
        for path in self._manifest_paths():
            data = self._load_manifest(path)
            if data.get("episode_id") == episode_id:
                return data, path
        raise ApiError(
            ErrorCode.EPISODE_NOT_FOUND,
            f"episode '{episode_id}' has no episode_manifest.json",
            "create projects/<project>/<episode>/episode_manifest.json first",
            recovery="do-X-first",
            http_status=404,
            context={"episode_id": episode_id},
        )

    def list_episodes(self) -> dict[str, Any]:
        seen: set[tuple[str, str]] = set()
        episodes = []
        for path in self._manifest_paths():
            data = self._load_manifest(path)
            key = (data["project_id"], data["episode_id"])
            if key in seen:
                continue
            seen.add(key)
            episodes.append(
                EpisodeSummary(
                    project_id=data["project_id"],
                    episode_id=data["episode_id"],
                    series_id=data.get("series_id"),
                    target_platform=data.get("target_platform"),
                )
            )
        episodes.sort(key=lambda e: (e.project_id, e.episode_id))
        return {"episodes": episodes, "count": len(episodes)}

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        return self._find_manifest(episode_id)[0]

    def get_policy(self, episode_id: str) -> ReviewPolicyModel:
        manifest, _path = self._find_manifest(episode_id)
        return ReviewPolicyModel(**(manifest.get("review_policy") or {}))

    def put_policy(self, episode_id: str, policy: ReviewPolicyModel) -> ReviewPolicyModel:
        manifest, path = self._find_manifest(episode_id)
        updated = {**manifest, "review_policy": policy.model_dump()}
        errors = sorted(_EPISODE_VALIDATOR.iter_errors(updated), key=lambda e: list(e.path))
        if errors:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"review policy for '{episode_id}' violates the episode manifest schema",
                "check mode/detail_ratio/face_sim_threshold/sampling_salt",
                http_status=422,
                context={"errors": [e.message for e in errors]},
            )
        path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return policy

    def get_shots(self, episode_id: str) -> dict[str, Any]:
        manifest, _path = self._find_manifest(episode_id)
        log = EpisodeLog(manifest["project_id"], manifest["episode_id"], self.projects_dir)
        states = log.shot_states()
        shots = [states[sid] for sid in sorted(states)]
        return {"episode_id": episode_id, "shots": shots}

    def stats(self, episode_id: str) -> dict[str, Any]:
        try:
            manifest, _path = self._find_manifest(episode_id)
            project, episode = manifest["project_id"], manifest["episode_id"]
        except ApiError:
            matches = [(p, e) for p, e, _s in self._stores() if e == episode_id]
            if not matches:
                raise
            project, episode = matches[0]

        review = {
            "total_items": 0,
            "decided": 0,
            "pending": 0,
            "action_distribution": {},
            "reason_code_distribution": {},
            "first_pass_accept_rate": None,
        }
        accepted = 0
        for p, e, store in self._stores():
            if (p, e) != (project, episode):
                continue
            s = store.stats()
            for key in ("total_items", "decided", "pending"):
                review[key] += s[key]
            for key in ("action_distribution", "reason_code_distribution"):
                for name, count in s[key].items():
                    review[key][name] = review[key].get(name, 0) + count
            accepted += s["action_distribution"].get("accept", 0)
        if review["decided"]:
            review["first_pass_accept_rate"] = accepted / review["decided"]

        states = EpisodeLog(project, episode, self.projects_dir).shot_states()
        distribution: dict[str, int] = {}
        for shot in states.values():
            distribution[shot["state"]] = distribution.get(shot["state"], 0) + 1
        return {
            "episode_id": episode_id,
            "review": review,
            "total_shots": len(states),
            "shot_states": distribution,
        }

    # --- review queue -----------------------------------------------------

    def _policy(self, episode_id: Optional[str], ratio: Optional[float], mode: Optional[str]) -> ReviewPolicyModel:
        base = ReviewPolicyModel()
        if episode_id:
            try:
                manifest, _path = self._find_manifest(episode_id)
                base = ReviewPolicyModel(**(manifest.get("review_policy") or {}))
            except ApiError:
                # Review data may exist before an episode manifest is authored.
                pass
        if ratio is not None:
            base.detail_ratio = ratio
        if mode is not None:
            base.mode = mode
        return base

    @staticmethod
    def _to_review_policy(policy: ReviewPolicyModel) -> ReviewPolicy:
        return ReviewPolicy(
            mode=policy.mode,
            detail_ratio=policy.detail_ratio,
            face_sim_threshold=policy.face_sim_threshold,
            sampling_salt=policy.sampling_salt,
        )

    def list_pending(
        self,
        *,
        episode: Optional[str] = None,
        stage: Optional[str] = None,
        priority: Optional[int] = None,
        ratio: Optional[float] = None,
        mode: Optional[Literal["all", "sampled"]] = None,
        stale: bool = False,
    ) -> dict[str, Any]:
        policy_model = self._policy(episode, ratio, mode)
        policy = self._to_review_policy(policy_model)
        items: list[dict[str, Any]] = []
        for _project, ep, store in self._stores():
            if episode and ep != episode:
                continue
            if stale:
                for item in store.items():
                    eff = store.get(item["review_id"])
                    if not self._is_stale(store, eff["item"], eff["decision"]):
                        continue
                    if stage and eff["item"].get("stage") != stage:
                        continue
                    if priority is not None and eff["item"].get("priority") != priority:
                        continue
                    items.append(eff["item"])
            else:
                for item in store.pending(policy):
                    if stage and item.get("stage") != stage:
                        continue
                    if priority is not None and item.get("priority") != priority:
                        continue
                    items.append(item)
        items.sort(key=lambda i: (i.get("priority", 0), i.get("slot", ""), i["review_id"]))
        return {"items": items, "count": len(items), "policy": policy_model}

    def get_review(self, review_id: str) -> dict[str, Any]:
        _store, eff = self._require(review_id)
        return {
            "item": eff["item"],
            "decision": eff["decision"],
            "stale": self._is_stale(_store, eff["item"], eff["decision"]),
        }

    # --- mutations --------------------------------------------------------

    def decide(
        self,
        review_id: str,
        request: DecideRequest,
        *,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        if idempotency_key:
            cached = self._idempotency.get(idempotency_key)
            if cached is not None:
                if cached["review_id"] != review_id:
                    raise ApiError(
                        ErrorCode.VALIDATION_ERROR,
                        f"Idempotency-Key '{idempotency_key}' was already used for review '{cached['review_id']}'",
                        "use a new Idempotency-Key for a different review",
                        http_status=409,
                        context={"idempotency_key": idempotency_key},
                    )
                return {**cached, "idempotent_replay": True}

        store, eff = self._require(review_id)
        item = eff["item"]
        action = canonical_action(request.action)
        decision = ReviewDecision(
            action=action,
            chosen_candidate=request.chosen_candidate,
            reason_code=request.reason_code,
            note=request.note,
            prompt_patch=request.prompt_patch,
            override=request.override,
            evidence=request.evidence,
            decided_by=request.decided_by,
        )
        try:
            record = store.decide(review_id, decision, candidate_version=request.candidate_version)
        except UnknownReviewError:
            raise ApiError(
                ErrorCode.REVIEW_NOT_FOUND,
                f"review_id '{review_id}' not found",
                "list pending reviews first",
                http_status=404,
                context={"review_id": review_id},
            )
        except StaleReviewError:
            raise ApiError(
                ErrorCode.STALE_REVIEW,
                f"decision targets candidate version '{request.candidate_version}', "
                f"current is '{item.get('candidate_version')}'",
                "reload the review item and decide on the current candidate",
                recovery="retry",
                http_status=409,
                context={
                    "review_id": review_id,
                    "candidate_version": request.candidate_version,
                    "current_version": item.get("candidate_version"),
                },
            )
        except ReviewValidationError:
            code, what, fix = classify_review_rejection(decision, item)
            raise ApiError(
                code,
                what,
                fix,
                http_status=422,
                context={
                    "review_id": review_id,
                    "stage": item.get("stage"),
                    "action": action,
                    "reason_code": request.reason_code,
                },
            )
        except ReviewError as exc:  # defensive: unknown review-layer failure
            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                f"review store rejected the decision: {exc}",
                "inspect reviews.jsonl and retry",
                recovery="ask-human",
                side_effects="unknown",
                http_status=500,
                trace=type(exc).__name__,
            )

        response = {
            "review_id": review_id,
            "project": record.get("project"),
            "episode": record.get("episode"),
            "stage": record.get("stage"),
            "slot": record.get("slot"),
            "candidate_version": record.get("candidate_version"),
            "review_version": record.get("review_version"),
            "decision": record["decision"],
            "created_at": record["created_at"],
            "idempotent_replay": False,
        }
        if idempotency_key:
            self._idempotency[idempotency_key] = response
        return response

    def undo(self, review_id: str, reason: str = "") -> dict[str, Any]:
        store, _eff = self._require(review_id)
        event = store.undo(review_id, reason=reason)
        return {"review_id": review_id, "undone": True, "event": event}

    def batch(self, request: BatchRequest) -> dict[str, Any]:
        if request.episode:
            scope = [(p, e, s) for p, e, s in self._stores() if e == request.episode]
        else:
            scope = self._stores()

        if request.review_ids is not None:
            checked = list(request.review_ids)
        else:
            checked = [item["review_id"] for _p, _e, store in scope for item in store.pending(
                self._to_review_policy(request.policy or ReviewPolicyModel())
            )]

        pending: list[str] = []
        stale: list[str] = []
        missing: list[str] = []
        not_in_episode: list[str] = []
        errors: list[dict[str, Any]] = []

        for review_id in checked:
            found = self._lookup(review_id)
            if not found:
                missing.append(review_id)
                errors.append(ApiError(
                    ErrorCode.REVIEW_NOT_FOUND,
                    f"review_id '{review_id}' not found",
                    "check the id or list pending reviews",
                    http_status=404,
                    context={"review_id": review_id},
                ).to_envelope())
                continue
            _store, eff = found
            item = eff["item"]
            if request.episode and item.get("episode") != request.episode:
                not_in_episode.append(review_id)
                errors.append(ApiError(
                    ErrorCode.BATCH_PRECONDITION_FAILED,
                    f"review_id '{review_id}' belongs to episode '{item.get('episode')}'",
                    f"run the batch for episode '{item.get('episode')}'",
                    http_status=422,
                    context={"review_id": review_id, "episode": item.get("episode")},
                ).to_envelope())
                continue
            if self._is_stale(_store, item, eff["decision"]):
                stale.append(review_id)
                errors.append(ApiError(
                    ErrorCode.STALE_REVIEW,
                    f"review_id '{review_id}' was decided on a stale candidate version",
                    "re-review the current candidate before batching",
                    http_status=409,
                    context={"review_id": review_id},
                ).to_envelope())
            if eff["decision"] is None:
                pending.append(review_id)

        if not missing and not not_in_episode and not stale and not pending:
            errors.append(ApiError(
                ErrorCode.NOTHING_PENDING,
                "batch preconditions found no pending review items",
                "nothing to do; inspect the queue with GET /api/v1/review/pending",
                recovery="do-X-first",
                http_status=409,
                context={"episode": request.episode},
            ).to_envelope())

        ok = not errors
        return {
            "ok": ok,
            "dry_run": request.dry_run,
            "checked": checked,
            "pending": pending,
            "stale": stale,
            "missing": missing,
            "not_in_episode": not_in_episode,
            "errors": errors,
        }

    # --- library ----------------------------------------------------------

    @staticmethod
    def _require_kind(kind: str) -> str:
        if kind not in LIBRARY_KINDS:
            raise ApiError(
                ErrorCode.UNSUPPORTED_LIBRARY_KIND,
                f"library kind '{kind}' is not supported",
                f"use one of: {', '.join(sorted(LIBRARY_KINDS))}",
                http_status=400,
                context={"kind": kind},
            )
        return kind

    def library_query(self, kind: str, **filters: Any) -> dict[str, Any]:
        self._require_kind(kind)
        # Additive UI-01 filter: providers that predate image search still work
        # for text/metadata queries because the kwarg is only sent when used.
        if filters.get("image") is None:
            filters.pop("image", None)
        return self.library.query(kind, **filters)

    @staticmethod
    def _asset_not_found(asset_id: str) -> ApiError:
        return ApiError(
            ErrorCode.LIBRARY_ASSET_NOT_FOUND,
            f"asset '{asset_id}' not found",
            "check asset_id or re-run the library query",
            http_status=404,
            context={"asset_id": asset_id},
        )

    def _library_asset(self, kind: str, asset_id: str) -> dict[str, Any]:
        """Fetch an asset and enforce that it belongs to ``kind`` (UI-02)."""
        try:
            asset = self.library.get(asset_id)
        except KeyError:  # lib.asset_library.AssetNotFoundError is a KeyError
            raise self._asset_not_found(asset_id) from None
        if asset is None:
            raise self._asset_not_found(asset_id)
        if asset.get("kind") != kind:
            raise ApiError(
                ErrorCode.LIBRARY_ASSET_NOT_FOUND,
                f"asset '{asset_id}' is not of kind '{kind}'",
                "check the asset kind in the library response",
                http_status=404,
                context={"asset_id": asset_id, "kind": kind},
            )
        return asset

    def library_get(self, kind: str, asset_id: str) -> dict[str, Any]:
        self._require_kind(kind)
        asset = self._library_asset(kind, asset_id)
        return {"provider": "mock", "asset": asset}

    # --- media ------------------------------------------------------------

    def locate_episode_media(self, episode_id: str, rel_path: str) -> Path:
        return self.locator.locate_episode(episode_id, rel_path)

    def locate_thumbnail(self, episode_id: str, rel_path: str) -> Path:
        return self.locator.locate_thumbnail(episode_id, rel_path)

    def locate_library_media(self, kind: str, asset_id: str, rel_path: str) -> Path:
        self._require_kind(kind)
        # Validate kind/asset_id as single path segments BEFORE touching the
        # library, so traversal is always reported as MEDIA_PATH_FORBIDDEN
        # rather than leaking "not found" for a malformed id.
        self.locator.library_asset_root(kind, asset_id)
        # Legal-access check: only assets that exist in the library and match
        # the requested kind may serve media (UI-02).
        self._library_asset(kind, asset_id)
        return self.locator.locate_library(kind, asset_id, rel_path)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    projects_dir: Optional[Path] = None,
    library_dir: Optional[Path] = None,
    library: Optional[Any] = None,
    allow_remote: bool = False,
    auth_token: Optional[str] = None,
) -> FastAPI:
    """Build the ASGI app. Tests inject a tmp projects root and a mock library.

    UI-02 security posture: the server is local-only unless ``allow_remote`` is
    set, and remote mode refuses to start without a bearer ``auth_token``.
    Every request is also checked for same-origin CSRF (``Origin`` /
    ``Sec-Fetch-Site``), and replies carry nosniff + same-origin CORP headers.
    """
    if allow_remote and not auth_token:
        raise ValueError("remote exposure requires auth_token (UI-02)")
    service = ShortdramaService(projects_dir, library=library, library_dir=library_dir)

    app = FastAPI(
        title="OpenMontage Short-Drama API",
        version="0.1.0",
        description="M0 minimal review + media surface (§10). Additive-only within /api/v1.",
    )
    app.state.service = service

    def get_service() -> ShortdramaService:
        return app.state.service

    @app.middleware("http")
    async def _access_guard(request: Request, call_next: Any) -> Any:
        # Runs outside ExceptionMiddleware, so rejections are returned directly
        # in the shared envelope rather than raised.
        rejection = _access_rejection(
            request, allow_remote=allow_remote, auth_token=auth_token
        )
        if rejection is not None:
            response = JSONResponse(rejection.to_envelope(), status_code=rejection.http_status)
        else:
            response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(ApiError)
    async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.to_envelope(include_trace=True), status_code=exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(err.get("loc", [])), "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        error = ApiError(
            ErrorCode.VALIDATION_ERROR,
            "request failed schema validation",
            "fix the request body/query parameters and retry",
            http_status=422,
            context={"errors": details},
        )
        return JSONResponse(error.to_envelope(), status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected_handler(_request: Request, exc: Exception) -> JSONResponse:
        # §10.3: a trace is emitted only for unexpected failures.
        error = ApiError(
            ErrorCode.INTERNAL_ERROR,
            f"unexpected server error: {type(exc).__name__}",
            "retry once; if it persists, ask a human to inspect the server log",
            recovery="ask-human",
            side_effects="unknown",
            http_status=500,
            trace=str(exc),
        )
        return JSONResponse(error.to_envelope(include_trace=True), status_code=500)

    # -- episodes ---------------------------------------------------------

    @app.get("/api/v1/episodes", response_model=EpisodeListResponse, tags=["episodes"],
             responses=_ERROR_RESPONSES)
    def list_episodes(svc: ShortdramaService = Depends(get_service)) -> dict[str, Any]:
        return svc.list_episodes()

    @app.get("/api/v1/episode/{episode_id}", response_model=EpisodeManifestModel, tags=["episodes"],
             responses=_ERROR_RESPONSES)
    def get_episode(episode_id: str, svc: ShortdramaService = Depends(get_service)) -> dict[str, Any]:
        return svc.get_episode(episode_id)

    @app.get("/api/v1/episode/{episode_id}/shots", response_model=ShotsResponse, tags=["episodes"],
             responses=_ERROR_RESPONSES)
    def get_shots(episode_id: str, svc: ShortdramaService = Depends(get_service)) -> dict[str, Any]:
        return svc.get_shots(episode_id)

    @app.get("/api/v1/episode/{episode_id}/review_policy", response_model=ReviewPolicyModel,
             tags=["episodes"], responses=_ERROR_RESPONSES)
    def get_policy(episode_id: str, svc: ShortdramaService = Depends(get_service)) -> ReviewPolicyModel:
        return svc.get_policy(episode_id)

    @app.put("/api/v1/episode/{episode_id}/review_policy", response_model=ReviewPolicyModel,
             tags=["episodes"], responses=_ERROR_RESPONSES)
    def put_policy(
        episode_id: str,
        policy: ReviewPolicyModel,
        svc: ShortdramaService = Depends(get_service),
    ) -> ReviewPolicyModel:
        return svc.put_policy(episode_id, policy)

    # -- review queue (specific paths before /{review_id}) ----------------

    @app.get("/api/v1/review/pending", response_model=PendingResponse, tags=["review"],
             responses=_ERROR_RESPONSES)
    def pending(
        stage: Optional[str] = None,
        priority: Optional[int] = None,
        ratio: Optional[float] = Query(default=None, ge=0, le=1),
        mode: Optional[Literal["all", "sampled"]] = None,
        stale: bool = False,
        episode: Optional[str] = None,
        svc: ShortdramaService = Depends(get_service),
    ) -> dict[str, Any]:
        return svc.list_pending(
            episode=episode, stage=stage, priority=priority, ratio=ratio, mode=mode, stale=stale
        )

    @app.post("/api/v1/review/batch", response_model=BatchResponse, tags=["review"],
              responses=_ERROR_RESPONSES)
    def batch(request: BatchRequest, svc: ShortdramaService = Depends(get_service)) -> dict[str, Any]:
        return svc.batch(request)

    @app.get("/api/v1/review/{review_id}", response_model=ReviewDetailResponse, tags=["review"],
             responses=_ERROR_RESPONSES)
    def get_review(review_id: str, svc: ShortdramaService = Depends(get_service)) -> dict[str, Any]:
        return svc.get_review(review_id)

    @app.post("/api/v1/review/{review_id}/decision", response_model=DecisionResponse, tags=["review"],
              responses=_ERROR_RESPONSES)
    def decide(
        review_id: str,
        request: DecideRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        svc: ShortdramaService = Depends(get_service),
    ) -> dict[str, Any]:
        return svc.decide(review_id, request, idempotency_key=idempotency_key)

    @app.post("/api/v1/review/{review_id}/undo", response_model=UndoResponse, tags=["review"],
              responses=_ERROR_RESPONSES)
    def undo(
        review_id: str,
        request: Optional[UndoRequest] = None,
        svc: ShortdramaService = Depends(get_service),
    ) -> dict[str, Any]:
        return svc.undo(review_id, reason=(request.reason if request else ""))

    # -- progress ----------------------------------------------------------

    @app.get("/api/v1/progress/{episode_id}/stats", response_model=StatsResponse, tags=["progress"],
             responses=_ERROR_RESPONSES)
    def stats(episode_id: str, svc: ShortdramaService = Depends(get_service)) -> dict[str, Any]:
        return svc.stats(episode_id)

    # -- library (mock until WP-A) ----------------------------------------

    @app.get("/api/v1/library/{kind}", response_model=LibraryListResponse, tags=["library"],
             responses=_ERROR_RESPONSES)
    def library_query(
        kind: str,
        text: Optional[str] = None,
        image: Optional[str] = None,
        character_id: Optional[str] = None,
        min_sim: float = Query(default=0.0, ge=0, le=1),
        reason_code: Optional[str] = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=100),
        svc: ShortdramaService = Depends(get_service),
    ) -> dict[str, Any]:
        return svc.library_query(
            kind,
            text=text,
            image=image,
            character_id=character_id,
            min_sim=min_sim,
            reason_code=reason_code,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/v1/library/{kind}/{asset_id}", response_model=LibraryAssetResponse, tags=["library"],
             responses=_ERROR_RESPONSES)
    def library_get(kind: str, asset_id: str, svc: ShortdramaService = Depends(get_service)) -> dict[str, Any]:
        return svc.library_get(kind, asset_id)

    # -- media (library route before the generic episode route) -----------

    @app.get("/media/library/{kind}/{asset_id}/{file_path:path}", response_class=FileResponse,
             tags=["media"], responses=_ERROR_RESPONSES)
    def library_media(kind: str, asset_id: str, file_path: str,
                      svc: ShortdramaService = Depends(get_service)) -> FileResponse:
        return FileResponse(svc.locate_library_media(kind, asset_id, file_path))

    @app.get("/media/{episode_id}/{file_path:path}", response_class=FileResponse, tags=["media"],
             responses=_ERROR_RESPONSES)
    def episode_media(episode_id: str, file_path: str,
                      svc: ShortdramaService = Depends(get_service)) -> FileResponse:
        return FileResponse(svc.locate_episode_media(episode_id, file_path))

    @app.get("/thumb/{episode_id}/{file_path:path}", response_class=FileResponse, tags=["media"],
             responses=_ERROR_RESPONSES)
    def episode_thumb(episode_id: str, file_path: str,
                      svc: ShortdramaService = Depends(get_service)) -> FileResponse:
        return FileResponse(svc.locate_thumbnail(episode_id, file_path))

    return app


app = create_app()