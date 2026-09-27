"""Tests for storage, change detection, review and the report.

The question these answer is not "does SQLite work" but "does this system tell
the truth about what changed" - which is mostly about *not* reporting things:
formatting, our own fixes, and questions a reviewer already settled.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl

from tariff_agent.config import Allowlist, MonitoringConfig, Product
from tariff_agent.documents.document import DocumentKind
from tariff_agent.extraction.normalize import normalize
from tariff_agent.extraction.schema import ExtractedField
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
from tariff_agent.rag.retrieval import Retriever
from tariff_agent.snapshots.diff import (
    ChangeKind,
    Provenance,
    Significance,
    baseline_diff,
    diff_snapshots,
)
from tariff_agent.snapshots.pipeline import review_requests, run_monitoring
from tariff_agent.snapshots.review import (
    AutoReviewer,
    Decision,
    ReviewLog,
    ReviewRequest,
    ReviewTrigger,
    ScriptedReviewer,
    ask,
)
from tariff_agent.snapshots.store import SnapshotStatus, SnapshotStore

ALLOWLIST = Allowlist(allowed_schemes=("https",), domains=("acba.am", "www.acba.am"))
PRODUCT = Product(id="mortgage", kind="loan", name_hy="Հիփոթեքային վարկ", name_en="Mortgage")


def evidence(quote: str = "Տարեկան անվանական տոկոսադրույք՝ 13,5%") -> Evidence:
    """Build an evidence record."""
    return Evidence(
        document_name="Տեղեկատվական ամփոփագիր",
        source_url=HttpUrl("https://www.acba.am/files/loan%20info.pdf"),
        page=3,
        section="Տոկոսադրույք",
        quote=quote,
    )


def found(value: str, kind: ValueKind = ValueKind.PERCENT, quote: str | None = None) -> FieldValue:
    """Build a FOUND field with normalization applied."""
    return FieldValue(
        value=value,
        normalized=normalize(value, kind),
        evidence=evidence(quote or f"Տարեկան անվանական տոկոսադրույք՝ {value}"),
        status=FieldStatus.FOUND,
    )


def extraction(
    *,
    method: str = "gemini:test",
    prompt_version: int = 4,
    document_date: date | None = None,
    edition: str | None = None,
    **fields: FieldValue,
) -> TariffExtraction:
    """Build a complete extraction with the given fields."""
    complete = {field_id: FieldValue.not_found() for field_id in FIELD_IDS}
    complete.update(fields)
    now = datetime.now(UTC)
    return TariffExtraction(
        bank="ACBA Bank",
        product_id="mortgage",
        document_name="Տեղեկատվական ամփոփագիր",
        source_url=HttpUrl("https://www.acba.am/files/loan%20info.pdf"),
        retrieved_at=now,
        checked_at=now,
        document_date=document_date,
        document_edition=edition,
        extraction_method=method,
        prompt_version=prompt_version,
        fields=complete,
    )


# --------------------------------------------------------------------------- #
# What is, and is not, a change
# --------------------------------------------------------------------------- #


def test_reformatting_is_not_a_change() -> None:
    """«10 000 000» and «10,000,000» are one number.

    Without this the monitor would cry wolf every time the bank rewrapped a
    page, and a reviewer would stop reading it.
    """
    before = extraction(amount=found("10 000 000 ՀՀ դրամ", ValueKind.AMOUNT))
    after = extraction(amount=found("10,000,000 ՀՀ դրամ", ValueKind.AMOUNT))
    diff = diff_snapshots(before, after)
    assert not diff.has_changes
    assert "amount" in diff.unchanged


def test_a_rate_change_is_measured_in_percentage_points() -> None:
    """12.5% to 13.5% is one point, not eight percent."""
    diff = diff_snapshots(
        extraction(nominal_rate=found("12.5%")), extraction(nominal_rate=found("13.5%"))
    )
    change = diff.changes[0]
    assert change.kind is ChangeKind.VALUE
    assert change.magnitude == pytest.approx(1.0)
    assert change.unit == "percentage points"
    assert change.significance is Significance.NOTABLE
    assert "12.5%" in change.described() and "13.5%" in change.described()


def test_a_large_rate_move_is_flagged_for_a_human() -> None:
    """Beyond two points, a person confirms before it is trusted."""
    diff = diff_snapshots(
        extraction(nominal_rate=found("11.5%")), extraction(nominal_rate=found("18.0%"))
    )
    assert diff.large_changes
    assert diff.changes[0].significance is Significance.LARGE


def test_an_amount_change_is_measured_relatively() -> None:
    """A million on a ten-million ceiling is ten percent, not «1000000»."""
    diff = diff_snapshots(
        extraction(amount=found("1,000,000-10,000,000 ՀՀ դրամ", ValueKind.AMOUNT)),
        extraction(amount=found("1,000,000-11,000,000 ՀՀ դրամ", ValueKind.AMOUNT)),
    )
    assert diff.changes[0].magnitude == pytest.approx(10.0)
    assert diff.changes[0].unit == "percent"


def test_both_evidence_quotes_travel_with_a_change() -> None:
    """A reviewer settles a change by reading both sides, not by trusting us."""
    diff = diff_snapshots(
        extraction(nominal_rate=found("12.5%")), extraction(nominal_rate=found("13.5%"))
    )
    change = diff.changes[0]
    assert change.evidence_before is not None and "12.5%" in change.evidence_before.quote
    assert change.evidence_after is not None and "13.5%" in change.evidence_after.quote


def test_a_field_appearing_says_it_may_be_ours() -> None:
    """A new value may be a new fee, or retrieval finally reaching it."""
    diff = diff_snapshots(
        extraction(),
        extraction(service_fee=found("0.5%", ValueKind.FEE)),
        retrieval_reached={"service_fee": True},
    )
    change = diff.changes[0]
    assert change.kind is ChangeKind.APPEARED
    assert change.note is not None and "newly retrieved" in change.note


def test_a_field_disappearing_says_whether_we_looked() -> None:
    """«The bank removed it» and «we failed to find it» are different reports."""
    diff = diff_snapshots(
        extraction(service_fee=found("0.5%", ValueKind.FEE)),
        extraction(),
        retrieval_reached={"service_fee": False},
    )
    change = diff.changes[0]
    assert change.kind is ChangeKind.DISAPPEARED
    assert change.note is not None and "may be ours" in change.note


def test_a_variant_moving_behind_an_unchanged_range_is_reported() -> None:
    """A branch rate moving matters to the customer who uses that branch."""
    def with_variants(branch: str) -> FieldValue:
        return FieldValue(
            value="17.5-21.6%",
            normalized=normalize("17.5-21.6%", ValueKind.PERCENT),
            evidence=evidence(),
            status=FieldStatus.FOUND,
            variants=(
                FieldVariant(
                    label="Մասնաճյուղ",
                    value=branch,
                    normalized=normalize(branch, ValueKind.PERCENT),
                    evidence=evidence(),
                ),
            ),
        )

    diff = diff_snapshots(
        extraction(nominal_rate=with_variants("20.1%")),
        extraction(nominal_rate=with_variants("21.0%")),
    )
    assert diff.changes[0].kind is ChangeKind.VARIANT
    assert "20.1%" in (diff.changes[0].note or "")


def test_a_change_of_confidence_is_not_a_change_of_tariff() -> None:
    """Verified becoming unverified is news about us, not about the bank."""
    before = extraction(nominal_rate=found("13.5%"))
    unverified = found("13.5%").model_copy(update={"status": FieldStatus.UNVERIFIED})
    diff = diff_snapshots(before, extraction(nominal_rate=unverified))
    assert diff.changes[0].kind is ChangeKind.STATUS


# --------------------------------------------------------------------------- #
# Changes that are ours, not the bank's
# --------------------------------------------------------------------------- #


def test_a_diff_across_extractors_is_annotated_not_reported_as_a_bank_change() -> None:
    """Comparing rule-based output with a model's measures the extractors."""
    diff = diff_snapshots(
        extraction(nominal_rate=found("12.5%"), method="rule_based"),
        extraction(nominal_rate=found("13.5%"), method="gemini:test"),
    )
    assert diff.provenance is Provenance.METHOD_CHANGED
    assert not diff.is_trustworthy
    assert any("not a change at the bank" in note for note in diff.notes)


def test_a_diff_across_prompt_versions_is_annotated() -> None:
    """Our own prompt fix moved four fields; that is not the bank repricing."""
    diff = diff_snapshots(
        extraction(service_fee=FieldValue.not_found(), prompt_version=1),
        extraction(service_fee=found("չի գանձվում", ValueKind.FEE), prompt_version=4),
    )
    assert diff.provenance is Provenance.PROMPT_CHANGED
    assert any("may be ours" in note for note in diff.notes)


def test_a_change_that_is_ours_is_reported_but_never_escalated() -> None:
    """A reviewer must not be asked to confirm our own extractor change.

    Found by running the pipeline offline: a rule-based run diffed against a
    stored Gemini snapshot asked a human whether the amount had really fallen
    from 50,000-10,000,000 to 3,000,000. The bank had published nothing; the
    extractor had changed. A reviewer trained to click through our changes
    clicks through the one that matters.
    """
    diff = diff_snapshots(
        extraction(nominal_rate=found("8.0%"), method="rule_based"),
        extraction(nominal_rate=found("20.1%"), method="gemini:test"),
    )
    assert diff.provenance is Provenance.METHOD_CHANGED
    assert diff.large_changes, "the move is still detected and reported"

    requests = review_requests("consumer_loan", diff, [], MonitoringConfig())
    assert requests == [], "but it is not put to a human"


def test_a_prompt_change_is_reported_but_never_escalated() -> None:
    """Same rule for a reworded prompt."""
    diff = diff_snapshots(
        extraction(nominal_rate=found("8.0%"), prompt_version=1),
        extraction(nominal_rate=found("20.1%"), prompt_version=4),
    )
    assert diff.provenance is Provenance.PROMPT_CHANGED
    assert diff.large_changes
    assert review_requests("consumer_loan", diff, [], MonitoringConfig()) == []


def test_a_comparable_change_of_the_same_size_is_escalated() -> None:
    """The control: provenance is what differs, not the size of the move."""
    diff = diff_snapshots(
        extraction(nominal_rate=found("8.0%")),
        extraction(nominal_rate=found("20.1%")),
    )
    assert diff.provenance is Provenance.COMPARABLE
    requests = review_requests("consumer_loan", diff, [], MonitoringConfig())
    assert len(requests) == 1
    assert requests[0].trigger is ReviewTrigger.LARGE_CHANGE


def test_a_new_edition_is_reported_separately_from_a_new_value() -> None:
    """Republishing and repricing are different events."""
    diff = diff_snapshots(
        extraction(nominal_rate=found("13.5%"), edition="131"),
        extraction(nominal_rate=found("13.5%"), edition="132"),
    )
    assert not diff.has_changes
    assert any("new edition" in note for note in diff.notes)


def test_the_first_run_is_a_baseline_not_ten_changes() -> None:
    """A first run reporting every field as «changed» would be alarming nonsense."""
    diff = baseline_diff("mortgage")
    assert diff.is_baseline
    assert not diff.has_changes


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def test_a_snapshot_round_trips(tmp_path: Path) -> None:
    """Including through the lenient loader, so a registry change survives."""
    store = SnapshotStore(tmp_path / "s.db")
    store.save(
        extraction(nominal_rate=found("13.5%"), document_date=date(2023, 5, 15), edition="132"),
        run_id="run-1",
        doc_id="d" * 64,
    )
    stored = store.latest("mortgage")
    assert stored is not None
    assert stored.extraction.fields["nominal_rate"].value == "13.5%"
    assert stored.document_date == "2023-05-15"
    assert stored.document_edition == "132"


def test_a_rejected_snapshot_is_never_used_as_a_baseline(tmp_path: Path) -> None:
    """A reviewer refused those values; the next run must not compare against them."""
    store = SnapshotStore(tmp_path / "s.db")
    good = store.save(extraction(nominal_rate=found("13.5%")), run_id="r1", doc_id="d")
    bad = store.save(extraction(nominal_rate=found("99%")), run_id="r2", doc_id="d")
    store.set_status(bad, SnapshotStatus.REJECTED)
    latest = store.latest("mortgage")
    assert latest is not None
    assert latest.id == good


def test_history_keeps_rejected_snapshots_for_the_audit_trail(tmp_path: Path) -> None:
    """Keeping them is the point: someone decided something, and it is recorded."""
    store = SnapshotStore(tmp_path / "s.db")
    store.save(extraction(nominal_rate=found("13.5%")), run_id="r1", doc_id="d")
    rejected = store.save(extraction(nominal_rate=found("99%")), run_id="r2", doc_id="d")
    store.set_status(rejected, SnapshotStatus.REJECTED)
    assert len(store.history("mortgage")) == 2


def test_no_history_is_not_an_error(tmp_path: Path) -> None:
    """A first run is a first run."""
    assert SnapshotStore(tmp_path / "s.db").latest("mortgage") is None


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #


def request(subject: str = "change:nominal_rate:12.5%->18.0%") -> ReviewRequest:
    """Build a review request."""
    return ReviewRequest(
        trigger=ReviewTrigger.LARGE_CHANGE,
        product_id="mortgage",
        subject=subject,
        question="Անվանական տոկոսադրույք: 12.5% → 18.0% — is this correct?",
        evidence=('was: "12.5%"', 'now: "18.0%"'),
    )


def test_a_settled_question_is_not_asked_again(tmp_path: Path) -> None:
    """A settled question must not be asked again.

    Asking nightly is an alarm nobody reads - and a reviewer who clicks through
    the routine one will click through the real one.
    """
    store = SnapshotStore(tmp_path / "s.db")
    log = ReviewLog(store)
    reviewer = ScriptedReviewer({request().subject: Decision.APPROVED})

    first = ask(reviewer, log, request())
    assert first.approved and not first.remembered

    second = ask(reviewer, log, request())
    assert second.approved and second.remembered
    assert len(reviewer.seen) == 1, "the reviewer was asked once"


def test_a_different_question_is_still_asked(tmp_path: Path) -> None:
    """Confirming 12.5% → 13.5% must not confirm next month's move as well."""
    store = SnapshotStore(tmp_path / "s.db")
    log = ReviewLog(store)
    reviewer = ScriptedReviewer(
        {
            "change:nominal_rate:12.5%->18.0%": Decision.APPROVED,
            "change:nominal_rate:18.0%->25.0%": Decision.REJECTED,
        }
    )
    ask(reviewer, log, request())
    second = ask(reviewer, log, request("change:nominal_rate:18.0%->25.0%"))
    assert second.decision is Decision.REJECTED
    assert len(reviewer.seen) == 2


def test_a_deferral_is_not_remembered(tmp_path: Path) -> None:
    """«Ask me later» means later, not never."""
    store = SnapshotStore(tmp_path / "s.db")
    log = ReviewLog(store)
    reviewer = ScriptedReviewer({})
    ask(reviewer, log, request())
    ask(reviewer, log, request())
    assert len(reviewer.seen) == 2


def test_an_unrecognised_answer_defers_rather_than_approving() -> None:
    """A mistyped keystroke must not confirm a tariff."""
    from tariff_agent.snapshots.review import CliReviewer

    written: list[str] = []

    class Sink:
        def write(self, text: str) -> None:
            written.append(text)

        def flush(self) -> None:
            return None

    reviewer = CliReviewer(stream=Sink(), prompt=lambda _: "maybe")
    assert reviewer.decide(request()).decision is Decision.DEFERRED
    assert any("REVIEW NEEDED" in line for line in written)
    assert any('was: "12.5%"' in line for line in written)


def test_the_audit_trail_names_an_automatic_decision_as_automatic() -> None:
    """«auto-approve» is honest; «reviewer» would not be."""
    assert AutoReviewer().name == "auto-approved"
    assert AutoReviewer(Decision.REJECTED).name == "auto-rejected"


# --------------------------------------------------------------------------- #
# A monitoring run, end to end
# --------------------------------------------------------------------------- #

RATE_PASSAGE = "Տոկոսադրույք Տարեկան անվանական տոկոսադրույք՝ 13.5% տարեկան պայմաններով"
HIGHER_PASSAGE = "Տոկոսադրույք Տարեկան անվանական տոկոսադրույք՝ 18.0% տարեկան պայմաններով"


def passage(text: str) -> Chunk:
    """Build a chunk for a monitoring run."""
    return Chunk(
        chunk_id="c001",
        doc_id="d" * 64,
        text=text,
        page=3,
        chunk_type=ChunkType.TEXT,
        source_role=SourceRole.PRIMARY,
        document_kind=DocumentKind.PDF,
        document_name="Տեղեկատվական ամփոփագիր",
        source_url="https://www.acba.am/files/loan%20info.pdf",
        language=Language.HY,
        retrieved_at=datetime.now(UTC),
        section="Տոկոսադրույք",
        document_date=date(2023, 5, 15),
    )


class OneFieldExtractor:
    """Returns a single prepared answer for the nominal rate."""

    def __init__(self, value: str, quote: str) -> None:
        """Record the answer to give."""
        self._value = value
        self._quote = quote
        self.calls = 0

    @property
    def method(self) -> str:
        """Identifier recorded on the extraction."""
        return "gemini:test"

    def extract(self, specs, chunks, *, product=None):  # type: ignore[no-untyped-def]
        """Answer nominal_rate and nothing else."""
        self.calls += 1
        return __import__(
            "tariff_agent.extraction.schema", fromlist=["ExtractionResponse"]
        ).ExtractionResponse(
            fields=[
                ExtractedField(
                    field_id=spec.id,
                    value=self._value if spec.id == "nominal_rate" else "NOT_FOUND",
                    quote=self._quote if spec.id == "nominal_rate" else "",
                    chunk_id="c001" if spec.id == "nominal_rate" else "",
                )
                for spec in specs
            ]
        )


def monitor(
    store: SnapshotStore, text: str, value: str, quote: str, reviewer=None  # type: ignore[no-untyped-def]
):
    """Run one monitoring cycle over a single passage."""
    return run_monitoring(
        PRODUCT,
        "ACBA Bank",
        Retriever([passage(text)]),
        OneFieldExtractor(value, quote),
        ALLOWLIST,
        store,
        monitoring=MonitoringConfig(),
        reviewer=reviewer,
        doc_id="d" * 64,
    )


def test_the_first_run_records_a_baseline_and_says_so(tmp_path: Path) -> None:
    """Ten «changes» on a first run would be alarming and wrong."""
    store = SnapshotStore(tmp_path / "s.db")
    result = monitor(store, RATE_PASSAGE, "13.5%", "Տարեկան անվանական տոկոսադրույք՝ 13.5%")
    assert result.diff.is_baseline
    assert result.status is SnapshotStatus.STORED
    assert "recorded as the baseline" in result.report


def test_an_unchanged_second_run_reports_nothing(tmp_path: Path) -> None:
    """The commonest outcome of a monitor is silence, and it must be silent."""
    store = SnapshotStore(tmp_path / "s.db")
    monitor(store, RATE_PASSAGE, "13.5%", "Տարեկան անվանական տոկոսադրույք՝ 13.5%")
    second = monitor(store, RATE_PASSAGE, "13.5%", "Տարեկան անվանական տոկոսադրույք՝ 13.5%")
    assert not second.diff.has_changes
    assert "No change since the previous run." in second.report


def test_a_large_change_is_stored_pending_rather_than_withheld(tmp_path: Path) -> None:
    """An unattended run must record what the bank published.

    Blocking the snapshot until someone confirms it would lose the history
    exactly when the change matters most.
    """
    store = SnapshotStore(tmp_path / "s.db")
    monitor(store, RATE_PASSAGE, "13.5%", "Տարեկան անվանական տոկոսադրույք՝ 13.5%")
    second = monitor(store, HIGHER_PASSAGE, "18.0%", "Տարեկան անվանական տոկոսադրույք՝ 18.0%")

    assert second.diff.large_changes
    assert second.status is SnapshotStatus.PENDING_REVIEW
    assert second.needs_attention
    assert "AWAITING REVIEW" in second.report
    assert store.latest("mortgage") is not None, "the snapshot exists despite being unconfirmed"


def test_a_reviewer_resolves_the_stored_snapshot(tmp_path: Path) -> None:
    """The decision settles a record that already exists."""
    store = SnapshotStore(tmp_path / "s.db")
    monitor(store, RATE_PASSAGE, "13.5%", "Տարեկան անվանական տոկոսադրույք՝ 13.5%")
    second = monitor(
        store,
        HIGHER_PASSAGE,
        "18.0%",
        "Տարեկան անվանական տոկոսադրույք՝ 18.0%",
        reviewer=AutoReviewer(Decision.APPROVED),
    )
    assert second.status is SnapshotStatus.CONFIRMED
    assert second.reviews and second.reviews[0][1].approved


def test_a_rejected_change_does_not_become_the_next_baseline(tmp_path: Path) -> None:
    """A value a human refused must not silently become the reference."""
    store = SnapshotStore(tmp_path / "s.db")
    monitor(store, RATE_PASSAGE, "13.5%", "Տարեկան անվանական տոկոսադրույք՝ 13.5%")
    rejected = monitor(
        store,
        HIGHER_PASSAGE,
        "18.0%",
        "Տարեկան անվանական տոկոսադրույք՝ 18.0%",
        reviewer=AutoReviewer(Decision.REJECTED),
    )
    assert rejected.status is SnapshotStatus.REJECTED
    latest = store.latest("mortgage")
    assert latest is not None
    assert latest.extraction.fields["nominal_rate"].value == "13.5%"


def test_the_report_puts_provenance_where_it_cannot_be_missed(tmp_path: Path) -> None:
    """A reader must see how the values were produced and how old they are.

    Someone reading 11.9-12.5% off a 2023 summary needs the date in front of
    them, not available on request.
    """
    store = SnapshotStore(tmp_path / "s.db")
    report = monitor(
        store, RATE_PASSAGE, "13.5%", "Տարեկան անվանական տոկոսադրույք՝ 13.5%"
    ).report

    assert "Last checked:" in report
    assert "Last changed:    2023-05-15" in report
    assert "Extraction:      gemini:test" in report
    assert "Prompt version:" in report
    assert "Completeness:" in report
    # And the provenance comes before any tariff value.
    assert report.index("How these values were produced") < report.index("Tariff")


def test_the_report_says_why_each_missing_field_is_missing(tmp_path: Path) -> None:
    """A blank line says neither «not charged» nor «we could not find it»."""
    store = SnapshotStore(tmp_path / "s.db")
    report = monitor(
        store, RATE_PASSAGE, "13.5%", "Տարեկան անվանական տոկոսադրույք՝ 13.5%"
    ).report
    assert "Not stated by these documents" in report
    assert "NOT_FOUND" in report
    assert "why:" in report
