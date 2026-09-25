"""Noticing when the bank's own documents disagree.

ACBA states a nominal mortgage rate of 11.9-12.5% in its information summary and
13.75-14.5% on the product page for the same loan. Both are official. Averaging
them, or silently preferring one, would report a rate the customer will not be
offered.

The comparison is made on **normalized** values, so «13,5%» and «13.5 %» are the
same number, and only genuinely disjoint ranges count as a conflict. The record
carries each source's own date, because the summary is from 2023 and the page is
current - which is usually what settles it, and is exactly what a reviewer needs
to see rather than deduce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from tariff_agent.models import Evidence, FieldStatus, FieldValue
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ConflictSource:
    """One side of a disagreement.

    Attributes:
        role: Whether this came from the product's primary or supporting source.
        value: The value as written.
        evidence: Where it was stated.
        document_date: The date that document gives for itself, when it does.
    """

    role: str
    value: str
    evidence: Evidence
    document_date: date | None = None

    @property
    def described(self) -> str:
        """A one-line description for a reviewer."""
        dated = f", dated {self.document_date.isoformat()}" if self.document_date else ""
        page = f", page {self.evidence.page}" if self.evidence.page else ""
        return f"{self.value} ({self.role}: {self.evidence.document_name}{page}{dated})"


@dataclass(frozen=True, slots=True)
class FieldConflict:
    """Two official sources stating different values for one field.

    Attributes:
        field_id: The field they disagree about.
        primary: The primary source's version.
        supporting: The supporting source's version.
        summary: A sentence describing the disagreement.
    """

    field_id: str
    primary: ConflictSource
    supporting: ConflictSource
    summary: str

    @property
    def older_source(self) -> ConflictSource | None:
        """Whichever source states an earlier date, when both state one."""
        if self.primary.document_date and self.supporting.document_date:
            return min(
                (self.primary, self.supporting), key=lambda source: source.document_date or date.max
            )
        return None


def detect_conflict(
    field_id: str,
    primary: FieldValue,
    supporting: FieldValue,
    *,
    primary_date: date | None = None,
    supporting_date: date | None = None,
) -> FieldConflict | None:
    """Compare two sources' answers for one field.

    Args:
        field_id: The field.
        primary: The primary source's value.
        supporting: A supporting source's value.
        primary_date: Date of the primary document.
        supporting_date: Date of the supporting document.

    Returns:
        The conflict, or ``None`` when the two agree, overlap, or when either
        side has nothing to compare. Silence is the right answer far more often
        than a conflict is, and a false conflict costs a reviewer's attention.
    """
    if primary.status is FieldStatus.NOT_FOUND or supporting.status is FieldStatus.NOT_FOUND:
        return None
    if primary.evidence is None or supporting.evidence is None:
        return None
    if not _disagree(primary, supporting):
        return None

    conflict = FieldConflict(
        field_id=field_id,
        primary=ConflictSource(
            role="primary",
            value=primary.value,
            evidence=primary.evidence,
            document_date=primary_date,
        ),
        supporting=ConflictSource(
            role="supporting",
            value=supporting.value,
            evidence=supporting.evidence,
            document_date=supporting_date,
        ),
        summary=(
            f"{field_id}: the primary source says {primary.value!r} and a supporting "
            f"source says {supporting.value!r}"
        ),
    )
    logger.warning(
        "sources_conflict",
        extra={
            "field": field_id,
            "primary": primary.value,
            "supporting": supporting.value,
            "primary_date": primary_date.isoformat() if primary_date else None,
            "supporting_date": supporting_date.isoformat() if supporting_date else None,
        },
    )
    return conflict


def _disagree(primary: FieldValue, supporting: FieldValue) -> bool:
    """Decide whether two values genuinely differ.

    Args:
        primary: The primary source's value.
        supporting: The supporting source's value.

    Returns:
        True when the values are incompatible. Numeric ranges that overlap at
        all are treated as agreeing: «17.5-21.6%» and «20.1-21.6%» describe the
        same product through different channels, not a contradiction.
    """
    left, right = primary.normalized, supporting.normalized
    if left is None or right is None:
        return _normalize_text(primary.value) != _normalize_text(supporting.value)

    for low_key, high_key in (("min", "max"), ("min_months", "max_months")):
        low_a, high_a = left.get(low_key), left.get(high_key)
        low_b, high_b = right.get(low_key), right.get(high_key)
        if all(isinstance(value, int | float) for value in (low_a, high_a, low_b, high_b)):
            return _disjoint(low_a, high_a, low_b, high_b)  # type: ignore[arg-type]

    if "codes" in left and "codes" in right:
        return set(left["codes"]) != set(right["codes"])
    if "text" in left and "text" in right:
        return str(left["text"]) != str(right["text"])
    return left != right


def _disjoint(low_a: float, high_a: float, low_b: float, high_b: float) -> bool:
    """Whether two ranges fail to overlap.

    Args:
        low_a: Lower bound of the first range.
        high_a: Upper bound of the first range.
        low_b: Lower bound of the second range.
        high_b: Upper bound of the second range.

    Returns:
        True when the ranges share no value.
    """
    return high_a < low_b or high_b < low_a


def _normalize_text(value: str) -> str:
    """Collapse a value for textual comparison.

    Args:
        value: The raw value.

    Returns:
        The value with whitespace collapsed and case folded.
    """
    return " ".join(value.split()).casefold()


def summarize(conflicts: list[FieldConflict]) -> dict[str, Any]:
    """Describe a set of conflicts for logging and reporting.

    Args:
        conflicts: The conflicts found.

    Returns:
        A mapping naming the fields and the sources involved.
    """
    return {
        "count": len(conflicts),
        "fields": [conflict.field_id for conflict in conflicts],
        "details": [
            {
                "field": conflict.field_id,
                "primary": conflict.primary.described,
                "supporting": conflict.supporting.described,
            }
            for conflict in conflicts
        ],
    }
