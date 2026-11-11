"""Request/response models for the M0 review + media API (§10.1).

Field names mirror the frozen artifacts (``review_decision.schema.json``,
``episode_manifest.schema.json``) so the HTTP contract and the append-only log
speak the same vocabulary. Request models forbid unknown fields; response
models ignore log-internal extras (``version``, ``event_type``).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# CLI grammar uses hyphens (``pick-variant``); the canonical log/API spelling
# uses underscores. Both layers normalize through ``canonical_action``.
ACTION_ALIASES = {
    "pick-variant": "pick_variant",
    "edit-prompt": "edit_prompt",
    "send-back": "send_back",
}


class Action(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    PICK_VARIANT = "pick_variant"
    EDIT_PROMPT = "edit_prompt"
    SEND_BACK = "send_back"
    SKIP = "skip"


def canonical_action(raw: str) -> str:
    value = (raw or "").strip().lower()
    return ACTION_ALIASES.get(value, value)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --- errors ---------------------------------------------------------------


class ErrorEnvelope(_Response):
    code: str
    what: str
    fix: str
    recovery: Literal["retry", "fix-then-retry", "do-X-first", "ask-human"]
    side_effects: Literal["none", "partial", "complete", "unknown"]
    context: dict[str, Any] = Field(default_factory=dict)
    trace: Optional[str] = None


# --- review ---------------------------------------------------------------


class ReviewDecisionModel(_Response):
    action: Action
    chosen_candidate: Optional[str] = None
    reason_code: Optional[str] = None
    note: str = ""
    prompt_patch: Optional[str] = None
    override: bool = False
    evidence: str = ""
    decided_at: str
    decided_by: str = "owner"


class ReviewItemModel(_Response):
    review_id: str
    project: str
    episode: str
    stage: str
    slot: str
    candidates: list[str] = Field(default_factory=list)
    criteria: list[str] = Field(default_factory=list)
    ref_context: dict[str, Any] = Field(default_factory=dict)
    cost_if_approved_usd: float = 0.0
    priority: int = 0
    priority_reasons: list[str] = Field(default_factory=list)
    face_similarity: Optional[float] = None
    candidate_version: str = "v1"
    review_version: str = "1"
    created_at: str


class ReviewPolicyModel(_Request):
    mode: Literal["all", "sampled"] = "sampled"
    detail_ratio: float = Field(default=0.35, ge=0, le=1)
    face_sim_threshold: float = Field(default=0.75, ge=0, le=1)
    sampling_salt: str = "default"


class DecideRequest(_Request):
    action: str
    chosen_candidate: Optional[str] = None
    reason_code: Optional[str] = None
    note: str = ""
    prompt_patch: Optional[str] = None
    override: bool = False
    evidence: str = ""
    candidate_version: Optional[str] = None
    decided_by: str = "owner"


class DecisionResponse(_Response):
    review_id: str
    project: str
    episode: str
    stage: str
    slot: Optional[str] = None
    candidate_version: Optional[str] = None
    review_version: Optional[str] = None
    decision: ReviewDecisionModel
    created_at: str
    idempotent_replay: bool = False


class PendingResponse(_Response):
    items: list[ReviewItemModel]
    count: int
    policy: ReviewPolicyModel


class ReviewDetailResponse(_Response):
    item: ReviewItemModel
    decision: Optional[ReviewDecisionModel] = None
    stale: bool = False


class UndoRequest(_Request):
    reason: str = ""


class UndoResponse(_Response):
    review_id: str
    undone: bool
    event: dict[str, Any]


class BatchRequest(_Request):
    episode: Optional[str] = None
    review_ids: Optional[list[str]] = None
    policy: Optional[ReviewPolicyModel] = None
    dry_run: bool = True


class BatchResponse(_Response):
    ok: bool
    dry_run: bool
    checked: list[str]
    pending: list[str]
    stale: list[str]
    missing: list[str]
    not_in_episode: list[str]
    errors: list[ErrorEnvelope] = Field(default_factory=list)


# --- episodes -------------------------------------------------------------


class EpisodeManifestModel(_Response):
    version: str
    series_id: str
    episode_id: str
    project_id: str
    target_platform: Literal["tiktok", "youtube_shorts", "instagram_reels"]
    duration_s: Optional[float] = None
    review_policy: Optional[ReviewPolicyModel] = None
    language: str = "en"
    metadata: dict[str, Any] = Field(default_factory=dict)


class EpisodeSummary(_Response):
    project_id: str
    episode_id: str
    series_id: Optional[str] = None
    target_platform: Optional[str] = None


class EpisodeListResponse(_Response):
    episodes: list[EpisodeSummary]
    count: int


class ShotModel(_Response):
    shot_id: str
    state: str
    reject_count: int = 0
    event_seq: int = 0
    candidate_version: Optional[str] = None


class ShotsResponse(_Response):
    episode_id: str
    shots: list[ShotModel]


class StatsResponse(_Response):
    episode_id: str
    review: dict[str, Any]
    total_shots: int
    shot_states: dict[str, int]


# --- library (mock interface until WP-A lands asset_library) --------------


class LibraryAsset(_Response):
    asset_id: str
    kind: str
    character_id: Optional[str] = None
    text: str = ""
    reason_code: Optional[str] = None
    similarity: Optional[float] = None
    tags: list[str] = Field(default_factory=list)
    media_path: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LibraryListResponse(_Response):
    provider: Literal["mock"] = "mock"
    kind: str
    items: list[LibraryAsset]
    total: int
    page: int
    page_size: int


class LibraryAssetResponse(_Response):
    provider: Literal["mock"] = "mock"
    asset: LibraryAsset