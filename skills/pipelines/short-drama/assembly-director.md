---
pipeline: short-drama
stage: assembly
inputs:
  - asset_manifest
  - scene_plan
outputs:
  - edit_decisions
schemas:
  edit_decisions: schemas/artifacts/edit_decisions.schema.json
slots:
  - tts_voice
  - music
  - subtitle
  - compose
gates:
  checkpoint_required: true
  human_approval_default: true
  review_point: 8
failure_modes:
  - PACING
  - DIALOGUE_CUT
  - LIP_SYNC_FAIL
  - SUBTITLE_MISALIGN
  - CLIPPING
  - MOTION_UNNATURAL
  - NARRATIVE_WEAK
  - MUSIC_UNAVAILABLE
  - OTHER
handoff:
  next_stage: delivery
  artifact: edit_decisions
references:
  - pipeline_defs/short-drama.yaml
  - bindings/slots.yaml
---

# Assembly Director - Short-Drama Pipeline

## When to Use

After every shot has an accepted clip. Assembly owns audio, subtitles, music,
and timing: the parts that make the episode feel like a short drama rather than
a stack of clips. Loudness and subtitle timing are measured here, not guessed.

## Inputs

- `asset_manifest` (required): accepted clips per shot.
- `scene_plan` (required): shot order, durations, dialogue.
- `script` (optional): line text and emotional direction.

## Outputs

A schema-valid `edit_decisions` at
`projects/<project-id>/artifacts/edit_decisions.json` with `cuts`,
`render_runtime`, audio plan, subtitles, and music. `render_runtime` must be
carried unchanged from the approved proposal; a runtime change needs a new
`render_runtime_selection` decision and user approval.

## Slots

- `tts_voice` (required): dialogue and narration synthesis.
- `music` (required for short-drama P0, AUD-08 revision): background bed,
  selected only from `music_library/` (D20). Present the music plan before
  generating.
- `subtitle` (required): subtitle generation and burn-in input.
- `compose` (required): the final render runtime.

All four resolve through the active profile. Announce the chosen provider and
model before generation.

## Sub-stages

The manifest predeclares three assembly sub-stages, run in this order as part
of every v2.5 assembly:

1. `rough_cut` - cut approved clips to the beat map; review pacing, dead air,
   and hook/re-hook before sound design.
2. `sound_design` - choose music only from `music_library/`, mix dialogue first,
   measure loudness to target.
3. `grade_finish` - colour-grade consistency across shots plus subtitle visuals
   (keyword emphasis/motion).

The sub-stage `condition` keys in the manifest are declarative labels for
workflow views, not runtime switches: the director runs all three phases
whether or not any evaluator has registered those keys. Skipping a phase
because its condition was absent is a bug, not a valid degraded path.

## Gates

Checkpoint required, `human_approval_default: true` (review point 8). Present
the cut list, the loudness measurement, and subtitle samples. Stop.

## Failure Modes

- `PACING`: cuts do not land on the beat, or the visual change interval exceeds
  three seconds. Re-time cuts against the beat map.
- `DIALOGUE_CUT`: a line is clipped by the cut. Extend the shot or re-cut the
  line; never truncate mid-word.
- `LIP_SYNC_FAIL`: audio and picture drift after assembly. Re-sync before
  rendering.
- `SUBTITLE_MISALIGN`: subtitle line breaks or timing do not follow speech, or
  text leaves the safe area.
- `CLIPPING`: audio peaks or the loudness target is missed. Re-measure with
  loudnorm; -14 LUFS +/-1 LU and true peak <= -1 dBTP.
- `MOTION_UNNATURAL`: a transition exposes a bad pose. Replace the transition
  or the shot.
- `NARRATIVE_WEAK`: music or pacing buries the reversal. Re-shape the edit.
- `MUSIC_UNAVAILABLE`: `music_library/` has no track matching the required
  emotional curve. Stop and ask; do not pull an unlicensed source.

## Process

1. Lock the cut list against the approved beat map.
2. Synthesize dialogue through `slot:tts_voice`; review pronunciation and
   dialect.
3. Place subtitles, verify line breaks and safe area, then measure loudness.
4. Select music from `music_library/` (the user's local royalty-free library,
   recorded in `music_library/licenses.json`), then duck it under dialogue. If no
   library track fits the emotional curve, stop and ask — never substitute an
   unlicensed or scraped track.
5. Write `edit_decisions` and self-evaluate against the schema.
6. Checkpoint and ask for approval.

## Self-Evaluate

Does the episode hold attention for its whole duration? Is every spoken word
audible and readable? Are subtitles inside the safe area? Does the audio hit the
platform loudness target? If not, fix it before delivery.