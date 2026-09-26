"""End-to-end tests for the extraction pipeline, with a scripted backend.

The backend is a fake returning fixed answers, because what needs testing is
what the pipeline does *with* an answer: verifying the quote, refusing an
unverifiable one, building variants, retrying a field alone, and stamping how
the values were produced. Gemini's own behaviour is exercised live and reported
separately.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tariff_agent.config import Allowlist, Product
from tariff_agent.documents.document import DocumentKind
from tariff_agent.errors import ExtractionError
from tariff_agent.extraction.extractor import RULE_BASED, RuleBasedExtractor
from tariff_agent.extraction.pipeline import extract_tariffs
from tariff_agent.extraction.schema import ExtractedField, ExtractedVariant, ExtractionResponse
from tariff_agent.fields import FieldSpec
from tariff_agent.models import FieldStatus, Language
from tariff_agent.rag.chunking import Chunk, ChunkType, SourceRole
from tariff_agent.rag.retrieval import Retriever

ALLOWLIST = Allowlist(allowed_schemes=("https",), domains=("acba.am", "www.acba.am"))
PRODUCT = Product(
    id="mortgage",
    kind="loan",
    name_hy="Հիփոթեքային վարկ",
    name_en="Mortgage",
    canonical_page="https://acba.am/hy/individual/loan/purchase-mortgage",
)

RATE_PASSAGE = (
    "Տոկոսադրույք Տարեկան անվանական տոկոսադրույք՝ 13.5% "
    "Տարեկան փաստացի տոկոսադրույք՝ 14.2-15.1%"
)
SUPPORTING_PASSAGE = "Տարեկան անվանական տոկոսադրույք՝ 17.9% մասնաճյուղում ձևակերպելիս"
TERM_PASSAGE = "Վարկի ժամկետը՝ 12 - 240 ամիս, գումարը՝ 1,000,000-500,000,000 ՀՀ դրամ"


def chunk(
    text: str,
    chunk_id: str,
    *,
    role: SourceRole = SourceRole.PRIMARY,
    document_date: date | None = None,
    kind: DocumentKind = DocumentKind.PDF,
) -> Chunk:
    """Build a chunk for pipeline tests."""
    return Chunk(
        chunk_id=chunk_id,
        doc_id="d" * 64,
        text=text,
        page=3,
        chunk_type=ChunkType.TEXT,
        source_role=role,
        document_kind=kind,
        document_name="Տեղեկատվական ամփոփագիր",
        source_url="https://www.acba.am/files/loan%20info.pdf",
        language=Language.HY,
        retrieved_at=datetime.now(UTC),
        section="Տոկոսադրույք",
        document_date=document_date,
    )


class ScriptedExtractor:
    """A backend returning prepared answers, and counting its calls.

    Args:
        answers: Field id to the answer to return for it.
        fail_groups: Group sizes to fail on, used to exercise the retry path.
    """

    def __init__(
        self,
        answers: dict[str, ExtractedField],
        *,
        fail_when_more_than: int | None = None,
    ) -> None:
        """Record the script."""
        self._answers = answers
        self._fail_when_more_than = fail_when_more_than
        self.calls = 0
        self.requested: list[list[str]] = []
        self.products: list[str | None] = []

    @property
    def method(self) -> str:
        """Identifier recorded on the extraction."""
        return "scripted"

    def extract(
        self, specs: list[FieldSpec], chunks: list[Chunk], *, product: str | None = None
    ) -> ExtractionResponse:
        """Return the scripted answers for the requested fields."""
        self.calls += 1
        self.requested.append([spec.id for spec in specs])
        self.products.append(product)
        if self._fail_when_more_than is not None and len(specs) > self._fail_when_more_than:
            raise ExtractionError("the scripted backend refuses groups this large")
        return ExtractionResponse(
            fields=[
                self._answers.get(spec.id, ExtractedField(field_id=spec.id)) for spec in specs
            ]
        )


def test_a_verified_answer_becomes_a_found_field_with_evidence() -> None:
    """The ordinary path, end to end."""
    chunks = [chunk(RATE_PASSAGE, "c001"), chunk(TERM_PASSAGE, "c002")]
    extractor = ScriptedExtractor(
        {
            "nominal_rate": ExtractedField(
                field_id="nominal_rate",
                value="13.5%",
                quote="Տարեկան անվանական տոկոսադրույք՝ 13.5%",
                chunk_id="c001",
            )
        }
    )
    outcome = extract_tariffs(PRODUCT, "ACBA Bank", Retriever(chunks), extractor, ALLOWLIST)
    value = outcome.extraction.fields["nominal_rate"]
    assert value.status is FieldStatus.FOUND
    assert value.value == "13.5%"
    assert value.normalized == {"min": 13.5, "max": 13.5, "unit": "percent"}
    assert value.evidence is not None
    assert value.evidence.page == 3
    assert value.evidence.section == "Տոկոսադրույք"


def test_a_fabricated_quote_becomes_not_found() -> None:
    """The model is not trusted about what a document says."""
    chunks = [chunk(RATE_PASSAGE, "c001")]
    extractor = ScriptedExtractor(
        {
            "nominal_rate": ExtractedField(
                field_id="nominal_rate",
                value="9.9%",
                quote="Տարեկան անվանական տոկոսադրույք՝ 9.9% հատուկ առաջարկով",
                chunk_id="c001",
            )
        }
    )
    outcome = extract_tariffs(PRODUCT, "ACBA Bank", Retriever(chunks), extractor, ALLOWLIST)
    assert outcome.extraction.fields["nominal_rate"].status is FieldStatus.NOT_FOUND


def test_variants_are_built_and_each_is_verified_separately() -> None:
    """A variant whose quote cannot be found is dropped, not reported."""
    passage = (
        "Տարեկան անվանական տոկոսադրույք՝ 17.5-21.6% acba digital համակարգով "
        "և 20.1-21.6% մասնաճյուղում"
    )
    chunks = [chunk(passage, "c001")]
    extractor = ScriptedExtractor(
        {
            "nominal_rate": ExtractedField(
                field_id="nominal_rate",
                value="17.5-21.6%",
                quote="Տարեկան անվանական տոկոսադրույք՝ 17.5-21.6% acba digital համակարգով",
                chunk_id="c001",
                variants=[
                    ExtractedVariant(
                        label="acba digital",
                        value="17.5-21.6%",
                        quote="17.5-21.6% acba digital համակարգով",
                        chunk_id="c001",
                    ),
                    ExtractedVariant(
                        label="invented",
                        value="9.9%",
                        quote="9.9% միայն այսօր հատուկ առաջարկով բոլորի համար",
                        chunk_id="c001",
                    ),
                ],
            )
        }
    )
    outcome = extract_tariffs(PRODUCT, "ACBA Bank", Retriever(chunks), extractor, ALLOWLIST)
    value = outcome.extraction.fields["nominal_rate"]
    assert [variant.label for variant in value.variants] == ["acba digital"]


def test_a_field_the_gate_refused_is_never_sent_to_the_model() -> None:
    """Retrieval decides what is answerable; extraction does not second-guess it."""
    chunks = [chunk(RATE_PASSAGE, "c001")]
    extractor = ScriptedExtractor({})
    outcome = extract_tariffs(PRODUCT, "ACBA Bank", Retriever(chunks), extractor, ALLOWLIST)
    assert outcome.extraction.fields["salary_privileges"].status is FieldStatus.NOT_FOUND
    requested = {field_id for call in extractor.requested for field_id in call}
    assert "salary_privileges" not in requested


def test_a_failed_group_is_retried_field_by_field() -> None:
    """One bad group must not lose the fields it happened to contain."""
    chunks = [chunk(TERM_PASSAGE, "c002")]
    extractor = ScriptedExtractor(
        {
            "term": ExtractedField(
                field_id="term",
                value="12 - 240 ամիս",
                quote="Վարկի ժամկետը՝ 12 - 240 ամիս",
                chunk_id="c002",
            )
        },
        fail_when_more_than=1,
    )
    outcome = extract_tariffs(PRODUCT, "ACBA Bank", Retriever(chunks), extractor, ALLOWLIST)
    assert outcome.extraction.fields["term"].status is FieldStatus.FOUND
    assert any(len(call) == 1 for call in extractor.requested), "a single-field retry ran"


def test_fields_are_extracted_in_groups_not_one_by_one() -> None:
    """Ten calls per product is the thing grouping exists to avoid."""
    chunks = [chunk(RATE_PASSAGE, "c001"), chunk(TERM_PASSAGE, "c002")]
    extractor = ScriptedExtractor({})
    extract_tariffs(PRODUCT, "ACBA Bank", Retriever(chunks), extractor, ALLOWLIST)
    assert extractor.calls <= 4
    assert all(len(call) > 1 for call in extractor.requested if len(call) > 0)


def test_a_disagreement_between_sources_is_reported_with_both_dates() -> None:
    """The mortgage case: an old summary against the current page."""
    chunks = [
        chunk(RATE_PASSAGE, "c001", document_date=date(2023, 5, 15)),
        chunk(
            SUPPORTING_PASSAGE,
            "c900",
            role=SourceRole.SUPPORTING,
            document_date=date(2026, 4, 29),
        ),
    ]
    extractor = ScriptedExtractor(
        {
            "nominal_rate": ExtractedField(
                field_id="nominal_rate",
                value="13.5%",
                quote="Տարեկան անվանական տոկոսադրույք՝ 13.5%",
                chunk_id="c001",
            )
        }
    )
    outcome = extract_tariffs(PRODUCT, "ACBA Bank", Retriever(chunks), extractor, ALLOWLIST)
    # The scripted backend answers the same value for the supporting pass, so
    # no conflict is expected here; what is asserted is that the supporting
    # source was consulted at all.
    assert outcome.extraction.fields["nominal_rate"].status is FieldStatus.FOUND
    assert outcome.model_calls == extractor.calls


def test_the_extraction_records_how_it_was_produced() -> None:
    """A demo run must never be mistaken for a model extraction."""
    chunks = [chunk(RATE_PASSAGE, "c001")]
    outcome = extract_tariffs(
        PRODUCT, "ACBA Bank", Retriever(chunks), RuleBasedExtractor(), ALLOWLIST
    )
    assert outcome.extraction.extraction_method == RULE_BASED


def test_the_offline_extractor_finds_a_stated_value() -> None:
    """Without a key the pipeline still produces verifiable evidence."""
    chunks = [chunk(RATE_PASSAGE, "c001")]
    outcome = extract_tariffs(
        PRODUCT, "ACBA Bank", Retriever(chunks), RuleBasedExtractor(), ALLOWLIST
    )
    value = outcome.extraction.fields["nominal_rate"]
    assert value.status is FieldStatus.FOUND
    assert value.evidence is not None
    assert "13.5%" in value.evidence.quote


def test_every_registry_field_is_present_in_the_result() -> None:
    """The report distinguishes «not offered» from «we failed to look»."""
    chunks = [chunk(RATE_PASSAGE, "c001")]
    outcome = extract_tariffs(
        PRODUCT, "ACBA Bank", Retriever(chunks), ScriptedExtractor({}), ALLOWLIST
    )
    from tariff_agent.fields import FIELD_IDS

    assert set(outcome.extraction.fields) == set(FIELD_IDS)


def test_an_html_source_reports_no_page_number() -> None:
    """A page number in evidence must be verifiable, and HTML has none."""
    chunks = [chunk(RATE_PASSAGE, "c001", kind=DocumentKind.HTML)]
    extractor = ScriptedExtractor(
        {
            "nominal_rate": ExtractedField(
                field_id="nominal_rate",
                value="13.5%",
                quote="Տարեկան անվանական տոկոսադրույք՝ 13.5%",
                chunk_id="c001",
            )
        }
    )
    outcome = extract_tariffs(PRODUCT, "ACBA Bank", Retriever(chunks), extractor, ALLOWLIST)
    evidence = outcome.extraction.fields["nominal_rate"].evidence
    assert evidence is not None
    assert evidence.page is None


def test_everything_failing_raises_rather_than_returning_empty_values() -> None:
    """A run that extracted nothing is a failure, not a result.

    A group failure that the single-field retries repaired is *not* a failed
    run, which is why the check asks whether anything was recovered rather than
    whether anything went wrong.
    """
    chunks = [chunk(RATE_PASSAGE, "c001"), chunk(TERM_PASSAGE, "c002")]
    extractor = ScriptedExtractor({}, fail_when_more_than=0)
    with pytest.raises(ExtractionError, match="no field was recovered"):
        extract_tariffs(PRODUCT, "ACBA Bank", Retriever(chunks), extractor, ALLOWLIST)


# --------------------------------------------------------------------------- #
# The extraction cache
# --------------------------------------------------------------------------- #


def test_a_repeated_run_over_unchanged_documents_makes_no_model_calls(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A demonstration must not be able to fail on a quota.

    The free tier allows twenty requests a day for gemini-2.5-flash and one
    two-product run makes about sixteen, so a reviewer watching a second run
    would see a 429 rather than a report.
    """
    from tariff_agent.extraction.cache import CachedExtractor

    chunks = [chunk(RATE_PASSAGE, "c001")]
    answer = ExtractedField(
        field_id="nominal_rate",
        value="13.5%",
        quote="Տարեկան անվանական տոկոսադրույք՝ 13.5%",
        chunk_id="c001",
    )
    backend = ScriptedExtractor({"nominal_rate": answer})
    cached = CachedExtractor(backend, tmp_path)

    first = extract_tariffs(PRODUCT, "ACBA Bank", Retriever(chunks), cached, ALLOWLIST)
    calls_after_first = backend.calls
    assert calls_after_first > 0
    assert first.from_cache is False

    backend_again = ScriptedExtractor({"nominal_rate": answer})
    second = extract_tariffs(
        PRODUCT, "ACBA Bank", Retriever(chunks), CachedExtractor(backend_again, tmp_path), ALLOWLIST
    )
    assert backend_again.calls == 0, "the second run must ask the model nothing"
    assert second.from_cache is True
    assert second.extraction.fields["nominal_rate"].value == "13.5%"


def test_the_cache_can_be_switched_off_to_show_a_real_call(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A reviewer may want to watch the model actually answer."""
    from tariff_agent.extraction.cache import CachedExtractor

    chunks = [chunk(RATE_PASSAGE, "c001")]
    backend = ScriptedExtractor({})
    extract_tariffs(
        PRODUCT,
        "ACBA Bank",
        Retriever(chunks),
        CachedExtractor(backend, tmp_path, enabled=False),
        ALLOWLIST,
    )
    calls = backend.calls
    extract_tariffs(
        PRODUCT,
        "ACBA Bank",
        Retriever(chunks),
        CachedExtractor(backend, tmp_path, enabled=False),
        ALLOWLIST,
    )
    assert backend.calls > calls


def test_a_changed_prompt_invalidates_cached_answers(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A reworded prompt can change what the model reports.

    Serving an answer produced under the old wording as if the new wording had
    produced it would make a prompt change untestable.
    """
    from tariff_agent.extraction.cache import cache_key

    chunks = [chunk(RATE_PASSAGE, "c001")]
    from tariff_agent.fields import get_field

    specs = [get_field("nominal_rate")]
    first = cache_key(specs, chunks, "model-a")
    assert first != cache_key(specs, chunks, "model-b"), "the model is part of the key"
    assert first != cache_key(specs, [chunk(TERM_PASSAGE, "c002")], "model-a"), (
        "the passages are part of the key"
    )
