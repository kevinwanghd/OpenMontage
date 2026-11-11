"""UI-01 / UI-02 contract tests: library API surface + media access security.

These tests lock the additive ``/api/v1`` library contract (§10.1) and the
security envelope around ``/media/library/{kind}/{asset_id}/...``:

- the frozen review+media surface keeps working unchanged (additive-only, X-16);
- library retrieval exposes the full LIB-05 filter set (``text``/``image``/
  ``character_id``/``min_sim``/``reason_code``/``page``/``page_size``);
- every rejection uses the shared §10.3 envelope;
- media only resolves for a real library asset of the requested kind, inside
  its own root, with traversal and out-of-root symlinks rejected, and no
  absolute server paths echoed back (X-12);
- requests are local-only by default, remote mode requires a bearer token, and
  same-origin CSRF rules reject cross-origin / cross-site callers.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lib.review import ReviewItem, ReviewStore
from shortdrama.api import create_app
from shortdrama.errors import ERROR_CATALOG, RECOVERY_VALUES, SIDE_EFFECT_VALUES, ErrorCode

ENVELOPE_KEYS = {"code", "what", "fix", "recovery", "side_effects", "context"}

LIBRARY_FILTER_PARAMS = {
    "text", "image", "character_id", "min_sim", "reason_code", "page", "page_size",
}


# --- fixtures / helpers ---------------------------------------------------


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def lib_dir(tmp_path):
    return tmp_path / "library"


@pytest.fixture
def client(projects, lib_dir):
    return TestClient(create_app(projects_dir=projects, library_dir=lib_dir))


@pytest.fixture
def media(projects, lib_dir):
    """One legal file per default mock asset kind."""
    shot = lib_dir / "shot" / "ast_shot_001" / "clip.mp4"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"shot-bytes")
    poster = lib_dir / "keyframe" / "ast_key_001" / "poster.png"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"poster-bytes")
    return shot, poster


def add_item(service, rid="r1", *, stage="shot_gen", project="drama", episode="ep01"):
    store = ReviewStore(project, episode, service.projects_dir)
    store.add_item(ReviewItem(
        review_id=rid, project=project, episode=episode, stage=stage,
        slot=f"sh_{rid}", candidates=["a.mp4", "b.mp4"], priority=0,
    ))
    return store


def _make_dir_link(link: Path, target: Path) -> None:
    """Create a directory symlink, or a Windows junction when symlinks are denied."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("directory links unavailable on this platform")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return
    pytest.skip("directory links unavailable on this platform")


def assert_envelope(body: dict, code: ErrorCode | str | None = None) -> None:
    assert ENVELOPE_KEYS <= set(body), body
    assert body["recovery"] in RECOVERY_VALUES
    assert body["side_effects"] in SIDE_EFFECT_VALUES
    assert isinstance(body["context"], dict)
    if code is not None:
        assert body["code"] == (code.value if isinstance(code, ErrorCode) else code)


# --- UI-01: additive library contract -------------------------------------


def test_openapi_library_surface_is_additive_and_versioned(client):
    spec = client.get("/openapi.json").json()
    paths = {(path, method) for path, ops in spec["paths"].items() for method in ops}

    # the frozen M0 surface is untouched...
    for frozen in (
        ("/api/v1/library/{kind}", "get"),
        ("/api/v1/library/{kind}/{asset_id}", "get"),
        ("/media/library/{kind}/{asset_id}/{file_path}", "get"),
    ):
        assert frozen in paths
    # ...and every API route stays under /api/v1 (additive-only, X-16).
    assert spec["info"]["version"] == "0.1.0"
    assert all(
        path.startswith(("/api/v1/", "/media/", "/thumb/"))
        for path, _method in paths
    )

    list_op = spec["paths"]["/api/v1/library/{kind}"]["get"]
    assert LIBRARY_FILTER_PARAMS <= {p["name"] for p in list_op["parameters"]}
    # existing filters are not required, so old callers keep working.
    required = {p["name"] for p in list_op["parameters"] if p.get("required")}
    assert required == {"kind"}

    schemas = spec["components"]["schemas"]
    for name in ("LibraryListResponse", "LibraryAssetResponse", "ErrorEnvelope"):
        assert name in schemas
    # rejections on the library surface are documented with the shared envelope
    for status in ("400", "404", "422"):
        ref = list_op["responses"][status]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorEnvelope")


def test_library_query_without_new_filters_is_unchanged(client):
    body = client.get("/api/v1/library/shot").json()
    assert body["provider"] == "mock"
    assert body["kind"] == "shot"
    assert body["total"] == 2
    assert [i["asset_id"] for i in body["items"]] == ["ast_shot_001", "ast_shot_002"]
    assert body["page"] == 1 and body["page_size"] == 50


def test_library_image_filter_is_additive_and_deterministic(client):
    by_image = client.get("/api/v1/library/keyframe", params={"image": "keyframes"})
    assert by_image.status_code == 200, by_image.text
    assert [i["asset_id"] for i in by_image.json()["items"]] == ["ast_key_001"]

    # combines with the existing metadata filters
    combined = client.get(
        "/api/v1/library/keyframe",
        params={"image": "keyframes", "character_id": "ch_wei"},
    ).json()
    assert combined["total"] == 0

    empty = client.get("/api/v1/library/keyframe", params={"image": "no-such-ref"}).json()
    assert empty["total"] == 0 and empty["items"] == []


def test_text_and_image_are_mutually_exclusive_with_envelope(client):
    resp = client.get("/api/v1/library/shot", params={"text": "window", "image": "shots"})
    assert resp.status_code == 422
    assert_envelope(resp.json(), ErrorCode.VALIDATION_ERROR)
    assert "either text or image" in resp.json()["fix"]


@pytest.mark.parametrize("path,status,code", [
    ("/api/v1/library/banana", 400, ErrorCode.UNSUPPORTED_LIBRARY_KIND),
    ("/api/v1/library/shot/ghost", 404, ErrorCode.LIBRARY_ASSET_NOT_FOUND),
    ("/api/v1/library/character/ast_shot_001", 404, ErrorCode.LIBRARY_ASSET_NOT_FOUND),
    ("/api/v1/library/shot?page_size=0", 422, ErrorCode.VALIDATION_ERROR),
    ("/api/v1/library/shot?page=0", 422, ErrorCode.VALIDATION_ERROR),
])
def test_library_rejections_use_shared_envelope(client, path, status, code):
    resp = client.get(path)
    assert resp.status_code == status, resp.text
    assert_envelope(resp.json(), code)


def test_error_catalog_still_covers_every_code():
    for code in ErrorCode:
        assert code.value in ERROR_CATALOG


# --- UI-02: legal media access + root confinement -------------------------


def test_library_media_serves_only_existing_assets(client, media):
    resp = client.get("/media/library/shot/ast_shot_001/clip.mp4")
    assert resp.status_code == 200
    assert resp.content == b"shot-bytes"

    # Range support survives the security middleware.
    ranged = client.get("/media/library/shot/ast_shot_001/clip.mp4",
                        headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206
    assert ranged.content == b"shot"

    keyframe = client.get("/media/library/keyframe/ast_key_001/poster.png")
    assert keyframe.status_code == 200 and keyframe.content == b"poster-bytes"


@pytest.mark.parametrize("path,status,code", [
    ("/media/library/shot/ghost/clip.mp4", 404, ErrorCode.LIBRARY_ASSET_NOT_FOUND),
    ("/media/library/keyframe/ast_shot_001/clip.mp4", 404, ErrorCode.LIBRARY_ASSET_NOT_FOUND),
    ("/media/library/banana/ast_shot_001/clip.mp4", 400, ErrorCode.UNSUPPORTED_LIBRARY_KIND),
    ("/media/library/shot/ast_shot_001/nope.mp4", 404, ErrorCode.MEDIA_NOT_FOUND),
])
def test_library_media_legal_access_rejections(client, media, path, status, code):
    resp = client.get(path)
    assert resp.status_code == status, resp.text
    assert_envelope(resp.json(), code)


@pytest.mark.parametrize("bad_path", [
    "..%2fsecret.txt",
    "%2e%2e%2fsecret.txt",
    "%2e%2e%5csecret.txt",
    "%2Fetc%2Fpasswd",
    "C%3A%5CWindows%5Cwin.ini",
    "..%2f..%2fother%2Fasset%2Fclip.mp4",
])
def test_library_media_path_traversal_is_forbidden(client, media, bad_path):
    resp = client.get(f"/media/library/shot/ast_shot_001/{bad_path}")
    assert resp.status_code == 403, resp.text
    assert_envelope(resp.json(), ErrorCode.MEDIA_PATH_FORBIDDEN)


@pytest.mark.parametrize("asset_id", ["..%2f..%2fsecret", "%2e%2e", "C%3A%5Ctmp"])
def test_library_media_bad_asset_segment_is_forbidden(client, media, asset_id):
    resp = client.get(f"/media/library/shot/{asset_id}/clip.mp4")
    assert resp.status_code == 403, resp.text
    assert_envelope(resp.json(), ErrorCode.MEDIA_PATH_FORBIDDEN)


def test_library_media_symlink_escape_is_forbidden(client, media, lib_dir, tmp_path):
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    _make_dir_link(lib_dir / "shot" / "ast_shot_001" / "link", outside)

    resp = client.get("/media/library/shot/ast_shot_001/link/secret.txt")
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert_envelope(body, ErrorCode.MEDIA_PATH_FORBIDDEN)
    # X-12: no absolute root leaks back to the client.
    assert "root" not in body["context"]
    assert str(lib_dir) not in json.dumps(body)


def test_library_media_cannot_read_a_sibling_asset_root(client, media, lib_dir):
    sibling = lib_dir / "shot" / "ast_shot_002" / "other.mp4"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"sibling")

    resp = client.get("/media/library/shot/ast_shot_001/..%2fast_shot_002%2Fother.mp4")
    assert resp.status_code == 403, resp.text
    assert_envelope(resp.json(), ErrorCode.MEDIA_PATH_FORBIDDEN)


def test_library_media_cannot_cross_into_another_kind_root(client, media, lib_dir):
    other_kind = lib_dir / "music" / "ast_shot_001" / "track.mp3"
    other_kind.parent.mkdir(parents=True)
    other_kind.write_bytes(b"audio")

    resp = client.get("/media/library/shot/ast_shot_001/..%2F..%2Fmusic%2Fast_shot_001%2Ftrack.mp3")
    assert resp.status_code == 403, resp.text
    assert_envelope(resp.json(), ErrorCode.MEDIA_PATH_FORBIDDEN)


# --- UI-02: CSRF / same-origin --------------------------------------------


def test_same_origin_requests_are_allowed(client):
    origin = client.get("/api/v1/episodes", headers={"Origin": "http://testserver"})
    assert origin.status_code == 200
    fetch_meta = client.get("/api/v1/episodes", headers={"Sec-Fetch-Site": "same-origin"})
    assert fetch_meta.status_code == 200


@pytest.mark.parametrize("origin", [
    "http://evil.example",
    "https://testserver",          # scheme mismatch
    "http://testserver:9999",      # port mismatch
    "null",
])
def test_cross_origin_requests_are_rejected(client, origin):
    resp = client.get("/api/v1/library/shot", headers={"Origin": origin})
    assert resp.status_code == 403, resp.text
    assert_envelope(resp.json(), ErrorCode.VALIDATION_ERROR)
    assert "cross-origin" in resp.json()["what"]


def test_cross_site_fetch_metadata_is_rejected(client):
    resp = client.get("/media/library/shot/ast_shot_001/clip.mp4",
                      headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert_envelope(resp.json(), ErrorCode.VALIDATION_ERROR)


def test_cross_origin_mutation_does_not_touch_the_log(projects, lib_dir):
    app = create_app(projects_dir=projects, library_dir=lib_dir)
    client = TestClient(app)
    add_item(app.state.service)

    resp = client.post(
        "/api/v1/review/r1/decision",
        json={"action": "accept"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert_envelope(resp.json(), ErrorCode.VALIDATION_ERROR)
    assert client.get("/api/v1/review/pending").json()["count"] == 1
    log = (projects / "drama" / "ep01" / "reviews.jsonl").read_text(encoding="utf-8")
    assert len(log.strip().splitlines()) == 1  # only the item record


# --- UI-02: localhost default + remote auth -------------------------------


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1",
                                  "127.0.0.1:8000", "[::1]:8000"])
def test_loopback_hosts_are_allowed_by_default(client, host):
    resp = client.get("/api/v1/episodes", headers={"Host": host})
    assert resp.status_code == 200, resp.text


def test_remote_host_is_rejected_by_default(client):
    resp = client.get("/api/v1/episodes", headers={"Host": "evil.example.com"})
    assert resp.status_code == 403
    assert_envelope(resp.json(), ErrorCode.VALIDATION_ERROR)
    assert "loopback" in resp.json()["what"]


def test_remote_mode_refuses_to_start_without_auth(projects, lib_dir):
    with pytest.raises(ValueError, match="auth_token"):
        create_app(projects_dir=projects, library_dir=lib_dir, allow_remote=True)


def test_remote_mode_requires_bearer_token(projects, lib_dir):
    app = create_app(projects_dir=projects, library_dir=lib_dir,
                     allow_remote=True, auth_token="s3cret")
    client = TestClient(app)
    remote = {"Host": "api.example.com"}

    missing = client.get("/api/v1/episodes", headers=remote)
    assert missing.status_code == 401
    assert_envelope(missing.json(), ErrorCode.VALIDATION_ERROR)

    wrong = client.get("/api/v1/episodes",
                       headers={**remote, "Authorization": "Bearer nope"})
    assert wrong.status_code == 401

    ok = client.get("/api/v1/episodes",
                    headers={**remote, "Authorization": "Bearer s3cret"})
    assert ok.status_code == 200

    # a configured token applies to loopback callers too
    local_no_token = client.get("/api/v1/episodes")
    assert local_no_token.status_code == 401


def test_security_headers_are_present_on_media_and_errors(client, media):
    served = client.get("/media/library/shot/ast_shot_001/clip.mp4")
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["cross-origin-resource-policy"] == "same-origin"

    rejected = client.get("/media/library/shot/ghost/clip.mp4")
    assert rejected.headers["x-content-type-options"] == "nosniff"
    assert rejected.headers["cross-origin-resource-policy"] == "same-origin"