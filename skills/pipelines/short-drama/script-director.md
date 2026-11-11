---
pipeline: short-drama
stage: script
inputs:
  - brief
  - beat_map
outputs:
  - script
schemas:
  script: schemas/artifacts/script.schema.json
slots: []
gates:
  checkpoint_required: true
  human_approval_default: true
  review_point: 3
failure_modes:
  - PACING
  - DIALOGUE_CUT
  - DIALECT_OFF
  - NARRATIVE_WEAK
  - HOOK_WEAK
  - OTHER
handoff:
  next_stage: character_cast
  artifact: script
references:
  - pipeline_defs/short-drama.yaml
  - skills/pipelines/short-drama/beat-map-director.md
---

# Script Director - Short-Drama Pipeline

## When to Use

After the approved `beat_map`, before characters are cast. This stage turns
rhythm into spoken lines and actable actions. Every line will later be spoken by
TTS and, when the binding supports it, lip-synced, so the script is also an
audio contract.

## Inputs

- `brief` (required): language, dialect, tone, duration.
- `beat_map` (required): beats, hook candidates, reversal.

## Outputs

A schema-valid `script` with `total_duration_seconds` and `sections`. Each
section must state its beat id, its single action, the speaker of every line,
and the emotional direction. Rules:

- One spoken sentence stays at or under 20 characters.
- Each shot carries one action, not a sequence of actions.
- Dialogue is what a person would say out loud; no written-language clauses.
- Narration never explains what the picture already shows.
- The language and dialect chosen in the brief are locked. A register change
  after approval requires a new decision, not a silent edit.

## Slots

No capability slot. The script is text; it spends nothing. However, the director
must keep the dialogue physically producible: a line that needs visible mouth
movement on a binding without `lipsync_from_dialogue` will force the predeclared
`separate_lipsync_path` in `shot_gen`. Note that cost early.

## Gates

Checkpoint required, `human_approval_default: true` (review point 3). Present
the full script with the beat mapping and the locked dialect. Stop and end the
turn.

## Failure Modes

- `PACING`: the script restates beats without adding turns. Return to the beat
  map before rewriting lines.
- `DIALOGUE_CUT`: a line is too long for the shot's duration at natural speech
  rate. Shorten it; never speed up TTS to fit.
- `DIALECT_OFF`: the line drifts out of the approved language or register.
  Rewrite it in the locked dialect.
- `NARRATIVE_WEAK` / `HOOK_WEAK`: the opening line explains instead of
  confronting. Rewrite the first exchange as conflict.
- `OTHER`: capture evidence and the proposed fix.

## Process

1. Copy the beat ids and durations from the approved beat map.
2. Write the hook exchange first; it is the only part the viewer is guaranteed
   to hear.
3. Write each section with one action and no more than two speakers.
4. Read every line aloud at natural speed and check it fits the shot duration.
5. Self-evaluate, then checkpoint for approval.

## Self-Evaluate

Does every line sound spoken rather than written? Can each section be filmed as
one shot with one action? Does the last line earn the cliffhanger? If a line can
be removed without losing meaning, remove it.