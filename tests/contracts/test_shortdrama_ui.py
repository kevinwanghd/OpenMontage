"""UI-11 contract tests: static L1 review console (P1a, no browser).

The console under ``web/review-console/`` is plain HTML/CSS/JS. These tests
lock its *structure* against the frozen requirements:

- required L1 regions (candidate wall / player / sampling / cost / status
  language) and the six review actions;
- the §11.3 keyboard map (J/K, A, R, E, U, S, B, 1/2/3, Esc) with a visible
  Review/Reject mode badge;
- REV-03 per-stage reject reason codes exactly mirror ``lib.review`` — the
  console must not offer PACING on shot_gen, etc.;
- only §10 API endpoints are referenced, and media URLs go through the
  LocalMediaLocator surfaces (``/media``, ``/thumb``, ``/media/library``);
- §11.2 status language is icon + text + tooltip, not colour-only;
- X-14 per-item cost plus spent/budget is displayed.

No browser, HTTP server or external API is started (M0 / NFR-02).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lib.review import ACTIONS, REASON_CODES, STAGE_REASON_CODES

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_DIR = REPO_ROOT / "web" / "review-console"
HTML_PATH = CONSOLE_DIR / "index.html"
CSS_PATH = CONSOLE_DIR / "styles.css"
JS_PATH = CONSOLE_DIR / "app.js"

STATUS_TOKENS = ["PASS", "FAIL", "UNKNOWN", "N/A", "BLOCKED", "STALE"]
STATUS_ICONS = ["✓", "✕", "?", "—", "⊘", "⟳"]

# §10 endpoints the console is allowed to consume (normalised templates).
EXPECTED_API = {
    "/api/v1/episodes",
    "/api/v1/episode/{}",
    "/api/v1/episode/{}/review_policy",
    "/api/v1/progress/{}/stats",
    "/api/v1/review/pending",
    "/api/v1/review/{}/decision",
    "/api/v1/review/{}/undo",
    "/api/v1/library/{}",
}

KEYBOARD_CONTRACT = {
    "j": "next_item",
    "k": "previous_item",
    "a": "accept",
    "r": "reject_mode",
    "e": "edit_prompt",
    "u": "undo",
    "s": "skip",
    "b": "flag_questionable",
    "1": "candidate_or_reason",
    "2": "candidate_or_reason",
    "3": "candidate_or_reason",
    "Escape": "exit_mode",
}

# Each §11.3 semantic action must be handled in app.js, not just documented.
KEYBOARD_HANDLERS = {
    "next_item": "moveSelection(1)",
    "previous_item": "moveSelection(-1)",
    "accept": 'submitDecision("accept")',
    "reject_mode": "enterRejectMode()",
    "edit_prompt": "submitPromptPatch()",
    "undo": "undoLast()",
    "skip": 'submitDecision("skip")',
    "flag_questionable": "toggleFlag()",
    "candidate_or_reason": "selectReasonByDigit",
    "exit_mode": "exitRejectMode()",
}


@pytest.fixture(scope="module")
def html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return JS_PATH.read_text(encoding="utf-8")


def json_block(document: str, block_id: str) -> object:
    match = re.search(
        rf'<script type="application/json" id="{re.escape(block_id)}">(.*?)</script>',
        document,
        re.S,
    )
    assert match, f"missing JSON contract block #{block_id}"
    return json.loads(match.group(1))


# --- files / M0 zero external API ----------------------------------------


def test_console_files_exist_and_are_static():
    for path in (HTML_PATH, CSS_PATH, JS_PATH):
        assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
        assert path.stat().st_size > 500, f"{path.name} looks like a stub"

    document = HTML_PATH.read_text(encoding="utf-8")
    assert "./styles.css" in document
    assert 'type="module"' in document and "./app.js" in document
    assert "<!DOCTYPE html>" in document
    assert 'charset="UTF-8"' in document
    assert 'lang="zh-CN"' in document


def test_no_external_network_dependencies(html, css, js):
    combined = html + css + js
    assert "googleapis" not in combined and "cdn." not in combined
    assert "<script src=\"http" not in combined
    assert "<link rel=\"stylesheet\" href=\"http" not in combined
    # M0 is zero external API: no absolute URL literal anywhere in the console.
    assert not re.search(r"https?://", combined), "console must not embed absolute URLs"


# --- required L1 regions --------------------------------------------------


def test_required_l1_regions(html):
    for region in ("candidate-wall", "player", "sampling", "cost", "status-language"):
        assert f'data-region="{region}"' in html, f"missing {region} region"

    assert 'id="wall-grid"' in html and 'role="grid"' in html
    assert 'id="wall-empty"' in html

    assert 'id="player"' in html
    assert 'id="player-video"' in html and "controls" in html
    assert 'id="player-image"' in html
    assert 'id="open-original"' in html and 'target="_blank"' in html
    assert 'rel="noopener"' in html

    assert 'id="sampling-controls"' in html
    assert 'name="sampling-mode"' in html
    assert 'value="all"' in html and 'value="sampled"' in html
    assert 'id="ratio-slider"' in html
    assert re.search(r'id="ratio-slider"[^>]*min="0"[^>]*max="1"', html)

    assert 'id="cost-panel"' in html
    for cost_id in ("item-cost", "spent", "budget-input", "pending-cost", "budget-bar"):
        assert f'id="{cost_id}"' in html, f"missing cost element #{cost_id}"
    assert "已花" in html and "预算" in html

    assert 'id="status-legend"' in html
    assert 'id="mode-badge"' in html


def test_contact_sheet_thumbnails_and_playback(js, html):
    # VIS-03 / D-10: L1 wall renders candidate thumbnails and can open originals.
    assert "wall-grid" in js
    assert "candidate-row" in js and "candidate-cell" in js
    assert "episodeThumbUrl" in js and "episodeMediaUrl" in js
    assert "createThumbMedia" in js
    assert "candidates" in js
    assert "打开原图/原视频" in html
    assert "player-video" in js and "player-image" in js


# --- six actions / REV-03 reason codes -----------------------------------


def test_six_actions_match_review_contract(html, js):
    buttons = set(re.findall(r'data-action="([^"]+)"', html))
    assert buttons == ACTIONS, f"action buttons {buttons} != lib.review.ACTIONS {ACTIONS}"

    for action in ACTIONS:
        assert f'submitDecision("{action}")' in js, f"action '{action}' never posted"

    # reject is gated on a reason code, both in the UI flow and the payload.
    assert re.search(r'if \(action === "reject" && !state\.selectedReason\)', js)
    assert re.search(r'reason_code:\s*action === "reject"', js)
    assert 'id="reject-panel"' in html
    assert 'id="reason-code-list"' in html
    assert 'id="confirm-reject"' in html


def test_reason_codes_mirror_lib_review(html, js):
    embedded = json_block(html, "reason-codes")
    expected = {stage: sorted(codes) for stage, codes in STAGE_REASON_CODES.items()}
    assert embedded == expected, (
        "web/review-console reason codes drifted from lib.review.STAGE_REASON_CODES"
    )

    flattened = {code for codes in embedded.values() for code in codes}
    assert flattened == REASON_CODES
    assert "PACING" not in embedded["shot_gen"], "REV-03: PACING is not valid on shot_gen"
    assert "SUBTITLE_MISALIGN" not in embedded["keyframe"]

    # The reject UI is driven by the item's stage, never a global code list.
    assert "STAGE_REASON_CODES[item.stage]" in js
    assert "dataset.reasonCode" in js


# --- API surface (§10) ----------------------------------------------------


def test_api_endpoints_match_section_10(js):
    found = set(re.findall(r"/api/v1/[A-Za-z0-9_/${}()\-]+", js))
    normalised = {re.sub(r"\$\{[^}]+\}", "{}", path) for path in found}
    assert normalised == EXPECTED_API, (
        f"console references unexpected API paths: {sorted(normalised - EXPECTED_API)}; "
        f"missing: {sorted(EXPECTED_API - normalised)}"
    )
    assert "/api/v2" not in js

    # Decision requests carry the §10.1 concurrency/version fields.
    assert '"Idempotency-Key"' in js
    assert "candidate_version" in js
    assert re.search(r'method:\s*"POST"', js)
    assert re.search(r'method:\s*"PUT"', js)


def test_media_urls_use_local_media_locator_surfaces(js, html):
    # LocalMediaLocator surface: /media/{episode}/... and /thumb/{episode}/...
    assert "`/media/${encodeURIComponent(episode)}/${path}`" in js
    assert "`/thumb/${encodeURIComponent(episode)}/${path}`" in js
    assert "`/media/library/${encodeURIComponent(kind)}/${encodeURIComponent(assetId)}/${path}`" in js

    # No absolute server paths leak into URLs (X-12): normalise separators and
    # reject traversal / drive letters before encoding.
    assert 'replace(/\\\\/g, "/")' in js
    assert 'part === ".."' in js
    assert 'part.includes(":")' in js
    assert "encodeURIComponent(part)" in js

    # Library assets resolve through media_path, not a client-side path guess.
    assert "asset.media_path" in js
    assert "libraryMediaUrl(kind, asset.asset_id, asset.media_path)" in js

    # M0 has no thumbnail endpoint for videos; video thumbs use /media directly.
    assert "video.src = mediaUrl" in js
    assert 'id="library-panel"' in html


# --- status language (§11.2) ---------------------------------------------


def test_status_language_is_icon_text_and_tooltip(html, css, js):
    for token in STATUS_TOKENS:
        pattern = rf'<span class="status-token" data-status="{re.escape(token)}" title="[^"]+">'
        assert re.search(pattern, html), f"missing icon+text+tooltip token {token}"
        assert f'[data-status="{token}"]' in css, f"no visual token rule for {token}"

    assert re.findall(r'class="status-icon"', html)
    assert re.findall(r'class="status-text"', html)
    for icon in STATUS_ICONS:
        assert icon in html, f"missing status icon {icon}"
    # The glyph is decorative; the text label carries the meaning for AT.
    assert 'aria-hidden="true"' in html

    for token in STATUS_TOKENS:
        assert f'"{token}"' in js or f"'{token}'" in js, f"{token} never produced by app.js"
    assert "itemStatus" in js
    assert "statusNode" in js


# --- cost (X-14) ----------------------------------------------------------


def test_per_item_cost_and_spent_budget(html, js):
    assert "cost_if_approved_usd" in js
    assert 'data-cost="item"' in html
    assert 'data-cost="spent"' in html
    assert 'data-cost="pending"' in html
    assert 'id="budget-status"' in html
    assert 'value="10.00"' in html
    # Budget default mirrors config.yaml budget.total_usd fallback; session spend
    # is persisted so a refresh does not reset the visible ledger.
    assert re.search(r"readNumber\(budgetKey\(\), 10\)", js)
    assert "om.review-console.budget" in js
    assert "om.review-console.spent." in js
    assert "addSpend" in js and "renderCost" in js


# --- interaction states (§11.1) ------------------------------------------


def test_interaction_states_are_covered(html, js):
    combined = html + js
    for phrase in (
        "未发现剧集",            # no episode
        "已审完—查看决策",        # empty queue
        "媒体 404",              # media missing
        "重试",                  # retry affordance
        "not-generated",         # partial generation
        "STALE",                 # stale decision banner
        "BLOCKED",               # budget / gate blocked
        "UNKNOWN",               # identity unknown
    ):
        assert phrase in combined, f"missing interaction state: {phrase}"

    assert 'id="stale-banner"' in html
    assert 'id="error-banner"' in html and 'role="alert"' in html
    assert "ApiFailure" in js and "side_effects" in js
    assert "loadPending" in js


# --- keyboard, mode, a11y ------------------------------------------------


def test_keyboard_map_complete_and_handled(html, js, css):
    mapping = json_block(html, "keyboard-map")
    for key, action in KEYBOARD_CONTRACT.items():
        assert key in mapping, f"keyboard map missing '{key}'"
        assert mapping[key]["action"] == action, f"'{key}' maps to {mapping[key]['action']!r}"

    for semantic, handler in KEYBOARD_HANDLERS.items():
        assert handler in js, f"keyboard action {semantic} not handled in app.js"

    assert "handleKeydown" in js
    assert "document.addEventListener(\"keydown\", handleKeydown)" in js
    # Digits are mode-dependent: candidates in Review, reason codes in Reject.
    assert "state.mode === \"reject\"" in js
    assert "selectCandidateByDigit" in js and "selectReasonByDigit" in js
    # Esc leaves reject mode; the badge is visible in both modes.
    assert "exitRejectMode" in js and 'key === "Escape"' in js
    assert "REJECT 模式" in js and "REVIEW 模式" in js
    assert 'body[data-mode="reject"]' in css


def test_accessibility_and_focus_retention(html, js, css):
    assert 'role="grid"' in html
    assert 'setAttribute("role", "row")' in js
    assert 'setAttribute("role", "gridcell")' in js
    # aria-label carries shot id, face result, priority and cost (§11.3).
    assert "aria-label" in js
    assert re.search(r"shot \$\{item\.slot\}", js)
    assert "face " in js and "must-view" in js and "cost " in js

    # 44x44 targets, visible focus ring, reduced motion, live announcements.
    assert "--target: 44px" in css
    assert "min-height: var(--target)" in css
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css
    assert 'aria-live="polite"' in html
    assert "focusCurrentRow" in js
    assert "tabIndex" in js

    # Active review must not full-render: a decision removes one row only.
    assert "state.items.splice(index, 1)" in js
    assert "renderWall()" in js


def test_sampling_policy_matches_review_policy_model(html, js):
    assert 'value="all"' in html and 'value="sampled"' in html
    assert 'id="ratio-slider"' in html
    assert "ENDPOINTS.reviewPolicy(state.episode)" in js
    for field in ("mode", "detail_ratio", "face_sim_threshold", "sampling_salt"):
        assert field in js, f"policy payload missing {field}"
    # 0..1 ratio, matching ReviewPolicyModel ge=0/le=1.
    assert 'min="0"' in html and 'max="1"' in html
    assert "schedulePolicySave" in js and "loadPending" in js


def test_review_queue_is_pending_only_and_prioritised(js):
    # L1 = pending items only, sorted by the API; render order is preserved.
    assert "ENDPOINTS.pending" in js
    assert "data.items" in js
    assert "state.items.forEach" in js
    # Priority labels: must-view / recommended / skippable.
    assert "必看" in js and "建议" in js and "可跳" in js