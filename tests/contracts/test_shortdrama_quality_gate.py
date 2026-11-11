"""WP-T contract tests — ORC-08 quality gate + ORC-09 invalidation.

Guards:
- seven checks with machine/human verdicts recorded separately, each with its
  own threshold version;
- UNKNOWN/N/A semantics: required FAIL/UNKNOWN block, N/A with reason does not,
  machine UNKNOWN never becomes PASS without recorded human evidence;
- hook/identity/composition cannot be released on machine evidence alone;
- upstream/binding/threshold changes mark the affected downstream artifacts,
  reviews and gates stale and re-queue affected shots, while unaffected nodes
  are retained;
- re-runs add versions and never overwrite history.
"""
import pytest

from lib.quality_gate import (
    QualityGate, QualityPolicy, default_thresholds, effective_status,
    evaluate_machine, CheckRecord, QualityGateError,
    CHECKS, PASS, FAIL, UNKNOWN, NA,
)
from lib.invalidation import InvalidationLog, InvalidationError, STALE


@pytest.fixture
def gate(tmp_path):
    return QualityGate("drama", "ep01", projects_dir=tmp_path)


def _pass_all(gate, *, human=True):
    gate.report_machine("aspect_ratio", {"width": 1080, "height": 1920})
    gate.report_machine("safe_area", {
        "face_detection_available": True,
        "subtitle_rect": [0.10, 0.70, 0.60, 0.08],
        "face_bboxes": [[0.30, 0.20, 0.20, 0.20]],
    })
    gate.report_machine("loudness", {"integrated_lufs": -14.2, "true_peak_dbtp": -1.5})
    gate.report_machine("hook", {"hook_at_s": 1.0, "hook_candidates": 6})
    gate.report_machine("identity", {
        "detector_available": True, "detection_coverage": 0.9,
        "face_similarity": 0.82,
    })
    gate.report_machine("subtitles", {"cues": [
        {"text": "hello there friend", "start_s": 0.0, "end_s": 1.5},
    ]})
    gate.report_machine("composition_background", {})
    if human:
        for check_id in ("hook", "identity", "composition_background"):
            gate.record_human(check_id, PASS, evidence="owner watched the shot",
                              confirmed_by="kevin")


# ---------------------------------------------------------------------------
# ORC-08: seven checks, machine vs human separation, threshold versions
# ---------------------------------------------------------------------------

def test_gate_covers_all_seven_checks(gate):
    assert set(CHECKS) == {
        "aspect_ratio", "safe_area", "loudness", "hook",
        "identity", "subtitles", "composition_background",
    }
    _pass_all(gate)
    evaluated = gate.evaluate()
    assert set(evaluated["checks"]) == set(CHECKS)
    assert all("machine" in c and "human" in c for c in evaluated["checks"].values())


def test_machine_and_human_verdicts_recorded_separately_with_threshold_versions(gate):
    _pass_all(gate)
    checks = gate.checks()

    for check_id in ("hook", "identity", "composition_background"):
        rec = checks[check_id]
        assert rec.machine_status in (PASS, UNKNOWN)
        assert rec.machine_threshold_version
        assert rec.human_status == PASS
        assert rec.human_evidence and rec.human_confirmed_by == "kevin"
        # machine and human threshold versions are independently recorded
        assert rec.human_threshold_version == rec.machine_threshold_version

    hook = checks["hook"]
    assert hook.machine_status == PASS          # structure only
    assert hook.human_status == PASS            # content effectiveness human
    assert gate.evaluate()["checks"]["hook"]["machine"]["evidence"]


def test_machine_pass_on_human_required_check_cannot_release(gate):
    _pass_all(gate, human=False)
    evaluated = gate.evaluate()
    blocking = set(evaluated["blocking_checks"])
    assert {"hook", "identity", "composition_background"} <= blocking
    assert evaluated["verdict"] == "BLOCKED"
    assert gate.can_release() is False


def test_human_confirmation_unblocks_human_required_checks(gate):
    _pass_all(gate, human=False)
    assert gate.can_release() is False
    for check_id in ("hook", "identity", "composition_background"):
        gate.record_human(check_id, PASS, evidence="reviewed contact sheet",
                          confirmed_by="kevin")
    assert gate.can_release() is True
    assert gate.evaluate()["verdict"] == PASS


def test_unknown_machine_result_is_never_pass(gate):
    gate.report_machine("identity", {"detector_available": False})
    rec = gate.checks()["identity"]
    assert rec.machine_status == UNKNOWN
    assert gate.evaluate()["checks"]["identity"]["effective_status"] == UNKNOWN

    with pytest.raises(QualityGateError):
        # override of UNKNOWN requires evidence
        gate.record_human("identity", PASS, evidence="", confirmed_by="kevin")


def test_human_override_of_unknown_requires_and_records_evidence(gate):
    gate.report_machine("identity", {"detector_available": False})
    gate.record_human("identity", PASS, evidence="manual identity check on 1080p",
                      confirmed_by="kevin")
    status = gate.evaluate()["checks"]["identity"]["effective_status"]
    assert status == PASS


def test_human_cannot_override_machine_fail(gate):
    gate.report_machine("identity", {
        "detector_available": True, "detection_coverage": 0.9,
        "face_similarity": 0.40,
    })
    assert gate.checks()["identity"].machine_status == FAIL
    with pytest.raises(QualityGateError):
        gate.record_human("identity", PASS, evidence="looks fine to me",
                          confirmed_by="kevin")


def test_required_fail_blocks_release(gate):
    gate.report_machine("aspect_ratio", {"width": 1920, "height": 1080})
    assert gate.checks()["aspect_ratio"].machine_status == FAIL
    evaluated = gate.evaluate()
    assert "aspect_ratio" in evaluated["blocking_checks"]
    assert evaluated["verdict"] == "BLOCKED"


def test_required_unknown_blocks_release(gate):
    gate.report_machine("loudness", {})  # measurement missing
    assert gate.checks()["loudness"].machine_status == UNKNOWN
    assert "loudness" in gate.blocking_checks()
    assert gate.can_release() is False


def test_na_with_reason_does_not_block(gate):
    _pass_all(gate)
    gate.record_na("composition_background", "no background plate in this shot")
    assert gate.checks()["composition_background"].machine_status == NA
    assert gate.evaluate()["checks"]["composition_background"]["effective_status"] == NA
    assert gate.can_release() is True


def test_na_requires_reason(gate):
    with pytest.raises(QualityGateError):
        gate.record_na("subtitles", "   ")
    gate.report_machine("subtitles", {"na_reason": ""})  # blank -> normal UNKNOWN path
    assert gate.checks()["subtitles"].machine_status == UNKNOWN


def test_human_na_requires_evidence(gate):
    gate.report_machine("hook", {"hook_at_s": 1.0, "hook_candidates": 6})
    with pytest.raises(QualityGateError):
        gate.record_human("hook", NA, evidence="", confirmed_by="kevin")
    gate.record_human("hook", NA, evidence="episode has no hook window by design",
                      confirmed_by="kevin")
    assert gate.evaluate()["checks"]["hook"]["effective_status"] == NA


def test_optional_check_failure_does_not_block(tmp_path):
    policy = QualityPolicy(required=frozenset(CHECKS) - {"composition_background"})
    gate = QualityGate("drama", "ep01", policy=policy, projects_dir=tmp_path)
    _pass_all(gate)
    gate.report_machine_result("composition_background", FAIL,
                               evidence="background contradicts the scene")
    evaluated = gate.evaluate()
    assert evaluated["checks"]["composition_background"]["effective_status"] == FAIL
    assert evaluated["checks"]["composition_background"]["blocking"] is False
    assert gate.can_release() is True


# ---------------------------------------------------------------------------
# ORC-08 machine checks accept already-measured inputs
# ---------------------------------------------------------------------------

def test_aspect_ratio_accepts_equivalent_dimensions(gate):
    assert evaluate_machine("aspect_ratio", {"width": 720, "height": 1280},
                            default_thresholds()["aspect_ratio"])[0] == PASS
    assert evaluate_machine("aspect_ratio", {"width": 1080, "height": 1080},
                            default_thresholds()["aspect_ratio"])[0] == FAIL


def test_safe_area_checks_subtitle_and_face_bbox(gate):
    spec = default_thresholds()["safe_area"]
    good = {
        "face_detection_available": True,
        "subtitle_rect": [0.1, 0.65, 0.6, 0.1],
        "face_bboxes": [[0.3, 0.2, 0.2, 0.2]],
    }
    assert evaluate_machine("safe_area", good, spec)[0] == PASS

    bad_subtitle = {**good, "subtitle_rect": [0.1, 0.65, 0.8, 0.2]}
    assert evaluate_machine("safe_area", bad_subtitle, spec)[0] == FAIL

    bad_face = {**good, "face_bboxes": [[0.75, 0.2, 0.2, 0.2]]}
    assert evaluate_machine("safe_area", bad_face, spec)[0] == FAIL

    no_detector = {**good, "face_detection_available": False}
    assert evaluate_machine("safe_area", no_detector, spec)[0] == UNKNOWN


def test_loudness_thresholds(gate):
    spec = default_thresholds()["loudness"]
    assert evaluate_machine("loudness", {"integrated_lufs": -14.4,
                                         "true_peak_dbtp": -1.5}, spec)[0] == PASS
    assert evaluate_machine("loudness", {"integrated_lufs": -10.0,
                                         "true_peak_dbtp": -2.0}, spec)[0] == FAIL
    assert evaluate_machine("loudness", {"integrated_lufs": -14.0,
                                         "true_peak_dbtp": -0.5}, spec)[0] == FAIL
    assert evaluate_machine("loudness", {"integrated_lufs": -14.0}, spec)[0] == UNKNOWN


def test_subtitle_limits(gate):
    spec = default_thresholds()["subtitles"]
    ok = {"cues": [{"text": "one two three", "start_s": 0, "end_s": 0.8}]}
    too_many = {"cues": [{"text": " ".join(["w"] * 13), "start_s": 0, "end_s": 1.0}]}
    too_long = {"cues": [{"text": "short", "start_s": 0, "end_s": 4.0}]}
    empty = {"cues": []}
    assert evaluate_machine("subtitles", ok, spec)[0] == PASS
    assert evaluate_machine("subtitles", too_many, spec)[0] == FAIL
    assert evaluate_machine("subtitles", too_long, spec)[0] == FAIL
    assert evaluate_machine("subtitles", empty, spec)[0] == FAIL


def test_machine_check_identity_unknown_paths(gate):
    spec = default_thresholds()["identity"]
    assert evaluate_machine("identity", {
        "detector_available": True, "detection_coverage": 0.1,
        "face_similarity": 0.9}, spec)[0] == UNKNOWN
    assert evaluate_machine("identity", {
        "detector_available": True, "detection_coverage": 0.9,
        "multi_face": True, "face_similarity": 0.9}, spec)[0] == UNKNOWN
    assert evaluate_machine("identity", {
        "detector_available": True, "detection_coverage": 0.9,
        "face_similarity": 0.76}, spec)[0] == PASS
    assert evaluate_machine("identity", {
        "detector_available": True, "detection_coverage": 0.9,
        "face_similarity": 0.50}, spec)[0] == FAIL


def test_precomputed_machine_result_is_accepted(gate):
    gate.report_machine_result("loudness", PASS, evidence="assembly loudnorm report",
                               measurement={"integrated_lufs": -14.0,
                                            "true_peak_dbtp": -1.2},
                               threshold_version="loudness-ebur128-v1")
    rec = gate.checks()["loudness"]
    assert rec.machine_status == PASS
    assert rec.machine_threshold_version == "loudness-ebur128-v1"
    assert rec.machine_evidence == "assembly loudnorm report"


def test_precomputed_result_rejects_pass_without_evidence(gate):
    with pytest.raises(QualityGateError):
        gate.report_machine_result("aspect_ratio", PASS, evidence="")


# ---------------------------------------------------------------------------
# ORC-09: threshold version change makes machine evidence stale
# ---------------------------------------------------------------------------

def test_threshold_version_change_stales_machine_result_and_blocks(tmp_path):
    gate = QualityGate("drama", "ep01", projects_dir=tmp_path)
    _pass_all(gate)
    assert gate.can_release() is True

    bumped = default_thresholds()
    from lib.quality_gate import ThresholdSpec
    bumped["loudness"] = ThresholdSpec("loudness", "loudness-ebur128-v2", {
        "target_lufs": -14.0, "tolerance_lu": 0.5, "max_true_peak_dbtp": -1.0,
    })
    policy_v2 = QualityPolicy(thresholds=bumped)
    regated = QualityGate("drama", "ep01", policy=policy_v2, projects_dir=tmp_path)

    evaluated = regated.evaluate()
    loudness = evaluated["checks"]["loudness"]
    assert loudness["machine"]["stale"] is True
    assert loudness["machine"]["threshold_version"] == "loudness-ebur128-v1"
    assert loudness["effective_status"] == UNKNOWN
    assert "loudness" in evaluated["blocking_checks"]
    assert evaluated["verdict"] == "BLOCKED"

    # re-measure under the new threshold version -> no longer stale
    regated.report_machine("loudness", {"integrated_lufs": -14.2,
                                        "true_peak_dbtp": -1.5})
    assert regated.checks()["loudness"].machine_threshold_version == "loudness-ebur128-v2"
    assert regated.evaluate()["checks"]["loudness"]["machine"]["stale"] is False

    # append-only history keeps the old v1 measurement for audit
    versions = {ev["threshold_version"] for ev in regated.history()
                if ev.get("check_id") == "loudness"}
    assert {"loudness-ebur128-v1", "loudness-ebur128-v2"} <= versions


def test_effective_status_rules_unit():
    rec = CheckRecord("identity", machine_status=UNKNOWN, human_status=PASS)
    assert effective_status(rec, human_required=True) == PASS
    assert effective_status(rec, human_required=True, stale=True) == UNKNOWN
    rec_fail = CheckRecord("identity", machine_status=FAIL, human_status=PASS)
    assert effective_status(rec_fail, human_required=True) == FAIL
    rec_na = CheckRecord("aspect_ratio", machine_status=NA)
    assert effective_status(rec_na, human_required=False, stale=True) == NA
    rec_hook = CheckRecord("hook", machine_status=PASS)
    assert effective_status(rec_hook, human_required=True) == UNKNOWN


# ---------------------------------------------------------------------------
# ORC-09: dependency invalidation
# ---------------------------------------------------------------------------

@pytest.fixture
def graph(tmp_path):
    g = InvalidationLog("drama", "ep01", projects_dir=tmp_path)
    g.register_node("script:ep01", "script", payload={"path": "script.json"})
    g.register_node("keyframe:sh1", "keyframe")
    g.register_node("shot_media:sh1", "shot_media")
    g.register_node("brief:ep01", "brief")
    g.add_dependency("script:ep01", "keyframe:sh1")
    g.add_dependency("keyframe:sh1", "shot_media:sh1")
    g.register_review("r1", targets=["shot_media:sh1"])
    return g


def test_upstream_change_marks_transitive_downstream_stale(graph):
    report = graph.invalidate("script:ep01", reason="script revised")
    assert set(report["affected"]) == {
        "keyframe:sh1", "shot_media:sh1", "review:r1"}
    assert graph.status_of("shot_media:sh1") == STALE
    assert graph.status_of("review:r1") == STALE
    # unrelated node retained and untouched
    assert graph.status_of("brief:ep01") == "CURRENT"
    assert "brief:ep01" in report["retained"]


def test_rerun_adds_version_and_never_overwrites_history(graph):
    graph.invalidate("script:ep01", new_version="v2",
                     reason="script revised", payload={"path": "script_v2.json"})
    old = graph.version("script:ep01", "v1")
    new = graph.version("script:ep01", "v2")
    assert old.payload == {"path": "script.json"}
    assert old.superseded_by == "v2"
    assert new.payload == {"path": "script_v2.json"}
    assert [nv.version for nv in graph.history("script:ep01")] == ["v1", "v2"]


def test_affected_artifact_rerun_clears_stale_but_keeps_old_version(graph):
    graph.invalidate("script:ep01", reason="script revised")
    assert graph.status_of("keyframe:sh1") == STALE
    graph.register_node("keyframe:sh1", "keyframe", version="v2",
                        payload={"rendered": True})
    assert graph.status_of("keyframe:sh1") == "CURRENT"
    assert graph.status_of("shot_media:sh1") == STALE  # still needs re-run
    v1 = graph.version("keyframe:sh1", "v1")
    assert v1.stale is True and v1.superseded_by == "v2"


def test_quality_gate_node_goes_stale_with_its_targets(graph):
    graph.register_quality_gate("gate-1", targets=["script:ep01"])
    graph.invalidate("script:ep01", reason="script revised")
    assert graph.status_of("quality_gate:gate-1") == STALE


def test_binding_change_stales_old_binding_scores_and_requeues_shots(graph):
    graph.register_identity_score("sh1", binding_id="molica", threshold_version="a4-v1",
                                  score=0.81)
    graph.register_identity_score("sh2", binding_id="molica", threshold_version="a4-v1",
                                  score=0.90)
    graph.register_identity_score("sh3", binding_id="holopix", threshold_version="a4-v1",
                                  score=0.88)
    graph.register_review("r_sh1", targets=["identity_score:sh1"])

    report = graph.binding_changed("molica", "holopix", reason="molica quota exhausted")

    assert report["requeue_shots"] == ["sh1", "sh2"]
    assert graph.status_of("identity_score:sh1") == UNKNOWN
    assert graph.status_of("identity_score:sh2") == UNKNOWN
    assert graph.status_of("identity_score:sh3") == PASS
    assert graph.status_of("review:r_sh1") == STALE
    assert "identity_score:sh3" in report["retained"]
    assert set(report["stale_identity_scores"]) == {
        "identity_score:sh1", "identity_score:sh2"}


def test_threshold_version_change_stales_only_scores_from_old_version(graph):
    graph.register_identity_score("sh1", binding_id="molica",
                                  threshold_version="a4-v1", score=0.81)
    graph.register_identity_score("sh2", binding_id="molica",
                                  threshold_version="a4-v2", score=0.81)

    report = graph.threshold_changed("identity", "a4-v1", "a4-v3",
                                     reason="M1 calibration")

    assert report["requeue_shots"] == ["sh1"]
    assert graph.status_of("identity_score:sh1") == UNKNOWN
    assert graph.status_of("identity_score:sh2") == PASS
    assert graph.version("threshold:identity", "a4-v3").payload["threshold_version"] == "a4-v3"


def test_non_identity_threshold_change_does_not_requeue_identity_scores(graph):
    graph.register_identity_score("sh1", binding_id="molica",
                                  threshold_version="a4-v1", score=0.81)
    report = graph.threshold_changed("loudness", "loud-v1", "loud-v2")
    assert report["requeue_shots"] == []
    assert graph.status_of("identity_score:sh1") == PASS


def test_invalidating_unknown_node_raises(graph):
    with pytest.raises(InvalidationError):
        graph.invalidate("script:missing")


def test_summary_exposes_stale_and_requeue(graph):
    graph.register_identity_score("sh1", binding_id="molica",
                                  threshold_version="a4-v1", score=0.81)
    graph.binding_changed("molica", "holopix")
    summary = graph.summary()
    assert summary["requeue_shots"] == ["sh1"]
    assert "identity_score:sh1" in summary["stale_nodes"]
    assert summary["nodes"]["identity_score:sh1"]["status"] == UNKNOWN