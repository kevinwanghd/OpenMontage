---
pipeline: short-drama
stage: beat_map
inputs:
  - brief
outputs:
  - beat_map
schemas:
  beat_map: schemas/artifacts/beat_map.schema.json
slots: []
gates:
  checkpoint_required: true
  human_approval_default: true
  review_point: 2
failure_modes:
  - PACING
  - HOOK_WEAK
  - NARRATIVE_WEAK
  - OTHER
handoff:
  next_stage: script
  artifact: beat_map
references:
  - pipeline_defs/short-drama.yaml
  - skills/pipelines/short-drama/idea-director.md
---

# Beat Map Director - Short-Drama Pipeline

## When to Use

After the approved `brief` from the `idea` stage, before any dialogue is written. The beat map owns
retention: it decides where the viewer's attention is re-earned. Writing the
script first and retrofitting rhythm is the most common cause of a short drama
that dies at second 20.

## Inputs

- `brief` (required): hook, key points, duration, tone, audience.

## Outputs

A schema-valid `beat_map` at `projects/<project-id>/artifacts/beat_map.json`
with `duration_s`, `segments`, and `beats`. Use the four-part 120-second shape:

| Window | Segment | Requirement |
|---|---|---|
| 0-3s | HOOK | strongest conflict frame plus one hook line |
| 3-30s | SETUP | introduce people and stakes, one actable action per beat |
| 30-45s | RISING | three escalating beats, a small hook every 15s |
| 45-60s | MID-TURN | first expectation reversal |
| 60-95s | ESCALATION | pressure increases plus an information turn |
| 95-110s | CLIMAX | hero moment, the strongest frame of the episode |
| 110-120s | RESOLUTION | landing line, cliffhanger or deliberate silence |

Mark at least five `hook_candidate` beats and one reversal position. The density
is machine-checkable, so do not hand-wave it.

## Slots

No capability slot is resolved here. The beat map is a document; it spends
nothing. If a beat assumes a capability the pipeline cannot deliver (for
example a multi-character dialogue shot with no lipsync binding), flag it now
instead of discovering it at `shot_gen`.

## Gates

Checkpoint required, `human_approval_default: true` (review point 2). Present
the timeline, the hook candidates, and the reversal. Stop and end the turn.

## Failure Modes

- `PACING`: a window longer than 15 seconds has no turn, or the escalation is
  flat. Insert a beat or move the reversal earlier.
- `HOOK_WEAK`: the 0-3 second beat is not the strongest conflict. Swap it.
- `NARRATIVE_WEAK`: the reversal does not change what the protagonist knows or
  wants. Rewrite the MID-TURN.
- `OTHER`: record evidence and the proposed fix in the review decision.

## Process

1. Lay out all beats with start/end seconds; no gaps, no overlaps.
2. Verify every 15-second window contains at least one beat.
3. Mark hook candidates and the reversal.
4. Confirm the last beat obeys the approved duration.
5. Checkpoint and ask for approval.

## Self-Evaluate

Could a viewer describe what happened after 15 seconds? Does each beat change
the situation rather than restate it? Is the climax placed late enough to
justify watching? If not, revise the map, not the script.