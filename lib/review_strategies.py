"""Reason-code → regeneration strategy map + retry circuit breaker (REV-05/06).

REV-05 — every canonical reason code has exactly one documented regeneration
path. ``strategy_for()`` / ``strategy_path()`` are the query surface used by the
director skill and the CLI; the table below is the source of truth. Available
levers: change the seed, add reference images, switch the prompt dialect, or
send the item back to an earlier pipeline stage.

| reason code       | strategy   | target stage    | seed | refs                                  | dialect | human |
|-------------------|------------|-----------------|------|---------------------------------------|---------|-------|
| FACE_MISMATCH     | send_back  | keyframe        | yes  | character_reference, approved_history | no      | no    |
| WARDROBE_DRIFT    | regenerate | same stage      | yes  | wardrobe_reference                    | no      | no    |
| OFF_POSE          | regenerate | same stage      | yes  | pose_reference                        | no      | no    |
| LIP_SYNC_FAIL     | send_back  | shot_gen        | yes  | —                                     | yes     | no    |
| MOTION_WRONG      | regenerate | same stage      | yes  | —                                     | yes     | no    |
| ARTIFACT          | regenerate | same stage      | yes  | —                                     | no      | no    |
| FRAMING_BAD       | regenerate | same stage      | yes  | framing_reference                     | no      | no    |
| LIGHTING_WRONG    | regenerate | same stage      | yes  | lighting_reference                    | no      | no    |
| PACING            | send_back  | beat_map        | no   | —                                     | no      | no    |
| DIALOGUE_CUT      | send_back  | script          | no   | —                                     | no      | no    |
| DIALECT_OFF       | send_back  | script          | no   | —                                     | yes     | no    |
| MOTION_UNNATURAL  | regenerate | same stage      | yes  | —                                     | yes     | no    |
| CLIPPING          | send_back  | assembly        | no   | —                                     | no      | no    |
| SUBTITLE_MISALIGN | send_back  | delivery        | no   | —                                     | no      | no    |
| HOOK_WEAK         | send_back  | brief           | no   | —                                     | no      | no    |
| NARRATIVE_WEAK    | send_back  | script          | no   | —                                     | no      | no    |
| IDENTITY_UNKNOWN  | send_back  | character_cast  | no   | character_reference                   | no      | yes   |
| EMOTION_FLAT      | send_back  | brief           | no   | —                                     | no      | no    |
| PACING_DRAG       | send_back  | beat_map        | no   | —                                     | no      | no    |
| PAYOFF_UNEARNED   | send_back  | script          | no   | —                                     | no      | no    |
| MUSIC_UNAVAILABLE | send_back  | assembly        | no   | —                                     | no      | yes   |
| OTHER             | regenerate | same stage      | yes  | —                                     | no      | no    |

REV-06 — consecutive-rejection circuit breaker. The streak is projected from
the append-only review log (undo compensates prior decisions, never rewrites):

- a reject increments the streak;
- accept / send_back / skip / pick_variant / edit_prompt reset it (they either
  end the retry loop or start a new prompt/candidate direction);
- a new candidate version resets it (reviews of a new render never inherit the
  old render's failures);
- at the limit the breaker is *open*: further reject / pick_variant /
  edit_prompt decisions are refused, and the only ways forward are
  ``send_back`` or a human override with recorded evidence.

M0: pure in-process logic over local files; zero external API calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

# Retry loop actions that clear a reject streak (REV-06 reset semantics).
RESET_ACTIONS = frozenset({"accept", "send_back", "skip", "pick_variant", "edit_prompt"})
# Actions refused once the breaker is open; send_back/accept/skip remain legal.
BLOCKED_WHEN_TRIPPED = frozenset({"reject", "pick_variant", "edit_prompt"})


@dataclass(frozen=True)
class RegenerationStrategy:
    """One documented regeneration path for a reason code (REV-05)."""

    reason_code: str
    strategy: str                       # regenerate | send_back
    target_stage: Optional[str] = None  # None = regenerate the item's own stage
    change_seed: bool = False
    add_reference: tuple[str, ...] = ()
    change_dialect: bool = False
    requires_human: bool = False
    rationale: str = ""

    def resolved_stage(self, review_stage: Optional[str] = None) -> Optional[str]:
        return self.target_stage if self.target_stage is not None else review_stage

    def as_dict(self, review_stage: Optional[str] = None) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "strategy": self.strategy,
            "target_stage": self.resolved_stage(review_stage),
            "change_seed": self.change_seed,
            "add_reference": list(self.add_reference),
            "change_dialect": self.change_dialect,
            "requires_human": self.requires_human,
            "rationale": self.rationale,
        }


def _s(code: str, strategy: str, stage: Optional[str] = None, *, seed: bool = False,
       refs: tuple[str, ...] = (), dialect: bool = False, human: bool = False,
       rationale: str = "") -> RegenerationStrategy:
    return RegenerationStrategy(
        reason_code=code, strategy=strategy, target_stage=stage, change_seed=seed,
        add_reference=refs, change_dialect=dialect, requires_human=human,
        rationale=rationale,
    )


STRATEGIES: dict[str, RegenerationStrategy] = {
    s.reason_code: s
    for s in (
        _s("FACE_MISMATCH", "send_back", "keyframe", seed=True,
           refs=("character_reference", "approved_history"),
           rationale="Re-lock the character reference and regenerate the keyframe; "
                     "escalate to character_cast if the reference itself is wrong."),
        _s("WARDROBE_DRIFT", "regenerate", seed=True, refs=("wardrobe_reference",),
           rationale="Add the approved wardrobe reference and re-roll the shot."),
        _s("OFF_POSE", "regenerate", seed=True, refs=("pose_reference",),
           rationale="Anchor the requested pose with a reference image and re-roll."),
        _s("LIP_SYNC_FAIL", "send_back", "shot_gen", seed=True, dialect=True,
           rationale="Resend to shot generation with a new seed and a dialect that "
                     "states dialogue timing explicitly."),
        _s("MOTION_WRONG", "regenerate", seed=True, dialect=True,
           rationale="Re-roll motion with a new seed and a clearer motion dialect."),
        _s("ARTIFACT", "regenerate", seed=True,
           rationale="Rendering artifact — same prompt, new seed."),
        _s("FRAMING_BAD", "regenerate", seed=True, refs=("framing_reference",),
           rationale="Add the approved framing/composition reference and re-roll."),
        _s("LIGHTING_WRONG", "regenerate", seed=True, refs=("lighting_reference",),
           rationale="Add the approved lighting reference and re-roll."),
        _s("PACING", "send_back", "beat_map",
           rationale="Pacing is a story-timing decision; revise the beat map."),
        _s("DIALOGUE_CUT", "send_back", "script",
           rationale="The line was cut or truncated; revise the script."),
        _s("DIALECT_OFF", "send_back", "script", dialect=True,
           rationale="Switch the prompt dialect and revise the script-level wording."),
        _s("MOTION_UNNATURAL", "regenerate", seed=True, dialect=True,
           rationale="Re-roll with a new seed and a motion dialect that states "
                     "physical constraints."),
        _s("CLIPPING", "send_back", "assembly",
           rationale="Audio/level clipping is introduced in assembly; remix there."),
        _s("SUBTITLE_MISALIGN", "send_back", "delivery",
           rationale="Subtitle timing is fixed in delivery; regenerate the subtitle track."),
        _s("HOOK_WEAK", "send_back", "brief",
           rationale="A weak hook is a concept problem; revise the brief."),
        _s("NARRATIVE_WEAK", "send_back", "script",
           rationale="Story quality is a script problem; revise the script."),
        _s("IDENTITY_UNKNOWN", "send_back", "character_cast", human=True,
           refs=("character_reference",),
           rationale="Face identity cannot be resolved automatically; lock the "
                     "character reference and have a human confirm identity."),
        # v2.5 creative-gate codes (DIR-02/03) and music-library gate (D20).
# These entries extend the REV-05 table above, which is the documented source
# of truth for regeneration paths (see the module docstring). The target stage
# is data the review layer returns to the skill/agent, not runtime
# orchestration: no stage is executed from here.
        _s("EMOTION_FLAT", "send_back", "brief",
           rationale="Emotional flatness is a creative-direction problem; revise the "
                     "director brief's emotional arc and audience trigger points."),
        _s("PACING_DRAG", "send_back", "beat_map",
           rationale="Dragging pacing is a story-timing problem; tighten the beat map "
                     "and re-cut against it."),
        _s("PAYOFF_UNEARNED", "send_back", "script",
           rationale="The payoff lacks causal setup; revise the script so the reversal "
                     "is earned by earlier events."),
        _s("MUSIC_UNAVAILABLE", "send_back", "assembly", human=True,
           rationale="No registered track in music_library/ fits the emotional curve; "
                     "a human must add a licensed track before sound_design proceeds (D20)."),
        _s("OTHER", "regenerate", seed=True,
           rationale="Unclassified rejection — safest automatic path is a same-stage "
                     "re-roll with a new seed, then human review."),
    )
}

_STRATEGY_KINDS = {"regenerate", "send_back"}


def strategy_for(reason_code: str) -> RegenerationStrategy:
    """Return the documented strategy for ``reason_code`` (KeyError if unknown)."""
    try:
        return STRATEGIES[reason_code]
    except KeyError:
        raise KeyError(f"no regeneration strategy for reason code '{reason_code}'") from None


def strategy_path(reason_code: str, review_stage: Optional[str] = None) -> dict[str, Any]:
    """Query surface: resolved regeneration path for a code at a review stage."""
    return strategy_for(reason_code).as_dict(review_stage)


def validate_table(reason_codes: Iterable[str]) -> list[str]:
    """Return human-readable problems; empty list means the table is complete.

    Checks (REV-05): one entry per code and no extras, a legal strategy kind, a
    resolvable stage, at least one lever, and a rationale.
    """
    expected = set(reason_codes)
    present = set(STRATEGIES)
    problems = [f"missing strategy for {c}" for c in sorted(expected - present)]
    problems += [f"strategy for unregistered code {c}" for c in sorted(present - expected)]
    for code, s in sorted(STRATEGIES.items()):
        if s.strategy not in _STRATEGY_KINDS:
            problems.append(f"{code}: unknown strategy kind '{s.strategy}'")
        if s.strategy == "send_back" and not s.target_stage:
            problems.append(f"{code}: send_back requires a target stage")
        if not (s.change_seed or s.add_reference or s.change_dialect
                or s.target_stage or s.requires_human):
            problems.append(f"{code}: no regeneration lever defined")
        if len(s.rationale.strip()) < 10:
            problems.append(f"{code}: rationale too thin")
    return problems


# --- REV-06: circuit breaker -------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Per-shot retry limit and reset semantics (REV-06, F26)."""

    max_consecutive_rejects: int = 3
    reset_actions: frozenset[str] = RESET_ACTIONS
    reset_on_new_candidate_version: bool = True


@dataclass(frozen=True)
class BreakerState:
    tripped: bool
    consecutive_rejects: int
    limit: int
    current_candidate_version: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tripped": self.tripped,
            "consecutive_rejects": self.consecutive_rejects,
            "limit": self.limit,
            "current_candidate_version": self.current_candidate_version,
            "recovery": ("send_back or human override with evidence"
                         if self.tripped else "retry allowed"),
        }


def _action(rec: Mapping[str, Any]) -> Optional[str]:
    decision = rec.get("decision")
    if isinstance(decision, Mapping):
        return decision.get("action")
    return rec.get("action")


def effective_decision_chain(records: Iterable[Mapping[str, Any]],
                             review_id: str) -> list[dict[str, Any]]:
    """Ordered decisions for one review that are still effective.

    An ``undo`` compensates every prior decision for its target (matching
    ``ReviewStore._effective``); appended decisions after an undo start a new
    effective chain. Never rewrites the log.
    """
    chain: list[dict[str, Any]] = []
    for rec in records:
        rid = rec.get("review_id")
        et = rec.get("event_type")
        if et == "decision" and rid == review_id:
            chain.append(dict(rec))
        elif et == "undo":
            target = rec.get("supersedes") or rid
            if target == review_id:
                chain = []
    return chain


def consecutive_reject_streak(
    chain: Iterable[Mapping[str, Any]],
    policy: Optional[RetryPolicy] = None,
    *,
    current_candidate_version: Optional[str] = None,
) -> int:
    """Project the consecutive-reject streak under the reset semantics."""
    policy = policy or RetryPolicy()
    decisions = list(chain)
    if policy.reset_on_new_candidate_version and decisions:
        last_version = decisions[-1].get("candidate_version")
        if (last_version is not None and current_candidate_version is not None
                and last_version != current_candidate_version):
            decisions = []  # a new render never inherits the old render's failures

    streak = 0
    last_version: Optional[str] = None
    seen_version = False
    for rec in decisions:
        version = rec.get("candidate_version")
        if version is not None:
            if seen_version and version != last_version:
                streak = 0  # new candidate version resets the count
            last_version = version
            seen_version = True
        action = _action(rec)
        if action == "reject":
            streak += 1
        elif action in policy.reset_actions:
            streak = 0
    return streak


def evaluate_breaker(
    chain: Iterable[Mapping[str, Any]],
    policy: Optional[RetryPolicy] = None,
    *,
    current_candidate_version: Optional[str] = None,
) -> BreakerState:
    policy = policy or RetryPolicy()
    streak = consecutive_reject_streak(
        chain, policy, current_candidate_version=current_candidate_version)
    return BreakerState(
        tripped=streak >= policy.max_consecutive_rejects,
        consecutive_rejects=streak,
        limit=policy.max_consecutive_rejects,
        current_candidate_version=current_candidate_version,
    )