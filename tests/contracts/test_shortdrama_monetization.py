"""WP-AG contract tests: MON-01 monetization eligibility evidence (offline).

MON-01 tracks whether the creator-fund + IAA evidence checklist is complete.
Everything here is deterministic and local: evidence records are supplied as
data and no platform API, region table or threshold is consulted. Tests assert
the hard rule -- any required item that is not MET with complete evidence
(UNKNOWN, UNMET, or MET without an evidence_ref) keeps the report BLOCKED --
and that unknown external facts stay UNKNOWN instead of being invented.
"""
from __future__ import annotations

import pytest

from lib.monetization_eligibility import (
    BLOCKED,
    DISCLAIMER,
    ELIGIBLE_READY,
    MET,
    PROGRAMS,
    REQUIRED_ITEM_NAMES,
    UNKNOWN,
    UNMET,
    EligibilityBlocked,
    MonetizationEligibilityError,
    build_evidence_template,
    evaluate_eligibility,
)

EXTERNAL_FACT_ITEMS = ("region_eligibility", "fan_or_view_threshold")


def _ready_item(**overrides):
    item = {
        "status": MET,
        "evidence_ref": "evidence/monetization/citation.txt",
        "reviewer": "owner",
        "reviewed_at": "2026-11-11",
    }
    item.update(overrides)
    return item


def _ready_evidence(**overrides):
    evidence = {name: _ready_item() for name in REQUIRED_ITEM_NAMES}
    evidence.update(overrides)
    return evidence


# --- template and UNKNOWN semantics ---------------------------------------


def test_template_lists_every_required_item_as_unknown_without_inventing_facts():
    template = build_evidence_template()

    assert tuple(template) == REQUIRED_ITEM_NAMES
    for name, record in template.items():
        assert record["status"] == UNKNOWN, name
        assert record["evidence_ref"] == ""
        assert record["reviewer"] == ""
        assert record["reviewed_at"] == ""
        assert record["value"] == UNKNOWN

    report = evaluate_eligibility(template)
    assert report.status == BLOCKED
    assert report.gap_items == list(REQUIRED_ITEM_NAMES)
    assert "cannot declare monetization eligibility" in report.reasons[0]
    assert report.items_with_status(MET) == []


def test_unknown_item_blocks_even_when_every_other_item_is_ready():
    evidence = _ready_evidence(
        region_eligibility=_ready_item(status=UNKNOWN, evidence_ref="", reviewer="", reviewed_at="")
    )

    report = evaluate_eligibility(evidence)

    assert report.status == BLOCKED
    assert report.gap_items == ["region_eligibility"]
    assert report.items["region_eligibility"]["status"] == UNKNOWN
    assert "not MET: region_eligibility" in report.reasons


def test_unmet_item_blocks():
    evidence = _ready_evidence(
        fan_or_view_threshold=_ready_item(status=UNMET, evidence_ref="")
    )

    report = evaluate_eligibility(evidence)

    assert report.status == BLOCKED
    assert report.gap_items == ["fan_or_view_threshold"]
    assert "status is UNMET, must be MET with evidence" in " ".join(report.gaps)


def test_missing_item_record_blocks():
    evidence = _ready_evidence()
    del evidence["tax_information"]

    report = evaluate_eligibility(evidence)

    assert report.status == BLOCKED
    assert report.gap_items == ["tax_information"]
    assert report.items["tax_information"]["status"] == UNKNOWN
    assert "missing evidence record" in " ".join(report.gaps)


# --- the MET-without-evidence hard rule -----------------------------------


def test_met_without_evidence_ref_blocks_and_does_not_count():
    evidence = _ready_evidence(
        payout_method=_ready_item(evidence_ref="")
    )

    report = evaluate_eligibility(evidence)

    assert report.status == BLOCKED
    assert report.gap_items == ["payout_method"]
    # Status is preserved as MET, but it is explicitly not accepted as evidence.
    assert report.items["payout_method"]["status"] == MET
    assert "MET without evidence_ref: payout_method" in report.reasons
    assert "payout_method: MET but no evidence_ref" in report.gaps


def test_met_without_reviewer_or_valid_review_date_blocks():
    missing_reviewer = _ready_evidence(
        account_ownership=_ready_item(reviewer="")
    )
    bad_date = _ready_evidence(
        program_enrollment=_ready_item(reviewed_at="last week")
    )

    reviewer_report = evaluate_eligibility(missing_reviewer)
    date_report = evaluate_eligibility(bad_date)

    assert reviewer_report.status == BLOCKED
    assert "account_ownership: MET but no reviewer" in reviewer_report.gaps
    assert date_report.status == BLOCKED
    assert "program_enrollment: MET but reviewed_at is missing or not ISO-8601" in date_report.gaps


def test_invalid_status_is_treated_as_unknown_not_met():
    evidence = _ready_evidence(
        content_policy_compliance=_ready_item(status="confirmed")
    )

    report = evaluate_eligibility(evidence)

    assert report.status == BLOCKED
    assert report.items["content_policy_compliance"]["status"] == UNKNOWN
    assert "invalid status 'confirmed'" in " ".join(report.gaps)
    assert "not MET: content_policy_compliance" in report.reasons


# --- the all-ready path ---------------------------------------------------


def test_all_required_items_met_with_evidence_is_eligible_ready():
    report = evaluate_eligibility(_ready_evidence(), platform="youtube_shorts")

    assert report.status == ELIGIBLE_READY
    assert report.ok is True
    assert report.eligible_ready is True
    assert report.gaps == []
    assert report.gap_items == []
    assert report.reasons == []
    assert report.platform == "youtube_shorts"
    assert report.items_with_status(MET) == list(REQUIRED_ITEM_NAMES)

    document = report.to_dict()
    assert document["status"] == ELIGIBLE_READY
    assert document["required_items"] == list(REQUIRED_ITEM_NAMES)
    assert document["programs"] == list(PROGRAMS)
    assert document["disclaimer"] == DISCLAIMER


def test_lowercase_met_is_normalized():
    evidence = _ready_evidence(
        account_ownership=_ready_item(status="met")
    )

    report = evaluate_eligibility(evidence)

    assert report.status == ELIGIBLE_READY
    assert report.items["account_ownership"]["status"] == MET


# --- external facts stay UNKNOWN, never fabricated ------------------------


def test_external_fact_values_are_preserved_only_when_supplied():
    evidence = _ready_evidence(
        region_eligibility=_ready_item(value="US"),
        fan_or_view_threshold=_ready_item(value="1000 subscribers"),
    )

    report = evaluate_eligibility(evidence)

    assert report.items["region_eligibility"]["value"] == "US"
    assert report.items["fan_or_view_threshold"]["value"] == "1000 subscribers"


def test_missing_external_fact_value_stays_unknown():
    evidence = _ready_evidence()

    report = evaluate_eligibility(evidence)

    for name in EXTERNAL_FACT_ITEMS:
        assert report.items[name]["value"] == UNKNOWN


def test_report_is_deterministic():
    evidence = _ready_evidence()

    first = evaluate_eligibility(evidence).to_dict()
    second = evaluate_eligibility(evidence).to_dict()

    assert first == second


# --- report / query surface -----------------------------------------------


def test_query_returns_one_item_or_the_whole_report():
    report = evaluate_eligibility(_ready_evidence(), platform="tiktok")

    item = report.query("payout_method")
    assert item["status"] == MET
    assert item["evidence_ref"].startswith("evidence/")

    full = report.query()
    assert full["platform"] == "tiktok"
    assert set(full["items"]) == set(REQUIRED_ITEM_NAMES)

    with pytest.raises(KeyError):
        report.query("free_fans")


def test_blocked_report_explains_the_gap():
    evidence = _ready_evidence(
        region_eligibility=_ready_item(status=UNKNOWN),
        tax_information=_ready_item(evidence_ref=""),
    )

    report = evaluate_eligibility(evidence)
    document = report.to_dict()

    assert document["status"] == BLOCKED
    assert document["ok"] is False
    assert document["gap_items"] == ["region_eligibility", "tax_information"]
    assert any("cannot declare monetization eligibility" in reason for reason in document["reasons"])
    assert any("not MET: region_eligibility" in reason for reason in document["reasons"])
    assert any("MET without evidence_ref: tax_information" in reason for reason in document["reasons"])


# --- strict mode and input validation -------------------------------------


def test_require_ready_raises_with_the_report():
    with pytest.raises(EligibilityBlocked) as excinfo:
        evaluate_eligibility(build_evidence_template(), require_ready=True)

    assert excinfo.value.report.status == BLOCKED
    assert len(excinfo.value.report.gaps) > 0


def test_non_mapping_evidence_is_rejected():
    with pytest.raises(MonetizationEligibilityError):
        evaluate_eligibility(["region_eligibility"])  # type: ignore[arg-type]


def test_unknown_program_is_rejected():
    with pytest.raises(MonetizationEligibilityError):
        evaluate_eligibility(_ready_evidence(), programs=("creator_fund", "iap"))


def test_programs_are_metadata_only():
    report = evaluate_eligibility(_ready_evidence(), programs=("creator_fund",))

    assert report.status == ELIGIBLE_READY
    assert report.programs == ("creator_fund",)
    assert report.to_dict()["programs"] == ["creator_fund"]