---
pipeline: short-drama
stage: scene_plan
inputs:
  - script
  - character_design
outputs:
  - scene_plan
schemas:
  scene_plan: schemas/artifacts/scene_plan.schema.json
slots: []
gates:
  checkpoint_required: true
  human_approval_default: true
  review_point: 5
failure_modes:
  - FRAMING_BAD
  - PACING
  - NARRATIVE_WEAK
  - OTHER
handoff:
  next_stage: keyframe
  artifact: scene_plan
references:
  - pipeline_defs/short-drama.yaml
  - schemas/artifacts/scene_plan.schema.json
---

# Scene Plan Director - Short-Drama Pipeline

## When to Use

After approved casting. The scene plan turns the script into a shot list that
downstream stages can execute mechanically: each shot names its characters, its
single action, its framing, and whether it needs lipsync. A vague plan becomes
expensive keyframe and video attempts.

## Inputs

- `script` (required): lines, speakers, emotional direction.
- `character_design` (required): registered `character_id`s and wardrobes.
- `beat_map` (optional): hook candidates and pacing anchors.

## Outputs

A schema-valid `scene_plan` at `projects/<project-id>/artifacts/scene_plan.json`
with a `shots` list. Every shot declares:

- `shot_id`, `beat_id`, `duration_s`;
- `shot_language`: shot size, camera movement, lens, lighting, depth, color
  temperature;
- `single_action`: exactly one actable action;
- `dialogue` when spoken, with speaker and line duration;
- `visual_evidence`: the concrete detail the frame must show;
- `reference_character_ids` and `reference_scene_ids`;
- `needs_lipsync`, `hook_candidate`, and candidate count.

Every entry in `scenes[]` must also declare, for GATE-05:

- `generation_block_seconds`: what one model call renders, 8-30s and never
  above the bound model's maximum duration. Continuity inside one block is free
  because it is a single generation;
- `edit_beats[]`: how that block is cut in the edit, 2-8s each. Beat durations
  must sum to `generation_block_seconds` — the validator rejects a plan whose
  beats do not exactly consume their block.

Plans written before this rule (no block fields) still validate, but do not
copy that shape into a new plan.

Vertical rules: 9:16 frame, 1080x1920, compose inside the 900x1400 safe area,
leave headroom and subtitle room, and avoid putting a face under the platform
UI overlays.

## Slots

No capability slot is resolved here. Planning spends nothing. Do not pre-commit
a provider or model; the shot only declares intent, and bindings decide how it
is produced.

## Gates

Checkpoint required, `human_approval_default: true` (review point 5). Present
the shot list with thumbnails of framing intent and hook markings. Stop.

## Failure Modes

- `FRAMING_BAD`: a subject sits outside the safe area, the eyeline is wrong, or
  the shot size does not serve the action. Rewrite the shot language.
- `PACING`: consecutive shots share size and energy, or a cut exceeds three
  seconds without a visual change. Break the run with a different shot size.
- `NARRATIVE_WEAK`: a shot exists without changing information or emotion.
  Remove or merge it.
- `OTHER`: record the evidence and the fix.

## Process

1. Walk the script beat by beat and assign one shot per actable action.
2. Choose shot sizes so no two adjacent shots repeat framing.
3. Confirm every shot inside the safe area; mark the subtitle band.
4. Link every shot to registered character ids.
5. Mark hook candidates carried over from the beat map.
6. Self-evaluate against the schema, then checkpoint.

## Self-Evaluate

Could a keyframe artist build each shot from the card alone? Is every face
clearly readable at 9:16? Does the hook candidate appear in the first shot? If
not, revise before generating any image.