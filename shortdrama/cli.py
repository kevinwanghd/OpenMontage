"""``om`` command line — the short-drama M0 entry point (T8, §10.2 / NFR-12).

The CLI is a thin, non-interactive presentation layer over
:class:`shortdrama.api.ShortdramaService`, the same application layer the HTTP
handlers use. Review mutations therefore run through the real
``lib.review.ReviewStore.decide`` authority and the CLI rejects the same
illegal input with the same machine code as the API (X-11 parity is
structural, not re-implemented).

Contract highlights:

- grammar: ``om review list|show|decide|policy|batch|undo|watch|stats``,
  ``om doctor``, ``om profile list|use``, ``om demo --mock``;
- ``--json`` writes a stable machine-readable document to stdout;
- failures write the shared §10.3 error envelope to **stderr** and exit with
  the code mapped by :class:`shortdrama.errors.ApiError`
  (0 ok / 2 usage / 3 nothing pending / 4 stale-blocked / 5 budget / 1 internal);
- M0 makes zero external API calls: ``demo --mock`` seeds a local episode and
  the whole hello-world path is local files only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import uuid
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from pydantic import ValidationError as PydanticValidationError

from lib import capability_slots as slots
from lib.episode_log import EpisodeLog
from lib.review import (
    ReviewDecision,
    ReviewItem,
    ReviewStore,
    ReviewValidationError,
    StaleReviewError,
    UnknownReviewError,
)
from shortdrama.api import ShortdramaService
from shortdrama.errors import ApiError, ErrorCode, classify_review_rejection
from shortdrama.models import (
    BatchRequest,
    DecideRequest,
    ReviewPolicyModel,
    canonical_action,
)

#: Actions that trigger regeneration/cost and therefore need explicit consent.
CONSEQUENTIAL_ACTIONS = {"reject", "send_back", "edit_prompt"}

#: Review stages that carry a whole-film watch pass (REV-11).
WATCH_STAGES = ("assembly", "delivery")

_ITEM_FIELDS = {field.name for field in fields(ReviewItem)}

EPISODE_MANIFEST_TEMPLATE = {
    "version": "1.0",
    "series_id": "demo-series",
    "episode_id": "ep01",
    "project_id": "demo",
    "target_platform": "tiktok",
    "duration_s": 30,
    "review_policy": {
        "mode": "sampled",
        "detail_ratio": 0.35,
        "face_sim_threshold": 0.75,
        "sampling_salt": "ep01",
    },
    "language": "en",
}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(data: Any, as_json: bool, *, stream: Optional[Any] = None) -> None:
    stream = stream or sys.stdout
    if as_json:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        text = _human(data)
    stream.write(text + "\n")


def _human(data: Any) -> str:
    """Compact human rendering; JSON is the contract, this is for eyeballs."""
    if isinstance(data, dict):
        lines = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False)
            else:
                rendered = str(value)
            lines.append(f"{key}: {rendered}")
        return "\n".join(lines)
    if isinstance(data, list):
        return "\n".join(_human(item) for item in data) if data else "(empty)"
    return str(data)


def _emit_error(exc: ApiError, as_json: bool) -> None:
    envelope = exc.to_envelope(include_trace=True)
    if as_json:
        sys.stderr.write(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
        return
    lines = [f"{exc.code.value}: {exc.what}"]
    if exc.fix:
        lines.append(f"  fix: {exc.fix}")
    lines.append(f"  recovery: {exc.recovery} | side_effects: {exc.side_effects}")
    if exc.context:
        lines.append(f"  context: {json.dumps(exc.context, ensure_ascii=False)}")
    if exc.trace:
        lines.append(f"  trace: {exc.trace}")
    sys.stderr.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


def _service(args: argparse.Namespace) -> ShortdramaService:
    projects = getattr(args, "projects_dir", None)
    return ShortdramaService(Path(projects) if projects else None)


def _require_yes(args: argparse.Namespace, action: str, what: str) -> None:
    """Confirmation gate for consequential actions (non-interactive safe)."""
    if action not in CONSEQUENTIAL_ACTIONS or getattr(args, "yes", False):
        return
    if sys.stdin.isatty():
        answer = input(f"{what} [{action}] — confirm? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            return
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"{what} aborted by the operator",
            "re-run and confirm, or pass --yes",
            recovery="retry",
            exit_code=2,
        )
    raise ApiError(
        ErrorCode.VALIDATION_ERROR,
        f"{what} needs explicit confirmation for action '{action}'",
        "re-run with --yes (non-interactive mode never assumes consent)",
        recovery="fix-then-retry",
        context={"action": action},
        exit_code=2,
    )


def _validate_decision(
    service: ShortdramaService,
    review_id: str,
    decision: ReviewDecision,
    candidate_version: Optional[str],
) -> None:
    """Run the real authority against a throwaway store (``--dry-run``).

    Seeds a temporary ``ReviewStore`` with the effective item and calls the
    unmodified ``ReviewStore.decide``. Nothing in the real episode log is
    touched, and a dry run rejects exactly what a real decide rejects.
    """
    item = service.get_review(review_id)["item"]
    with tempfile.TemporaryDirectory(prefix="om-dry-run-") as tmp:
        store = ReviewStore("dry-run", "dry-run", Path(tmp))
        store.add_item(ReviewItem(**{k: item[k] for k in _ITEM_FIELDS if k in item}))
        try:
            store.decide(review_id, decision, candidate_version=candidate_version)
        except StaleReviewError:
            raise ApiError(
                ErrorCode.STALE_REVIEW,
                f"decision targets candidate version '{candidate_version}', "
                f"current is '{item.get('candidate_version')}'",
                "reload the review item and decide on the current candidate",
                recovery="retry",
                http_status=409,
                context={"review_id": review_id},
            )
        except ReviewValidationError:
            code, what, fix = classify_review_rejection(decision, item)
            raise ApiError(
                code,
                what,
                fix,
                http_status=422,
                context={"review_id": review_id, "stage": item.get("stage")},
            )


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def _cmd_review_list(args: argparse.Namespace) -> int:
    service = _service(args)
    result = service.list_pending(
        episode=args.episode,
        stage=args.stage,
        priority=args.priority,
        stale=args.stale,
    )
    policy = result["policy"]
    policy_dict = policy.model_dump() if hasattr(policy, "model_dump") else policy
    result = {**result, "policy": policy_dict}
    if args.json:
        _write(result, True)
    else:
        rows = [
            {
                "review_id": item["review_id"],
                "stage": item.get("stage"),
                "slot": item.get("slot"),
                "priority": item.get("priority", 0),
                "candidates": len(item.get("candidates") or []),
            }
            for item in result["items"]
        ]
        _write(
            {
                "pending": result["count"],
                "policy": result["policy"].model_dump(),
                "items": rows,
            },
            False,
        )
        for row in rows:
            sys.stdout.write(
                f"  {row['review_id']}\t{row['stage']}\t{row['slot']}\t"
                f"priority={row['priority']}\tcandidates={row['candidates']}\n"
            )
    return 0 if result["count"] else 3


def _cmd_review_show(args: argparse.Namespace) -> int:
    service = _service(args)
    result = service.get_review(args.review_id)
    _write(result, args.json)
    return 0


def _cmd_review_decide(args: argparse.Namespace) -> int:
    service = _service(args)
    action = canonical_action(args.action)
    decision = ReviewDecision(
        action=action,
        chosen_candidate=args.candidate,
        reason_code=args.reason,
        note=args.note or "",
        prompt_patch=args.prompt_patch,
        override=args.override,
        evidence=args.evidence or "",
    )

    # Validate through the real authority even on a dry run, so the CLI rejects
    # the same illegal input as the API (X-11) and a dry run never mutates.
    _validate_decision(service, args.review_id, decision, args.candidate_version)
    if args.dry_run:
        _write(
            {
                "dry_run": True,
                "review_id": args.review_id,
                "decision": asdict(decision),
                "would_write": True,
            },
            args.json,
        )
        return 0

    _require_yes(args, action, f"decide {args.review_id}")
    result = service.decide(
        args.review_id,
        DecideRequest(
            action=action,
            chosen_candidate=args.candidate,
            reason_code=args.reason,
            note=args.note or "",
            prompt_patch=args.prompt_patch,
            override=args.override,
            evidence=args.evidence or "",
            candidate_version=args.candidate_version,
        ),
    )
    if args.json:
        _write(result, True)
    else:
        _write(
            {
                "review_id": result["review_id"],
                "action": result["decision"]["action"],
                "candidate_version": result.get("candidate_version"),
                "review_version": result.get("review_version"),
            },
            False,
        )
    return 0


def _cmd_review_policy(args: argparse.Namespace) -> int:
    service = _service(args)
    current = service.get_policy(args.episode)
    if args.mode is None and args.ratio is None:
        _write(current.model_dump(), args.json)
        return 0

    updated = ReviewPolicyModel(**current.model_dump())
    if args.mode is not None:
        updated.mode = args.mode
    if args.ratio is not None:
        updated.detail_ratio = args.ratio
    result = service.put_policy(args.episode, updated)
    _write(result.model_dump(), args.json)
    return 0


def _cmd_review_batch(args: argparse.Namespace) -> int:
    service = _service(args)
    request = BatchRequest(episode=args.episode, dry_run=args.dry_run)
    try:
        result = service.batch(request)
    except PydanticValidationError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"batch request failed validation: {exc.errors()[0]['msg']}",
            "check --episode and retry",
            exit_code=2,
        )
    _write(result, args.json)
    if result["ok"]:
        return 0
    first = result["errors"][0]
    return ApiError(ErrorCode(first["code"]), first["what"], first["fix"]).exit_code


def _cmd_review_undo(args: argparse.Namespace) -> int:
    service = _service(args)
    result = service.undo(args.review_id, reason=args.reason)
    _write(result, args.json)
    return 0


def _time_ms() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_watch_media(episode_dir: Path) -> Optional[Path]:
    """Return the newest whole-film media file under the episode, if any."""
    explicit = [
        episode_dir / "media" / "final.mp4",
        episode_dir / "renders" / "final.mp4",
        episode_dir / "media" / "assembly.mp4",
        episode_dir / "media" / "delivery.mp4",
    ]
    for path in explicit:
        if path.is_file():
            return path
    found: list[Path] = []
    for pattern in ("media/*.mp4", "renders/*.mp4"):
        found.extend(p for p in episode_dir.glob(pattern) if p.is_file())
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cmd_review_watch(args: argparse.Namespace) -> int:
    service = _service(args)
    manifest = service.get_episode(args.episode)
    project, episode = manifest["project_id"], manifest["episode_id"]
    episode_dir = service.projects_dir / project / episode
    media = _find_watch_media(episode_dir)
    duration = manifest.get("duration_s")

    shot_states = EpisodeLog(project, episode, service.projects_dir).shot_states()
    payload: dict[str, Any] = {
        "episode_id": episode,
        "stage": args.stage,
        "state": "ready" if media else "not-generated",
        "duration_s": duration,
        "shots": {sid: s["state"] for sid, s in sorted(shot_states.items())},
    }
    if media is not None:
        relative = media.relative_to(episode_dir).as_posix()
        payload["media_url"] = f"/media/{episode}/{relative}"
        payload["version"] = _sha256(media)
        if args.verbose:
            payload["media_path"] = str(media)

    if not args.verify:
        _write(payload, args.json)
        return 0 if media else 3

    if media is None:
        raise ApiError(
            ErrorCode.MEDIA_NOT_FOUND,
            f"episode '{episode}' has no assembled media under {episode_dir}",
            "assemble the episode first, then re-run `om review watch --verify`",
            recovery="do-X-first",
            context={"episode_id": episode, "stage": args.stage},
        )

    unfinished = {
        sid: state for sid, state in payload["shots"].items()
        if state not in ("accepted", "assembled", "skipped")
    }
    if unfinished:
        raise ApiError(
            ErrorCode.BATCH_PRECONDITION_FAILED,
            f"cannot full-watch stage '{args.stage}': shots are not assembled",
            "finish or explicitly skip the remaining shots, then re-run",
            recovery="do-X-first",
            context={"episode_id": episode, "unfinished": unfinished},
        )

    if args.watched_seconds is not None and duration and args.watched_seconds < duration:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"watched {args.watched_seconds}s of {duration}s; full watch required (REV-11)",
            "watch the whole film before recording a full_watch decision",
            recovery="fix-then-retry",
            context={"watched_seconds": args.watched_seconds, "duration_s": duration},
        )

    version = payload["version"]
    review_id = f"fw:{episode}:{args.stage}"
    store = ReviewStore(project, episode, service.projects_dir)
    try:
        existing = store.get(review_id)
    except UnknownReviewError:
        existing = None

    if existing and existing["item"].get("candidate_version") == version:
        if existing["decision"] is not None:
            payload.update({"verified": True, "already_verified": True, "review_id": review_id})
            _write(payload, args.json)
            return 0
        review_version = existing["item"].get("review_version", "1")
    else:
        previous = 1
        if existing:
            previous = int(existing["item"].get("review_version") or 1) + 1
        store.add_item(ReviewItem(
            review_id=review_id,
            project=project,
            episode=episode,
            stage=args.stage,
            slot="full_watch",
            candidates=[version],
            criteria=["full_watch"],
            priority=0,
            candidate_version=version,
            review_version=str(previous),
        ))
        review_version = str(previous)

    watched = args.watched_seconds if args.watched_seconds is not None else duration
    note = (
        f"full_watch stage={args.stage} duration={duration}s "
        f"watched={watched}s version={version[:12]}"
    )
    store.decide(
        review_id,
        ReviewDecision(
            action="accept",
            note=note,
            evidence=f"full_watch sha256:{version} stage:{args.stage} at:{_now()}",
            decided_by="owner",
        ),
        candidate_version=version,
    )
    payload.update(
        {
            "verified": True,
            "already_verified": False,
            "review_id": review_id,
            "review_version": review_version,
            "note": note,
        }
    )
    _write(payload, args.json)
    return 0


def _cmd_review_stats(args: argparse.Namespace) -> int:
    service = _service(args)
    if args.episode:
        result = service.stats(args.episode)
        _write(result, args.json)
        return 0

    review = {
        "total_items": 0,
        "decided": 0,
        "pending": 0,
        "action_distribution": {},
        "reason_code_distribution": {},
        "first_pass_accept_rate": None,
    }
    accepted = 0
    shot_states: dict[str, int] = {}
    episodes: set[str] = set()
    projects_dir = service.projects_dir
    if projects_dir.is_dir():
        for path in sorted(projects_dir.glob("*/*/reviews.jsonl")):
            project, episode = path.parent.parent.name, path.parent.name
            episodes.add(episode)
            stats = ReviewStore(project, episode, projects_dir).stats()
            for key in ("total_items", "decided", "pending"):
                review[key] += stats[key]
            for key in ("action_distribution", "reason_code_distribution"):
                for name, count in stats[key].items():
                    review[key][name] = review[key].get(name, 0) + count
            accepted += stats["action_distribution"].get("accept", 0)
            for shot in EpisodeLog(project, episode, projects_dir).shot_states().values():
                shot_states[shot["state"]] = shot_states.get(shot["state"], 0) + 1
    if review["decided"]:
        review["first_pass_accept_rate"] = accepted / review["decided"]
    _write(
        {
            "scope": "all",
            "episodes": sorted(episodes),
            "review": review,
            "total_shots": sum(shot_states.values()),
            "shot_states": shot_states,
        },
        args.json,
    )
    return 0


# ---------------------------------------------------------------------------
# doctor / profile / demo
# ---------------------------------------------------------------------------


def _cmd_doctor(args: argparse.Namespace) -> int:
    profile = args.profile or slots.active_profile()
    report = slots.doctor(profile)
    _write(report, args.json)
    return 4 if report.get("status") == "BLOCKED" else 0


def _profiles_dir() -> Path:
    return slots.bindings_dir() / "profiles"


def _cmd_profile_list(args: argparse.Namespace) -> int:
    directory = _profiles_dir()
    names = sorted(p.stem for p in directory.glob("*.yaml")) if directory.is_dir() else []
    _write({"active": slots.active_profile(), "profiles": names}, args.json)
    return 0


def _append_profile_decision(
    bindings_dir: Path,
    previous: str,
    chosen: str,
) -> str:
    """Append a provider_selection decision to the bindings audit log."""
    decision_id = f"dec_profile_{uuid.uuid4().hex[:12]}"
    path = bindings_dir / "decision_log.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {"version": "1.0", "decisions": []}
    data.setdefault("decisions", []).append(
        {
            "decision_id": decision_id,
            "category": "provider_selection",
            "subject": "active_profile",
            "options_considered": [previous, chosen],
            "chosen": chosen,
            "reason": "om profile use (operator-confirmed binding switch)",
            "decided_at": _now(),
            "decided_by": "owner",
        }
    )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return decision_id


def _cmd_profile_use(args: argparse.Namespace) -> int:
    bindings_dir = slots.bindings_dir()
    try:
        slots.load_profile(args.name)  # validates the profile before touching config
    except slots.UnknownProfileError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            str(exc),
            "run `om profile list` and choose a listed profile",
            context={"profile": args.name},
            exit_code=2,
        )
    previous = slots.active_profile()
    if previous == args.name:
        _write({"active": previous, "changed": False}, args.json)
        return 0

    if not args.yes:
        if sys.stdin.isatty():
            answer = input(f"switch active profile {previous!r} -> {args.name!r}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR,
                    "profile switch aborted by the operator",
                    "re-run and confirm, or pass --yes",
                    recovery="retry",
                    exit_code=2,
                )
        else:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"switching to profile '{args.name}' needs explicit confirmation",
                "re-run with --yes (non-interactive mode never assumes consent)",
                recovery="fix-then-retry",
                context={"profile": args.name},
                exit_code=2,
            )

    pointer = bindings_dir / "model_bindings.yaml"
    data = yaml.safe_load(pointer.read_text(encoding="utf-8")) if pointer.is_file() else {}
    data = data if isinstance(data, dict) else {}
    data["version"] = str(data.get("version") or "1.0")
    data["active_profile"] = args.name
    data.setdefault("profiles_dir", "profiles")
    pointer.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    decision_id = _append_profile_decision(bindings_dir, previous, args.name)
    _write(
        {
            "active": args.name,
            "previous": previous,
            "changed": True,
            "decision_id": decision_id,
        },
        args.json,
    )
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    if not args.mock:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "`om demo` requires --mock in M0 (real generation is M1)",
            "re-run: om demo --mock",
            recovery="fix-then-retry",
            exit_code=2,
        )
    service = _service(args)
    project, episode = "demo", "ep01"
    episode_dir = service.projects_dir / project / episode
    episode_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = episode_dir / "episode_manifest.json"
    created_manifest = False
    if not manifest_path.exists():
        manifest_path.write_text(
            json.dumps(EPISODE_MANIFEST_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        created_manifest = True

    store = ReviewStore(project, episode, service.projects_dir)
    seeded_items = []
    demo_items = [
        dict(
            review_id="r_demo_hook",
            stage="shot_gen",
            slot="sh_001",
            candidates=["sh_001_v1.mp4", "sh_001_v2.mp4"],
            criteria=["face_similarity", "motion"],
            priority=0,
            priority_reasons=["hook"],
        ),
        dict(
            review_id="r_demo_reaction",
            stage="shot_gen",
            slot="sh_002",
            candidates=["sh_002_v1.mp4"],
            criteria=["motion"],
            priority=0,
            priority_reasons=["character_consistency"],
        ),
    ]
    for spec in demo_items:
        try:
            store.get(spec["review_id"])
        except UnknownReviewError:
            store.add_item(ReviewItem(project=project, episode=episode, **spec))
            seeded_items.append(spec["review_id"])

    result = {
        "mock": True,
        "project_id": project,
        "episode_id": episode,
        "episode_dir": str(episode_dir),
        "created_manifest": created_manifest,
        "seeded_items": seeded_items,
        "external_api_calls": 0,
        "next": [
            f"om review list --episode {episode} --json",
            f"om review decide r_demo_hook --action accept --yes",
            "om review stats --json",
        ],
    }
    if args.json:
        _write(result, True)
    else:
        _write(
            {
                "mock": True,
                "episode": episode,
                "created_manifest": created_manifest,
                "seeded_items": seeded_items,
            },
            False,
        )
        sys.stdout.write("next:\n")
        for step in result["next"]:
            sys.stdout.write(f"  {step}\n")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="om",
        description="OpenMontage short-drama CLI (M0: local files only, zero external API calls).",
    )
    parser.add_argument("--projects-dir", type=Path, default=None,
                        help="projects root (default: OPENMONTAGE_PROJECTS_DIR or ./projects)")
    parser.add_argument("--bindings-dir", type=Path, default=None,
                        help="bindings root (default: ./bindings)")
    parser.add_argument("--verbose", action="store_true",
                        help="include absolute media paths (REV-09 / X-12)")
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review", help="review queue and decisions")
    review_sub = review.add_subparsers(dest="review_command", required=True)

    p = review_sub.add_parser("list", help="list pending review items")
    p.add_argument("--episode")
    p.add_argument("--stage")
    p.add_argument("--priority", type=int)
    p.add_argument("--stale", action="store_true", help="decisions made on superseded candidates")
    _add_json(p)
    p.set_defaults(func=_cmd_review_list)

    p = review_sub.add_parser("show", help="show one review item and its decision")
    p.add_argument("review_id")
    _add_json(p)
    p.set_defaults(func=_cmd_review_show)

    p = review_sub.add_parser("decide", help="record a decision (six canonical actions)")
    p.add_argument("review_id")
    p.add_argument("--action", required=True,
                   help="accept|reject|pick-variant|edit-prompt|send-back|skip (hyphen or underscore)")
    p.add_argument("--candidate")
    p.add_argument("--reason", help="REASON_CODE (required for reject)")
    p.add_argument("--note", default="")
    p.add_argument("--prompt-patch")
    p.add_argument("--override", action="store_true", help="human resolves an UNKNOWN")
    p.add_argument("--evidence", default="", help="required with --override")
    p.add_argument("--candidate-version")
    p.add_argument("--yes", action="store_true", help="confirm consequential actions")
    p.add_argument("--dry-run", action="store_true", help="validate without writing")
    _add_json(p)
    p.set_defaults(func=_cmd_review_decide)

    p = review_sub.add_parser("policy", help="show or update an episode review policy")
    p.add_argument("episode")
    p.add_argument("--mode", choices=["all", "sampled"])
    p.add_argument("--ratio", type=float)
    _add_json(p)
    p.set_defaults(func=_cmd_review_policy)

    p = review_sub.add_parser("batch", help="REV-07 batch precondition check")
    p.add_argument("--episode", required=True)
    p.add_argument("--dry-run", action="store_true")
    _add_json(p)
    p.set_defaults(func=_cmd_review_batch)

    p = review_sub.add_parser("undo", help="compensate a decision (REV-13, append-only)")
    p.add_argument("review_id")
    p.add_argument("--reason", required=True)
    _add_json(p)
    p.set_defaults(func=_cmd_review_undo)

    p = review_sub.add_parser("watch", help="whole-film watch surface (REV-11)")
    p.add_argument("episode")
    p.add_argument("--stage", choices=list(WATCH_STAGES), required=True)
    p.add_argument("--verify", action="store_true",
                   help="record a full_watch decision for the current media version")
    p.add_argument("--watched-seconds", type=float, default=None,
                   help="playback progress asserted by the operator")
    _add_json(p)
    p.set_defaults(func=_cmd_review_watch)

    p = review_sub.add_parser("stats", help="review and shot statistics (REV-01)")
    p.add_argument("--episode")
    _add_json(p)
    p.set_defaults(func=_cmd_review_stats)

    p = commands.add_parser("doctor", help="profile health: GREEN / DEGRADED / BLOCKED")
    p.add_argument("--profile")
    _add_json(p)
    p.set_defaults(func=_cmd_doctor)

    profile = commands.add_parser("profile", help="inspect or switch binding profiles")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    p = profile_sub.add_parser("list", help="list registered profiles")
    _add_json(p)
    p.set_defaults(func=_cmd_profile_list)
    p = profile_sub.add_parser("use", help="switch the active profile (writes decision_log)")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    _add_json(p)
    p.set_defaults(func=_cmd_profile_use)

    p = commands.add_parser("demo", help="seed a mock episode (NFR-12 hello-world)")
    p.add_argument("--mock", action="store_true", help="required in M0: zero external API")
    _add_json(p)
    p.set_defaults(func=_cmd_demo)

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    as_json = bool(getattr(args, "json", False))
    previous_bindings = os.environ.get(slots.BINDINGS_DIR_ENV)
    if getattr(args, "bindings_dir", None):
        os.environ[slots.BINDINGS_DIR_ENV] = str(args.bindings_dir)
    try:
        return _dispatch(args, as_json)
    finally:
        # A CLI process exits anyway; in-process callers (tests, embedding)
        # must not inherit a bindings override.
        if previous_bindings is None:
            os.environ.pop(slots.BINDINGS_DIR_ENV, None)
        else:
            os.environ[slots.BINDINGS_DIR_ENV] = previous_bindings


def _dispatch(args: argparse.Namespace, as_json: bool) -> int:
    try:
        return int(args.func(args))
    except ApiError as exc:
        _emit_error(exc, as_json)
        return exc.exit_code
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"msg": "invalid value"}
        error = ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"invalid value: {first.get('msg', '')}",
            "check the command arguments and retry",
            exit_code=2,
            context={"errors": [
                {"loc": list(err.get("loc", [])), "msg": err.get("msg", "")}
                for err in exc.errors()
            ]},
        )
        _emit_error(error, as_json)
        return error.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # defensive: §10.3 trace is only for unexpected failures
        error = ApiError(
            ErrorCode.INTERNAL_ERROR,
            f"unexpected error: {type(exc).__name__}",
            "retry once; if it persists, ask a human to inspect the CLI log",
            recovery="ask-human",
            side_effects="unknown",
            trace=str(exc),
        )
        _emit_error(error, as_json)
        return error.exit_code


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())