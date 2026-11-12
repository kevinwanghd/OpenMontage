"""Contract tests for the AUD-05 word-level subtitle alignment adapter.

All inputs are synthetic word timings; no audio is read, no aligner model is
loaded and no network is used (the session-wide socket guard enforces the
latter). The tests pin the central distinction of AUD-05: TTS-provided
timings and forced alignment are separate, named sources, and ordinary
transcription must never be reported as forced alignment.
"""
import math

import pytest

from lib.subtitle import (
    CUE_TOO_LONG,
    CUE_TOO_SHORT,
    EMPTY_TEXT,
    NOT_MONOTONIC,
    OUT_OF_RANGE,
    TOO_MANY_WORDS,
)
from lib.subtitle_alignment import (
    CALIBRATION_PENDING,
    CALIBRATION_VERIFIED,
    CODE_ALIGNER_FAILED,
    CODE_BLOCKED_NO_ALIGNER,
    CODE_EMPTY_ALIGNMENT,
    CODE_INVALID_WORD_TIMING,
    CODE_MISSING_WORD_TIMESTAMPS,
    CODE_NO_WORDS,
    CODE_TRANSCRIPTION_NOT_FORCED,
    CODE_UNKNOWN_METHOD,
    METHOD_FORCED,
    METHOD_TTS,
    METHOD_UNKNOWN,
    STATUS_ALIGNED,
    STATUS_BLOCKED,
    STATUS_REVIEW_REQUIRED,
    STATUS_UNKNOWN,
    align_from_forced_alignment,
    align_from_tts_timestamps,
)


def words(*triples):
    return [{"word": text, "start": start, "end": end}
            for text, start, end in triples]


def codes(result):
    return [issue.code for issue in result.issues]


def tts_segments(*triples, index=0, text="", start=0.0, end=0.0):
    entry = {"id": index, "start": start, "end": end, "text": text}
    entry["words"] = words(*triples)
    return [entry]


class DeclaredAligner:
    """A minimal forced aligner double that satisfies the declared contract."""

    alignment_method = METHOD_FORCED

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, audio_path, transcript):
        self.calls.append((audio_path, transcript))
        return self.payload


# ---------------------------------------------------------------------------
# TTS timestamps — happy path and grouping
# ---------------------------------------------------------------------------

def test_tts_words_produce_monotonic_cues_within_audio():
    result = align_from_tts_timestamps(
        tts_segments(
            ("Hi", 0.0, 0.3), ("there", 0.3, 0.7),
            ("how", 0.7, 1.1), ("are", 1.1, 1.5), ("you", 1.5, 2.0),
            text="Hi there how are you", start=0.0, end=2.0,
        ),
        audio_duration_s=2.0,
    )
    assert result.report.status == STATUS_ALIGNED
    assert result.issues == ()
    assert [cue.text for cue in result.cues] == ["Hi there how are you"]
    assert result.cues[0].start == 0.0
    assert result.cues[0].end == 2.0
    assert result.cues[0].index == 0


def test_tts_cues_split_at_word_and_duration_limits():
    twelve = [
        (f"w{i}", i * 0.5, (i + 1) * 0.5) for i in range(12)
    ]
    result = align_from_tts_timestamps(tts_segments(*twelve), audio_duration_s=6.0)
    assert len(result.cues) == 2
    assert all(cue.word_count <= 12 for cue in result.cues)
    assert all(cue.duration <= 3.0 for cue in result.cues)
    assert TOO_MANY_WORDS not in codes(result)
    assert CUE_TOO_LONG not in codes(result)
    assert result.report.status == STATUS_ALIGNED


def test_multi_word_phrase_chunk_over_limit_is_flagged_not_truncated():
    text = " ".join(f"w{i}" for i in range(13))
    phrase = {"words": [{"word": text, "start": 0.0, "end": 3.0}]}
    result = align_from_tts_timestamps(phrase, audio_duration_s=3.0)
    assert TOO_MANY_WORDS in codes(result)
    assert result.cues[0].text == text  # no rewrite, no truncation
    assert result.cues[0].word_count == 13
    assert result.report.status == STATUS_REVIEW_REQUIRED


def test_tts_accepts_flat_word_list():
    result = align_from_tts_timestamps(
        words(("one", 0.0, 0.4), ("two", 0.4, 0.9)), audio_duration_s=1.0)
    assert result.report.status == STATUS_ALIGNED
    assert result.cues[0].text == "one two"


# ---------------------------------------------------------------------------
# TTS timestamps — validation, no silent fixes
# ---------------------------------------------------------------------------

def test_single_word_longer_than_three_seconds_is_flagged():
    result = align_from_tts_timestamps(
        tts_segments(("Sustained", 0.0, 3.2)), audio_duration_s=3.2)
    assert CUE_TOO_LONG in codes(result)
    assert result.cues[0].end == 3.2
    assert result.report.status == STATUS_REVIEW_REQUIRED


def test_short_display_cue_is_flagged():
    result = align_from_tts_timestamps(
        tts_segments(("Hi", 0.0, 0.2)), audio_duration_s=1.0)
    assert CUE_TOO_SHORT in codes(result)
    assert result.report.status == STATUS_REVIEW_REQUIRED


def test_non_monotonic_word_timeline_is_rejected():
    result = align_from_tts_timestamps(
        tts_segments(("second", 1.0, 1.5), ("first", 0.5, 0.9)),
        audio_duration_s=2.0,
    )
    assert NOT_MONOTONIC in codes(result)
    assert [cue.text for cue in result.cues] == ["second", "first"]
    assert result.report.status == STATUS_REVIEW_REQUIRED


def test_cue_beyond_audio_duration_is_rejected():
    result = align_from_tts_timestamps(
        tts_segments(("late", 1.5, 2.1)), audio_duration_s=2.0)
    assert OUT_OF_RANGE in codes(result)
    assert result.report.status == STATUS_REVIEW_REQUIRED


def test_negative_start_is_out_of_range():
    result = align_from_tts_timestamps(
        tts_segments(("early", -0.1, 0.5)), audio_duration_s=1.0)
    assert OUT_OF_RANGE in codes(result)


def test_invalid_word_timing_is_reported_and_other_words_kept():
    payload = {"words": [
        {"word": "valid", "start": 0.0, "end": 1.0},
        {"word": "broken", "start": math.nan, "end": 1.5},
    ]}
    result = align_from_tts_timestamps(payload, audio_duration_s=2.0)
    assert CODE_INVALID_WORD_TIMING in codes(result)
    assert [cue.text for cue in result.cues] == ["valid"]
    assert result.report.status == STATUS_REVIEW_REQUIRED


def test_empty_word_list_is_blocked():
    result = align_from_tts_timestamps([], audio_duration_s=1.0)
    assert result.report.status == STATUS_BLOCKED
    assert result.cues == ()
    assert CODE_NO_WORDS in codes(result)


def test_segment_level_timestamps_are_blocked_not_promoted():
    segments = [{"id": 0, "start": 0.0, "end": 1.0, "text": "hello world"}]
    result = align_from_tts_timestamps(segments, audio_duration_s=1.0)
    assert result.report.status == STATUS_BLOCKED
    assert result.cues == ()
    assert CODE_MISSING_WORD_TIMESTAMPS in codes(result)


def test_tts_report_names_method_error_and_calibration():
    result = align_from_tts_timestamps(
        tts_segments(("one", 0.0, 0.5), ("two", 0.5, 1.0)),
        audio_duration_s=1.0,
    )
    report = result.report
    assert report.alignment_method == METHOD_TTS
    assert report.alignment_method != METHOD_FORCED
    assert report.calibration_status == CALIBRATION_PENDING
    assert report.tolerance_ms == pytest.approx(200.0)
    # No measured error was supplied: report it as unknown, not as zero.
    assert report.estimated_error_ms is None
    assert "unmeasured" in report.notes
    assert report.as_dict()["cue_count"] == 1


def test_tts_rejects_invalid_audio_duration():
    with pytest.raises(ValueError):
        align_from_tts_timestamps([], audio_duration_s=0.0)
    with pytest.raises(ValueError):
        align_from_tts_timestamps([], audio_duration_s=math.inf)


# ---------------------------------------------------------------------------
# Forced alignment — accepted path
# ---------------------------------------------------------------------------

def test_declared_forced_aligner_produces_cues_and_metadata():
    aligner = DeclaredAligner({
        "alignment_method": METHOD_FORCED,
        "words": words(("hello", 0.0, 0.4), ("world", 0.4, 0.9)),
        "estimated_error_ms": 45.0,
        "calibration_status": CALIBRATION_VERIFIED,
    })
    result = align_from_forced_alignment(
        "dialogue.wav", "hello world", aligner, audio_duration_s=1.0)

    assert aligner.calls == [("dialogue.wav", "hello world")]
    assert result.report.alignment_method == METHOD_FORCED
    assert result.report.status == STATUS_ALIGNED
    assert result.report.estimated_error_ms == pytest.approx(45.0)
    assert result.report.calibration_status == CALIBRATION_VERIFIED
    assert [cue.text for cue in result.cues] == ["hello world"]


def test_forced_alignment_accepts_segment_wrapped_words():
    aligner = DeclaredAligner({"segments": [
        {"start": 0.0, "end": 1.0, "text": "one two",
         "words": words(("one", 0.0, 0.5), ("two", 0.5, 1.0))},
    ]})
    result = align_from_forced_alignment(
        "dialogue.wav", "one two", aligner, audio_duration_s=1.0)
    assert result.report.status == STATUS_ALIGNED
    assert result.cues[0].text == "one two"


def test_forced_alignment_still_validates_audio_range():
    aligner = DeclaredAligner({"words": words(("late", 0.0, 2.1))})
    result = align_from_forced_alignment(
        "dialogue.wav", "late", aligner, audio_duration_s=2.0)
    assert result.report.alignment_method == METHOD_FORCED
    assert OUT_OF_RANGE in codes(result)
    assert result.report.status == STATUS_REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# Forced alignment — blocked / unknown, never a transcription fallback
# ---------------------------------------------------------------------------

def test_missing_aligner_is_blocked_unknown():
    result = align_from_forced_alignment("dialogue.wav", "hello")
    assert result.report.status == STATUS_BLOCKED
    assert result.report.alignment_method == METHOD_UNKNOWN
    assert result.cues == ()
    assert CODE_BLOCKED_NO_ALIGNER in codes(result)
    assert "not a forced alignment" in result.report.notes


def test_unmarked_aligner_is_unknown_not_forced():
    result = align_from_forced_alignment(
        "dialogue.wav", "hello",
        lambda audio, transcript: words(("hello", 0.0, 1.0)),
        audio_duration_s=1.0,
    )
    assert result.report.status == STATUS_UNKNOWN
    assert result.report.alignment_method == METHOD_UNKNOWN
    assert CODE_UNKNOWN_METHOD in codes(result)
    assert result.cues == ()


def test_failing_aligner_is_blocked_with_no_transcription_fallback():
    class ExplodingAligner:
        alignment_method = METHOD_FORCED

        def __call__(self, audio_path, transcript):
            raise RuntimeError("alignment model unavailable")

    result = align_from_forced_alignment(
        "dialogue.wav", "hello", ExplodingAligner(), audio_duration_s=1.0)
    assert result.report.status == STATUS_BLOCKED
    assert result.report.alignment_method == METHOD_UNKNOWN
    assert CODE_ALIGNER_FAILED in codes(result)
    assert "RuntimeError" in result.report.notes
    assert result.cues == ()


@pytest.mark.parametrize("marker", [None, "transcription"])
def test_transcriber_output_is_not_accepted_as_forced_alignment(marker):
    payload = {
        "segments": [{
            "start": 0.0, "end": 1.0, "text": "hello",
            "words": words(("hello", 0.0, 1.0)),
        }],
        "word_timestamps": words(("hello", 0.0, 1.0)),
        "language": "en",
    }

    class AsrLikeAligner:
        def __init__(self):
            if marker is not None:
                self.alignment_method = marker

        def __call__(self, audio_path, transcript):
            return payload

    result = align_from_forced_alignment(
        "dialogue.wav", "hello", AsrLikeAligner(), audio_duration_s=1.0)
    assert result.report.status == STATUS_BLOCKED
    assert result.report.alignment_method == METHOD_UNKNOWN
    assert CODE_TRANSCRIPTION_NOT_FORCED in codes(result)
    assert result.cues == ()
    assert result.report.alignment_method != METHOD_FORCED


def test_forced_aligner_returning_no_words_is_blocked():
    result = align_from_forced_alignment(
        "dialogue.wav", "hello", DeclaredAligner({"words": []}),
        audio_duration_s=1.0)
    assert result.report.status == STATUS_BLOCKED
    assert CODE_EMPTY_ALIGNMENT in codes(result)


def test_forced_aligner_returning_segment_text_only_is_blocked():
    result = align_from_forced_alignment(
        "dialogue.wav", "hello",
        DeclaredAligner({"segments": [
            {"start": 0.0, "end": 1.0, "text": "hello"}]}),
        audio_duration_s=1.0)
    assert result.report.status == STATUS_BLOCKED
    assert CODE_MISSING_WORD_TIMESTAMPS in codes(result)


def test_forced_alignment_rejects_invalid_audio_duration():
    with pytest.raises(ValueError):
        align_from_forced_alignment(
            "dialogue.wav", "hello", DeclaredAligner({"words": []}),
            audio_duration_s=-1.0)