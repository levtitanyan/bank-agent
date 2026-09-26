"""The business report: what the tariffs are, where each number came from, what moved.

Written for someone who has to act on it, not for whoever built it. Three things
are therefore stated before any value:

* **How the values were produced** - which extractor, which model, which prompt,
  and whether the run asked the model anything at all. A cached run and a live
  one are different claims, and a rule-based run is a different claim again.
* **When the source was last checked and when it last changed.** ACBA's mortgage
  summary states «Թարմացվել է առ՝ 15.05.2023թ.»; a reader seeing 11.9-12.5%
  needs to know that without going to look for it.
* **Whether anything is unconfirmed.** A value awaiting review is shown, marked,
  and not presented as settled.

Missing fields are listed with the reason they are missing, because "the bank
does not charge this" and "we could not find it" are different answers and a
blank line says neither.
"""

from __future__ import annotations

from tariff_agent.extraction.conflict import FieldConflict
from tariff_agent.extraction.pipeline import ExtractionOutcome
from tariff_agent.fields import FIELD_IDS, FIELDS_BY_ID
from tariff_agent.models import FieldStatus, TariffExtraction
from tariff_agent.snapshots.diff import Provenance, Significance, SnapshotDiff
from tariff_agent.snapshots.store import SnapshotStatus

_RULE = "─" * 78
_STATUS_LABEL = {
    FieldStatus.FOUND: "",
    FieldStatus.UNVERIFIED: "  [unverified — needs a human]",
    FieldStatus.CONFLICT: "  [sources disagree]",
}


def render_report(
    outcome: ExtractionOutcome,
    *,
    diff: SnapshotDiff | None = None,
    status: SnapshotStatus = SnapshotStatus.STORED,
    review_note: str | None = None,
) -> str:
    """Render one monitoring run for a business reader.

    Args:
        outcome: What the run extracted, validated and found in conflict.
        diff: The comparison against the previous snapshot, when there is one.
        status: Whether the snapshot is confirmed or awaiting review.
        review_note: What a reviewer said, when they have said anything.

    Returns:
        The report as plain text.
    """
    extraction = outcome.extraction
    lines: list[str] = []
    lines.extend(_header(extraction, outcome, status, review_note))
    lines.extend(_provenance(extraction, outcome))
    lines.extend(_values(extraction))
    lines.extend(_missing(extraction, outcome))
    if diff is not None:
        lines.extend(_changes(diff))
    if outcome.conflicts:
        lines.extend(_conflicts(outcome.conflicts))
    return "\n".join(lines)


def _header(
    extraction: TariffExtraction,
    outcome: ExtractionOutcome,
    status: SnapshotStatus,
    review_note: str | None,
) -> list[str]:
    """Build the title block.

    Args:
        extraction: The extracted values.
        outcome: The run outcome.
        status: The snapshot's review status.
        review_note: A reviewer's comment, if any.

    Returns:
        The lines.
    """
    lines = [
        _RULE,
        f"{extraction.bank} — {extraction.product_id}",
        _RULE,
    ]
    if status is SnapshotStatus.PENDING_REVIEW:
        lines.append(
            "⚠ AWAITING REVIEW — the values below are recorded but not confirmed. "
            "See «Detected changes» for what a reviewer must settle."
        )
        lines.append("")
    elif status is SnapshotStatus.REJECTED:
        lines.append("⚠ REJECTED BY A REVIEWER — kept for the audit trail, not for use.")
        lines.append("")
    if review_note:
        lines.append(f"Reviewer note: {review_note}")
        lines.append("")
    return lines


def _provenance(extraction: TariffExtraction, outcome: ExtractionOutcome) -> list[str]:
    """Build the block saying how and when these values were produced.

    Args:
        extraction: The extracted values.
        outcome: The run outcome.

    Returns:
        The lines.
    """
    checked = extraction.checked_at or extraction.retrieved_at
    changed = extraction.document_date
    edition = f", edition {extraction.document_edition}" if extraction.document_edition else ""
    age = ""
    if changed is not None:
        age = "  ← the bank's own date on this document"

    lines = [
        "Source",
        f"  Document:        {extraction.document_name}{edition}",
        f"  URL:             {extraction.source_url}",
        f"  Last checked:    {checked:%Y-%m-%d %H:%M UTC}  (we asked the bank)",
        (
            f"  Last changed:    {changed:%Y-%m-%d}{age}"
            if changed is not None
            else "  Last changed:    not stated by the document"
        ),
        "",
        "How these values were produced",
        f"  Extraction:      {extraction.extraction_method}"
        + (
            "   ← pattern matching, not a model"
            if extraction.extraction_method == "rule_based"
            else ""
        ),
        f"  Prompt version:  {extraction.prompt_version}",
        f"  Model calls:     {outcome.model_calls}"
        + ("   ← replayed from cache, nothing was asked" if outcome.from_cache else ""),
        f"  Completeness:    {outcome.validation.completeness:.0%} of required fields",
        "",
    ]
    return lines


def _values(extraction: TariffExtraction) -> list[str]:
    """Build the tariff table, with evidence under each value.

    Args:
        extraction: The extracted values.

    Returns:
        The lines.
    """
    lines = ["Tariff", _RULE]
    for field_id in FIELD_IDS:
        value = extraction.fields[field_id]
        if value.status is FieldStatus.NOT_FOUND:
            continue
        spec = FIELDS_BY_ID[field_id]
        marker = _STATUS_LABEL.get(value.status, "")
        lines.append(f"{spec.label_hy}:  {value.value}{marker}")
        for variant in value.variants:
            lines.append(f"    · {variant.label}: {variant.value}")
        if value.evidence is not None:
            page = f"page {value.evidence.page}" if value.evidence.page else "web page"
            section = f" → {value.evidence.section}" if value.evidence.section else ""
            lines.append(f"    evidence: {value.evidence.document_name} → {page}{section}")
            lines.append(f'    quote:    "{value.evidence.quote}"')
        lines.append("")
    return lines


def _missing(extraction: TariffExtraction, outcome: ExtractionOutcome) -> list[str]:
    """List the fields with no value, and why each has none.

    Args:
        extraction: The extracted values.
        outcome: The run outcome, whose retrieval results explain the absences.

    Returns:
        The lines.
    """
    missing = [
        field_id
        for field_id in FIELD_IDS
        if extraction.fields[field_id].status is FieldStatus.NOT_FOUND
    ]
    if not missing:
        return []
    lines = ["Not stated by these documents", _RULE]
    for field_id in missing:
        spec = FIELDS_BY_ID[field_id]
        retrieval = outcome.retrieval.get(field_id)
        reason = retrieval.reason if retrieval else "no evidence was retrieved"
        lines.append(f"{spec.label_hy}:  NOT_FOUND")
        lines.append(f"    why: {reason}")
    lines.append("")
    return lines


def _changes(diff: SnapshotDiff) -> list[str]:
    """Describe what moved since the previous run.

    Args:
        diff: The comparison.

    Returns:
        The lines.
    """
    lines = ["Detected changes", _RULE]
    if diff.is_baseline:
        lines.extend(["First run for this product — recorded as the baseline.", ""])
        return lines
    if not diff.is_trustworthy:
        marker = (
            "the extractor changed"
            if diff.provenance is Provenance.METHOD_CHANGED
            else "our instructions changed"
        )
        lines.append(f"⚠ Not comparable with the previous run: {marker}.")
    for note in diff.notes:
        lines.append(f"  note: {note}")
    if not diff.has_changes:
        lines.extend(["No change since the previous run.", ""])
        return lines

    for change in diff.changes:
        flag = "  ⚠ LARGE" if change.significance is Significance.LARGE else ""
        lines.append(f"{change.described()}{flag}")
        if change.note:
            lines.append(f"    {change.note}")
        if change.evidence_before is not None:
            lines.append(f'    was:  "{change.evidence_before.quote}"')
        if change.evidence_after is not None:
            lines.append(f'    now:  "{change.evidence_after.quote}"')
    lines.append("")
    return lines


def _conflicts(conflicts: list[FieldConflict]) -> list[str]:
    """Describe disagreements between official sources.

    Args:
        conflicts: The conflicts found.

    Returns:
        The lines.
    """
    lines = ["Sources disagree", _RULE]
    for conflict in conflicts:
        lines.append(FIELDS_BY_ID[conflict.field_id].label_hy + ":")
        lines.append(f"    primary:    {conflict.primary.described}")
        lines.append(f"    supporting: {conflict.supporting.described}")
        older = conflict.older_source
        if older is not None and older.document_date is not None:
            lines.append(
                f"    the {older.role} source is the older of the two "
                f"({older.document_date:%Y-%m-%d})"
            )
    lines.append("")
    return lines
