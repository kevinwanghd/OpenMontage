---
pipeline: short-drama
stage: idea
inputs: []
outputs:
  - brief
schemas:
  brief: schemas/artifacts/brief.schema.json
slots: []
gates:
  checkpoint_required: true
  human_approval_default: true
  review_point: 1
failure_modes:
  - HOOK_WEAK
  - NARRATIVE_WEAK
  - OTHER
handoff:
  next_stage: beat_map
  artifact: brief
references:
  - pipeline_defs/short-drama.yaml
  - skills/meta/checkpoint-protocol.md
---

# Idea / Brief Director - Short-Drama Pipeline

The pipeline stage is `idea` (the repo-wide planning-stage name); its canonical
artifact is `brief`, and review point 1 records decisions under the review stage
`brief`. Keep the two names straight: `idea` schedules the stage, `brief` names
the artifact and the review vocabulary.

## When to Use

Read this before producing the first artifact of any short-drama episode. The
brief is the only stage where a wrong direction is still cheap to fix: every
later stage inherits its hook, audience, language, and duration. Do not start a
beat map until the user approves the brief.

## Inputs

No upstream artifact. The director gathers the user's premise, the target
platform, the spoken language/dialect, the series context (`series_id`,
`episode_number`, prior episodes, character roster) and the target duration.
When the user supplies a reference video, route through
`skills/meta/video-reference-analyst.md` first and record the grounded summary
in `brief.reference_material`.

## Outputs

Produce a schema-valid `brief` at
`projects/<project-id>/artifacts/brief.json`:

- `hook`: the visible conflict of the first 2-3 seconds, written as a shot, not
  as a title card.
- `key_points`: three to five escalating story beats.
- `core_message`: one sentence the episode must land.
- `target_audience`, `target_platform` (TikTok), `target_duration_seconds`
  (60-120 for P0), `tone`, `style`.

## Slots

This stage resolves no capability slot. It is pure planning: zero external API
calls, zero spend. If a tool would be needed to inspect a reference, use the
local analysis tools and record their output in `reference_material`.

## Render Runtime Contract

The pipeline has no separate `proposal` stage, so the runtime conversation
happens here, before the brief is approved. When both Remotion and hyperframes
are available on the machine, **Present Both** runtimes to the user with a
one-sentence strength, a one-sentence tradeoff, and a recommendation tied to
this episode. Wait for explicit approval, then record the choice as
`render_runtime` in the brief metadata and log a `render_runtime_selection`
decision with every considered runtime in `options_considered`. If only one
runtime is installed, say so explicitly and record why the other was rejected.
Never silently default to Remotion; the runtime is carried unchanged through
assembly and delivery.

## Gates

Checkpoint required, `human_approval_default: true` (review point 1). Present
the hook, the one-sentence proposition, the audience, and the duration. Then
stop and end the turn. Do not continue into `beat_map` in the same response.

## Failure Modes

- `HOOK_WEAK`: the opening is exposition, a date card, or a voice-over. Rewrite
  it as a visible conflict and re-present the brief.
- `NARRATIVE_WEAK`: the premise has no stakes or no reversal. Escalate the
  conflict before the beat map.
- `OTHER`: anything that does not fit; record evidence in the review decision.

## Process

1. Interrogate the premise: who wants what, who blocks it, what changes.
2. Write three hook candidates and pick the most physical one.
3. Fix the language/dialect now; it cannot drift after TTS is chosen.
4. Fill the brief artifact and self-evaluate against the schema.
5. Write the `idea`-stage checkpoint with the artifact and present it.

## Self-Evaluate

Before checkpointing, answer: can a stranger name the conflict from the hook
alone? Is the audience concrete? Is the duration compatible with the 120-second
retention shape? If any answer is no, revise before asking for approval.