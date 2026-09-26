"""One monitoring run, start to finish.

Retrieve, extract, compare with last time, ask a human if anything needs one,
store, report. The order matters and is the phase's whole argument:

* the snapshot is stored **before** the human answers, not after. An unattended
  nightly run must record what the bank published exactly when it changed, and
  a reviewer's later decision resolves that record rather than creating it.
* a large change or a source conflict marks the snapshot ``pending_review`` and
  the report says so, so nothing unconfirmed is presented as settled.
* a decision already given about the same thing is not asked again.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from tariff_agent.config import Allowlist, MonitoringConfig, Product
from tariff_agent.documents.document import Document
from tariff_agent.extraction.conflict import FieldConflict
from tariff_agent.extraction.extractor import Extractor
from tariff_agent.extraction.pipeline import ExtractionOutcome, extract_tariffs
from tariff_agent.fields import FIELDS_BY_ID
from tariff_agent.observability.logging import get_logger
from tariff_agent.rag.retrieval import Retriever
from tariff_agent.snapshots.diff import (
    FieldChange,
    SnapshotDiff,
    baseline_diff,
    diff_snapshots,
)
from tariff_agent.snapshots.report import render_report
from tariff_agent.snapshots.review import (
    Decision,
    Reviewer,
    ReviewLog,
    ReviewOutcome,
    ReviewRequest,
    ReviewTrigger,
    ask,
)
from tariff_agent.snapshots.store import SnapshotStatus, SnapshotStore

logger = get_logger(__name__)


@dataclass
class MonitoringResult:
    """Everything one monitoring run produced.

    Attributes:
        product_id: The product monitored.
        outcome: What was extracted and validated.
        diff: What changed since the previous snapshot.
        snapshot_id: The row this run stored.
        status: Whether that row is confirmed or awaiting review.
        reviews: The decisions taken, or recalled, during the run.
        report: The rendered business report.
    """

    product_id: str
    outcome: ExtractionOutcome
    diff: SnapshotDiff
    snapshot_id: int
    status: SnapshotStatus
    reviews: list[tuple[ReviewRequest, ReviewOutcome]] = dataclass_field(default_factory=list)
    report: str = ""

    @property
    def needs_attention(self) -> bool:
        """Whether a person still has to look at this run."""
        return self.status is SnapshotStatus.PENDING_REVIEW


def run_monitoring(
    product: Product,
    bank: str,
    retriever: Retriever,
    extractor: Extractor,
    allowlist: Allowlist,
    store: SnapshotStore,
    *,
    monitoring: MonitoringConfig,
    reviewer: Reviewer | None = None,
    doc_id: str = "",
    top_k: int = 4,
    primary_document: Document | None = None,
) -> MonitoringResult:
    """Run one monitoring cycle for a product.

    Args:
        product: The product to monitor.
        bank: Bank name, recorded on the snapshot.
        retriever: The product's knowledge layer.
        extractor: The extraction backend.
        allowlist: Domains evidence may come from.
        store: Where snapshots and decisions are kept.
        monitoring: Thresholds and review policy.
        reviewer: Who to ask when something needs a human. Without one, nothing
            is asked and anything needing review stays pending.
        doc_id: Content hash of the primary document.
        top_k: Passages per field per source role.
        primary_document: The document the values mostly come from, supplying
            the dates and edition the report states.

    Returns:
        The result, including the rendered report.
    """
    outcome = extract_tariffs(
        product,
        bank,
        retriever,
        extractor,
        allowlist,
        top_k=top_k,
        primary_document=primary_document,
    )

    previous = store.latest(product.id)
    if previous is None:
        diff = baseline_diff(product.id)
    else:
        diff = diff_snapshots(
            previous.extraction,
            outcome.extraction,
            large_rate_points=monitoring.thresholds.large_rate_points,
            large_amount_percent=monitoring.thresholds.large_amount_percent,
            retrieval_reached={
                field_id: result.is_relevant for field_id, result in outcome.retrieval.items()
            },
        )

    requests = review_requests(product.id, diff, outcome.conflicts, monitoring)
    status = SnapshotStatus.PENDING_REVIEW if requests else SnapshotStatus.STORED

    # Stored first, deliberately: the record of what the bank published must not
    # depend on someone being awake to confirm it.
    snapshot_id = store.save(outcome.extraction, run_id=_run_id(), doc_id=doc_id, status=status)

    reviews: list[tuple[ReviewRequest, ReviewOutcome]] = []
    if requests and reviewer is not None:
        log = ReviewLog(store)
        decisions = []
        for request in requests:
            result = ask(reviewer, log, request, snapshot_id=snapshot_id)
            reviews.append((request, result))
            decisions.append(result.decision)
        status = resolve_status(store, snapshot_id, decisions)

    note = next((outcome_.note for _, outcome_ in reviews if outcome_.note), None)
    report = render_report(outcome, diff=diff, status=status, review_note=note)

    logger.info(
        "monitoring_run_complete",
        extra={
            "product_id": product.id,
            "snapshot_id": snapshot_id,
            "status": status.value,
            "changes": len(diff.changes),
            "large_changes": len(diff.large_changes),
            "conflicts": len(outcome.conflicts),
            "reviews": len(reviews),
            "baseline": diff.is_baseline,
        },
    )
    return MonitoringResult(
        product_id=product.id,
        outcome=outcome,
        diff=diff,
        snapshot_id=snapshot_id,
        status=status,
        reviews=reviews,
        report=report,
    )


def _run_id() -> str:
    """Return the current run's identifier, or a fresh one.

    Returns:
        The correlation id every log line of this run already carries.
    """
    from tariff_agent.observability.logging import current_run_id, new_run_id

    return current_run_id() or new_run_id()


def review_requests(
    product_id: str,
    diff: SnapshotDiff,
    conflicts: list[FieldConflict],
    monitoring: MonitoringConfig,
) -> list[ReviewRequest]:
    """Decide what, if anything, a human must settle.

    Public because the agent tools in Phase 8 must raise the *same* questions
    this pipeline raises. Two copies of the review policy would drift, and the
    one that drifted would be the one nobody ran nightly.

    Args:
        product_id: The product.
        diff: What changed.
        conflicts: Disagreements between sources.
        monitoring: Which triggers are switched on.

    Returns:
        The questions to ask, each carrying the evidence needed to answer it.
    """
    requests: list[ReviewRequest] = []

    if monitoring.review.on_large_change:
        requests.extend(_large_change_request(product_id, change) for change in diff.large_changes)

    if monitoring.review.on_source_conflict:
        for conflict in conflicts:
            evidence = [
                f"primary:    {conflict.primary.described}",
                f'            "{conflict.primary.evidence.quote}"',
                f"supporting: {conflict.supporting.described}",
                f'            "{conflict.supporting.evidence.quote}"',
            ]
            older = conflict.older_source
            if older is not None and older.document_date is not None:
                evidence.append(
                    f"the {older.role} source is older ({older.document_date:%Y-%m-%d})"
                )
            requests.append(
                ReviewRequest(
                    trigger=ReviewTrigger.SOURCE_CONFLICT,
                    product_id=product_id,
                    subject=f"conflict:{conflict.field_id}:"
                    f"{conflict.primary.value}|{conflict.supporting.value}",
                    question=(
                        f"Two official sources state different values for "
                        f"{FIELDS_BY_ID[conflict.field_id].label_hy}. Which should be reported?"
                    ),
                    evidence=tuple(evidence),
                    details={"field_id": conflict.field_id},
                )
            )
    return requests


def _large_change_request(product_id: str, change: FieldChange) -> ReviewRequest:
    """Build the question asked about one large change.

    Args:
        product_id: The product.
        change: The change needing confirmation.

    Returns:
        The request, carrying both quotes so the reviewer can check the move.
    """
    evidence = []
    if change.evidence_before is not None:
        evidence.append(f'was:  "{change.evidence_before.quote}"')
    if change.evidence_after is not None:
        evidence.append(f'now:  "{change.evidence_after.quote}"')
        page = change.evidence_after.page
        evidence.append(
            f"source: {change.evidence_after.document_name}"
            + (f", page {page}" if page else "")
            + (f" → {change.evidence_after.section}" if change.evidence_after.section else "")
        )
    return ReviewRequest(
        trigger=ReviewTrigger.LARGE_CHANGE,
        product_id=product_id,
        # Keyed by the specific move, so confirming 12.5% → 13.5% does not also
        # confirm whatever it changes to next month.
        subject=f"change:{change.field_id}:{change.before}->{change.after}",
        question=f"{change.described()} — is this correct?",
        evidence=tuple(evidence),
        details={"field_id": change.field_id, "magnitude": change.magnitude},
    )


def resolve_status(
    store: SnapshotStore, snapshot_id: int, decisions: list[Decision]
) -> SnapshotStatus:
    """Apply the reviewer's decisions to the stored snapshot.

    Public for the same reason as :func:`review_requests`: the agent resolves a
    snapshot exactly as the scheduled run does.

    Args:
        store: Where the snapshot lives.
        snapshot_id: The row to resolve.
        decisions: What was decided about it.

    Returns:
        The snapshot's new status. One rejection rejects the snapshot; anything
        deferred leaves it pending; otherwise it is confirmed.
    """
    if Decision.REJECTED in decisions:
        status = SnapshotStatus.REJECTED
    elif Decision.DEFERRED in decisions:
        status = SnapshotStatus.PENDING_REVIEW
    else:
        status = SnapshotStatus.CONFIRMED
    store.set_status(snapshot_id, status)
    return status
