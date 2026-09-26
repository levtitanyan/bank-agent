"""The six things the agent can do.

**Why these six, and not more or fewer (requirement 5.2).** A tool boundary is
drawn where the agent's *next* choice could legitimately differ. Resolving a
product may end the run by asking a human; finding sources may be skipped
entirely when a stored snapshot is fresh; extracting may be skipped for the same
reason; storing-and-diffing is a decision not every question needs; and asking a
person is only sometimes right. Those are five real forks, plus reading the
store, and each is one tool.

Everything with no fork after it stays *inside* one tool. Fetching, following
redirects, parsing a PDF, falling back to OCR, cleaning Armenian text, chunking
and indexing are seven steps with exactly one legitimate order, so they are one
tool - ``find_sources`` and ``extract_tariffs`` - rather than seven the model
would have to sequence correctly. Making them tools would add failure modes
without adding a single decision worth making.

**The contract every tool keeps.**

* Arguments are ids minted by an earlier tool, never a URL, a path or a query
  fragment. The only string a caller may originate is the user's own product
  query, and that goes through the deterministic matcher.
* The return value is always a mapping with a ``status`` of ``ok``, ``error`` or
  ``needs_review``. Nothing raises into the model: an exception the model cannot
  see is an exception it cannot route around, and a stack trace in a transcript
  is a prompt-injection surface.
* Payloads stay in the session. What comes back is counts, ids, field names and
  short quotes - enough to decide what to do next, never a document.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any, TypeVar

from tariff_agent.agent.metrics import ToolCall
from tariff_agent.agent.session import ExtractionRecord, SourceRecord, current_session
from tariff_agent.discovery.product_matcher import ResolutionStatus
from tariff_agent.discovery.product_matcher import resolve_product as _resolve
from tariff_agent.extraction.pipeline import extract_tariffs as _run_extraction
from tariff_agent.fields import FIELDS_BY_ID
from tariff_agent.models import FieldStatus
from tariff_agent.observability.logging import get_logger
from tariff_agent.pipeline import load_sources
from tariff_agent.snapshots.diff import baseline_diff, diff_snapshots
from tariff_agent.snapshots.pipeline import resolve_status, review_requests
from tariff_agent.snapshots.review import Decision, ReviewLog, ReviewOutcome, ask
from tariff_agent.snapshots.store import SnapshotStatus

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., dict[str, Any]])

QUOTE_CHARS = 120
"""How much of a quote the model is shown. Enough to recognise, not to re-read."""


def _require_product(session: Any, product_id: str) -> Any:
    """Look up a product, refusing an id that is not one of ours.

    Args:
        session: The run's session.
        product_id: The id to look up.

    Returns:
        The product.

    Raises:
        ValueError: When the id is unknown. The wrapper turns that into an
            error payload, so the model is told rather than crashed.
    """
    product = session.catalog.get(product_id)
    if product is None:
        raise ValueError(f"{product_id!r} is not a monitored product")
    return product


def _tool(function: F) -> F:
    """Wrap a tool with the contract every tool keeps.

    Charges the call against the run's budget, times it, records it in the
    metrics, and turns any exception into an error payload.

    Args:
        function: The tool implementation.

    Returns:
        The wrapped tool, with its signature preserved so ADK can read it.
    """

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        session = current_session()
        signature = "|".join([*(str(a) for a in args), *(f"{k}={v}" for k, v in kwargs.items())])
        refusal = session.charge(function.__name__, signature)
        if refusal is not None:
            return refusal

        started = time.monotonic()
        error_type: str | None = None
        try:
            result = function(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - nothing may raise into the model
            error_type = type(exc).__name__
            logger.warning(
                "tool_failed",
                extra={"tool": function.__name__, "error_type": error_type, "error": str(exc)},
            )
            result = {
                "status": "error",
                "error_type": error_type,
                "message": f"{function.__name__} failed: {exc}",
            }
        duration = round(time.monotonic() - started, 3)
        session.metrics.record(
            ToolCall(
                name=function.__name__,
                duration_s=duration,
                status=str(result.get("status", "ok")),
                error_type=error_type,
            )
        )
        logger.info(
            "tool_call",
            extra={
                "tool": function.__name__,
                "status": result.get("status"),
                "duration_s": duration,
            },
        )
        return result

    return wrapper  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 1. Which product
# --------------------------------------------------------------------------- #


@_tool
def resolve_product(query: str) -> dict[str, Any]:
    """Resolve a product name to a monitored product id.

    Accepts Armenian, English or Russian, and tolerates typos. This is the only
    tool that takes free text, and it is deterministic: fuzzy matching against a
    fixed catalogue, never a judgement about what the user probably meant.

    Args:
        query: The product as the user named it.

    Returns:
        ``ok`` with ``product_id`` and the name that matched; ``needs_review``
        with the candidates when several fit, which is a question for a person
        and not a coin toss; ``error`` when nothing monitored resembles it.
    """
    session = current_session()
    resolution = _resolve(query, session.catalog)

    if resolution.status is ResolutionStatus.RESOLVED and resolution.best is not None:
        product = _require_product(session, resolution.best.product_id)
        return {
            "status": "ok",
            "product_id": product.id,
            "name_hy": product.name_hy,
            "name_en": product.name_en,
            "matched": resolution.best.matched_name,
            "score": round(resolution.best.score, 1),
        }

    if resolution.status is ResolutionStatus.AMBIGUOUS:
        return {
            "status": "needs_review",
            "reason": resolution.reason,
            "question": f"Which product does {query!r} mean?",
            "candidates": [
                {
                    "product_id": match.product_id,
                    "score": round(match.score, 1),
                    "matched": match.matched_name,
                }
                for match in ([resolution.best] if resolution.best else [])
                + list(resolution.alternatives)
            ],
        }

    return {
        "status": "error",
        "error_type": "product_not_found",
        "message": resolution.reason,
        "monitored": [product.id for product in session.catalog.products],
    }


# --------------------------------------------------------------------------- #
# 2. Which documents
# --------------------------------------------------------------------------- #


@_tool
def find_sources(product_id: str) -> dict[str, Any]:
    """Find and read the official ACBA documents that state a product's tariffs.

    Discovers the product's pages and PDFs, fetches them through the allow-listed
    client, parses them (falling back to OCR where a scan needs it), and indexes
    the result. The documents themselves stay in the session; what comes back is
    a ``source_set_id`` and a description of what was found.

    Args:
        product_id: An id returned by ``resolve_product``.

    Returns:
        ``ok`` with the ``source_set_id`` and the sources' titles and kinds;
        ``needs_review`` when two documents are equally plausible as the primary
        one; ``error`` when nothing official could be found or read.
    """
    session = current_session()
    product = _require_product(session, product_id)
    loaded = load_sources(
        session.client,
        product,
        session.discovery,
        session.settings,
        embedder=session.embedder,
    )

    record_id = session.mint("src")
    session.sources[record_id] = SourceRecord(
        product_id=product_id,
        retriever=loaded.retriever,
        primary_document=loaded.primary_document,
        doc_id=loaded.doc_id,
    )

    payload: dict[str, Any] = {
        "status": "needs_review" if loaded.requires_review else "ok",
        "source_set_id": record_id,
        "product_id": product_id,
        "primary": loaded.primary_title,
        "supporting": loaded.supporting_titles,
        "documents_read": 1 + len(loaded.supporting_titles) - len(loaded.unreadable),
        "pages_fetched": loaded.pages_fetched,
    }
    if loaded.unreadable:
        payload["unreadable"] = loaded.unreadable
    if loaded.requires_review:
        payload["question"] = (
            f"Two documents are equally plausible as the official source for {product_id}."
        )
        payload["notes"] = loaded.notes
    return payload


# --------------------------------------------------------------------------- #
# 3. What we already know
# --------------------------------------------------------------------------- #


@_tool
def get_latest_snapshot(product_id: str) -> dict[str, Any]:
    """Read the most recent stored tariff snapshot for a product.

    Cheap, offline, and often enough on its own: a question about today's rate
    does not need the bank re-read if it was read this morning. Check the
    ``age_hours`` and decide - re-extracting a fresh snapshot wastes a quota and
    changes nothing, while trusting a stale one reports yesterday's tariff as
    today's.

    Args:
        product_id: An id returned by ``resolve_product``.

    Returns:
        ``ok`` with ``found: false`` when there is no history yet, or with the
        snapshot's age, status and every field's value and short quote.
    """
    session = current_session()
    stored = session.store.latest(product_id)
    if stored is None:
        return {"status": "ok", "found": False, "product_id": product_id}

    extraction = stored.extraction
    age_hours = round((_now() - stored.taken_at).total_seconds() / 3600, 1)
    return {
        "status": "ok",
        "found": True,
        "product_id": product_id,
        "snapshot_id": stored.id,
        "taken_at": stored.taken_at.isoformat(),
        "age_hours": age_hours,
        "snapshot_status": stored.status.value,
        "extraction_method": stored.extraction_method,
        "document_date": stored.document_date,
        "fields": _field_summary(extraction),
    }


def _now() -> Any:
    """Return the current UTC time.

    Returns:
        A timezone-aware datetime, isolated here so tests can freeze it.
    """
    from datetime import UTC, datetime

    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# 4. Reading the documents
# --------------------------------------------------------------------------- #


@_tool
def extract_tariffs(source_set_id: str) -> dict[str, Any]:
    """Extract every tariff field from a set of sources already read.

    Each value comes back with a quote verified against the passage it was taken
    from; anything that cannot be verified is reported as NOT_FOUND rather than
    guessed. Nothing is stored by this tool - use ``diff_against_previous`` for
    that.

    Args:
        source_set_id: An id returned by ``find_sources``.

    Returns:
        ``ok`` with an ``extraction_id``, each field's value and short quote,
        the fields that are genuinely absent, and any disagreement between two
        official sources.
    """
    session = current_session()
    record = session.sources.get(source_set_id)
    if record is None or record.retriever is None:
        return {
            "status": "error",
            "error_type": "unknown_source_set",
            "message": f"{source_set_id!r} is not a source set from this run",
        }

    product = _require_product(session, record.product_id)
    outcome = _run_extraction(
        product,
        session.catalog.bank,
        record.retriever,
        session.extractor,
        session.allowlist,
        top_k=session.settings.rag.top_k,
        primary_document=record.primary_document,
    )

    extraction_id = session.mint("ext")
    session.extractions[extraction_id] = ExtractionRecord(
        product_id=record.product_id,
        outcome=outcome,
        doc_id=record.doc_id,
        primary_document=record.primary_document,
    )

    found = sum(
        1 for value in outcome.extraction.fields.values() if value.status is FieldStatus.FOUND
    )
    session.metrics.fields_required += sum(1 for spec in FIELDS_BY_ID.values() if spec.required)
    session.metrics.fields_found += sum(
        1
        for field_id, value in outcome.extraction.fields.items()
        if FIELDS_BY_ID[field_id].required and value.status is FieldStatus.FOUND
    )
    session.metrics.validation_failures += len(outcome.validation.issues)
    session.metrics.model_calls += outcome.model_calls

    return {
        "status": "ok",
        "extraction_id": extraction_id,
        "product_id": record.product_id,
        "fields_found": found,
        "fields_total": len(outcome.extraction.fields),
        "completeness": round(outcome.validation.completeness, 3),
        "model_calls": outcome.model_calls,
        "from_cache": outcome.from_cache,
        "fields": _field_summary(outcome.extraction),
        "not_found": [
            field_id
            for field_id, value in outcome.extraction.fields.items()
            if value.status is FieldStatus.NOT_FOUND
        ],
        "conflicts": [
            {
                "field_id": conflict.field_id,
                "primary": conflict.primary.value,
                "supporting": conflict.supporting.value,
            }
            for conflict in outcome.conflicts
        ],
    }


# --------------------------------------------------------------------------- #
# 5. Storing it, and seeing what moved
# --------------------------------------------------------------------------- #


@_tool
def diff_against_previous(extraction_id: str) -> dict[str, Any]:
    """Store an extraction as a snapshot and compare it with the previous one.

    Storing and comparing are one step because the snapshot must be recorded
    whatever the comparison says - an unattended run has to capture what the
    bank published, not only what somebody later agreed with.

    Args:
        extraction_id: An id returned by ``extract_tariffs``.

    Returns:
        ``ok`` with what moved, or ``needs_review`` when something needs a
        person, in which case each question carries a ``review_id`` to pass to
        ``request_review``.
    """
    session = current_session()
    record = session.extractions.get(extraction_id)
    if record is None:
        return {
            "status": "error",
            "error_type": "unknown_extraction",
            "message": f"{extraction_id!r} is not an extraction from this run",
        }

    outcome = record.outcome
    previous = session.store.latest(record.product_id)
    if previous is None:
        diff = baseline_diff(record.product_id)
    else:
        diff = diff_snapshots(
            previous.extraction,
            outcome.extraction,
            large_rate_points=session.monitoring.thresholds.large_rate_points,
            large_amount_percent=session.monitoring.thresholds.large_amount_percent,
            retrieval_reached={
                field_id: result.is_relevant for field_id, result in outcome.retrieval.items()
            },
        )

    requests = review_requests(record.product_id, diff, outcome.conflicts, session.monitoring)
    status = SnapshotStatus.PENDING_REVIEW if requests else SnapshotStatus.STORED
    snapshot_id = session.store.save(
        outcome.extraction,
        run_id=_run_id(),
        doc_id=record.doc_id,
        status=status,
    )
    session.snapshots[extraction_id] = snapshot_id

    pending = []
    for request in requests:
        review_id = session.mint("rev")
        session.reviews[review_id] = (request, snapshot_id)
        pending.append(
            {
                "review_id": review_id,
                "trigger": request.trigger.value,
                "question": request.question,
                "evidence": list(request.evidence),
            }
        )

    return {
        "status": "needs_review" if pending else "ok",
        "product_id": record.product_id,
        "snapshot_id": snapshot_id,
        "snapshot_status": status.value,
        "baseline": diff.is_baseline,
        "provenance": diff.provenance.value,
        "changes": [
            {
                "field_id": change.field_id,
                "before": change.before,
                "after": change.after,
                "described": change.described(),
                "significance": change.significance.value,
            }
            for change in diff.changes
        ],
        "pending_reviews": pending,
        "notes": list(diff.notes),
    }


def _run_id() -> str:
    """Return the current correlation id, or mint one.

    Returns:
        The run id every log line of this run already carries.
    """
    from tariff_agent.observability.logging import current_run_id, new_run_id

    return current_run_id() or new_run_id()


# --------------------------------------------------------------------------- #
# 6. Asking a person
# --------------------------------------------------------------------------- #


@_tool
def request_review(review_id: str) -> dict[str, Any]:
    """Settle one question that needs a human.

    A decision already given about the same thing is *not* asked again: the
    stored answer comes straight back, marked ``remembered``. Asking a reviewer
    the same question every night is not human-in-the-loop, it is an alarm
    nobody reads.

    Args:
        review_id: An id from ``pending_reviews`` in a previous tool's result.

    Returns:
        ``ok`` with the decision and whether it was remembered or freshly given;
        ``needs_review`` when nobody is attached to this run to answer, in which
        case the snapshot stays pending and the question is still open.
    """
    session = current_session()
    entry = session.reviews.get(review_id)
    if entry is None:
        return {
            "status": "error",
            "error_type": "unknown_review",
            "message": f"{review_id!r} is not an open question from this run",
        }
    request, snapshot_id = entry
    log = ReviewLog(session.store)

    remembered = log.recall(request)
    if remembered is not None:
        outcome: ReviewOutcome = remembered
        session.metrics.reviews_remembered += 1
    elif session.reviewer is None:
        return {
            "status": "needs_review",
            "review_id": review_id,
            "question": request.question,
            "message": (
                "no reviewer is attached to this run and no earlier decision covers this "
                "question; the snapshot stays pending"
            ),
        }
    else:
        outcome = ask(session.reviewer, log, request, snapshot_id=snapshot_id)
        session.metrics.reviews_asked += 1

    decisions = [outcome.decision]
    status = resolve_status(session.store, snapshot_id, decisions)
    return {
        "status": "ok",
        "review_id": review_id,
        "decision": outcome.decision.value,
        "decided_by": outcome.decided_by,
        "remembered": outcome.remembered,
        "note": outcome.note,
        "snapshot_status": status.value,
        "approved": outcome.decision is Decision.APPROVED,
    }


# --------------------------------------------------------------------------- #
# Shared shaping
# --------------------------------------------------------------------------- #


def _field_summary(extraction: Any) -> list[dict[str, Any]]:
    """Summarize an extraction small enough to put in a prompt.

    Args:
        extraction: The :class:`~tariff_agent.models.TariffExtraction`.

    Returns:
        One entry per field: the label, the value as written, the status, and a
        truncated quote. Never the passage it came from.
    """
    summary = []
    for field_id, value in extraction.fields.items():
        spec = FIELDS_BY_ID[field_id]
        entry: dict[str, Any] = {
            "field_id": field_id,
            "label": spec.label_en,
            "value": value.value,
            "status": value.status.value,
        }
        if value.evidence is not None:
            entry["document"] = value.evidence.document_name
            entry["quote"] = value.evidence.quote[:QUOTE_CHARS]
        if value.variants:
            entry["variants"] = [
                {"label": variant.label, "value": variant.value} for variant in value.variants
            ]
        summary.append(entry)
    return summary


TOOLS = [
    resolve_product,
    find_sources,
    get_latest_snapshot,
    extract_tariffs,
    diff_against_previous,
    request_review,
]
"""The complete set the agent is given. Nothing else reaches the model."""
