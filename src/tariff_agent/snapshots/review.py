"""Asking a human, and remembering the answer.

Four things in this system can be detected but not responsibly settled alone: a
query that fits two products, two plausible official documents, two official
sources stating different values, and a tariff that moved further than tariffs
usually move. Each stops for a person.

Two design points matter more than the plumbing.

**A decision is remembered.** Asking the same question every night is not
human-in-the-loop, it is an alarm nobody reads - and a reviewer who clicks
through the nightly one will click through the real one. Decisions are stored
against *what they were about*, so a resolved question stays resolved and only a
genuinely new one is raised.

**A request carries its evidence.** A reviewer choosing between two documents
needs both quotes, both pages and both dates in front of them. A prompt that
says "conflict detected, approve?" is not review, it is consent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from tariff_agent.errors import SnapshotError
from tariff_agent.observability.logging import get_logger
from tariff_agent.snapshots.store import SnapshotStore

logger = get_logger(__name__)


class ReviewTrigger(StrEnum):
    """Why a human is being asked."""

    AMBIGUOUS_PRODUCT = "ambiguous_product"
    """The query fits more than one monitored product."""

    RIVAL_DOCUMENTS = "rival_documents"
    """Two plausible official documents were found for one product."""

    SOURCE_CONFLICT = "source_conflict"
    """Two official sources state different values for a field."""

    LARGE_CHANGE = "large_change"
    """A tariff moved further than tariffs usually move."""

    LOW_QUALITY = "low_quality"
    """The document was too poorly read to trust without a look."""


class Decision(StrEnum):
    """What the reviewer decided."""

    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    """Left for later: the snapshot stays pending and is asked about again."""


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """A question for a human, with the evidence needed to answer it.

    Attributes:
        trigger: Why the question is being asked.
        product_id: The product concerned.
        subject: What specifically is being decided - a field, a document pair.
            Decisions are remembered against this, so the same question is not
            asked twice and a different one still is.
        question: The question in one sentence.
        evidence: Lines a reviewer reads before deciding: documents, pages,
            sections, quotes, dates.
        details: Anything a machine consumer needs, kept out of the display.
    """

    trigger: ReviewTrigger
    product_id: str
    subject: str
    question: str
    evidence: tuple[str, ...] = ()
    details: dict[str, Any] = dataclass_field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """A decision, and where it came from.

    Attributes:
        decision: What was decided.
        decided_by: Who or what decided.
        note: Optional explanation from the reviewer.
        remembered: True when this came from a stored earlier decision rather
            than from a person just now.
    """

    decision: Decision
    decided_by: str
    note: str | None = None
    remembered: bool = False

    @property
    def approved(self) -> bool:
        """Whether the thing under review may be trusted."""
        return self.decision is Decision.APPROVED


class Reviewer(Protocol):
    """Anything that can answer a review request."""

    @property
    def name(self) -> str:
        """Recorded with the decision, so an audit shows who decided."""
        ...

    def decide(self, request: ReviewRequest) -> ReviewOutcome:
        """Answer one request."""
        ...


class AutoReviewer:
    """Decides every request the same way, for unattended runs and demos.

    Args:
        decision: What to answer.
        name: Recorded with the decision. Deliberately explicit - an audit trail
            showing ``auto-approve`` is honest; one showing ``reviewer`` is not.
    """

    def __init__(self, decision: Decision = Decision.APPROVED, *, name: str | None = None) -> None:
        """Record the fixed answer."""
        self._decision = decision
        self._name = name or f"auto-{decision.value}"

    @property
    def name(self) -> str:
        """Who decided."""
        return self._name

    def decide(self, request: ReviewRequest) -> ReviewOutcome:
        """Return the fixed answer.

        Args:
            request: The question, logged so an unattended run still records
                what it decided on a human's behalf.

        Returns:
            The fixed outcome.
        """
        logger.info(
            "review_auto_decided",
            extra={
                "trigger": request.trigger.value,
                "product_id": request.product_id,
                "subject": request.subject,
                "decision": self._decision.value,
            },
        )
        return ReviewOutcome(decision=self._decision, decided_by=self._name)


class ScriptedReviewer:
    """Answers from a prepared script, for tests.

    Args:
        answers: Subject to decision. Anything unlisted is deferred, so a test
            that forgets a case fails loudly rather than silently approving.
    """

    def __init__(self, answers: dict[str, Decision]) -> None:
        """Record the script."""
        self._answers = answers
        self.seen: list[ReviewRequest] = []

    @property
    def name(self) -> str:
        """Who decided."""
        return "scripted"

    def decide(self, request: ReviewRequest) -> ReviewOutcome:
        """Answer from the script.

        Args:
            request: The question.

        Returns:
            The scripted outcome, deferring anything unscripted.
        """
        self.seen.append(request)
        return ReviewOutcome(
            decision=self._answers.get(request.subject, Decision.DEFERRED),
            decided_by=self.name,
        )


class CliReviewer:
    """Asks a person at the terminal.

    Args:
        reviewer_name: Recorded with the decision.
        stream: Where to write the request; defaults to standard output.
        prompt: How the answer is read; defaults to :func:`input`.
    """

    def __init__(
        self,
        reviewer_name: str = "cli",
        *,
        stream: Any | None = None,
        prompt: Any | None = None,
    ) -> None:
        """Wire up the terminal interaction, injectable for tests."""
        import sys

        self._name = reviewer_name
        self._stream = stream or sys.stdout
        self._prompt = prompt or input

    @property
    def name(self) -> str:
        """Who decided."""
        return self._name

    def decide(self, request: ReviewRequest) -> ReviewOutcome:
        """Show the evidence and read a decision.

        Args:
            request: The question and its evidence.

        Returns:
            The reviewer's decision. Anything not understood defers rather than
            approves: a mistyped answer must not confirm a tariff.
        """
        write = self._stream.write
        write("\n" + "=" * 78 + "\n")
        write(f"REVIEW NEEDED — {request.trigger.value}\n")
        write(f"Product: {request.product_id}\n")
        write(f"{request.question}\n\n")
        for line in request.evidence:
            write(f"  {line}\n")
        write("\n[a]pprove / [r]eject / [d]efer: ")
        self._stream.flush()

        answer = str(self._prompt("")).strip().lower()
        decision = {
            "a": Decision.APPROVED,
            "approve": Decision.APPROVED,
            "r": Decision.REJECTED,
            "reject": Decision.REJECTED,
        }.get(answer, Decision.DEFERRED)
        write(f"recorded: {decision.value}\n")
        return ReviewOutcome(decision=decision, decided_by=self._name)


class ReviewLog:
    """Stored decisions, so a settled question is not asked again.

    Args:
        store: The snapshot store, whose database also holds the decisions.
    """

    def __init__(self, store: SnapshotStore) -> None:
        """Record the store."""
        self._store = store

    def recall(self, request: ReviewRequest) -> ReviewOutcome | None:
        """Return an earlier decision about the same thing, if there is one.

        Args:
            request: The question about to be asked.

        Returns:
            The remembered outcome, or ``None``. A deferral is not remembered -
            "ask me later" means later, not never.
        """
        try:
            with self._store._connect() as connection:  # noqa: SLF001
                row = connection.execute(
                    "SELECT decision, decided_by, note FROM reviews "
                    "WHERE product_id = ? AND trigger = ? AND subject = ?",
                    (request.product_id, request.trigger.value, request.subject),
                ).fetchone()
        except sqlite3.Error as exc:
            raise SnapshotError(f"could not read review decisions: {exc}") from exc
        if row is None or row["decision"] == Decision.DEFERRED.value:
            return None
        logger.info(
            "review_recalled",
            extra={
                "trigger": request.trigger.value,
                "subject": request.subject,
                "decision": row["decision"],
            },
        )
        return ReviewOutcome(
            decision=Decision(row["decision"]),
            decided_by=str(row["decided_by"]),
            note=row["note"],
            remembered=True,
        )

    def record(
        self, request: ReviewRequest, outcome: ReviewOutcome, *, snapshot_id: int | None = None
    ) -> None:
        """Store a decision so it is not asked again.

        Args:
            request: What was asked.
            outcome: What was decided.
            snapshot_id: The snapshot it settles, when it settles one.
        """
        if outcome.decision is Decision.DEFERRED:
            return
        try:
            with self._store._connect() as connection:  # noqa: SLF001
                connection.execute(
                    "INSERT OR REPLACE INTO reviews "
                    "(product_id, trigger, subject, decision, decided_by, decided_at, note, "
                    " snapshot_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request.product_id,
                        request.trigger.value,
                        request.subject,
                        outcome.decision.value,
                        outcome.decided_by,
                        datetime.now(UTC).isoformat(),
                        outcome.note,
                        snapshot_id,
                    ),
                )
        except sqlite3.Error as exc:
            raise SnapshotError(f"could not record a review decision: {exc}") from exc
        logger.info(
            "review_recorded",
            extra={
                "trigger": request.trigger.value,
                "subject": request.subject,
                "decision": outcome.decision.value,
                "by": outcome.decided_by,
            },
        )


def ask(
    reviewer: Reviewer, log: ReviewLog, request: ReviewRequest, *, snapshot_id: int | None = None
) -> ReviewOutcome:
    """Answer a request from memory if possible, otherwise ask and remember.

    Args:
        reviewer: Who to ask.
        log: Where decisions are kept.
        request: The question.
        snapshot_id: The snapshot the answer settles.

    Returns:
        The outcome, marked ``remembered`` when it came from a stored decision.
    """
    remembered = log.recall(request)
    if remembered is not None:
        return remembered
    outcome = reviewer.decide(request)
    log.record(request, outcome, snapshot_id=snapshot_id)
    return outcome
