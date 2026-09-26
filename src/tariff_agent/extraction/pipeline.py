"""Retrieved passages in, a verified extraction out.

The order is the point:

1. retrieve per field (Phase 5), and stop there for anything the gate refused;
2. ask the model once per *group* of fields, showing only the passages retrieved
   for that group's own fields;
3. verify every quote against the passage it was attributed to;
4. normalize deterministically;
5. re-ask for any single field the group answered badly;
6. compare the primary source against supporting ones and record disagreements.

Steps 3 to 6 are where a wrong answer is caught, and none of them asks the model
anything. A field that survives all of them is quotable; a field that does not
becomes NOT_FOUND or UNVERIFIED, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime

from pydantic import HttpUrl

from tariff_agent.config import Allowlist, Product
from tariff_agent.errors import ExtractionError
from tariff_agent.extraction.conflict import FieldConflict, detect_conflict
from tariff_agent.extraction.extractor import Extractor
from tariff_agent.extraction.groups import FIELD_GROUPS
from tariff_agent.extraction.normalize import normalize
from tariff_agent.extraction.prompt import PROMPT_VERSION
from tariff_agent.extraction.schema import ExtractedField
from tariff_agent.extraction.validate import ValidationReport, validate_extraction
from tariff_agent.extraction.verify import verify_quote
from tariff_agent.fields import FIELD_IDS, FIELDS_BY_ID, FieldSpec, ValueKind
from tariff_agent.models import (
    Evidence,
    FieldStatus,
    FieldValue,
    FieldVariant,
    TariffExtraction,
)
from tariff_agent.observability.logging import get_logger
from tariff_agent.rag.chunking import Chunk, SourceRole
from tariff_agent.rag.retrieval import FieldRetrieval, Retriever

logger = get_logger(__name__)


@dataclass
class ExtractionOutcome:
    """Everything one extraction run produced.

    Attributes:
        extraction: The validated extraction.
        validation: What the deterministic checks found.
        conflicts: Disagreements between the primary and supporting sources.
        retrieval: The per-field retrieval results, kept so a report can show
            why a field was reported missing.
        model_calls: How many model calls were made.
        from_cache: True when every answer was replayed from disk, so the run
            cost nothing and made no request. Shown in the report, because a
            cached run and a live one are different claims.
    """

    extraction: TariffExtraction
    validation: ValidationReport
    conflicts: list[FieldConflict] = dataclass_field(default_factory=list)
    retrieval: dict[str, FieldRetrieval] = dataclass_field(default_factory=dict)
    model_calls: int = 0
    from_cache: bool = False


def extract_tariffs(
    product: Product,
    bank: str,
    retriever: Retriever,
    extractor: Extractor,
    allowlist: Allowlist,
    *,
    top_k: int = 4,
) -> ExtractionOutcome:
    """Extract, verify and validate every tariff field for one product.

    Args:
        product: The product being monitored.
        bank: Bank name, recorded on the extraction.
        retriever: The product's knowledge layer.
        extractor: Which backend reads the passages.
        allowlist: Domains evidence may come from.
        top_k: Passages per field per source role.

    Returns:
        The outcome, including what could not be answered and why.

    Raises:
        ExtractionError: If the backend fails for every group.
    """
    retrieval = {
        field_id: retriever.search_field(FIELDS_BY_ID[field_id], k=top_k)
        for field_id in FIELD_IDS
    }
    fields: dict[str, FieldValue] = {}
    calls_before = getattr(extractor, "calls", 0)
    attempted = 0
    failures = 0

    for group_name, member_ids in FIELD_GROUPS:
        specs = [FIELDS_BY_ID[field_id] for field_id in member_ids]
        answerable = [spec for spec in specs if retrieval[spec.id].is_relevant]
        for spec in specs:
            if spec not in answerable:
                fields[spec.id] = FieldValue.not_found()
                logger.info(
                    "field_not_retrievable",
                    extra={"field": spec.id, "reason": retrieval[spec.id].reason},
                )
        if not answerable:
            continue

        chunks = _passages_for(answerable, retrieval)
        attempted += 1
        try:
            response = extractor.extract(answerable, chunks, product=product.name_hy)
        except ExtractionError as exc:
            failures += 1
            logger.warning(
                "group_extraction_failed",
                extra={"group": group_name, "fields": [s.id for s in answerable],
                       "error": str(exc)[:200]},
            )
            for spec in answerable:
                fields[spec.id] = _retry_single(
                    spec,
                    retrieval,
                    extractor,
                    reason="the group call failed",
                    product_name=product.name_hy,
                )
            continue

        answers = {answer.field_id: answer for answer in response.fields}
        for spec in answerable:
            answer = answers.get(spec.id)
            value = _build_field(spec, answer, retrieval[spec.id])
            if (
                value.status is FieldStatus.NOT_FOUND
                and answer is not None
                and not answer.is_not_found
            ):
                # The model gave an answer and it failed verification. One more
                # attempt, alone, with only this field's passages. A model that
                # *correctly* answered NOT_FOUND is not retried: doing so spent
                # a call per absent field and taught us nothing.
                retried = _retry_single(
                    spec,
                    retrieval,
                    extractor,
                    reason="the answer's quote could not be verified",
                    product_name=product.name_hy,
                )
                if retried.status is not FieldStatus.NOT_FOUND:
                    value = retried
            fields[spec.id] = value

    recovered = any(value.status is FieldStatus.FOUND for value in fields.values())
    if attempted and failures == attempted and not recovered:
        # Counted against groups actually attempted, and only when the
        # single-field retries recovered nothing either: a group failure that
        # the retries repaired is not a failed run.
        raise ExtractionError(
            f"every field group attempted for {product.id} failed "
            f"({failures} of {attempted}) and no field was recovered"
        )

    primary_source = _primary_chunk(retrieval)
    extraction = TariffExtraction(
        bank=bank,
        product_id=product.id,
        document_name=primary_source.document_name if primary_source else product.name_en,
        source_url=HttpUrl(
            primary_source.source_url
            if primary_source
            else (product.canonical_page or "https://acba.am/")
        ),
        retrieved_at=primary_source.retrieved_at if primary_source else datetime.now(UTC),
        extraction_method=extractor.method,
        prompt_version=PROMPT_VERSION,
        fields=fields,
    )
    validated, report = validate_extraction(extraction, allowlist)
    conflicts = _find_conflicts(validated, retrieval, extractor, allowlist)

    return ExtractionOutcome(
        extraction=validated,
        validation=report,
        conflicts=conflicts,
        retrieval=retrieval,
        model_calls=getattr(extractor, "calls", 0) - calls_before,
        from_cache=bool(getattr(extractor, "served_from_cache", False)),
    )


def _passages_for(specs: list[FieldSpec], retrieval: dict[str, FieldRetrieval]) -> list[Chunk]:
    """Collect the passages retrieved for a group's own fields.

    Args:
        specs: The group's fields.
        retrieval: Retrieval results per field.

    Returns:
        The passages, de-duplicated, primary source first.
    """
    seen: dict[str, Chunk] = {}
    for spec in specs:
        for scored in retrieval[spec.id].all_chunks:
            seen.setdefault(scored.chunk.chunk_id, scored.chunk)
    return sorted(seen.values(), key=lambda chunk: chunk.source_role is not SourceRole.PRIMARY)


def _build_field(
    spec: FieldSpec, answer: ExtractedField | None, retrieval: FieldRetrieval
) -> FieldValue:
    """Turn one model answer into a verified field value.

    Args:
        spec: The field.
        answer: What the backend said, or None when it said nothing.
        retrieval: What was retrieved for this field.

    Returns:
        The field value: FOUND with verified evidence, or NOT_FOUND.
    """
    if answer is None or answer.is_not_found:
        return FieldValue.not_found()

    chunks = [scored.chunk for scored in retrieval.all_chunks]
    check = verify_quote(answer.quote, answer.chunk_id, chunks)
    if not check.verified or check.chunk is None:
        logger.warning(
            "field_rejected_unverified_quote",
            extra={"field": spec.id, "reason": check.reason, "value": answer.value[:60]},
        )
        return FieldValue.not_found()

    evidence = _evidence_from(check.chunk, answer.quote)
    variants = _build_variants(answer, chunks, spec.kind)
    return FieldValue(
        value=answer.value.strip(),
        normalized=normalize(answer.value, spec.kind),
        evidence=evidence,
        status=FieldStatus.FOUND,
        variants=variants,
    )


def _build_variants(
    answer: ExtractedField, chunks: list[Chunk], kind: ValueKind
) -> tuple[FieldVariant, ...]:
    """Verify and build each per-channel variant.

    Args:
        answer: The model's answer.
        chunks: Passages retrieved for this field.
        kind: The field's value kind, so each variant is normalized and can be
            checked against the headline range.

    Returns:
        The variants whose quotes verified. An unverifiable variant is dropped
        rather than reported, and the field keeps its overall range.
    """
    variants: list[FieldVariant] = []
    for item in answer.variants:
        check = verify_quote(item.quote, item.chunk_id, chunks)
        if not check.verified or check.chunk is None:
            logger.info("variant_dropped_unverified", extra={"label": item.label})
            continue
        variants.append(
            FieldVariant(
                label=item.label.strip(),
                value=item.value.strip(),
                normalized=normalize(item.value, kind),
                evidence=_evidence_from(check.chunk, item.quote),
            )
        )
    return tuple(variants)


def _evidence_from(chunk: Chunk, quote: str) -> Evidence:
    """Build evidence pointing at the passage a quote was verified against.

    Args:
        chunk: The passage.
        quote: The quoted text.

    Returns:
        The evidence record. HTML documents report no page number, because they
        have none, rather than a plausible "page 1".
    """
    return Evidence(
        document_name=chunk.document_name,
        source_url=HttpUrl(chunk.source_url),
        page=chunk.evidence_page,
        section=chunk.section,
        quote=" ".join(quote.split()),
    )


def _retry_single(
    spec: FieldSpec,
    retrieval: dict[str, FieldRetrieval],
    extractor: Extractor,
    *,
    reason: str,
    product_name: str | None = None,
) -> FieldValue:
    """Re-ask for one field on its own.

    Args:
        spec: The field.
        retrieval: Retrieval results per field.
        extractor: The backend.
        reason: Why the retry is happening, logged so that a run making more
            calls than the group count can be explained rather than guessed at.
        product_name: The product being monitored.

    Returns:
        The field value, NOT_FOUND when the retry also fails.
    """
    logger.info("field_retried_alone", extra={"field": spec.id, "reason": reason})
    try:
        response = extractor.extract(
            [spec], _passages_for([spec], retrieval), product=product_name
        )
    except ExtractionError:
        logger.warning("single_field_retry_failed", extra={"field": spec.id})
        return FieldValue.not_found()
    answer = next((item for item in response.fields if item.field_id == spec.id), None)
    return _build_field(spec, answer, retrieval[spec.id])


def _primary_chunk(retrieval: dict[str, FieldRetrieval]) -> Chunk | None:
    """Find the primary document these values mostly came from.

    Args:
        retrieval: Retrieval results per field.

    Returns:
        A chunk from the primary source, or None.
    """
    for result in retrieval.values():
        for scored in result.primary:
            return scored.chunk
    return None


def _find_conflicts(
    extraction: TariffExtraction,
    retrieval: dict[str, FieldRetrieval],
    extractor: Extractor,
    allowlist: Allowlist,
) -> list[FieldConflict]:
    """Compare the primary source's answers with supporting sources.

    Args:
        extraction: The validated extraction, whose values came from the
            primary source.
        retrieval: Retrieval results per field, which hold the supporting
            passages.
        extractor: The backend, used to read the supporting passages.
        allowlist: Domains evidence may come from.

    Returns:
        The disagreements found.
    """
    conflicts: list[FieldConflict] = []
    # Grouped like the primary pass. Asking per field would double the call
    # count - ten more per product - to answer a question that is usually "no".
    for _, member_ids in FIELD_GROUPS:
        # Free-text fields are not conflict-checked. Two documents describing
        # collateral or a salary privilege in different words is normal, and
        # comparing them produced pure noise - a privilege sentence "against"
        # a percentage from a different product's row.
        checkable = [
            field_id
            for field_id in member_ids
            if extraction.fields[field_id].status is FieldStatus.FOUND
            and retrieval[field_id].supporting
            and FIELDS_BY_ID[field_id].kind is not ValueKind.TEXT
        ]
        if not checkable:
            continue
        specs = [FIELDS_BY_ID[field_id] for field_id in checkable]
        supporting = _supporting_passages(checkable, retrieval)
        try:
            response = extractor.extract(specs, supporting, product=extraction.product_id)
        except ExtractionError:
            logger.warning("conflict_check_failed", extra={"fields": checkable})
            continue

        answers = {answer.field_id: answer for answer in response.fields}
        for field_id in checkable:
            answer = answers.get(field_id)
            if answer is None or answer.is_not_found:
                continue
            check = verify_quote(answer.quote, answer.chunk_id, supporting)
            if not check.verified or check.chunk is None:
                continue
            spec = FIELDS_BY_ID[field_id]
            other = FieldValue(
                value=answer.value.strip(),
                normalized=normalize(answer.value, spec.kind),
                evidence=_evidence_from(check.chunk, answer.quote),
                status=FieldStatus.FOUND,
            )
            conflict = detect_conflict(
                field_id,
                extraction.fields[field_id],
                other,
                primary_date=_date_of(retrieval[field_id], SourceRole.PRIMARY),
                supporting_date=check.chunk.document_date,
            )
            if conflict is not None:
                conflicts.append(conflict)
    return conflicts


def _supporting_passages(
    field_ids: list[str], retrieval: dict[str, FieldRetrieval]
) -> list[Chunk]:
    """Collect the supporting passages retrieved for a group's fields.

    Args:
        field_ids: The fields being compared.
        retrieval: Retrieval results per field.

    Returns:
        The supporting passages, de-duplicated.
    """
    seen: dict[str, Chunk] = {}
    for field_id in field_ids:
        for scored in retrieval[field_id].supporting:
            seen.setdefault(scored.chunk.chunk_id, scored.chunk)
    return list(seen.values())


def _date_of(retrieval: FieldRetrieval, role: SourceRole) -> date | None:
    """Return the document date of the first chunk with the given role.

    Args:
        retrieval: The field's retrieval result.
        role: Which source role to look at.

    Returns:
        The date, or None.
    """
    pool = retrieval.primary if role is SourceRole.PRIMARY else retrieval.supporting
    for scored in pool:
        if scored.chunk.document_date is not None:
            return scored.chunk.document_date
    return None


__all__ = ["ExtractionOutcome", "extract_tariffs"]
