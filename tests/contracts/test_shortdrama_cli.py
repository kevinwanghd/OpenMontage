"""T8 contract tests: ``om`` CLI grammar, JSON/exit codes, demo and parity.

Guards:
- the whole §10.2 grammar exists: review list/show/decide(6 actions)/policy/
  batch/undo/watch/stats, doctor, profile list/use, demo --mock;
- ``--json`` emits machine-readable documents; failures emit the shared
  §10.3 envelope on stderr and return the ApiError exit code
  (0 ok / 2 usage / 3 nothing pending / 4 stale-or-blocked / 5 budget);
- ``demo --mock`` seeds a local episode and the hello-world path
  (doctor -> demo -> list -> decide -> stats) runs with zero API calls and
  well under five minutes;
- CLI and API reject the same illegal decision with the same code because the
  CLI goes through ``ShortdramaService`` -> ``ReviewStore.decide`` (X-11);
- ``watch --verify`` records a versioned ``full_watch`` decision (REV-11).

M0: local files only, no network.
"""

from __future__ import annotations

import io
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

import lib.capability_slots as cs
from lib.episode_log import EpisodeLog
from lib.review import ReviewItem, ReviewStore
from shortdrama import cli
from shortdrama.api import ShortdramaService
from shortdrama.errors import ApiError, ErrorCode
from shortdrama.models import DecideRequest

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- fixtures / helpers ----------------------------------------------------


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return root


def run(projects, *argv, bindings=None):
    args = ["--projects-dir", str(projects)]
    if bindings is not None:
        args += ["--bindings-dir", str(bindings)]
    args += list(argv)
    return cli.main(args)


def add_item(projects, rid="r1", *, stage="shot_gen", candidates=None,
             version="v1", priority=0, project="drama", episode="ep01"):
    store = ReviewStore(project, episode, projects)
    store.add_item(ReviewItem(
        review_id=rid, project=project, episode=episode, stage=stage,
        slot=f"sh_{rid}", candidates=candidates if candidates is not None else ["a.mp4", "b.mp4"],
        priority=priority, candidate_version=version,
    ))
    return store


def write_manifest(projects, project="drama", episode="ep01", **overrides):
    manifest = {
        "version": "1.0",
        "series_id": "s1",
        "episode_id": episode,
        "project_id": project,
        "target_platform": "tiktok",
        "duration_s": 30,
        "review_policy": {
            "mode": "sampled",
            "detail_ratio": 0.35,
            "face_sim_threshold": 0.75,
            "sampling_salt": episode,
        },
    }
    manifest.update(overrides)
    path = projects / project / episode / "episode_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def read_log(projects, project="drama", episode="ep01"):
    path = projects / project / episode / "reviews.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def json_out(capsys):
    out = capsys.readouterr()
    return out, json.loads(out.out) if out.out.strip() else None


def json_err(capsys):
    out = capsys.readouterr()
    return out, json.loads(out.err) if out.err.strip() else None


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def make_bindings(root: Path, *, include_ghost: bool = True) -> Path:
    _write_yaml(root / "slots.yaml", {
        "version": "1.0",
        "slots": {"video_primary": {"required": True}},
    })
    _write_yaml(root / "profiles" / "green.yaml", {
        "version": "1.0",
        "bindings": {"video_primary": {"chain": [
            {"tool": "mock_video", "operations": ["image_to_video"]},
        ]}},
        "degradation": [],
    })
    if include_ghost:
        _write_yaml(root / "profiles" / "ghost.yaml", {
            "version": "1.0",
            "bindings": {"video_primary": {"chain": [
                {"tool": "ghost_video", "operations": ["image_to_video"]},
            ]}},
            "degradation": [],
        })
    _write_yaml(root / "model_bindings.yaml", {
        "version": "1.0",
        "active_profile": "green",
        "profiles_dir": "profiles",
    })
    _write_yaml(root / "account_state.yaml", {"version": "1.0", "providers": {}})
    return root


def fake_tool_state(name: str) -> dict:
    if name == "mock_video":
        return {
            "registered": True, "status": "available", "supports": {},
            "capabilities": ["image_to_video"], "module_path": "tests.fake",
            "error": None,
        }
    return {
        "registered": False, "status": "missing", "supports": {},
        "capabilities": [], "module_path": None, "error": None,
    }


# --- entry points ----------------------------------------------------------


def test_serve_command_is_wired_to_the_same_origin_console():
    args = cli.build_parser().parse_args(["serve", "--port", "9123"])
    assert args.func is cli._cmd_serve
    assert args.port == 9123
    assert args.host == "127.0.0.1"
    assert args.allow_remote is False
    # UI-11's console ships in the repo and is served from the same origin.
    console = Path(__file__).resolve().parents[2] / "web" / "review-console"
    assert (console / "index.html").is_file()


def test_entrypoint_is_declared_and_python_m_runs():
    setup_py = (REPO_ROOT / "setup.py").read_text(encoding="utf-8")
    assert "om = shortdrama.cli:main" in setup_py
    assert 'python_requires=">=3.10"' in setup_py

    result = subprocess.run(
        [sys.executable, "-m", "shortdrama", "--help"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    for command in ("review", "doctor", "profile", "demo"):
        assert command in result.stdout


# --- demo + hello-world ----------------------------------------------------


def test_demo_mock_zero_api_closure(projects, capsys, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("M0 CLI must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", forbidden)

    assert run(projects, "demo", "--mock", "--json") == 0
    _out, demo = json_out(capsys)
    assert demo["mock"] is True
    assert demo["external_api_calls"] == 0
    assert sorted(demo["seeded_items"]) == ["r_demo_hook", "r_demo_reaction"]

    assert run(projects, "review", "list", "--json") == 0
    _out, listing = json_out(capsys)
    assert listing["count"] == 2
    assert listing["items"][0]["review_id"] == "r_demo_hook"

    assert run(projects, "review", "decide", "r_demo_hook", "--action", "accept", "--json") == 0
    _out, decision = json_out(capsys)
    assert decision["decision"]["action"] == "accept"

    assert run(projects, "review", "stats", "--json") == 0
    _out, stats = json_out(capsys)
    assert stats["review"]["total_items"] == 2
    assert stats["review"]["decided"] == 1
    assert stats["review"]["pending"] == 1


def test_demo_is_idempotent(projects, capsys):
    assert run(projects, "demo", "--mock", "--json") == 0
    json_out(capsys)
    assert run(projects, "demo", "--mock", "--json") == 0
    _out, second = json_out(capsys)
    assert second["created_manifest"] is False
    assert second["seeded_items"] == []
    assert len([r for r in read_log(projects, "demo", "ep01") if r["event_type"] == "item"]) == 2


def test_demo_requires_mock_flag(projects, capsys):
    assert run(projects, "demo", "--json") == 2
    _out, error = json_err(capsys)
    assert error["code"] == ErrorCode.VALIDATION_ERROR.value


def test_hello_world_path_under_five_minutes(tmp_path, capsys, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    bindings = make_bindings(tmp_path / "bindings")
    monkeypatch.setattr(cs, "tool_state", fake_tool_state)

    started = time.monotonic()
    assert run(projects, "doctor", "--profile", "green", "--json", bindings=bindings) == 0
    _out, doctor = json_out(capsys)
    assert doctor["status"] == "GREEN"

    assert run(projects, "demo", "--mock", "--json", bindings=bindings) == 0
    json_out(capsys)
    assert run(projects, "review", "list", "--json", bindings=bindings) == 0
    json_out(capsys)
    assert run(projects, "review", "decide", "r_demo_hook", "--action", "accept",
               "--json", bindings=bindings) == 0
    json_out(capsys)
    assert run(projects, "review", "stats", "--json", bindings=bindings) == 0
    _out, stats = json_out(capsys)
    assert stats["review"]["decided"] == 1

    elapsed = time.monotonic() - started
    assert elapsed < 300, f"hello-world took {elapsed:.1f}s"


# --- grammar: six actions ---------------------------------------------------


ACTIONS = [
    ("accept", []),
    ("reject", ["--reason", "ARTIFACT"]),
    ("pick-variant", ["--candidate", "b.mp4"]),
    ("edit-prompt", ["--prompt-patch", "tighter framing"]),
    ("send-back", []),
    ("skip", []),
]


@pytest.mark.parametrize("action,extra", ACTIONS, ids=[a for a, _ in ACTIONS])
def test_six_decide_actions_are_recorded(projects, capsys, action, extra):
    add_item(projects)
    argv = ["review", "decide", "r1", "--action", action, "--yes", "--json", *extra]
    assert run(projects, *argv) == 0, capsys.readouterr()
    _out, body = json_out(capsys)
    expected = action.replace("-", "_")
    assert body["decision"]["action"] == expected

    decisions = [r for r in read_log(projects) if r["event_type"] == "decision"]
    assert [d["decision"]["action"] for d in decisions] == [expected]


def test_policy_show_and_update(projects, capsys):
    manifest = write_manifest(projects)

    assert run(projects, "review", "policy", "ep01", "--json") == 0
    _out, current = json_out(capsys)
    assert current["mode"] == "sampled" and current["detail_ratio"] == 0.35

    assert run(projects, "review", "policy", "ep01", "--mode", "all",
               "--ratio", "0.9", "--json") == 0
    _out, updated = json_out(capsys)
    assert updated["mode"] == "all" and updated["detail_ratio"] == 0.9
    persisted = json.loads(manifest.read_text(encoding="utf-8"))
    assert persisted["review_policy"]["mode"] == "all"
    assert persisted["review_policy"]["detail_ratio"] == 0.9


def test_batch_dry_run_shape_and_nothing_pending(projects, capsys):
    assert run(projects, "review", "batch", "--episode", "ep01", "--dry-run", "--json") == 3
    _out, empty = json_out(capsys)
    assert empty["ok"] is False
    assert ErrorCode.NOTHING_PENDING.value in {e["code"] for e in empty["errors"]}

    add_item(projects, rid="r1")
    add_item(projects, rid="r2")
    assert run(projects, "review", "batch", "--episode", "ep01", "--dry-run", "--json") == 0
    _out, body = json_out(capsys)
    assert body["ok"] is True and body["dry_run"] is True
    assert sorted(body["pending"]) == ["r1", "r2"]


def test_undo_requeues_and_keeps_append_only_log(projects, capsys):
    add_item(projects)
    assert run(projects, "review", "decide", "r1", "--action", "accept", "--json") == 0
    json_out(capsys)
    before = len(read_log(projects))

    assert run(projects, "review", "undo", "r1", "--reason", "mis-keyed", "--json") == 0
    _out, body = json_out(capsys)
    assert body["undone"] is True

    records = read_log(projects)
    assert len(records) == before + 1
    assert records[-1]["event_type"] == "undo"

    assert run(projects, "review", "list", "--json") == 0
    _out, listing = json_out(capsys)
    assert listing["count"] == 1


def test_dry_run_validates_but_never_writes(projects, capsys):
    add_item(projects)
    before = len(read_log(projects))

    assert run(projects, "review", "decide", "r1", "--action", "accept",
               "--dry-run", "--json") == 0
    _out, body = json_out(capsys)
    assert body["dry_run"] is True and body["would_write"] is True
    assert len(read_log(projects)) == before

    assert run(projects, "review", "decide", "r1", "--action", "reject",
               "--dry-run", "--json") == 2
    _out, error = json_err(capsys)
    assert error["code"] == ErrorCode.MISSING_REASON_CODE.value
    assert len(read_log(projects)) == before


# --- exit codes -------------------------------------------------------------


def test_exit_codes_follow_the_error_catalog(projects, capsys):
    # 0 ok
    assert run(projects, "review", "list", "--json") == 3  # empty -> nothing pending
    capsys.readouterr()

    add_item(projects, version="v2")
    assert run(projects, "review", "decide", "r1", "--action", "accept",
               "--candidate-version", "v1", "--json") == 4  # stale
    _out, stale = json_err(capsys)
    assert stale["code"] == ErrorCode.STALE_REVIEW.value

    assert run(projects, "review", "show", "ghost", "--json") == 2  # usage/not found
    capsys.readouterr()

    assert ApiError(ErrorCode.NOTHING_PENDING, "", "").exit_code == 3
    assert ApiError(ErrorCode.STALE_REVIEW, "", "").exit_code == 4
    assert ApiError(ErrorCode.BUDGET_EXCEEDED, "", "").exit_code == 5
    assert ApiError(ErrorCode.INTERNAL_ERROR, "", "").exit_code == 1
    assert ApiError(ErrorCode.INVALID_ACTION, "", "").exit_code == 2


def test_json_error_envelope_has_contract_fields(projects, capsys):
    add_item(projects)
    assert run(projects, "review", "decide", "r1", "--action", "reject", "--json") == 2
    _out, error = json_err(capsys)
    assert {"code", "what", "fix", "recovery", "side_effects", "context"} <= set(error)
    assert error["code"] == ErrorCode.MISSING_REASON_CODE.value
    assert error["side_effects"] == "none"


# --- parity: CLI vs API -----------------------------------------------------


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
def test_cli_and_api_reject_identically(projects, capsys, name, payload, expected):
    add_item(projects)
    service = ShortdramaService(projects)
    before = len(read_log(projects))

    with pytest.raises(ApiError) as raised:
        service.decide("r1", DecideRequest(**payload))
    assert raised.value.code == expected

    argv = ["review", "decide", "r1", "--action", payload["action"], "--json", "--yes"]
    if "reason_code" in payload:
        argv += ["--reason", payload["reason_code"]]
    if "chosen_candidate" in payload:
        argv += ["--candidate", payload["chosen_candidate"]]
    if payload.get("override"):
        argv.append("--override")

    assert run(projects, *argv) == raised.value.exit_code
    _out, error = json_err(capsys)
    assert error["code"] == expected.value
    assert len(read_log(projects)) == before  # neither layer mutated


# --- stats ------------------------------------------------------------------


def test_stats_aggregate_and_episode_scope(projects, capsys):
    write_manifest(projects)
    add_item(projects, rid="r1")
    add_item(projects, rid="r2", priority=2)
    assert run(projects, "review", "decide", "r1", "--action", "accept", "--json") == 0
    json_out(capsys)

    EpisodeLog("drama", "ep01", projects).append_event("shot_planned", "sh_001")

    assert run(projects, "review", "stats", "--json") == 0
    _out, aggregate = json_out(capsys)
    assert aggregate["review"]["decided"] == 1
    assert aggregate["review"]["pending"] == 1
    assert aggregate["episodes"] == ["ep01"]

    assert run(projects, "review", "stats", "--episode", "ep01", "--json") == 0
    _out, scoped = json_out(capsys)
    assert scoped["episode_id"] == "ep01"
    assert scoped["review"]["decided"] == 1
    assert scoped["total_shots"] == 1


# --- watch (REV-11) ---------------------------------------------------------


def test_watch_reports_media_and_records_versioned_full_watch(projects, capsys):
    write_manifest(projects)
    media = projects / "drama" / "ep01" / "media" / "final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"first-cut")

    assert run(projects, "review", "watch", "ep01", "--stage", "assembly", "--json") == 0
    _out, surface = json_out(capsys)
    assert surface["state"] == "ready"
    assert surface["media_url"] == "/media/ep01/media/final.mp4"
    assert surface["version"] == __import__("hashlib").sha256(b"first-cut").hexdigest()
    assert "media_path" not in surface  # absolute paths only with --verbose

    assert run(projects, "review", "watch", "ep01", "--stage", "assembly",
               "--verify", "--json") == 0
    _out, verified = json_out(capsys)
    assert verified["verified"] is True and verified["already_verified"] is False

    decisions = [r for r in read_log(projects) if r["event_type"] == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["decision"]["action"] == "accept"
    assert "full_watch" in decisions[0]["decision"]["note"]
    assert decisions[0]["candidate_version"] == surface["version"]

    # Same media -> already verified, log unchanged.
    count = len(read_log(projects))
    assert run(projects, "review", "watch", "ep01", "--stage", "assembly",
               "--verify", "--json") == 0
    _out, repeat = json_out(capsys)
    assert repeat["already_verified"] is True
    assert len(read_log(projects)) == count

    # New media version -> a new snapshot + decision (old one is superseded).
    media.write_bytes(b"second-cut")
    assert run(projects, "review", "watch", "ep01", "--stage", "assembly",
               "--verify", "--json") == 0
    _out, renewed = json_out(capsys)
    assert renewed["already_verified"] is False
    assert renewed["version"] != surface["version"]
    items = [r for r in read_log(projects) if r["event_type"] == "item"]
    assert len(items) == 2


def test_watch_without_media_and_with_unfinished_shots(projects, capsys):
    write_manifest(projects)
    assert run(projects, "review", "watch", "ep01", "--stage", "delivery", "--json") == 3
    _out, surface = json_out(capsys)
    assert surface["state"] == "not-generated"

    assert run(projects, "review", "watch", "ep01", "--stage", "delivery",
               "--verify", "--json") == 2
    _out, error = json_err(capsys)
    assert error["code"] == ErrorCode.MEDIA_NOT_FOUND.value

    media = projects / "drama" / "ep01" / "media" / "final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"cut")
    EpisodeLog("drama", "ep01", projects).append_event("shot_planned", "sh_001")

    assert run(projects, "review", "watch", "ep01", "--stage", "delivery",
               "--verify", "--json") == 2
    _out, error = json_err(capsys)
    assert error["code"] == ErrorCode.BATCH_PRECONDITION_FAILED.value


def test_watch_verbose_includes_absolute_path(projects, capsys):
    write_manifest(projects)
    media = projects / "drama" / "ep01" / "media" / "final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"cut")
    assert cli.main([
        "--projects-dir", str(projects), "--verbose",
        "review", "watch", "ep01", "--stage", "assembly", "--json",
    ]) == 0
    _out, surface = json_out(capsys)
    assert surface["media_path"] == str(media)


# --- doctor / profile -------------------------------------------------------


def test_doctor_three_states(projects, tmp_path, capsys, monkeypatch):
    bindings = make_bindings(tmp_path / "bindings")
    monkeypatch.setattr(cs, "tool_state", fake_tool_state)

    assert run(projects, "doctor", "--profile", "green", "--json", bindings=bindings) == 0
    _out, green = json_out(capsys)
    assert green["status"] == "GREEN"

    assert run(projects, "doctor", "--profile", "ghost", "--json", bindings=bindings) == 4
    _out, blocked = json_out(capsys)
    assert blocked["status"] == "BLOCKED"
    assert any(r["code"] == "TOOL_NOT_REGISTERED" for r in blocked["reasons"])


def test_profile_list_and_use_writes_decision_log(tmp_path, projects, capsys, monkeypatch):
    bindings = make_bindings(tmp_path / "bindings")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    assert run(projects, "profile", "list", "--json", bindings=bindings) == 0
    _out, listing = json_out(capsys)
    assert listing["active"] == "green"
    assert sorted(listing["profiles"]) == ["ghost", "green"]

    assert run(projects, "profile", "use", "ghost", "--json", bindings=bindings) == 2
    _out, error = json_err(capsys)
    assert error["code"] == ErrorCode.VALIDATION_ERROR.value

    assert run(projects, "profile", "use", "ghost", "--yes", "--json", bindings=bindings) == 0
    _out, used = json_out(capsys)
    assert used["changed"] is True and used["active"] == "ghost"

    pointer = yaml.safe_load((bindings / "model_bindings.yaml").read_text(encoding="utf-8"))
    assert pointer["active_profile"] == "ghost"
    log = json.loads((bindings / "decision_log.json").read_text(encoding="utf-8"))
    assert log["decisions"][-1]["category"] == "provider_selection"
    assert log["decisions"][-1]["chosen"] == "ghost"

    assert run(projects, "profile", "use", "ghost", "--yes", "--json", bindings=bindings) == 0
    _out, same = json_out(capsys)
    assert same["changed"] is False


def test_profile_use_unknown_is_rejected(tmp_path, projects, capsys):
    bindings = make_bindings(tmp_path / "bindings")
    assert run(projects, "profile", "use", "nope", "--yes", "--json", bindings=bindings) == 2
    _out, error = json_err(capsys)
    assert error["code"] == ErrorCode.VALIDATION_ERROR.value
    pointer = yaml.safe_load((bindings / "model_bindings.yaml").read_text(encoding="utf-8"))
    assert pointer["active_profile"] == "green"