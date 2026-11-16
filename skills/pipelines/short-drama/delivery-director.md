---
pipeline: short-drama
stage: delivery
inputs:
  - edit_decisions
outputs:
  - render_report
  - publish_log
schemas:
  render_report: schemas/artifacts/render_report.schema.json
  publish_log: schemas/artifacts/publish_log.schema.json
slots:
  - compose
  - upscale
gates:
  checkpoint_required: true
  human_approval_default: true
  review_point: 9
failure_modes:
  - SUBTITLE_MISALIGN
  - CLIPPING
  - FRAMING_BAD
  - ARTIFACT
  - OTHER
handoff:
  next_stage: null
  artifact: render_report
references:
  - pipeline_defs/short-drama.yaml
  - bindings/profiles/tiktok-draft.yaml
---

# Delivery Director - Short-Drama Pipeline

## When to Use

After the approved edit. Delivery renders the final MP4, verifies the technical
spec, records rights and disclosure, and writes the publish log. This is the
last gate before the user publishes; it must not paper over a degraded output.

## Inputs

- `edit_decisions` (required): cuts, audio, subtitles, `render_runtime`.
- `asset_manifest` and `scene_plan` (optional): provenance and shot metadata.

## Outputs

- `render_report`: schema-valid report with the output path, encoding profile,
  resolution, duration, loudness measurement, runtime actually used, and any
  DEGRADED labelling.
- `publish_log`: schema-valid log with per-platform entry, rights evidence,
  account/region, AI-disclosure status, and timestamp.

Canonical output path: `projects/<project-id>/renders/final.mp4`.

## Slots

- `compose` (required): the render runtime locked at proposal.
- `upscale` (required only on the declared draft path): 480P draft to 1080x1920.

The manifest predeclares two delivery sub-stages:

1. `native_delivery` - the draft already meets the target resolution.
2. `delivery_upscale` - the profile renders a 480P draft and upscales it. This
   is DEGRADED with `lost=native_1080p`; `render_report` must say so and the
   plan must be declared before rendering (DEL-01 / F34).

## Gates

Checkpoint required, `human_approval_default: true` (review point 9). Present
the final file, ffprobe summary, loudness measurement, and the publish
checklist. Stop and end the turn; publishing is a user action.

## Failure Modes

- `SUBTITLE_MISALIGN`: burned subtitles drift or leave the safe area. Re-render
  with corrected subtitle timing.
- `CLIPPING`: final audio clips or misses loudness. Fix the mix and re-render;
  do not ship it.
- `FRAMING_BAD`: upscaling or cropping pushed the subject out of frame.
  Re-render with a corrected crop.
- `ARTIFACT`: encoding artifacts, black frames, or a truncated file. Re-render
  and re-verify.
- `OTHER`: record evidence and the correction.

## Process

1. Confirm `render_runtime` matches the approved decision.
2. Render through `slot:compose`; if the profile declares a draft resolution,
   follow `delivery_upscale` and label the result DEGRADED.
3. Verify with ffprobe: 1080x1920, expected duration, audio stream present.
4. Measure loudness: -14 LUFS +/-1 LU, true peak <= -1 dBTP.
5. Sample opening, middle, and closing frames for visual checks.
6. Record rights, platform rules, and AI disclosure in `publish_log`. For every
   music track used, verify its `music_library/licenses.json` entry:
   `commercial_use` must be true, and `attribution_required` tracks must carry
   their credit in the rights evidence (D20/AUD-10).
7. Write both artifacts, checkpoint, and ask for approval.

## Self-Evaluate

Is the delivered file exactly the approved edit? Are the technical specs
measured rather than assumed? Is every degraded path labelled? Is anything
blocked on rights or platform rules? If any answer is no, do not present it as
ready.