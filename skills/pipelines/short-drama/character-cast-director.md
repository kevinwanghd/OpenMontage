---
pipeline: short-drama
stage: character_cast
inputs:
  - script
outputs:
  - character_design
  - drama_character
schemas:
  character_design: schemas/artifacts/character_design.schema.json
  drama_character: schemas/artifacts/drama_character.schema.json
slots:
  - image_character_key
gates:
  checkpoint_required: true
  human_approval_default: true
  review_point: 4
failure_modes:
  - FACE_MISMATCH
  - WARDROBE_DRIFT
  - IDENTITY_UNKNOWN
  - OTHER
handoff:
  next_stage: scene_plan
  artifact: character_design
references:
  - pipeline_defs/short-drama.yaml
  - bindings/slots.yaml
  - lib/asset_library.py
---

# Character Cast Director - Short-Drama Pipeline

## When to Use

After the approved script and before the scene plan. This is the first
generation stage and the root of every identity decision downstream: if the
costume key image is wrong here, every keyframe and every shot inherits the
error. Casting is also the first asset-library write, so the same character can
be reused across episodes (CHAR-09).

## Inputs

- `script` (required): speaking roles, wardrobe notes, emotional range.

## Outputs

- `character_design`: a schema-valid design with one entry per speaking role
  (age range, hair, wardrobe, silhouette, emotional range, required views).
- `drama_character`: a schema-valid character record with a stable
  `character_id`, canonical description, wardrobe, and optional voice id. The
  record is registered through `lib/asset_library.py` so later episodes resolve
  the same identity without re-casting.

## Slots

- `image_character_key` (required): the capability slot that generates costume
  key and reference images. The manifest names the slot; the active binding
  profile decides the actual provider and model. Never hardcode a provider.

Before generating, run preflight for the slot and report the real capability
envelope. Record provider, model, prompt, and seed for every generated image.

## Gates

Checkpoint required, `human_approval_default: true` (review point 4). Present
the character cards and the key images side by side with the written
description. Stop and end the turn; never cast silently.

## Failure Modes

- `FACE_MISMATCH`: the key image is not the written person. Regenerate with a
  corrected prompt, not a cosmetic edit to the description.
- `WARDROBE_DRIFT`: clothing differs between views or from the script. Fix the
  wardrobe block and regenerate all affected views.
- `IDENTITY_UNKNOWN`: the image cannot be compared to a prior episode. Do not
  accept it as known; mark it unknown and keep it out of the accepted set.
- `OTHER`: record the evidence and the regeneration strategy.

## Process

1. Extract every speaking role from the script and assign a stable
   `character_id`.
2. Write the canonical description and wardrobe before generating anything.
3. Generate key images through `slot:image_character_key` after reading the
   bound tool's Layer 3 skill.
4. Run identity comparison where available; a low score is a finding, not a
   warning to bury.
5. Register accepted characters in the asset library with lineage.
6. Checkpoint and ask for approval.

## Self-Evaluate

Would a viewer recognize this person in a different scene? Is the wardrobe
specific enough to reproduce? Are reference angles sufficient for keyframe
conditioning? If not, do not proceed to the scene plan.