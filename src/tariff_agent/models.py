"""Data contracts shared by every stage of the pipeline.

These models are the interface between stages: discovery hands documents to
processing, processing hands chunks to retrieval, retrieval hands evidence to
extraction, and extraction hands a :class:`TariffExtraction` to validation,
storage and diffing.

Two invariants are enforced here rather than trusted to the model or to callers:

1. A field is either *found with evidence* or explicitly :data:`NOT_FOUND`.
   There is no third "probably 13.5%" state, and a NOT_FOUND field cannot carry
   a value, a normalization or evidence.
2. A :class:`TariffExtraction` always covers exactly the registry in
   :mod:`tariff_agent.fields` - no missing keys silently dropped, no invented
   extra keys accepted from the LLM.

This module holds no business logic: no parsing, no normalization, no I/O.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from tariff_agent.fields import FIELD_IDS

NOT_FOUND: Final[str] = "NOT_FOUND"
"""Sentinel for "this document does not state a value for this field".

Used instead of ``None`` so the absence is explicit and survives JSON
round-trips into snapshots, reports and the model's own structured output.
The model is instructed to emit this string rather than guess a value.
"""


class FieldStatus(StrEnum):
    """Outcome of extracting one tariff field."""

    EXTRACTED = "extracted"
    """A value was found and its evidence quote was verified against the source."""

    NOT_FOUND = "not_found"
    """The retrieved evidence does not state this field. Value is :data:`NOT_FOUND`."""

    NEEDS_REVIEW = "needs_review"
    """A value was proposed but something is off (weak evidence, failed check).
    It must not be reported as fact without a human decision."""


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
        page: 1-based PDF page number; ``None`` for HTML documents.
        section: Nearest heading above the quoted text, when one was detected.
        quote: Verbatim snippet from the document supporting the value. Phase 6
            fuzzy-matches this against the cited chunk; a quote that is not
            actually in the source downgrades the field to NOT_FOUND.
    """

    model_config = ConfigDict(frozen=True)

    document_name: str
    source_url: HttpUrl
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    quote: str = Field(min_length=1)


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
        confidence: 0.0-1.0. Combines the model's own confidence with
            deterministic signals (evidence match, document quality).
    """

    model_config = ConfigDict(frozen=True)

    value: str = Field(min_length=1)
    normalized: dict[str, Any] | None = None
    evidence: Evidence | None = None
    status: FieldStatus
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_not_found_invariants(self) -> Self:
        """Keep "missing" and "found" from blurring into each other.

        Raises:
            ValueError: If a NOT_FOUND field carries data, or a found field
                lacks evidence.
        """
        is_sentinel = self.value == NOT_FOUND
        if is_sentinel != (self.status is FieldStatus.NOT_FOUND):
            raise ValueError(
                f"value={self.value!r} and status={self.status!r} disagree: "
                f"the {NOT_FOUND} sentinel and status 'not_found' must be used together"
            )
        if is_sentinel and (self.evidence is not None or self.normalized is not None):
            raise ValueError(f"a {NOT_FOUND} field must not carry evidence or a normalized value")
        if not is_sentinel and self.evidence is None:
            raise ValueError(f"field value {self.value!r} has no evidence; use {NOT_FOUND} instead")
        return self

    @classmethod
    def not_found(cls, confidence: float = 1.0) -> FieldValue:
        """Build the canonical "this document does not state it" value.

        Args:
            confidence: How sure we are that the field is genuinely absent
                rather than missed by retrieval. Lower it when retrieval was
                weak (Phase 5 marks irrelevant retrieval this way).

        Returns:
            A NOT_FOUND :class:`FieldValue`.
        """
        return cls(value=NOT_FOUND, status=FieldStatus.NOT_FOUND, confidence=confidence)

    @property
    def is_found(self) -> bool:
        """Whether this field holds a usable value."""
        return self.status is FieldStatus.EXTRACTED


class TariffExtraction(BaseModel):
    """The full set of tariff fields for one product, from one monitoring run.

    This is what gets validated, stored as a snapshot, diffed against the
    previous snapshot and rendered into the business report.

    Attributes:
        bank: Bank name, e.g. ``"ACBA Bank"``.
        product_id: Product registry id, e.g. ``"consumer_loan"``.
        document_name: Primary document the values were extracted from.
        source_url: URL of that document.
        retrieved_at: When the document was fetched (UTC).
        fields: Every registry field id mapped to its value. Always complete.
    """

    model_config = ConfigDict(frozen=True)

    bank: str
    product_id: str
    document_name: str
    source_url: HttpUrl
    retrieved_at: datetime
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

    @property
    def found_field_ids(self) -> tuple[str, ...]:
        """Ids of fields that hold a usable value, in registry order."""
        return tuple(fid for fid in FIELD_IDS if self.fields[fid].is_found)

    @property
    def not_found_field_ids(self) -> tuple[str, ...]:
        """Ids of fields the document does not state, in registry order."""
        return tuple(
            fid for fid in FIELD_IDS if self.fields[fid].status is FieldStatus.NOT_FOUND
        )
