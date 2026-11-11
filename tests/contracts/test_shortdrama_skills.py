"""T9 contract tests for the short-drama manifest and its nine director skills.

Machine-checkable guarantees (ORC-05 / ORC-06 / MOD-01 / MOD-06):

- The manifest loads, declares the nine stages in production order, and is the
  single source of stage names (the skills must agree with it).
- Every stage references capability *slots*, never a provider tool name, and
  every referenced slot resolves through ``lib.capability_slots``.
- Every artifact a stage produces or consumes exists as a schema under
  ``schemas/artifacts/``.
- Every stage has a director skill with the required template sections
  (stage/inputs/outputs/slots/gates/failure_modes/handoff/references), a body
  over 500 characters, real slot names, and review reason codes that are
  applicable to that stage in ``lib.review.STAGE_REASON_CODES``.
- Degraded sub-stages are predeclared in the YAML: the separated TTS + lipsync
  + mux path on ``shot_gen`` and the draft-to-delivery upscale on ``delivery``.
  Nothing in a skill may name a sub-stage the manifest does not declare.

M0: local files only, zero external API calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lib.capability_slots import load_slots, slot_definition
from lib.pipeline_loader import load_pipeline, get_stage_human_approval_default, get_stage_order
from lib.review import STAGE_REASON_CODES

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_NAME = "short-drama"
MANIFEST_PATH = REPO_ROOT / "pipeline_defs" / f"{MANIFEST_NAME}.yaml"
SKILLS_DIR = REPO_ROOT / "skills" / "pipelines" / MANIFEST_NAME
ARTIFACT_SCHEMA_DIR = REPO_ROOT / "schemas" / "artifacts"

EXPECTED_STAGES = [
    # The planning stage uses the repo-wide `idea` name (canonical artifact:
    # `brief`); review point 1 records decisions under the review stage
    # `brief`. See the manifest comment and REVIEW_STAGE_ALIASES below.
    "idea",
    "beat_map",
    "script",
    "character_cast",
    "scene_plan",
    "keyframe",
    "shot_gen",
    "assembly",
    "delivery",
]

REQUIRED_SKILL_SECTIONS = [
    "stage",
    "inputs",
    "outputs",
    "slots",
    "gates",
    "failure_modes",
    "handoff",
    "references",
]

SLOT_PREFIX = "slot:"

#: Pipeline stage name -> review-stage vocabulary (lib/review.py). `idea` is the
#: repo-wide planning stage; its canonical artifact is `brief` and review point
#: 1 is recorded under the review stage `brief` (REV-03).
REVIEW_STAGE_ALIASES = {"idea": "brief"}


def _review_stage(stage_name: str) -> str:
    return REVIEW_STAGE_ALIASES.get(stage_name, stage_name)


def _manifest_raw() -> dict:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def _front_matter(path: Path) -> tuple[dict, str]:
    """Return (front_matter_dict, body) for a director skill."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name}: missing YAML front matter"
    _, raw_front_matter, body = text.split("---", 2)
    data = yaml.safe_load(raw_front_matter)
    assert isinstance(data, dict), f"{path.name}: front matter is not a mapping"
    return data, body


def _skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*-director.md"))


def _stage_skill_paths(manifest: dict) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for stage in manifest["stages"]:
        skill = str(stage["skill"])  # e.g. pipelines/short-drama/brief-director
        paths[stage["name"]] = REPO_ROOT / "skills" / f"{skill}.md"
    return paths


def _stage_slot_entries(stage: dict) -> list[tuple[str, str]]:
    """Return (where, raw_entry) for every tools_available entry in a stage."""
    entries = [("tools_available", str(tool)) for tool in (stage.get("tools_available") or [])]
    for sub_stage in stage.get("sub_stages") or []:
        entries.extend(
            (f"sub_stage:{sub_stage['name']}", str(tool))
            for tool in (sub_stage.get("tools_available") or [])
        )
    return entries


def _stage_slot_refs(stage: dict) -> list[tuple[str, str]]:
    """Return (where, slot_name) for every slot reference in a stage."""
    return [
        (where, entry[len(SLOT_PREFIX):])
        for where, entry in _stage_slot_entries(stage)
        if entry.startswith(SLOT_PREFIX)
    ]


def _stage_slot_names(stage: dict) -> set[str]:
    return {slot for _where, slot in _stage_slot_refs(stage)}


def _artifact_names(manifest: dict) -> set[str]:
    names: set[str] = set()
    for stage in manifest["stages"]:
        for key in ("required_artifacts_in", "optional_artifacts_in", "produces"):
            names.update(str(item) for item in (stage.get(key) or []))
    return names


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_loads_against_the_pipeline_schema() -> None:
    manifest = load_pipeline(MANIFEST_NAME)
    assert manifest["name"] == MANIFEST_NAME
    assert manifest["stages"]


def test_manifest_declares_the_nine_stages_in_production_order() -> None:
    manifest = load_pipeline(MANIFEST_NAME)
    assert get_stage_order(manifest) == EXPECTED_STAGES


def test_manifest_declares_one_named_skill_per_stage() -> None:
    manifest = _manifest_raw()
    declared = manifest["required_skills"]

    for stage in manifest["stages"]:
        assert stage["skill"] in declared, (
            f"{stage['name']} skill {stage['skill']!r} is not in required_skills"
        )
        path = REPO_ROOT / "skills" / f"{stage['skill']}.md"
        assert path.is_file(), f"missing director skill file: {path}"


def test_planning_stage_matches_the_canonical_checkpoint_mapping() -> None:
    """The repo maps stage `idea` to the canonical artifact `brief`."""
    from lib.checkpoint import CANONICAL_STAGE_ARTIFACTS

    manifest = _manifest_raw()
    idea = next(stage for stage in manifest["stages"] if stage["name"] == "idea")
    assert CANONICAL_STAGE_ARTIFACTS["idea"] in idea["produces"]


def test_every_manifest_stage_gates_on_human_approval() -> None:
    manifest = load_pipeline(MANIFEST_NAME)
    for stage in manifest["stages"]:
        assert get_stage_human_approval_default(manifest, stage["name"]) is True


def test_stage_tools_are_capability_slots_never_tool_names() -> None:
    """MOD-01: a manifest that names a provider tool cannot survive a profile swap."""
    manifest = _manifest_raw()
    declared_slots = set(load_slots())

    for stage in manifest["stages"]:
        for where, entry in _stage_slot_entries(stage):
            assert entry.startswith(SLOT_PREFIX), (
                f"{stage['name']}/{where}: {entry!r} is not a slot reference"
            )

    for stage in manifest["stages"]:
        for where, slot in _stage_slot_refs(stage):
            assert slot in declared_slots, (
                f"{stage['name']}/{where}: slot {slot!r} is not declared in bindings/slots.yaml"
            )


def test_every_referenced_slot_resolves_through_capability_slots() -> None:
    manifest = _manifest_raw()
    for stage in manifest["stages"]:
        for where, slot in _stage_slot_refs(stage):
            definition = slot_definition(slot)  # raises UnknownSlotError otherwise
            assert definition, f"{stage['name']}/{where}: empty slot definition for {slot}"


def test_every_referenced_artifact_has_a_schema() -> None:
    manifest = _manifest_raw()
    for artifact in _artifact_names(manifest):
        schema = ARTIFACT_SCHEMA_DIR / f"{artifact}.schema.json"
        assert schema.is_file(), f"artifact {artifact!r} has no schema at {schema}"


def test_every_stage_reads_artifacts_produced_upstream() -> None:
    manifest = _manifest_raw()
    produced: set[str] = set()
    for stage in manifest["stages"]:
        for artifact in stage.get("required_artifacts_in") or []:
            assert artifact in produced, (
                f"{stage['name']} requires {artifact!r} before any stage produces it"
            )
        produced.update(str(item) for item in (stage.get("produces") or []))


# ---------------------------------------------------------------------------
# Degraded sub-stages (MOD-06 / F14)
# ---------------------------------------------------------------------------


def _sub_stages(manifest: dict, stage_name: str) -> dict[str, dict]:
    for stage in manifest["stages"]:
        if stage["name"] == stage_name:
            return {sub["name"]: sub for sub in (stage.get("sub_stages") or [])}
    raise AssertionError(f"stage {stage_name} not found")


def test_all_sub_stages_are_fully_predeclared() -> None:
    manifest = _manifest_raw()
    for stage in manifest["stages"]:
        for sub_stage in stage.get("sub_stages") or []:
            assert sub_stage.get("condition"), (
                f"{stage['name']}/{sub_stage['name']}: degradation branch needs a condition"
            )
            assert sub_stage.get("description"), (
                f"{stage['name']}/{sub_stage['name']}: needs a human-readable description"
            )


def test_shot_gen_predeclares_native_and_separated_lipsync_paths() -> None:
    manifest = _manifest_raw()
    sub_stages = _sub_stages(manifest, "shot_gen")

    assert "native_dialogue_lipsync" in sub_stages
    assert "separate_lipsync_path" in sub_stages

    native = sub_stages["native_dialogue_lipsync"]
    separated = sub_stages["separate_lipsync_path"]
    assert native["condition"] != separated["condition"]

    separated_slots = {
        str(tool)[len(SLOT_PREFIX):]
        for tool in separated.get("tools_available") or []
        if str(tool).startswith(SLOT_PREFIX)
    }
    assert {"video_primary", "tts_voice", "lipsync"} <= separated_slots
    assert separated.get("human_approval_default") is True, (
        "a degraded, spend-incurring path must require explicit approval"
    )


def test_delivery_predeclares_native_and_upscale_paths() -> None:
    manifest = _manifest_raw()
    sub_stages = _sub_stages(manifest, "delivery")

    assert {"native_delivery", "delivery_upscale"} <= set(sub_stages)
    upscale = sub_stages["delivery_upscale"]
    upscale_slots = {
        str(tool)[len(SLOT_PREFIX):]
        for tool in upscale.get("tools_available") or []
        if str(tool).startswith(SLOT_PREFIX)
    }
    assert "upscale" in upscale_slots
    assert upscale["condition"] == "draft_resolution_below_target"


# ---------------------------------------------------------------------------
# Director skills
# ---------------------------------------------------------------------------


def test_exactly_nine_director_skills_exist() -> None:
    stages = set(EXPECTED_STAGES)
    expected = {f"{stage.replace('_', '-')}-director.md" for stage in stages}
    assert {path.name for path in _skill_files()} == expected


@pytest.mark.parametrize("stage_name", EXPECTED_STAGES)
def test_director_skill_has_the_required_template(stage_name: str) -> None:
    manifest = _manifest_raw()
    path = _stage_skill_paths(manifest)[stage_name]
    front_matter, body = _front_matter(path)

    for section in REQUIRED_SKILL_SECTIONS:
        assert section in front_matter, f"{path.name}: front matter missing {section!r}"
    assert front_matter["stage"] == stage_name
    assert front_matter["pipeline"] == MANIFEST_NAME

    # The body is instruction, not a stub: size gate first, then structure.
    assert len(body.strip()) > 500, f"{path.name}: body is too short to direct a stage"
    for heading in (
        "## When to Use",
        "## Inputs",
        "## Outputs",
        "## Slots",
        "## Gates",
        "## Failure Modes",
        "## Process",
        "## Self-Evaluate",
    ):
        assert heading in body, f"{path.name}: missing {heading!r}"


@pytest.mark.parametrize("stage_name", EXPECTED_STAGES)
def test_director_skill_matches_manifest_contract(stage_name: str) -> None:
    manifest = _manifest_raw()
    stage = next(item for item in manifest["stages"] if item["name"] == stage_name)
    path = _stage_skill_paths(manifest)[stage_name]
    front_matter, _body = _front_matter(path)

    assert set(front_matter["inputs"]) == set(stage.get("required_artifacts_in") or [])
    assert set(front_matter["outputs"]) == set(stage.get("produces") or [])

    skill_slots = set(front_matter["slots"])
    declared_slots = set(load_slots())
    assert skill_slots <= declared_slots, (
        f"{path.name}: undeclared slots {sorted(skill_slots - declared_slots)}"
    )
    assert _stage_slot_names(stage) <= skill_slots, (
        f"{path.name}: manifest stage uses slots the skill never documents: "
        f"{sorted(_stage_slot_names(stage) - skill_slots)}"
    )

    assert front_matter["handoff"]["artifact"] in set(front_matter["outputs"])


@pytest.mark.parametrize("stage_name", EXPECTED_STAGES)
def test_director_skill_failure_modes_are_review_codes_for_that_stage(stage_name: str) -> None:
    """Review codes are not free text (REV-03): an inapplicable code is rejected."""
    manifest = _manifest_raw()
    path = _stage_skill_paths(manifest)[stage_name]
    front_matter, _body = _front_matter(path)

    review_stage = _review_stage(stage_name)
    allowed = STAGE_REASON_CODES[review_stage]
    for code in front_matter["failure_modes"]:
        assert code in allowed, (
            f"{path.name}: failure mode {code!r} is not a review reason code "
            f"applicable to review stage {review_stage!r} (allowed: {sorted(allowed)})"
        )


@pytest.mark.parametrize("stage_name", EXPECTED_STAGES)
def test_director_skill_schemas_exist(stage_name: str) -> None:
    manifest = _manifest_raw()
    path = _stage_skill_paths(manifest)[stage_name]
    front_matter, _body = _front_matter(path)

    schemas = front_matter.get("schemas") or {}
    assert set(schemas) == set(front_matter["outputs"]), (
        f"{path.name}: every output needs a schema reference"
    )
    for artifact, schema_path in schemas.items():
        assert (REPO_ROOT / schema_path).is_file(), (
            f"{path.name}: schema for {artifact!r} points at a missing file {schema_path}"
        )


@pytest.mark.parametrize("stage_name", EXPECTED_STAGES)
def test_director_skill_references_exist(stage_name: str) -> None:
    manifest = _manifest_raw()
    path = _stage_skill_paths(manifest)[stage_name]
    front_matter, _body = _front_matter(path)

    for reference in front_matter["references"]:
        assert (REPO_ROOT / reference).is_file(), (
            f"{path.name}: reference {reference!r} does not exist"
        )


def test_handoff_chain_follows_the_declared_stage_order() -> None:
    manifest = _manifest_raw()
    paths = _stage_skill_paths(manifest)
    for index, stage_name in enumerate(EXPECTED_STAGES[:-1]):
        front_matter, _ = _front_matter(paths[stage_name])
        assert front_matter["handoff"]["next_stage"] == EXPECTED_STAGES[index + 1]

    last, _ = _front_matter(paths[EXPECTED_STAGES[-1]])
    assert last["handoff"]["next_stage"] is None