---
pipeline: short-drama
stage: shot_gen
inputs:
  - asset_manifest
  - scene_plan
outputs:
  - asset_manifest
schemas:
  asset_manifest: schemas/artifacts/asset_manifest.schema.json
slots:
  - video_primary
  - tts_voice
  - lipsync
gates:
  checkpoint_required: true
  human_approval_default: true
  review_point: 7
failure_modes:
  - FACE_MISMATCH
  - WARDROBE_DRIFT
  - OFF_POSE
  - LIP_SYNC_FAIL
  - MOTION_WRONG
  - MOTION_UNNATURAL
  - ARTIFACT
  - FRAMING_BAD
  - LIGHTING_WRONG
  - DIALOGUE_CUT
  - IDENTITY_UNKNOWN
  - OTHER
handoff:
  next_stage: assembly
  artifact: asset_manifest
references:
  - pipeline_defs/short-drama.yaml
  - bindings/slots.yaml
  - bindings/capability_catalog.yaml
---

# Shot Gen Director - Short-Drama Pipeline

## When to Use

After approved keyframes. This stage turns each approved keyframe into a moving
clip. It is the most expensive stage, so the director must settle the binding,
the degraded path, and the budget before the first paid call.

## Inputs

- `asset_manifest` (required): approved keyframe per shot, with provenance.
- `scene_plan` (required): duration, dialogue, `needs_lipsync`, single action.

## Outputs

The updated `asset_manifest` with one accepted video clip per shot, linked to
its keyframe, with provider, model, prompt, seed, duration, and cost recorded.
Never overwrite the keyframe entry; append the clip as a new asset with
lineage.

## Slots

- `video_primary` (required): keyframe-to-video generation.
- `tts_voice` (required only on the degraded path): dialogue synthesis.
- `lipsync` (optional, only on the degraded path): face lip sync.

The manifest predeclares two sub-stages:

1. `native_dialogue_lipsync` - the binding declares
   `lipsync_from_dialogue: true`; audio and mouth shapes come from the video
   model.
2. `separate_lipsync_path` - the binding does not. Run three declared
   sub-stages: TTS dialogue, lipsync pass, then mux the track onto the silent
   clip. This is DEGRADED with `lost=native_lipsync, added=3`. Log it in
   `decision_log` and get approval before spending.

Run `lib.capability_slots.plan_degradation(["video_primary"], profile,
requirements)` and follow the returned path. Never invent a sub-stage that is
not predeclared; when the planner returns BLOCKED, stop and report it.

## Gates

Checkpoint required, `human_approval_default: true` (review point 7, with L2
sampling). Present clips shot by shot: lipsync, motion, identity, and duration.
Stop and end the turn.

## Failure Modes

- `LIP_SYNC_FAIL`: mouth shapes drift from the dialogue. Regenerate or run the
  declared lipsync pass again; do not accept a visibly mismatched line.
- `MOTION_WRONG` / `MOTION_UNNATURAL`: the clip leaves the shot's action or
  looks floaty. Adjust the motion prompt or regenerate with a new seed.
- `FACE_MISMATCH` / `WARDROBE_DRIFT` / `IDENTITY_UNKNOWN`: identity broke in
  motion. Reject and regenerate from the approved keyframe.
- `ARTIFACT`: warped hands, flicker, or morphing background. Reject.
- `FRAMING_BAD` / `LIGHTING_WRONG`: the model reframed or relit the shot.
- `DIALOGUE_CUT`: the spoken line does not fit the clip duration. Do not speed
  up audio; recut the line or extend the shot after approval.
- `OTHER`: record evidence and the regeneration strategy.

## Process

1. Confirm the binding and its capability declaration for `video_primary`.
2. Pick the predeclared sub-stage from the binding's declared capability; log
   any degradation and get approval.
3. Generate one sample shot first. Review it end to end.
4. Batch the remaining shots only after the sample passes.
5. Record every submission in the ledger before polling, so a crash is
   recoverable.
6. Update the asset manifest with accepted clips and checkpoint.

## Self-Evaluate

Does the clip deliver the shot's single action and land the emotion? Is the
person still recognizable? Does the duration match the plan? If any answer is
no, the clip is not accepted, regardless of cost already spent.