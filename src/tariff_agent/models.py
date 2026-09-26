"""Data contracts shared by every stage of the pipeline.

These models are the interface between stages: discovery hands documents to
processing, processing hands chunks to retrieval, retrieval hands evidence to
extraction, and extraction hands a :class:`TariffExtraction` to validation,
storage and diffing.

Three invariants are enforced here rather than trusted to the model or to callers:

1. A field is either *found with evidence* or explicitly :data:`NOT_FOUND`.
   A value that cannot cite a source cannot be constructed at all.
2. A freshly built :class:`TariffExtraction` covers exactly the registry in
   :mod:`tariff_agent.fields` - no missing keys silently dropped, no invented
   extra keys accepted from the model.
3. A *stored* snapshot, however, may predate a registry change, so
   :meth:`TariffExtraction.from_stored` migrates it forward instead of failing.
   That leniency is deliberately confined to data read back from storage.

Per-field trouble (an unverifiable quote, two sources disagreeing) is recorded in
:class:`FieldStatus`. Whether the *run* stops for a human is a separate, run-level
decision carried by :class:`~tariff_agent.errors.NeedsReviewError` and the HITL
gate - not by a field status.

This module holds no business logic: no parsing, no normalization, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, model_validator

from tariff_agent.errors import SnapshotError
from tariff_agent.fields import FIELD_IDS
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

NOT_FOUND: Final[str] = "NOT_FOUND"
"""Sentinel for "this document does not state a value for this field".

Used instead of ``None`` so the absence is explicit and survives JSON
round-trips into snapshots, reports and the model's own structured output.
The model is instructed to emit this string rather than guess a value.
"""

SCHEMA_VERSION: Final[int] = 1
"""Version of the extraction/snapshot payload format.

Bumped whenever a change would make an older stored snapshot load differently,
for example adding a tariff field to the registry. Stored snapshots carry the
version they were written with, so :meth:`TariffExtraction.from_stored` can
migrate them forward instead of crashing the diff.
"""


class FieldStatus(StrEnum):
    """Outcome of extracting one tariff field."""

    FOUND = "found"
    """A value was found and its evidence quote was verified against the source."""

    NOT_FOUND = "not_found"
    """The retrieved evidence does not state this field. Value is :data:`NOT_FOUND`."""

    UNVERIFIED = "unverified"
    """A value was proposed but its quote could not be matched back to the cited
    chunk, or a deterministic check failed. It must not be reported as fact."""

    CONFLICT = "conflict"
    """Two official sources state different values for this field. Resolving the
    disagreement is a human decision."""


class Language(StrEnum):
    """Language of a source document or a user query."""

    HY = "hy"
    EN = "en"
    RU = "ru"


class Evidence(BaseModel):
    """Where a value came from, in enough detail for a human to re-check it.

    Attributes:
        document_name: Human-readable document title, e.g. the PDF's file title
            or the product page's ``<title>``.
        source_url: Exact URL the document was retrieved from.
        page: 1-based PDF page number. ``None`` for HTML documents, which have
            no pagination - the product page is a first-class source here.
        section: Nearest heading above the quoted text, when one was detected.
        quote: Verbatim snippet from the document supporting the value. Phase 6
            fuzzy-matches this against the cited chunk; a quote that is not
            actually in the source downgrades the field to
            :attr:`FieldStatus.UNVERIFIED`.
    """

    model_config = ConfigDict(frozen=True)

    document_name: str
    source_url: HttpUrl
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    quote: str = Field(min_length=1)


class FieldVariant(BaseModel):
    """One channel's version of a field, when the bank states several.

    ACBA's consumer page states three nominal rates at once - 17.5-21.6% through
    the app, 20.1-21.6% at a branch, and 15.9% (13.9% for salary customers) on a
    special offer. Picking one silently would report a rate the customer may
    never be offered, and collapsing them to a range alone loses which channel
    each belongs to. So the field carries the full range *and* the breakdown.

    Attributes:
        label: What distinguishes this variant, in the document's own words -
            «acba digital», «Մասնաճյուղ», «աշխատավարձային».
        value: The value for this variant, verbatim.
        normalized: Machine-comparable form, filled by validation.
        evidence: Where this specific variant is stated. Each variant is quoted
            separately, so a reviewer can check them one at a time.
    """

    model_config = ConfigDict(frozen=True)

    label: str = Field(min_length=1)
    value: str = Field(min_length=1)
    evidence: Evidence
    normalized: dict[str, Any] | None = None


class FieldValue(BaseModel):
    """One extracted tariff field: the value, how to verify it, how sure we are.

    Attributes:
        value: Raw text as it appears in the document, or :data:`NOT_FOUND`.
            Kept verbatim so the report can show what the bank actually wrote.
        normalized: Machine-comparable form produced by deterministic validation
            (Phase 6), e.g. ``{"min": 17.5, "max": 21.6, "unit": "percent"}``.
            ``None`` until validation runs, or when normalization failed.
        evidence: Source location of the value. ``None`` only when NOT_FOUND.
        status: See :class:`FieldStatus`.
        variants: Per-channel breakdown, when the bank states more than one
            value for this field. Empty when a single value applies.
        confidence: 0.0-1.0, or ``None`` when not computed yet.

            This is **never** the model's self-reported confidence: an LLM's own
            estimate is not calibrated, so the Gemini response schema does not
            contain this field at all. Phase 6 computes it deterministically from
            the quote-match score, the document's OCR/parse quality score and the
            retrieval score of the cited chunk. Until then it stays ``None``
            rather than a made-up 1.0.
    """

    model_config = ConfigDict(frozen=True)

    value: str = Field(min_length=1)
    normalized: dict[str, Any] | None = None
    evidence: Evidence | None = None
    status: FieldStatus
    variants: tuple[FieldVariant, ...] = ()
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_not_found_invariants(self) -> Self:
        """Keep "missing" and "found" from blurring into each other.

        Raises:
            ValueError: If a NOT_FOUND field carries data, or a field holding a
                real value lacks evidence.
        """
        is_sentinel = self.value == NOT_FOUND
        if is_sentinel != (self.status is FieldStatus.NOT_FOUND):
            raise ValueError(
                f"value={self.value!r} and status={self.status!r} disagree: "
                f"the {NOT_FOUND} sentinel and status 'not_found' must be used together"
            )
        if is_sentinel and (self.evidence is not None or self.normalized is not None):
            raise ValueError(f"a {NOT_FOUND} field must not carry evidence or a normalized value")
        if is_sentinel and self.variants:
            raise ValueError(
                f"a {NOT_FOUND} field must not carry variants: a field the document does "
                "not state cannot have per-channel versions"
            )
        if not is_sentinel and self.evidence is None:
            raise ValueError(f"field value {self.value!r} has no evidence; use {NOT_FOUND} instead")
        return self

    @property
    def has_variants(self) -> bool:
        """Whether the bank states more than one value for this field."""
        return bool(self.variants)

    @classmethod
    def not_found(cls, confidence: float | None = None) -> FieldValue:
        """Build the canonical "this document does not state it" value.

        Args:
            confidence: How sure we are that the field is genuinely absent
                rather than missed by retrieval. Left ``None`` unless a caller
                has computed it; Phase 5 lowers it when retrieval was weak.

        Returns:
            A NOT_FOUND :class:`FieldValue`.
        """
        return cls(value=NOT_FOUND, status=FieldStatus.NOT_FOUND, confidence=confidence)

    @property
    def is_found(self) -> bool:
        """Whether this field holds a value that may be reported as fact."""
        return self.status is FieldStatus.FOUND


class TariffExtraction(BaseModel):
    """The full set of tariff fields for one product, from one monitoring run.

    This is what gets validated, stored as a snapshot, diffed against the
    previous snapshot and rendered into the business report.

    Attributes:
        schema_version: Payload format version. Fresh extractions use
            :data:`SCHEMA_VERSION`; an object loaded from storage keeps the
            version it was written with, so a report can say which baseline it
            is comparing against.
        bank: Bank name, e.g. ``"ACBA Bank"``.
        product_id: Product registry id, e.g. ``"consumer_loan"``.
        document_name: Primary document the values were extracted from.
        source_url: URL of that document.
        retrieved_at: When the primary document's bytes were downloaded (UTC).
        checked_at: When the bank last confirmed the document current (UTC). A
            business user reading a 2023 summary needs to see that we checked
            today and the bank has not changed it since - "last verified" and
            "last changed" are different facts (P2-D23).
        document_date: The date the primary document states for itself.
        document_edition: The edition it states for itself, when it does.
        extraction_method: What produced these values - ``"gemini:<model>"`` or
            ``"rule_based"`` for the offline extractor. Stamped on the report
            too, so a demo run can never be mistaken for a model extraction.
        prompt_version: Which set of instructions produced them. A reworded
            prompt can change what the model reports, and a diff across two
            versions is measuring our change, not the bank's.
        fields: Every registry field id mapped to its value. Always complete.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = Field(default=SCHEMA_VERSION, ge=0)
    bank: str
    product_id: str
    document_name: str
    source_url: HttpUrl
    retrieved_at: datetime
    checked_at: datetime | None = None
    document_date: date | None = None
    document_edition: str | None = None
    extraction_method: str = "unknown"
    prompt_version: int = 0
    fields: dict[str, FieldValue]

    @model_validator(mode="after")
    def _check_fields_match_registry(self) -> Self:
        """Require exactly the registry's fields - no gaps, no inventions.

        Raises:
            ValueError: If field ids are missing or unknown.
        """
        expected = set(FIELD_IDS)
        actual = set(self.fields)
        if missing := expected - actual:
            raise ValueError(f"extraction is missing tariff fields: {sorted(missing)}")
        if unknown := actual - expected:
            raise ValueError(f"extraction contains unknown tariff fields: {sorted(unknown)}")
        return self

    @classmethod
    def from_stored(cls, payload: Mapping[str, Any]) -> TariffExtraction:
        """Load a snapshot written by an earlier version of the registry.

        The strict constructor above is right for fresh model output, but wrong
        for stored data: if an eleventh tariff field is added, every previously
        stored snapshot would fail to load and the diff would crash. So reading
        from storage migrates instead of failing:

        * a field the stored snapshot does not have becomes :data:`NOT_FOUND`
          (it was genuinely never captured - not a value we may invent);
        * a field no longer in the registry is dropped, with a log line;
        * the stored ``schema_version`` is preserved, defaulting to ``0`` for
          snapshots written before versioning existed.

        This leniency is confined to this method. Extractions coming from Gemini
        still go through the strict constructor.

        Args:
            payload: Decoded snapshot JSON.

        Returns:
            A complete :class:`TariffExtraction`.

        Raises:
            SnapshotError: If the payload is not a usable snapshot at all.
        """
        data = dict(payload)
        stored_fields = data.pop("fields", None)
        if not isinstance(stored_fields, Mapping):
            raise SnapshotError("stored snapshot has no 'fields' mapping")

        known = {fid: value for fid, value in stored_fields.items() if fid in FIELD_IDS}
        if dropped := sorted(set(stored_fields) - set(known)):
            logger.warning(
                "snapshot_fields_dropped",
                extra={"dropped_fields": dropped, "reason": "not in current registry"},
            )
        if added := [fid for fid in FIELD_IDS if fid not in known]:
            logger.info(
                "snapshot_fields_backfilled",
                extra={"backfilled_fields": added, "filled_with": NOT_FOUND},
            )
            for fid in added:
                known[fid] = FieldValue.not_found()

        data.setdefault("schema_version", 0)
        try:
            return cls(fields=known, **data)
        except ValidationError as exc:
            raise SnapshotError(f"stored snapshot could not be loaded: {exc}") from exc

    @property
    def found_field_ids(self) -> tuple[str, ...]:
        """Ids of fields that hold a reportable value, in registry order."""
        return tuple(fid for fid in FIELD_IDS if self.fields[fid].is_found)

    @property
    def not_found_field_ids(self) -> tuple[str, ...]:
        """Ids of fields the document does not state, in registry order."""
        return tuple(fid for fid in FIELD_IDS if self.fields[fid].status is FieldStatus.NOT_FOUND)

    @property
    def review_field_ids(self) -> tuple[str, ...]:
        """Ids of fields a human should look at (unverified or conflicting).

        Note that this describes the *fields*; whether the run itself stops for
        review is decided by the HITL gate, not here.
        """
        return tuple(
            fid
            for fid in FIELD_IDS
            if self.fields[fid].status in (FieldStatus.UNVERIFIED, FieldStatus.CONFLICT)
        )
