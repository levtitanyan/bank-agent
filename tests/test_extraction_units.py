"""Tests for normalization, quote verification, validation and conflicts.

Every input here is a string the real ACBA documents actually contain, or a
plausible corruption of one. Normalizers written against invented examples pass
their tests and fail on the bank.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import HttpUrl

from tariff_agent.config import Allowlist
from tariff_agent.documents.document import DocumentKind
from tariff_agent.extraction.conflict import detect_conflict, summarize
from tariff_agent.extraction.groups import FIELD_GROUPS, group_of
from tariff_agent.extraction.normalize import normalize
from tariff_agent.extraction.validate import validate_extraction
from tariff_agent.extraction.verify import verify_quote
from tariff_agent.fields import FIELD_IDS, ValueKind
from tariff_agent.models import (
    Evidence,
    FieldStatus,
    FieldValue,
    FieldVariant,
    Language,
    TariffExtraction,
)
from tariff_agent.rag.chunking import Chunk, ChunkType, SourceRole

ALLOWLIST = Allowlist(allowed_schemes=("https",), domains=("acba.am", "www.acba.am"))


def chunk(text: str, chunk_id: str = "c001", url: str = "https://acba.am/hy/x") -> Chunk:
    """Build a chunk for verification tests."""
    return Chunk(
        chunk_id=chunk_id,
        doc_id="d" * 64,
        text=text,
        page=3,
        chunk_type=ChunkType.TEXT,
        source_role=SourceRole.PRIMARY,
        document_kind=DocumentKind.PDF,
        document_name="Տեղեկատվական ամփոփագիր",
        source_url=url,
        language=Language.HY,
        retrieved_at=datetime.now(UTC),
        section="Տոկոսադրույք",
    )


def evidence(url: str = "https://www.acba.am/files/loan%20info.pdf") -> Evidence:
    """Build an evidence record."""
    return Evidence(
        document_name="Տեղեկատվական ամփոփագիր",
        source_url=HttpUrl(url),
        page=3,
        section="Տոկոսադրույք",
        quote="Տարեկան անվանական տոկոսադրույք՝ 13,5%",
    )


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected_min", "expected_max"),
    [
        ("20.1-21.6%", 20.1, 21.6),
        ("13,5 %", 13.5, 13.5),
        ("17.5 - 21.6%", 17.5, 21.6),
        ("11.9-12.5%", 11.9, 12.5),
    ],
)
def test_rates_normalize(text: str, expected_min: float, expected_max: float) -> None:
    """Ranges, comma decimals and spaced hyphens all appear in the documents."""
    result = normalize(text, ValueKind.PERCENT)
    assert result == {"min": expected_min, "max": expected_max, "unit": "percent"}


@pytest.mark.parametrize(
    "text",
    ["50,000-10,000,000 ՀՀ դրամ", "50.000-10.000.000 ՀՀ դրամ", "50 000 - 10 000 000 ՀՀ դրամ"],
)
def test_the_same_amount_written_three_ways_normalizes_identically(text: str) -> None:
    """ACBA uses comma, dot and space grouping, sometimes in one document.

    If these did not collapse to one number, the Phase 7 diff would report a
    tariff change every time the bank reformatted its page.
    """
    assert normalize(text, ValueKind.AMOUNT) == {
        "min": 50000.0,
        "max": 10000000.0,
        "unit": "amount",
        "currency": "AMD",
    }


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [("9-60 ամիս", 9.0, 60.0), ("12 - 240 ամիս", 12.0, 240.0), ("3 տարի", 36.0, 36.0)],
)
def test_terms_normalize_to_months(text: str, low: float, high: float) -> None:
    """Years become months so that «3 տարի» and «36 ամիս» compare equal."""
    result = normalize(text, ValueKind.TERM)
    assert result == {"min_months": low, "max_months": high, "unit": "months"}


def test_an_open_ended_term_starts_at_zero() -> None:
    """«մինչև 240 ամիս» states a maximum, not a single value."""
    assert normalize("մինչև 240 ամիս", ValueKind.TERM) == {
        "min_months": 0.0,
        "max_months": 240.0,
        "unit": "months",
    }


def test_currencies_become_codes_and_foreign_is_flagged_not_invented() -> None:
    """«արտարժույթ» means "foreign currency" without saying which one."""
    assert normalize("ՀՀ դրամ, ԱՄՆ դոլար", ValueKind.CURRENCY) == {"codes": ["AMD", "USD"]}
    assert normalize("ՀՀ դրամ և արտարժույթ", ValueKind.CURRENCY) == {
        "codes": ["AMD"],
        "includes_foreign": True,
    }


@pytest.mark.parametrize("text", ["անվճար", "չի գանձվում", "առկա չէ"])
def test_a_fee_the_bank_says_it_does_not_charge_is_an_explicit_zero(text: str) -> None:
    """Stating that no fee applies is information, unlike stating nothing."""
    assert normalize(text, ValueKind.FEE) == {"kind": "none", "value": 0.0, "unit": "free"}


def test_a_percentage_fee_and_a_flat_fee_are_distinguished() -> None:
    """0.5% monthly and 5,000 AMD once are not the same commitment."""
    assert normalize("0.5% ամսական", ValueKind.FEE)["kind"] == "percent"
    assert normalize("5,000 ՀՀ դրամ", ValueKind.FEE)["kind"] == "amount"


def test_an_unparseable_value_returns_none_rather_than_a_guess() -> None:
    """A wrong normalization would invent a change or hide one."""
    assert normalize("ըստ պայմանագրի", ValueKind.PERCENT) is None
    assert normalize("", ValueKind.AMOUNT) is None


def test_an_impossible_rate_is_refused() -> None:
    """250% is a misparse, not a tariff."""
    assert normalize("250%", ValueKind.PERCENT) is None


# --------------------------------------------------------------------------- #
# Quote verification
# --------------------------------------------------------------------------- #

PASSAGE = (
    "Տոկոսադրույք Տարեկան անվանական տոկոսադրույք՝ 20.1-21.6% "
    "acba digital-ով ձևակերպման դեպքում 17.5-21.6%"
)


def test_a_real_quote_verifies() -> None:
    """The ordinary case: the model copied text that is there."""
    check = verify_quote("Տարեկան անվանական տոկոսադրույք՝ 20.1-21.6%", "c001", [chunk(PASSAGE)])
    assert check.verified
    assert check.score >= 0.90
    assert not check.corrected


def test_a_fabricated_quote_is_refused() -> None:
    """This is the guarantee the whole project rests on."""
    check = verify_quote("Տարեկան անվանական տոկոսադրույք՝ 9,9%", "c001", [chunk(PASSAGE)])
    assert not check.verified
    assert "does not occur" in check.reason


def test_a_citation_slip_within_the_field_is_corrected() -> None:
    """The quote is real; the model attributed it to the wrong passage."""
    passages = [chunk("Ժամկետը 60 ամիս", "c001"), chunk(PASSAGE, "c002")]
    check = verify_quote("Տարեկան անվանական տոկոսադրույք՝ 20.1-21.6%", "c001", passages)
    assert check.verified
    assert check.corrected
    assert check.chunk is not None
    assert check.chunk.chunk_id == "c002"


def test_a_quote_from_outside_the_field_is_refused() -> None:
    """Correction is narrow on purpose.

    A quote found in a passage that was never retrieved for this field is not a
    citation slip - it is evidence that the answer came from somewhere we did
    not ask about.
    """
    check = verify_quote(
        "Տարեկան անվանական տոկոսադրույք՝ 20.1-21.6%", "c001", [chunk("Ժամկետը 60 ամիս")]
    )
    assert not check.verified


def test_a_quote_too_short_to_mean_anything_is_refused() -> None:
    """«13,5%» alone matches half the document and proves nothing."""
    assert not verify_quote("13,5%", "c001", [chunk(PASSAGE)]).verified


def test_whitespace_differences_do_not_break_verification() -> None:
    """Line breaks differ between a chunk and a quoted fragment."""
    check = verify_quote(
        "Տարեկան անվանական\n  տոկոսադրույք՝ 20.1-21.6%", "c001", [chunk(PASSAGE)]
    )
    assert check.verified


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #


def test_every_field_belongs_to_exactly_one_group() -> None:
    """A field in no group would never be extracted at all."""
    grouped = [field_id for _, members in FIELD_GROUPS for field_id in members]
    assert sorted(grouped) == sorted(FIELD_IDS)
    assert len(grouped) == len(set(grouped))
    assert group_of("nominal_rate") == "rates"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def build_extraction(**fields: FieldValue) -> TariffExtraction:
    """Build an extraction with the given fields, others NOT_FOUND."""
    complete = {field_id: FieldValue.not_found() for field_id in FIELD_IDS}
    complete.update(fields)
    return TariffExtraction(
        bank="ACBA Bank",
        product_id="mortgage",
        document_name="Տեղեկատվական ամփոփագիր",
        source_url=HttpUrl("https://www.acba.am/files/loan%20info.pdf"),
        retrieved_at=datetime.now(UTC),
        extraction_method="gemini:test",
        fields=complete,
    )


def found(value: str, kind: ValueKind, url: str = "https://www.acba.am/files/x.pdf") -> FieldValue:
    """Build a FOUND field value with normalization applied."""
    return FieldValue(
        value=value,
        normalized=normalize(value, kind),
        evidence=evidence(url),
        status=FieldStatus.FOUND,
    )


def test_an_effective_rate_below_the_nominal_one_is_downgraded() -> None:
    """The effective rate includes the nominal plus charges, so it cannot be lower."""
    extraction = build_extraction(
        nominal_rate=found("13.5%", ValueKind.PERCENT),
        effective_rate=found("9.5%", ValueKind.PERCENT),
    )
    validated, report = validate_extraction(extraction, ALLOWLIST)
    assert validated.fields["effective_rate"].status is FieldStatus.UNVERIFIED
    assert "not possible" in report.issues_for("effective_rate")[0]
    assert validated.fields["effective_rate"].value == "9.5%", "the value is never edited"


def test_evidence_from_an_unapproved_domain_is_downgraded() -> None:
    """Evidence must point somewhere a reviewer is allowed to check."""
    extraction = build_extraction(
        nominal_rate=found("13.5%", ValueKind.PERCENT, url="https://evil.example/x.pdf")
    )
    validated, report = validate_extraction(extraction, ALLOWLIST)
    assert validated.fields["nominal_rate"].status is FieldStatus.UNVERIFIED
    assert "not an approved source" in report.issues_for("nominal_rate")[0]


def test_a_numeric_field_that_could_not_be_read_is_flagged() -> None:
    """A rate nobody can parse is not a rate a report should state as fact."""
    unparseable = FieldValue(
        value="ըստ պայմանագրի", evidence=evidence(), status=FieldStatus.FOUND
    )
    validated, report = validate_extraction(
        build_extraction(nominal_rate=unparseable), ALLOWLIST
    )
    assert validated.fields["nominal_rate"].status is FieldStatus.UNVERIFIED
    assert report.issues_for("nominal_rate")


def test_completeness_counts_only_required_fields_that_are_usable() -> None:
    """A downgraded field does not count towards completeness."""
    extraction = build_extraction(
        nominal_rate=found("13.5%", ValueKind.PERCENT),
        effective_rate=found("14.2%", ValueKind.PERCENT),
        currency=found("ՀՀ դրամ", ValueKind.CURRENCY),
    )
    _, report = validate_extraction(extraction, ALLOWLIST)
    assert report.completeness == pytest.approx(3 / 6)
    assert report.is_valid


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #


def test_disjoint_ranges_from_two_official_sources_are_a_conflict() -> None:
    """The real case: the 2023 summary and the current page state different rates."""
    conflict = detect_conflict(
        "nominal_rate",
        found("11.9-12.5%", ValueKind.PERCENT),
        found("13.75-14.5%", ValueKind.PERCENT),
        primary_date=date(2023, 5, 15),
        supporting_date=date(2026, 4, 29),
    )
    assert conflict is not None
    assert "11.9-12.5%" in conflict.summary
    assert conflict.older_source is not None
    assert conflict.older_source.document_date == date(2023, 5, 15)
    assert "dated 2023-05-15" in conflict.primary.described


def test_overlapping_ranges_are_not_a_conflict() -> None:
    """Different channels of one product, not a contradiction."""
    assert (
        detect_conflict(
            "nominal_rate",
            found("17.5-21.6%", ValueKind.PERCENT),
            found("20.1-21.6%", ValueKind.PERCENT),
        )
        is None
    )


def test_the_same_value_formatted_differently_is_not_a_conflict() -> None:
    """«13,5%» and «13.5 %» are one number; a false alert costs attention."""
    assert (
        detect_conflict(
            "nominal_rate",
            found("13,5%", ValueKind.PERCENT),
            found("13.5 %", ValueKind.PERCENT),
        )
        is None
    )


def test_a_field_missing_from_one_source_is_not_a_conflict() -> None:
    """Silence is not disagreement."""
    assert (
        detect_conflict(
            "service_fee", found("0.5%", ValueKind.FEE), FieldValue.not_found()
        )
        is None
    )


def test_conflicts_summarize_for_a_report() -> None:
    """A reviewer needs both sides named, with their dates."""
    conflict = detect_conflict(
        "nominal_rate",
        found("11.9-12.5%", ValueKind.PERCENT),
        found("13.75-14.5%", ValueKind.PERCENT),
        primary_date=date(2023, 5, 15),
    )
    assert conflict is not None
    summary = summarize([conflict])
    assert summary["count"] == 1
    assert summary["fields"] == ["nominal_rate"]


# --------------------------------------------------------------------------- #
# Variants on the model
# --------------------------------------------------------------------------- #


def test_variants_carry_their_own_evidence() -> None:
    """Each channel is quoted separately so it can be checked separately."""
    value = FieldValue(
        value="15.9-21.6%",
        evidence=evidence(),
        status=FieldStatus.FOUND,
        variants=(
            FieldVariant(label="acba digital", value="17.5-21.6%", evidence=evidence()),
            FieldVariant(label="Մասնաճյուղ", value="20.1-21.6%", evidence=evidence()),
        ),
    )
    assert value.has_variants
    assert len(value.variants) == 2
    assert value.variants[0].evidence.quote


def test_a_missing_field_cannot_have_variants() -> None:
    """A field the document does not state has no per-channel versions."""
    with pytest.raises(ValueError, match="must not carry variants"):
        FieldValue(
            value="NOT_FOUND",
            status=FieldStatus.NOT_FOUND,
            variants=(
                FieldVariant(label="x", value="1%", evidence=evidence()),
            ),
        )


# --------------------------------------------------------------------------- #
# Formats found by running the extractor against the live pages
# --------------------------------------------------------------------------- #


def test_a_range_written_with_the_ablative_suffix_keeps_its_lower_bound() -> None:
    """«9-ից 60 ամիս» is how ACBA writes a range on the consumer page.

    Without allowing «-ից» between the numbers, this collapsed to 60-60 and the
    report claimed a fixed 60-month term for a loan offered from 9 months.
    """
    assert normalize("9-ից 60 ամիս", ValueKind.TERM) == {
        "min_months": 9.0,
        "max_months": 60.0,
        "unit": "months",
    }


def test_an_ocr_damaged_up_to_is_still_an_upper_bound() -> None:
    """«մինչն» is «մինչև» after Tesseract reads «և» as «ն».

    Missing it turned "up to 120 months" into "exactly 120 months", which then
    looked disjoint from a 9-60 month term and manufactured a conflict the two
    documents did not have.
    """
    assert normalize("մինչն 120 ամիս", ValueKind.TERM) == {
        "min_months": 0.0,
        "max_months": 120.0,
        "unit": "months",
    }


def test_a_term_inside_another_terms_range_is_not_a_conflict() -> None:
    """9-60 months sits inside 0-120 months; that is agreement, not dispute."""
    assert (
        detect_conflict(
            "term", found("9-ից 60 ամիս", ValueKind.TERM), found("մինչն 120 ամիս", ValueKind.TERM)
        )
        is None
    )
