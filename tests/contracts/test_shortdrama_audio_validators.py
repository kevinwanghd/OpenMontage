"""Contract tests for the short-drama audio/subtitle validators.

Covers WP-N validation logic only (zero external APIs, no real audio):

- AUD-05 subtitle timeline monotonicity/range and ≤200 ms alignment errors
- AUD-06 ≤12 words and ≤3 s per cue, with manual flags instead of truncation
- AUD-07 −14 LUFS ±1 LU and true peak ≤ −1 dBTP
- AUD-08 dialogue/BGM mix spec, with N/A when there is no BGM
- AUD-09 voice-table registration/query and cross-episode reuse
- LOC-02 English line breaking and the provisional TikTok safe area
"""
import math

import pytest

from lib.asset_library import AssetLibrary
from lib.loudness import (
    BGM_MASKS_DIALOGUE,
    DIALOGUE_TOO_QUIET,
    INSUFFICIENT_DUCKING,
    INVALID_MEASUREMENT,
    LOUDNESS_OUT_OF_RANGE,
    MAX_LUFS,
    MAX_TRUE_PEAK_DBTP,
    MIN_LUFS,
    TRUE_PEAK_TOO_HIGH,
    LoudnessMeasurement,
    MixMeasurement,
    evaluate_loudness,
    evaluate_mix,
    mix_spec,
)
from lib.subtitle import (
    ALIGNMENT_END_ERROR,
    ALIGNMENT_START_ERROR,
    CUE_TOO_LONG,
    CUE_TOO_SHORT,
    EMPTY_TEXT,
    MISSING_ALIGNMENT,
    NON_POSITIVE_DURATION,
    NOT_MONOTONIC,
    OUT_OF_RANGE,
    OVERLAPPING_CUES,
    RATE_TOO_FAST,
    RATE_TOO_SLOW,
    SAFE_AREA_INVALID_RECT,
    SAFE_AREA_PRESET_UNCALIBRATED,
    SAFE_AREA_VIOLATION,
    STYLE_LINE_TOO_LONG,
    STYLE_ORPHAN_WORD,
    STYLE_TOO_MANY_LINES,
    TOO_MANY_WORDS,
    UNKNOWN_ALIGNMENT,
    SafeArea,
    SubtitleCue,
    SubtitleRect,
    SubtitleStyle,
    TIKTOK_9X16,
    count_words,
    manual_review_items,
    validate_alignment,
    validate_rules,
    validate_safe_area,
    validate_style,
    validate_timeline,
    wrap_english_text,
)
from lib.voices import (
    DEFAULT_STORAGE_KIND,
    DuplicateVoiceError,
    InvalidVoiceError,
    VoiceNotFoundError,
    VoiceRegistry,
)


def cue(index, start, end, text="one two three"):
    return SubtitleCue(index=index, start=start, end=end, text=text)


def codes(issues):
    return [issue.code for issue in issues]


# ---------------------------------------------------------------------------
# AUD-05 — timeline monotonicity, range and alignment
# ---------------------------------------------------------------------------

def test_valid_contiguous_timeline_has_no_issues():
    cues = [cue(0, 0.0, 1.0), cue(1, 1.0, 2.0), cue(2, 2.0, 3.0)]
    assert validate_timeline(cues, audio_duration=3.0) == []


def test_timeline_rejects_cue_beyond_audio_range():
    issues = validate_timeline([cue(0, 2.5, 3.2)], audio_duration=3.0)
    assert OUT_OF_RANGE in codes(issues)


def test_timeline_rejects_negative_start_and_zero_duration():
    issues = validate_timeline([cue(0, -0.1, 0.5), cue(1, 0.5, 0.5)])
    assert OUT_OF_RANGE in codes(issues)
    assert NON_POSITIVE_DURATION in codes(issues)


def test_timeline_rejects_non_monotonic_start():
    issues = validate_timeline([cue(0, 2.0, 2.5), cue(1, 1.0, 1.5)])
    assert NOT_MONOTONIC in codes(issues)


def test_timeline_rejects_overlap():
    issues = validate_timeline([cue(0, 0.0, 2.0), cue(1, 1.5, 2.5)])
    assert OVERLAPPING_CUES in codes(issues)


def test_timeline_rejects_non_finite_time():
    issues = validate_timeline([cue(0, math.nan, 1.0)])
    assert "INVALID_TIME" in codes(issues)
    assert issues[0].requires_manual_confirmation is False


def test_timeline_without_audio_duration_skips_range_check():
    assert validate_timeline([cue(0, 10.0, 11.0)]) == []


def test_alignment_within_200ms_passes():
    aligned = [cue(0, 1.00, 2.00), cue(1, 2.00, 3.00)]
    reference = [cue(0, 1.20, 2.15), cue(1, 2.10, 3.20)]
    assert validate_alignment(aligned, reference) == []


def test_alignment_start_error_over_200ms_is_flagged():
    issues = validate_alignment([cue(0, 1.00, 2.00)], [cue(0, 1.21, 2.00)])
    assert ALIGNMENT_START_ERROR in codes(issues)
    assert issues[0].requires_manual_confirmation is True


def test_alignment_end_error_over_200ms_is_flagged():
    issues = validate_alignment([cue(0, 1.00, 2.00)], [cue(0, 1.00, 2.21)])
    assert ALIGNMENT_END_ERROR in codes(issues)


def test_alignment_reports_missing_and_unknown_cues():
    issues = validate_alignment(
        [cue(0, 1.0, 2.0), cue(9, 3.0, 4.0)],
        [cue(0, 1.0, 2.0), cue(1, 2.0, 3.0)],
    )
    assert UNKNOWN_ALIGNMENT in codes(issues)
    assert MISSING_ALIGNMENT in codes(issues)


def test_alignment_accepts_custom_tolerance():
    # 120 ms error fails a tightened 100 ms tolerance, passes the 200 ms default.
    aligned = [cue(0, 1.00, 2.00)]
    reference = [cue(0, 1.12, 2.00)]
    assert validate_alignment(aligned, reference) == []
    assert validate_alignment(aligned, reference, tolerance_ms=100) != []


# ---------------------------------------------------------------------------
# AUD-06 — word/duration/rate rules (flag, never truncate)
# ---------------------------------------------------------------------------

def test_twelve_words_in_three_seconds_passes():
    text = "one two three four five six seven eight nine ten eleven twelve"
    assert count_words(text) == 12
    assert validate_rules([cue(0, 0.0, 3.0, text)]) == []


def test_thirteen_words_are_flagged_not_truncated():
    text = "one two three four five six seven eight nine ten eleven twelve thirteen"
    cues = [cue(0, 0.0, 3.0, text)]
    issues = validate_rules(cues)
    assert TOO_MANY_WORDS in codes(issues)
    assert all(issue.requires_manual_confirmation for issue in issues)
    # The validator reports; it must not rewrite or shorten the cue.
    assert cues[0].text == text
    assert cues[0].word_count == 13


def test_cue_longer_than_three_seconds_is_flagged():
    issues = validate_rules([cue(0, 0.0, 3.2, "one two")])
    assert CUE_TOO_LONG in codes(issues)


def test_very_short_cue_is_flagged_for_manual_confirmation():
    issues = validate_rules([cue(0, 0.0, 0.2, "one two three")])
    assert CUE_TOO_SHORT in codes(issues)
    assert RATE_TOO_FAST in codes(issues)


def test_slow_speech_rate_is_flagged():
    issues = validate_rules([cue(0, 0.0, 2.0, "hello")])
    assert RATE_TOO_SLOW in codes(issues)
    assert all(issue.requires_manual_confirmation for issue in issues)


def test_empty_cue_text_is_flagged():
    assert EMPTY_TEXT in codes(validate_rules([cue(0, 0.0, 1.0, "   ")]))


def test_manual_review_items_excludes_machine_only_issues():
    machine = campaign = validate_timeline([cue(0, math.nan, 1.0)])
    human = validate_rules([cue(1, 0.0, 4.0, "one two")])
    assert manual_review_items(machine) == []
    assert len(manual_review_items(human)) == len(human)


# ---------------------------------------------------------------------------
# LOC-02 — English line breaking and safe area
# ---------------------------------------------------------------------------

def test_wrap_breaks_at_word_boundaries_within_line_limit():
    text = "This is a longer subtitle line that should wrap onto two lines"
    lines = wrap_english_text(text, max_chars_per_line=42)
    assert " ".join(lines) == text
    assert len(lines) > 1
    assert all(len(line) <= 42 for line in lines)


def test_wrap_keeps_an_overlong_word_whole():
    word = "pneumonoultramicroscopicsilicovolcanoconiosis"
    assert wrap_english_text(word, max_chars_per_line=20) == [word]
    assert STYLE_LINE_TOO_LONG in codes(validate_style(word))


def test_style_flags_too_many_lines():
    text = " ".join(["alpha"] * 30)
    assert STYLE_TOO_MANY_LINES in codes(validate_style(text))


def test_style_flags_orphan_final_word():
    # The first line fills all 42 characters, pushing the last word alone.
    text = "x" * 20 + " " + "y" * 21 + " z"
    lines = wrap_english_text(text, max_chars_per_line=42)
    assert len(lines) == 2 and lines[-1] == "z"
    assert STYLE_ORPHAN_WORD in codes(validate_style(text))


def test_style_empty_text():
    assert EMPTY_TEXT in codes(validate_style("  "))


def test_default_style_matches_initial_thresholds():
    assert SubtitleStyle().max_words_per_cue == 12
    assert SubtitleStyle().max_duration_s == 3.0


def test_rect_inside_provisional_safe_area_only_needs_calibration():
    rect = SubtitleRect(x=0.1, y=0.5, width=0.6, height=0.15)
    issues = validate_safe_area(rect)
    assert codes(issues) == [SAFE_AREA_PRESET_UNCALIBRATED]
    assert issues[0].requires_manual_confirmation is True


def test_rect_outside_safe_area_is_violation():
    # Extends into TikTok's right interaction rail.
    rect = SubtitleRect(x=0.6, y=0.5, width=0.3, height=0.15)
    assert SAFE_AREA_VIOLATION in codes(validate_safe_area(rect))


def test_rect_below_safe_area_is_violation():
    # Extends into the bottom caption/music band.
    rect = SubtitleRect(x=0.1, y=0.7, width=0.6, height=0.1)
    assert SAFE_AREA_VIOLATION in codes(validate_safe_area(rect))


def test_invalid_rect_is_rejected_without_calibration_noise():
    issues = validate_safe_area(SubtitleRect(x=0.1, y=0.5, width=0, height=0.1))
    assert codes(issues) == [SAFE_AREA_INVALID_RECT]


def test_calibrated_safe_area_passes_cleanly():
    calibrated = SafeArea(
        name="tiktok-9x16",
        version="m1-calibrated",
        x_min=0.05, x_max=0.80, y_min=0.10, y_max=0.75,
        calibrated=True,
    )
    rect = SubtitleRect(x=0.1, y=0.5, width=0.6, height=0.15)
    assert validate_safe_area(rect, calibrated) == []
    assert TIKTOK_9X16.calibrated is False


# ---------------------------------------------------------------------------
# AUD-07 — loudness gate
# ---------------------------------------------------------------------------

def test_target_loudness_and_peak_pass():
    report = evaluate_loudness(LoudnessMeasurement(-14.0, -1.0))
    assert report.passed
    assert report.violations == ()


@pytest.mark.parametrize("lufs", [MIN_LUFS, MAX_LUFS, -14.0])
def test_loudness_bounds_are_inclusive(lufs):
    assert evaluate_loudness(LoudnessMeasurement(lufs, -3.0)).passed


@pytest.mark.parametrize("lufs", [MIN_LUFS - 0.1, MAX_LUFS + 0.1])
def test_loudness_outside_pm_one_lu_fails(lufs):
    report = evaluate_loudness(LoudnessMeasurement(lufs, -3.0))
    assert not report.passed
    assert [v.code for v in report.violations] == [LOUDNESS_OUT_OF_RANGE]


def test_true_peak_above_minus_one_fails():
    report = evaluate_loudness(
        LoudnessMeasurement(-14.0, MAX_TRUE_PEAK_DBTP + 0.1))
    assert not report.passed
    assert [v.code for v in report.violations] == [TRUE_PEAK_TOO_HIGH]


def test_non_finite_measurement_fails_instead_of_silently_passing():
    report = evaluate_loudness(LoudnessMeasurement(math.nan, -2.0))
    assert not report.passed
    assert [v.code for v in report.violations] == [INVALID_MEASUREMENT]


# ---------------------------------------------------------------------------
# AUD-08 — mix spec
# ---------------------------------------------------------------------------

def test_mix_without_bgm_is_not_applicable():
    report = evaluate_mix(MixMeasurement(dialogue_rms_dbfs=-12.0))
    assert report.not_applicable
    assert report.status == "N/A"
    assert report.passed
    assert report.separation_db is None


def test_mix_with_clear_dialogue_passes():
    report = evaluate_mix(MixMeasurement(-12.0, bgm_rms_dbfs=-20.0,
                                         ducking_depth_db=4.0))
    assert report.status == "PASS"
    assert report.separation_db == pytest.approx(8.0)


def test_bgm_masking_dialogue_fails():
    report = evaluate_mix(MixMeasurement(-15.0, bgm_rms_dbfs=-20.0))
    assert report.status == "FAIL"
    assert [v.code for v in report.violations] == [BGM_MASKS_DIALOGUE]


def test_insufficient_ducking_fails():
    report = evaluate_mix(MixMeasurement(-12.0, bgm_rms_dbfs=-20.0,
                                         ducking_depth_db=1.0))
    assert [v.code for v in report.violations] == [INSUFFICIENT_DUCKING]


def test_dialogue_below_minimum_level_fails():
    report = evaluate_mix(MixMeasurement(-32.0, bgm_rms_dbfs=-40.0))
    assert DIALOGUE_TOO_QUIET in [v.code for v in report.violations]


def test_mix_spec_declares_thresholds_and_na_rule():
    spec = mix_spec()
    assert spec["spec_version"] == "AUD-08.v1"
    assert spec["bgm_absent_status"] == "N/A"
    assert spec["min_dialogue_bgm_separation_db"] == 6.0
    assert spec["min_ducking_depth_db"] == 3.0


# ---------------------------------------------------------------------------
# AUD-09 — voices table
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path):
    return VoiceRegistry(AssetLibrary(tmp_path / "library"))


def test_register_and_get_voice_roundtrip(registry):
    voice_id = registry.register(
        "v_nova_en", "elevenlabs", "en-US", "warm low narrator",
        preview_url="auditions/nova.mp3", tags=["Female", "Narrator"])
    assert voice_id == "v_nova_en"
    voice = registry.get("v_nova_en")
    assert voice.provider == "elevenlabs"
    assert voice.language == "en-US"
    assert voice.description == "warm low narrator"
    assert voice.preview_url == "auditions/nova.mp3"
    assert voice.tags == ("female", "narrator")


def test_duplicate_voice_is_rejected_not_overwritten(registry):
    registry.register("v_nova_en", "elevenlabs", "en-US", "warm narrator")
    with pytest.raises(DuplicateVoiceError):
        registry.register("v_nova_en", "other", "en-GB", "replacement")
    assert registry.get("v_nova_en").description == "warm narrator"


@pytest.mark.parametrize("kwargs", [
    {"voice_id": "bad id", "provider": "p", "language": "en-US", "description": "d"},
    {"voice_id": "v1", "provider": "", "language": "en-US", "description": "d"},
    {"voice_id": "v1", "provider": "p", "language": "english", "description": "d"},
    {"voice_id": "v1", "provider": "p", "language": "en-US", "description": ""},
    {"voice_id": "v1", "provider": "p", "language": "en-US", "description": "d",
     "preview_url": "   "},
])
def test_invalid_voice_fields_are_rejected(registry, kwargs):
    with pytest.raises(InvalidVoiceError):
        registry.register(**kwargs)


def test_get_unknown_voice_raises(registry):
    with pytest.raises(VoiceNotFoundError):
        registry.get("missing")


def test_query_filters_by_language_provider_and_tags(registry):
    registry.register("v_nova_en", "elevenlabs", "en-US", "warm narrator",
                      tags=["narrator"])
    registry.register("v_atlas_en", "openai", "en-US", "bright host",
                      tags=["host"])
    registry.register("v_luna_en_gb", "elevenlabs", "en-GB", "soft narrator",
                      tags=["narrator"])

    assert [v.voice_id for v in registry.query(provider="elevenlabs")] == [
        "v_luna_en_gb", "v_nova_en"]
    assert [v.voice_id for v in registry.query(language="en-US")] == [
        "v_atlas_en", "v_nova_en"]
    assert [v.voice_id for v in registry.query(
        provider="elevenlabs", tags=["narrator"])] == ["v_luna_en_gb", "v_nova_en"]


def test_text_query_hits_matching_voice(registry):
    registry.register("v_nova_en", "elevenlabs", "en-US", "warm narrator")
    registry.register("v_atlas_en", "openai", "en-US", "bright energetic host")
    hits = registry.query(text="narrator")
    assert [v.voice_id for v in hits] == ["v_nova_en"]


def test_voices_are_reused_across_episodes(tmp_path):
    library_dir = tmp_path / "library"
    first = VoiceRegistry(AssetLibrary(library_dir))
    first.register("v_nova_en", "elevenlabs", "en-US", "warm narrator")

    second = VoiceRegistry(AssetLibrary(library_dir))
    assert [v.voice_id for v in second.all()] == ["v_nova_en"]
    assert second.get("v_nova_en").provider == "elevenlabs"


def test_retired_voice_is_excluded_from_query_but_still_gettable(registry):
    registry.register("v_nova_en", "elevenlabs", "en-US", "warm narrator")
    registry.library.retire("v_nova_en")
    assert registry.query() == []
    assert [v.voice_id for v in registry.query(include_retired=True)] == ["v_nova_en"]
    assert registry.get("v_nova_en").voice_id == "v_nova_en"


def test_non_voice_assets_do_not_appear_in_voice_queries(registry):
    registry.library.register(DEFAULT_STORAGE_KIND, "ch_ling",
                              {"asset_type": "character", "name": "Ling"})
    registry.register("v_nova_en", "elevenlabs", "en-US", "warm narrator")
    assert [v.voice_id for v in registry.query()] == ["v_nova_en"]


def test_preview_url_is_optional(registry):
    registry.register("v_nova_en", "elevenlabs", "en-US", "warm narrator")
    assert registry.get("v_nova_en").preview_url is None