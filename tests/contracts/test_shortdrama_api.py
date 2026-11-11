"""T7 contract tests: shortdrama API + models + errors + media locator (§10).

Guards:
- the M0 OpenAPI minimal set exists under /api/v1 with the frozen schemas;
- every rejection uses the shared machine-checkable error envelope (X-2);
- review mutations run through the REAL ``lib.review.ReviewStore`` (X-11):
  the decision lands in ``reviews.jsonl`` and validates against the frozen schema;
- CLI path and HTTP path reject the same illegal input with the same code
  (the CLI calls ``ShortdramaService``, exactly as the handlers do);
- asset-library access is the mock interface (WP-A owns the real library);
- media paths cannot escape episode/library roots and Range is preserved.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from lib.episode_log import EpisodeLog
from lib.review import ReviewItem, ReviewStore
from shortdrama.api import MockAssetLibrary, ShortdramaService, create_app
from shortdrama.errors import (
    ERROR_CATALOG,
    RECOVERY_VALUES,
    SIDE_EFFECT_VALUES,
    ApiError,
    ErrorCode,
)
from shortdrama.media_locator import MediaLocator
from shortdrama.models import DecideRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
REVIEW_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "artifacts" / "review_decision.schema.json").read_text(encoding="utf-8")
)
REVIEW_VALIDATOR = Draft202012Validator(REVIEW_SCHEMA)

MINIMAL_PATHS = {
    ("/api/v1/episodes", "get"),
    ("/api/v1/episode/{episode_id}", "get"),
    ("/api/v1/episode/{episode_id}/shots", "get"),
    ("/api/v1/review/pending", "get"),
    ("/api/v1/review/{review_id}", "get"),
    ("/api/v1/review/{review_id}/decision", "post"),
    ("/api/v1/review/batch", "post"),
    ("/api/v1/review/{review_id}/undo", "post"),
    ("/api/v1/episode/{episode_id}/review_policy", "get"),
    ("/api/v1/episode/{episode_id}/review_policy", "put"),
    ("/api/v1/library/{kind}", "get"),
    ("/api/v1/library/{kind}/{asset_id}", "get"),
    ("/api/v1/progress/{episode_id}/stats", "get"),
    ("/media/{episode_id}/{file_path}", "get"),
    ("/media/library/{kind}/{asset_id}/{file_path}", "get"),
    ("/thumb/{episode_id}/{file_path}", "get"),
}


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def lib_dir(tmp_path):
    return tmp_path / "library"


@pytest.fixture
def service(projects, lib_dir):
    return ShortdramaService(projects, library_dir=lib_dir)


@pytest.fixture
def client(projects, lib_dir):
    return TestClient(create_app(projects_dir=projects, library_dir=lib_dir))


def add_item(service, rid="r1", *, stage="shot_gen", priority=0, candidates=None,
             version="v1", project="drama", episode="ep01"):
    store = ReviewStore(project, episode, service.projects_dir)
    store.add_item(ReviewItem(
        review_id=rid, project=project, episode=episode, stage=stage,
        slot=f"sh_{rid}", candidates=candidates if candidates is not None else ["a.mp4", "b.mp4"],
        priority=priority, candidate_version=version,
    ))
    return store


def write_manifest(projects, project="drama", episode="ep01", *, at_project_root=False, **overrides):
    manifest = {
        "version": "1.0",
        "series_id": "s1",
        "episode_id": episode,
        "project_id": project,
        "target_platform": "tiktok",
        "duration_s": 120,
        "review_policy": {
            "mode": "sampled",
            "detail_ratio": 0.35,
            "face_sim_threshold": 0.75,
            "sampling_salt": episode,
        },
    }
    manifest.update(overrides)
    path = projects / project / ("episode_manifest.json" if at_project_root else Path(episode) / "episode_manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def envelope_keys():
    return {"code", "what", "fix", "recovery", "side_effects", "context"}


def _make_dir_link(link: Path, target: Path) -> None:
    """Create a directory symlink, or a Windows junction when symlinks are denied."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return
    pytest.skip("directory links unavailable on this platform")


# --- OpenAPI --------------------------------------------------------------


def test_openapi_minimal_set_paths_and_versioning(client):
    spec = client.get("/openapi.json").json()
    paths = {(path, method) for path, ops in spec["paths"].items() for method in ops}
    assert MINIMAL_PATHS <= paths
    assert all(path.startswith("/api/v1/") or path.startswith(("/media/", "/thumb/"))
               for path, _method in paths)
    assert spec["info"]["version"] == "0.1.0"


def test_openapi_exposes_frozen_schemas(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    for name in ("ReviewItemModel", "ReviewDecisionModel", "ReviewPolicyModel",
                 "ErrorEnvelope", "DecideRequest", "BatchResponse"):
        assert name in schemas
    decision = schemas["ReviewDecisionModel"]["properties"]
    action_enum = schemas["Action"]["enum"]  # action is a $ref to the Action enum
    assert set(action_enum) == {
        "accept", "reject", "pick_variant", "edit_prompt", "send_back", "skip"}
    for field in ("override", "evidence", "chosen_candidate", "reason_code"):
        assert field in decision
    envelope = schemas["ErrorEnvelope"]["properties"]
    assert {"code", "what", "fix", "recovery", "side_effects", "context"} <= set(envelope)


def test_error_code_catalog_is_complete_and_machine_readable():
    for code in ErrorCode:
        assert code.value in ERROR_CATALOG
    assert set(RECOVERY_VALUES) == {"retry", "fix-then-retry", "do-X-first", "ask-human"}
    assert set(SIDE_EFFECT_VALUES) == {"none", "partial", "complete", "unknown"}


# --- review: happy path through the real store ---------------------------


def test_decide_accept_writes_real_review_log(client, service):
    add_item(service)
    resp = client.post("/api/v1/review/r1/decision", json={"action": "accept"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"]["action"] == "accept"
    assert body["idempotent_replay"] is False

    log_path = service.projects_dir / "drama" / "ep01" / "reviews.jsonl"
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    # item + decision: the actual ReviewStore authority wrote the log.
    assert [r["event_type"] for r in records] == ["item", "decision"]
    for record in records:
        REVIEW_VALIDATOR.validate(record)

    detail = client.get("/api/v1/review/r1").json()
    assert detail["decision"]["action"] == "accept"
    assert detail["stale"] is False
    assert client.get("/api/v1/review/pending").json()["count"] == 0


def test_cli_grammar_aliases_are_canonicalized(client, service):
    add_item(service)
    resp = client.post(
        "/api/v1/review/r1/decision",
        json={"action": "pick-variant", "chosen_candidate": "b.mp4"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"]["action"] == "pick_variant"


def test_undo_requeues(client, service):
    add_item(service)
    client.post("/api/v1/review/r1/decision", json={"action": "accept"})
    resp = client.post("/api/v1/review/r1/undo", json={"reason": "mis-keyed"})
    assert resp.status_code == 200
    assert resp.json()["undone"] is True
    assert client.get("/api/v1/review/r1").json()["decision"] is None
    assert client.get("/api/v1/review/pending").json()["count"] == 1


def test_idempotency_key_prevents_duplicate_decision(client, service):
    add_item(service)
    payload = {"action": "accept"}
    first = client.post("/api/v1/review/r1/decision", json=payload,
                        headers={"Idempotency-Key": "k-1"})
    second = client.post("/api/v1/review/r1/decision", json=payload,
                         headers={"Idempotency-Key": "k-1"})
    assert first.status_code == second.status_code == 200
    assert second.json()["idempotent_replay"] is True

    log_path = service.projects_dir / "drama" / "ep01" / "reviews.jsonl"
    assert len(log_path.read_text(encoding="utf-8").strip().splitlines()) == 2  # no duplicate

    add_item(service, rid="r2")
    conflict = client.post("/api/v1/review/r2/decision", json=payload,
                           headers={"Idempotency-Key": "k-1"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == ErrorCode.VALIDATION_ERROR.value


# --- parity: CLI path (service) vs HTTP path -----------------------------


PARITY_CASES = [
    ("invalid_action", {"action": "frobnicate"}, ErrorCode.INVALID_ACTION),
    ("missing_reason", {"action": "reject"}, ErrorCode.MISSING_REASON_CODE),
    ("unknown_reason", {"action": "reject", "reason_code": "NOT_A_CODE"}, ErrorCode.UNKNOWN_REASON_CODE),
    ("inapplicable_reason", {"action": "reject", "reason_code": "PACING"},
     ErrorCode.REASON_CODE_NOT_APPLICABLE),
    ("bad_candidate", {"action": "pick_variant", "chosen_candidate": "zzz.mp4"},
     ErrorCode.INVALID_CANDIDATE),
    ("override_no_evidence", {"action": "accept", "override": True},
     ErrorCode.OVERRIDE_EVIDENCE_REQUIRED),
]


@pytest.mark.parametrize("name,payload,expected", PARITY_CASES, ids=[c[0] for c in PARITY_CASES])
def test_cli_api_parity_same_invalid_input_same_rejection(service, client, name, payload, expected):
    add_item(service)

    # HTTP layer
    http = client.post("/api/v1/review/r1/decision", json=payload)
    assert http.status_code == 422, http.text
    http_body = http.json()
    assert http_body["code"] == expected.value
    assert envelope_keys() <= set(http_body)

    # CLI layer entry point: the exact service call WP-G will make.
    with pytest.raises(ApiError) as caught:
        service.decide("r1", DecideRequest(**payload))
    assert caught.value.code == expected
    assert caught.value.to_envelope()["code"] == http_body["code"]
    assert caught.value.exit_code == 2

    # Neither layer mutated the log.
    log_path = service.projects_dir / "drama" / "ep01" / "reviews.jsonl"
    assert len(log_path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_envelope_contract_fields_for_validation_error(client, service):
    add_item(service)
    body = client.post("/api/v1/review/r1/decision", json={"action": "reject"}).json()
    assert envelope_keys() <= set(body)
    assert body["recovery"] in RECOVERY_VALUES
    assert body["side_effects"] == "none"
    assert body["context"]["stage"] == "shot_gen"
    assert "trace" not in body or isinstance(body["trace"], str)


def test_request_validation_uses_shared_envelope(client):
    resp = client.post("/api/v1/review/r1/decision", json={"note": "missing action"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ErrorCode.VALIDATION_ERROR.value
    assert envelope_keys() <= set(body)


def test_stale_review_is_409(client, service):
    add_item(service, version="v2")
    resp = client.post("/api/v1/review/r1/decision",
                       json={"action": "accept", "candidate_version": "v1"})
    assert resp.status_code == 409
    assert resp.json()["code"] == ErrorCode.STALE_REVIEW.value


def test_unknown_review_is_404(client):
    resp = client.get("/api/v1/review/nope")
    assert resp.status_code == 404
    assert resp.json()["code"] == ErrorCode.REVIEW_NOT_FOUND.value


# --- queue filters --------------------------------------------------------


def test_pending_filters_stage_priority_and_is_deterministic(client, service):
    add_item(service, rid="must", priority=0)
    add_item(service, rid="opt", priority=2)
    add_item(service, rid="asm", stage="assembly", priority=1)

    opts = [r["review_id"] for r in
            client.get("/api/v1/review/pending", params={"stage": "shot_gen", "priority": 2})
            .json()["items"]]
    assert opts == ["opt"]

    first = [r["review_id"] for r in client.get("/api/v1/review/pending").json()["items"]]
    second = [r["review_id"] for r in client.get("/api/v1/review/pending").json()["items"]]
    assert first == second
    assert first[0] == "must"  # must-view sorts first, never sampled away

    restricted = client.get("/api/v1/review/pending", params={"ratio": 0.0, "mode": "sampled"}).json()
    assert [r["review_id"] for r in restricted["items"]] == ["must"]
    assert restricted["policy"]["detail_ratio"] == 0.0


def test_stale_filter_finds_decisions_on_superseded_items(client, service):
    store = add_item(service, version="v1")
    client.post("/api/v1/review/r1/decision", json={"action": "accept"})
    # New candidate snapshot supersedes v1; the old decision is now stale.
    store.add_item(ReviewItem(
        review_id="r1", project="drama", episode="ep01", stage="shot_gen",
        slot="sh_r1", candidates=["c.mp4"], priority=0, candidate_version="v2",
    ))
    stale = client.get("/api/v1/review/pending", params={"stale": "true"}).json()
    assert [r["review_id"] for r in stale["items"]] == ["r1"]
    assert client.get("/api/v1/review/r1").json()["stale"] is True


# --- batch preconditions (REV-07) ----------------------------------------


def test_batch_all_pending_is_ok(client, service):
    add_item(service, rid="r1")
    add_item(service, rid="r2")
    body = client.post("/api/v1/review/batch", json={"episode": "ep01", "dry_run": True}).json()
    assert body["ok"] is True
    assert sorted(body["pending"]) == ["r1", "r2"]
    assert body["dry_run"] is True


def test_batch_reports_missing_and_stale(client, service):
    store = add_item(service, rid="r1")
    client.post("/api/v1/review/r1/decision", json={"action": "accept"})
    store.add_item(ReviewItem(
        review_id="r1", project="drama", episode="ep01", stage="shot_gen",
        slot="sh_r1", candidates=["c.mp4"], priority=0, candidate_version="v2",
    ))
    body = client.post("/api/v1/review/batch",
                       json={"episode": "ep01", "review_ids": ["r1", "ghost"]}).json()
    assert body["ok"] is False
    assert body["stale"] == ["r1"]
    assert body["missing"] == ["ghost"]
    codes = {err["code"] for err in body["errors"]}
    assert {ErrorCode.STALE_REVIEW.value, ErrorCode.REVIEW_NOT_FOUND.value} <= codes


def test_batch_nothing_pending_is_machine_readable(client, service):
    body = client.post("/api/v1/review/batch", json={"episode": "ep01"}).json()
    assert body["ok"] is False
    assert ErrorCode.NOTHING_PENDING.value in {err["code"] for err in body["errors"]}


# --- episode surfaces -----------------------------------------------------


def test_episode_listing_detail_and_not_found(client, projects):
    write_manifest(projects)
    listing = client.get("/api/v1/episodes").json()
    assert listing["count"] == 1
    assert listing["episodes"][0]["episode_id"] == "ep01"

    detail = client.get("/api/v1/episode/ep01").json()
    assert detail["series_id"] == "s1" and detail["target_platform"] == "tiktok"

    missing = client.get("/api/v1/episode/ghost")
    assert missing.status_code == 404
    assert missing.json()["code"] == ErrorCode.EPISODE_NOT_FOUND.value


def test_review_policy_get_put_persists(client, projects):
    manifest_path = write_manifest(projects)
    got = client.get("/api/v1/episode/ep01/review_policy").json()
    assert got["mode"] == "sampled" and got["detail_ratio"] == 0.35

    put = client.put("/api/v1/episode/ep01/review_policy",
                     json={"mode": "all", "detail_ratio": 0.9,
                           "face_sim_threshold": 0.8, "sampling_salt": "ep01"})
    assert put.status_code == 200, put.text
    assert put.json()["mode"] == "all"
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted["review_policy"]["detail_ratio"] == 0.9
    assert persisted["episode_id"] == "ep01"  # other manifest fields preserved

    bad = client.put("/api/v1/episode/ep01/review_policy",
                     json={"mode": "sampled", "detail_ratio": 2.0,
                           "face_sim_threshold": 0.8, "sampling_salt": "ep01"})
    assert bad.status_code == 422
    assert bad.json()["code"] == ErrorCode.VALIDATION_ERROR.value


def test_shots_projection_and_stats(client, projects, service):
    write_manifest(projects)
    log = EpisodeLog("drama", "ep01", projects)
    log.append_event("shot_planned", "sh_001")
    log.append_event("shot_keyframing", "sh_001")
    log.append_event("shot_planned", "sh_002")

    shots = client.get("/api/v1/episode/ep01/shots").json()
    states = {shot["shot_id"]: shot["state"] for shot in shots["shots"]}
    assert states == {"sh_001": "keyframing", "sh_002": "planned"}

    add_item(service)
    client.post("/api/v1/review/r1/decision", json={"action": "accept"})
    stats = client.get("/api/v1/progress/ep01/stats").json()
    assert stats["total_shots"] == 2
    assert stats["shot_states"] == {"keyframing": 1, "planned": 1}
    assert stats["review"]["decided"] == 1
    assert stats["review"]["first_pass_accept_rate"] == 1.0


# --- library: mock boundary ----------------------------------------------


def test_library_query_is_mock_and_filterable(client):
    body = client.get("/api/v1/library/shot").json()
    assert body["provider"] == "mock"
    assert body["total"] == 2
    assert {item["asset_id"] for item in body["items"]} == {"ast_shot_001", "ast_shot_002"}

    by_text = client.get("/api/v1/library/shot", params={"text": "window"}).json()
    assert [i["asset_id"] for i in by_text["items"]] == ["ast_shot_001"]

    by_char = client.get("/api/v1/library/shot", params={"character_id": "ch_wei"}).json()
    assert [i["asset_id"] for i in by_char["items"]] == ["ast_shot_002"]

    by_sim = client.get("/api/v1/library/shot", params={"min_sim": 0.9}).json()
    assert [i["asset_id"] for i in by_sim["items"]] == ["ast_shot_001"]

    by_reason = client.get("/api/v1/library/shot", params={"reason_code": "OFF_POSE"}).json()
    assert [i["asset_id"] for i in by_reason["items"]] == ["ast_shot_002"]

    paged = client.get("/api/v1/library/shot", params={"page_size": 1, "page": 2}).json()
    assert paged["total"] == 2 and len(paged["items"]) == 1


def test_library_get_and_errors(client):
    asset = client.get("/api/v1/library/shot/ast_shot_001").json()
    assert asset["provider"] == "mock"
    assert asset["asset"]["character_id"] == "ch_ling"

    wrong_kind = client.get("/api/v1/library/character/ast_shot_001")
    assert wrong_kind.status_code == 404
    assert wrong_kind.json()["code"] == ErrorCode.LIBRARY_ASSET_NOT_FOUND.value

    unknown_kind = client.get("/api/v1/library/banana")
    assert unknown_kind.status_code == 400
    assert unknown_kind.json()["code"] == ErrorCode.UNSUPPORTED_LIBRARY_KIND.value


def test_injected_mock_library_is_used(projects, lib_dir):
    custom = MockAssetLibrary([{
        "asset_id": "only_asset", "kind": "shot", "character_id": "ch_x",
        "text": "custom fixture", "tags": [],
    }])
    app = create_app(projects_dir=projects, library_dir=lib_dir, library=custom)
    body = TestClient(app).get("/api/v1/library/shot").json()
    assert body["provider"] == "mock"
    assert [i["asset_id"] for i in body["items"]] == ["only_asset"]


# --- media: confinement + serving ----------------------------------------


@pytest.fixture
def media_files(projects, lib_dir):
    ep_dir = projects / "drama" / "ep01" / "media"
    ep_dir.mkdir(parents=True)
    clip = ep_dir / "clip.mp4"
    clip.write_bytes(b"0123456789")
    lib_file = lib_dir / "shot" / "ast_shot_001" / "clip.mp4"
    lib_file.parent.mkdir(parents=True)
    lib_file.write_bytes(b"library-bytes")
    return clip, lib_file


def test_media_and_thumb_serve_files_with_range(client, media_files):
    resp = client.get("/media/ep01/media/clip.mp4")
    assert resp.status_code == 200
    assert resp.content == b"0123456789"

    ranged = client.get("/media/ep01/media/clip.mp4", headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206
    assert ranged.content == b"0123"

    thumb = client.get("/thumb/ep01/media/clip.mp4")
    assert thumb.status_code == 200


def test_library_media_serves_and_rejects_unknown_kind(client, media_files):
    resp = client.get("/media/library/shot/ast_shot_001/clip.mp4")
    assert resp.status_code == 200
    assert resp.content == b"library-bytes"

    unknown = client.get("/media/library/banana/ast_shot_001/clip.mp4")
    assert unknown.status_code == 400
    assert unknown.json()["code"] == ErrorCode.UNSUPPORTED_LIBRARY_KIND.value


@pytest.mark.parametrize("bad_path", [
    "../secret.txt",
    "..\\secret.txt",
    "/etc/passwd",
    "C:/Windows/win.ini",
    "media/../../secret.txt",
    "media/%2e%2e/secret.txt",
])
def test_media_path_traversal_is_forbidden(client, media_files, bad_path):
    resp = client.get(f"/media/ep01/{bad_path}")
    assert resp.status_code in (403, 404), resp.text
    if resp.status_code == 403:
        assert resp.json()["code"] == ErrorCode.MEDIA_PATH_FORBIDDEN.value


def test_library_traversal_is_forbidden(client, media_files):
    resp = client.get("/media/library/shot/..%2f..%2fsecret/clip.mp4")
    assert resp.status_code == 403
    assert resp.json()["code"] == ErrorCode.MEDIA_PATH_FORBIDDEN.value


def test_media_missing_file_is_404(client, projects):
    (projects / "drama" / "ep01").mkdir(parents=True)
    resp = client.get("/media/ep01/media/nope.mp4")
    assert resp.status_code == 404
    assert resp.json()["code"] == ErrorCode.MEDIA_NOT_FOUND.value


def test_locator_rejects_absolute_and_drive_paths(projects, lib_dir):
    (projects / "drama" / "ep01").mkdir(parents=True)
    locator = MediaLocator(projects, lib_dir)
    for path in ("/etc/passwd", "C:/Windows/win.ini", "..\\x", "\\\\server\\share\\x"):
        with pytest.raises(ApiError) as caught:
            locator.locate_episode("ep01", path)
        assert caught.value.code == ErrorCode.MEDIA_PATH_FORBIDDEN


def test_locator_rejects_symlink_escape(projects, lib_dir, tmp_path):
    ep_dir = projects / "drama" / "ep01"
    ep_dir.mkdir(parents=True)
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    _make_dir_link(ep_dir / "link", outside)
    locator = MediaLocator(projects, lib_dir)
    with pytest.raises(ApiError) as caught:
        locator.locate_episode("ep01", "link/secret.txt")
    assert caught.value.code == ErrorCode.MEDIA_PATH_FORBIDDEN


def test_episode_root_falls_back_to_project_root_manifest(projects, lib_dir):
    manifest = write_manifest(projects, episode="ep02", at_project_root=True)
    assert manifest.parent == projects / "drama"
    asset = projects / "drama" / "assets" / "poster.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"png")
    locator = MediaLocator(projects, lib_dir)
    assert locator.locate_episode("ep02", "assets/poster.png") == asset.resolve()


def test_media_root_must_not_escape_via_symlinked_root(projects, lib_dir, tmp_path):
    outside = tmp_path / "outside_root"
    outside.mkdir()
    (outside / "x.txt").write_text("x", encoding="utf-8")
    episode_link = projects / "drama" / "ep01"
    episode_link.parent.mkdir(parents=True)
    _make_dir_link(episode_link, outside)
    locator = MediaLocator(projects, lib_dir)
    # The symlinked root resolves to the outside dir; confinement is relative
    # to the resolved root, so reading inside it is allowed and traversal is not.
    assert locator.locate_episode("ep01", "x.txt").is_file()
    with pytest.raises(ApiError):
        locator.locate_episode("ep01", "../x.txt")


# --- error envelope for review-layer classification ----------------------


def test_classification_covers_every_parity_code():
    from shortdrama.errors import classify_review_rejection
    sample_item = {"stage": "shot_gen", "candidates": ["a.mp4"]}

    class D:
        def __init__(self, action, chosen_candidate=None, reason_code=None,
                     override=False, evidence=""):
            self.action = action
            self.chosen_candidate = chosen_candidate
            self.reason_code = reason_code
            self.override = override
            self.evidence = evidence

    assert classify_review_rejection(D("nope"), sample_item)[0] == ErrorCode.INVALID_ACTION
    assert classify_review_rejection(D("reject"), sample_item)[0] == ErrorCode.MISSING_REASON_CODE
    assert classify_review_rejection(D("reject", reason_code="X"), sample_item)[0] == ErrorCode.UNKNOWN_REASON_CODE
    assert classify_review_rejection(D("reject", reason_code="PACING"), sample_item)[0] == \
        ErrorCode.REASON_CODE_NOT_APPLICABLE
    assert classify_review_rejection(D("pick_variant", chosen_candidate="x"), sample_item)[0] == \
        ErrorCode.INVALID_CANDIDATE
    assert classify_review_rejection(D("accept", override=True), sample_item)[0] == \
        ErrorCode.OVERRIDE_EVIDENCE_REQUIRED