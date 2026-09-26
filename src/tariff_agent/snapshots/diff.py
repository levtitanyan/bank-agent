"""Comparing a new extraction with the last one, and reporting only what moved.

Three kinds of difference look alike in the data and mean entirely different
things to a reader, so each is named rather than lumped into "changed":

* **The bank changed a value.** «12.5%» became «13.5%». This is the event the
  system exists to catch.
* **We started or stopped finding something.** A field going from NOT_FOUND to a
  value may mean the bank introduced a fee - or that retrieval finally reached a
  passage it had been missing. The second is our news, not the bank's, and the
  diff says which by reporting whether retrieval reached the field before.
* **We changed.** A new prompt, a different model, a switch between the model and
  the offline extractor: all of these can move a value while the document sits
  untouched. Such a comparison is **annotated, never reported as a bank change**.

Everything is compared on normalized values, so «10 000 000» and «10,000,000»
are silent, and magnitude is measured in the unit the field is actually written
in - percentage points for a rate, relative percent for an amount, months for a
term - which the field registry already knows.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from typing import Any

from tariff_agent.fields import FIELD_IDS, FIELDS_BY_ID, ValueKind
from tariff_agent.models import Evidence, FieldStatus, FieldValue, TariffExtraction
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)


class ChangeKind(StrEnum):
    """What sort of difference was found."""

    VALUE = "value"
    """A stated value moved."""

    APPEARED = "appeared"
    """A field that was missing now has a value."""

    DISAPPEARED = "disappeared"
    """A field that had a value is now missing."""

    VARIANT = "variant"
    """A per-channel breakdown changed while the overall value held."""

    STATUS = "status"
    """The field's confidence changed - verified to unverified, say - without
    the value itself moving."""


class Significance(StrEnum):
    """How much attention a change deserves."""

    SILENT = "silent"
    """Formatting only. Not reported."""

    NOTABLE = "notable"
    """A real change within normal bounds."""

    LARGE = "large"
    """Big enough that a human should confirm it before it is trusted."""


class Provenance(StrEnum):
    """Whether the two snapshots are actually comparable."""

    COMPARABLE = "comparable"
    """Same extractor and same prompt: a difference means the bank changed."""

    METHOD_CHANGED = "method_changed"
    """Different extractors. A difference measures them, not the bank."""

    PROMPT_CHANGED = "prompt_changed"
    """Different prompt versions. Same problem, different cause."""


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One field's difference between two snapshots.

    Attributes:
        field_id: The field.
        kind: What sort of difference.
        before: The previous value, as written.
        after: The new value, as written.
        magnitude: How far it moved, in the field's own unit, when measurable.
        unit: What the magnitude is expressed in.
        significance: How much attention it deserves.
        note: Anything a reader needs in order not to misread it.
        evidence_before: Where the previous value was stated.
        evidence_after: Where the new value is stated.
    """

    field_id: str
    kind: ChangeKind
    before: str
    after: str
    significance: Significance
    magnitude: float | None = None
    unit: str | None = None
    note: str | None = None
    evidence_before: Evidence | None = None
    evidence_after: Evidence | None = None

    def described(self) -> str:
        """Render the change as a reader sees it.

        Returns:
            A single line naming the field, both values and the movement.
        """
        label = FIELDS_BY_ID[self.field_id].label_hy
        if self.magnitude is not None:
            movement = f" ({self.magnitude:+.2f} {self.unit})"
        else:
            movement = ""
        return f"{label}: {self.before} → {self.after}{movement}"


@dataclass
class SnapshotDiff:
    """The difference between two snapshots of one product.

    Attributes:
        product_id: The product.
        changes: Everything that moved, most significant first.
        unchanged: Fields that were identical after normalization.
        provenance: Whether the two snapshots are comparable at all.
        notes: Anything qualifying the whole comparison.
        is_baseline: True when there was no previous snapshot, so nothing could
            be compared and nothing is a change.
    """

    product_id: str
    changes: list[FieldChange] = dataclass_field(default_factory=list)
    unchanged: list[str] = dataclass_field(default_factory=list)
    provenance: Provenance = Provenance.COMPARABLE
    notes: list[str] = dataclass_field(default_factory=list)
    is_baseline: bool = False

    @property
    def has_changes(self) -> bool:
        """Whether anything worth reporting moved."""
        return bool(self.changes)

    @property
    def large_changes(self) -> list[FieldChange]:
        """The changes that need a human before they are trusted."""
        return [change for change in self.changes if change.significance is Significance.LARGE]

    @property
    def is_trustworthy(self) -> bool:
        """Whether differences can be attributed to the bank at all."""
        return self.provenance is Provenance.COMPARABLE


def baseline_diff(product_id: str) -> SnapshotDiff:
    """Describe a first run, where there is nothing to compare against.

    Args:
        product_id: The product.

    Returns:
        A diff marked as a baseline. A first run reporting ten "changes" would
        be both wrong and alarming.
    """
    return SnapshotDiff(
        product_id=product_id,
        is_baseline=True,
        notes=["first run for this product: recorded as the baseline, nothing compared"],
    )


def diff_snapshots(
    previous: TariffExtraction,
    current: TariffExtraction,
    *,
    large_rate_points: float = 2.0,
    large_amount_percent: float = 25.0,
    retrieval_reached: dict[str, bool] | None = None,
) -> SnapshotDiff:
    """Compare two snapshots of the same product.

    Args:
        previous: The last stored extraction.
        current: The new one.
        large_rate_points: Percentage points beyond which a rate change is large.
        large_amount_percent: Relative percent beyond which an amount change is large.
        retrieval_reached: Per field, whether retrieval found anything this run.
            Used to tell "the bank added a fee" from "we finally found it".

    Returns:
        The differences, with provenance recorded.
    """
    diff = SnapshotDiff(product_id=current.product_id)
    diff.provenance = _provenance(previous, current)
    if diff.provenance is Provenance.METHOD_CHANGED:
        diff.notes.append(
            f"the previous snapshot was produced by {previous.extraction_method!r} and this one "
            f"by {current.extraction_method!r}: differences below measure the two extractors, "
            "not a change at the bank"
        )
    elif diff.provenance is Provenance.PROMPT_CHANGED:
        diff.notes.append(
            f"the instructions changed between these runs (prompt v{previous.prompt_version} "
            f"→ v{current.prompt_version}): differences below may be ours rather than the bank's"
        )

    if previous.document_edition != current.document_edition:
        diff.notes.append(
            f"the bank published a new edition of the source document "
            f"({previous.document_edition or 'unstated'} → "
            f"{current.document_edition or 'unstated'})"
        )
    if previous.document_date != current.document_date:
        diff.notes.append(
            f"the source document's own date moved "
            f"({previous.document_date or 'unstated'} → {current.document_date or 'unstated'})"
        )

    for field_id in FIELD_IDS:
        before = previous.fields.get(field_id) or FieldValue.not_found()
        after = current.fields[field_id]
        change = _compare_field(
            field_id,
            before,
            after,
            large_rate_points=large_rate_points,
            large_amount_percent=large_amount_percent,
            reached=(retrieval_reached or {}).get(field_id),
        )
        if change is None:
            diff.unchanged.append(field_id)
        else:
            diff.changes.append(change)

    order = {Significance.LARGE: 0, Significance.NOTABLE: 1, Significance.SILENT: 2}
    diff.changes.sort(key=lambda change: order[change.significance])
    logger.info(
        "snapshot_compared",
        extra={
            "product_id": current.product_id,
            "changes": len(diff.changes),
            "large": len(diff.large_changes),
            "unchanged": len(diff.unchanged),
            "provenance": diff.provenance.value,
        },
    )
    return diff


def _provenance(previous: TariffExtraction, current: TariffExtraction) -> Provenance:
    """Decide whether two snapshots can be compared as the bank's own history.

    Args:
        previous: The earlier snapshot.
        current: The later one.

    Returns:
        What, if anything, changed on our side between them.
    """
    if previous.extraction_method != current.extraction_method:
        return Provenance.METHOD_CHANGED
    if previous.prompt_version != current.prompt_version:
        return Provenance.PROMPT_CHANGED
    return Provenance.COMPARABLE


def _compare_field(
    field_id: str,
    before: FieldValue,
    after: FieldValue,
    *,
    large_rate_points: float,
    large_amount_percent: float,
    reached: bool | None,
) -> FieldChange | None:
    """Compare one field across two snapshots.

    Args:
        field_id: The field.
        before: Its previous value.
        after: Its new value.
        large_rate_points: Threshold for a large rate move.
        large_amount_percent: Threshold for a large amount move.
        reached: Whether retrieval found anything for this field this run.

    Returns:
        The change, or ``None`` when nothing meaningful moved.
    """
    was_missing = before.status is FieldStatus.NOT_FOUND
    is_missing = after.status is FieldStatus.NOT_FOUND

    if was_missing and is_missing:
        return None
    if was_missing:
        return FieldChange(
            field_id=field_id,
            kind=ChangeKind.APPEARED,
            before="NOT_FOUND",
            after=after.value,
            significance=Significance.NOTABLE,
            note=(
                "this field was previously missing; it may be newly published, or newly "
                "retrieved by us" if reached is not False else None
            ),
            evidence_after=after.evidence,
        )
    if is_missing:
        return FieldChange(
            field_id=field_id,
            kind=ChangeKind.DISAPPEARED,
            before=before.value,
            after="NOT_FOUND",
            significance=Significance.NOTABLE,
            note=(
                "retrieval found nothing for this field in this run, so its absence may be ours"
                if reached is False
                else "the document no longer states this field"
            ),
            evidence_before=before.evidence,
        )

    # Comparing a verified value with an unverified one measures our confidence,
    # not the bank's tariff.
    if before.status is not after.status:
        return FieldChange(
            field_id=field_id,
            kind=ChangeKind.STATUS,
            before=f"{before.value} [{before.status.value}]",
            after=f"{after.value} [{after.status.value}]",
            significance=Significance.NOTABLE,
            note="the value's confidence changed; the stated value itself may not have",
            evidence_before=before.evidence,
            evidence_after=after.evidence,
        )

    magnitude, unit = _magnitude(field_id, before.normalized, after.normalized)
    if _same_value(before, after):
        variant_change = _variant_change(field_id, before, after)
        return variant_change

    significance = _significance(
        field_id, magnitude, large_rate_points=large_rate_points,
        large_amount_percent=large_amount_percent,
    )
    return FieldChange(
        field_id=field_id,
        kind=ChangeKind.VALUE,
        before=before.value,
        after=after.value,
        magnitude=magnitude,
        unit=unit,
        significance=significance,
        evidence_before=before.evidence,
        evidence_after=after.evidence,
    )


def _same_value(before: FieldValue, after: FieldValue) -> bool:
    """Whether two values are the same after normalization.

    Args:
        before: The previous value.
        after: The new one.

    Returns:
        True when they mean the same thing. Normalized forms are compared when
        both exist - «10 000 000» and «10,000,000» are one number - and the
        verbatim text only as a fallback.
    """
    if before.normalized is not None and after.normalized is not None:
        return before.normalized == after.normalized
    return " ".join(before.value.split()) == " ".join(after.value.split())


def _variant_change(field_id: str, before: FieldValue, after: FieldValue) -> FieldChange | None:
    """Detect a per-channel change behind an unchanged headline value.

    Args:
        field_id: The field.
        before: The previous value.
        after: The new one.

    Returns:
        The change, or None when the variants match too. A branch rate moving
        while the overall range holds is a real event for the customer who uses
        that channel.
    """
    previous = {variant.label: variant.value for variant in before.variants}
    current = {variant.label: variant.value for variant in after.variants}
    if previous == current:
        return None
    moved = [
        f"{label}: {previous.get(label, 'absent')} → {value}"
        for label, value in current.items()
        if previous.get(label) != value
    ]
    gone = [label for label in previous if label not in current]
    if gone:
        moved.append(f"no longer stated: {', '.join(gone)}")
    return FieldChange(
        field_id=field_id,
        kind=ChangeKind.VARIANT,
        before="; ".join(f"{k}: {v}" for k, v in previous.items()) or "no breakdown",
        after="; ".join(f"{k}: {v}" for k, v in current.items()) or "no breakdown",
        significance=Significance.NOTABLE,
        note="the overall value is unchanged; a channel's own value moved: " + "; ".join(moved),
        evidence_before=before.evidence,
        evidence_after=after.evidence,
    )


def _magnitude(
    field_id: str, before: dict[str, Any] | None, after: dict[str, Any] | None
) -> tuple[float | None, str | None]:
    """Measure how far a value moved, in the unit the field is written in.

    Args:
        field_id: The field.
        before: The previous normalized value.
        after: The new one.

    Returns:
        The movement and its unit, or ``(None, None)`` when it cannot be
        measured. A rate moves in percentage points, an amount in relative
        percent, a term in months - reporting a rate change as "8% higher"
        when it went from 12.5% to 13.5% would be true and useless.
    """
    if before is None or after is None:
        return None, None
    kind = FIELDS_BY_ID[field_id].kind

    if kind is ValueKind.PERCENT:
        low_before, low_after = before.get("min"), after.get("min")
        if isinstance(low_before, int | float) and isinstance(low_after, int | float):
            return round(low_after - low_before, 4), "percentage points"
    if kind is ValueKind.AMOUNT:
        high_before, high_after = before.get("max"), after.get("max")
        if (
            isinstance(high_before, int | float)
            and isinstance(high_after, int | float)
            and high_before
        ):
            return round((high_after - high_before) / high_before * 100.0, 2), "percent"
    if kind is ValueKind.TERM:
        high_before, high_after = before.get("max_months"), after.get("max_months")
        if isinstance(high_before, int | float) and isinstance(high_after, int | float):
            return round(high_after - high_before, 2), "months"
    return None, None


def _significance(
    field_id: str,
    magnitude: float | None,
    *,
    large_rate_points: float,
    large_amount_percent: float,
) -> Significance:
    """Decide how much attention a change deserves.

    Args:
        field_id: The field.
        magnitude: How far it moved, in its own unit.
        large_rate_points: Threshold for rates.
        large_amount_percent: Threshold for amounts.

    Returns:
        The significance. A change whose size cannot be measured is notable
        rather than silent: unmeasurable is not the same as small.
    """
    if magnitude is None:
        return Significance.NOTABLE
    kind = FIELDS_BY_ID[field_id].kind
    if kind is ValueKind.PERCENT and abs(magnitude) > large_rate_points:
        return Significance.LARGE
    if kind is ValueKind.AMOUNT and abs(magnitude) > large_amount_percent:
        return Significance.LARGE
    return Significance.NOTABLE
