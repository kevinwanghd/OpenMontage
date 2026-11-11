---
pipeline: short-drama
stage: keyframe
inputs:
  - scene_plan
  - character_design
outputs:
  - asset_manifest
schemas:
  asset_manifest: schemas/artifacts/asset_manifest.schema.json
slots:
  - image_reference
  - image_character_key
gates:
  checkpoint_required: true
  human_approval_default: true
  review_point: 6
failure_modes:
  - FACE_MISMATCH
  - WARDROBE_DRIFT
  - OFF_POSE
  - FRAMING_BAD
  - LIGHTING_WRONG
  - IDENTITY_UNKNOWN
  - ARTIFACT
  - OTHER
handoff:
  next_stage: shot_gen
  artifact: asset_manifest
references:
  - pipeline_defs/short-drama.yaml
  - bindings/slots.yaml
  - skills/pipelines/short-drama/character-cast-director.md
---

# Keyframe Director - Short-Drama Pipeline

## When to Use

After the approved scene plan. Keyframes are the conditioning input for
image-to-video; the video model will preserve whatever the keyframe gets wrong.
This stage is the cheapest place to reject a bad frame, so review per shot, not
per batch.

## Inputs

- `scene_plan` (required): shots, framing, action, dialogue.
- `character_design` (required): costume key images and canonical descriptions.

## Outputs

A schema-valid `asset_manifest` at
`projects/<project-id>/artifacts/asset_manifest.json` containing every keyframe
candidate with path, shot id, provider, model, prompt, seed, and candidate
index. Generate three to five candidates per shot; more only when the first
round fails review.

## Slots

- `image_reference` (required): scene keyframe generation.
- `image_character_key` (required): identity conditioning from the costume key
  image; reuse the registered character reference instead of re-describing the
  person in text.

Resolve the slots through the active profile, never a hardcoded tool. Before the
first paid call, announce the exact tool, provider, model, reason, and sample
vs batch scope. Read the bound tool's Layer 3 skill before writing prompts.

## Gates

Checkpoint required, `human_approval_default: true` (review point 6). Present
candidates shot by shot with the reference character card beside them. Stop and
end the turn; approve one keyframe per shot before `shot_gen`.

## Failure Modes

- `FACE_MISMATCH`: the frame is not the cast person. Regenerate with stronger
  reference conditioning; do not accept "close enough".
- `WARDROBE_DRIFT`: costume differs from the key image or script. Correct the
  wardrobe prompt and regenerate.
- `OFF_POSE`: the body cannot perform the shot's single action. Re-pose.
- `FRAMING_BAD`: subject outside the safe area or wrong shot size.
- `LIGHTING_WRONG`: light direction or temperature breaks continuity.
- `IDENTITY_UNKNOWN`: no comparison is available. Keep it unaccepted and say so.
- `ARTIFACT`: extra fingers, text, watermarks, or broken geometry. Reject it.

## Process

1. For each shot, assemble the prompt from the shot language plus the
   character's registered reference.
2. Generate a small sample first, review it, then batch the rest.
3. Compare each candidate against the cast key image and the shot card.
4. Record rejected candidates in the rejection library with the reason code.
5. Checkpoint with only approved keyframes marked accepted.

## Self-Evaluate

Is this the same person as the cast reference? Would this frame, animated for
three seconds, still make sense? Is anything in the frame unexplainable by the
shot card? If yes to the last question, regenerate.