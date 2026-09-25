"""Checking that a quoted sentence really occurs where the model says it does.

This is the mechanism behind the project's central promise. A model asked for a
verbatim quote will usually give one; when it does not, the difference between a
real quote and a plausible paraphrase is the difference between a tariff and a
guess. Every quote is matched back against the passage it was attributed to,
and anything that cannot be found becomes NOT_FOUND.

Citation correction is deliberately narrow: a quote found in a *different*
passage is accepted only if that passage was retrieved for the same field. A
quote that turns up somewhere else entirely is not a citation slip, it is
evidence that the answer did not come from what we asked about.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from tariff_agent.observability.logging import get_logger
from tariff_agent.rag.chunking import Chunk

logger = get_logger(__name__)

MATCH_THRESHOLD = 0.90
"""How closely a quote must match the passage. Below this it is not a quote."""

MIN_QUOTE_CHARS = 8
"""Shorter than this, a "quote" matches almost anything and proves nothing."""


@dataclass(frozen=True, slots=True)
class QuoteCheck:
    """The outcome of verifying one quote.

    Attributes:
        verified: Whether the quote was found in an acceptable passage.
        chunk: The passage it was actually found in, when it was.
        score: Best match score, 0-1.
        corrected: True when the quote was found in a different passage than
            the model cited, and that passage was retrieved for this field.
        reason: What happened, in words a reviewer can read.
    """

    verified: bool
    score: float
    chunk: Chunk | None = None
    corrected: bool = False
    reason: str = ""


def _similarity(quote: str, text: str) -> float:
    """Score how well a quote matches a passage.

    Uses partial matching, because a quote is a fragment of its passage, and
    normalizes whitespace, because line breaks differ between the two.

    Args:
        quote: The quoted text.
        text: The passage.

    Returns:
        A score from 0 to 1.
    """
    needle = " ".join(quote.split())
    haystack = " ".join(text.split())
    if not needle or not haystack:
        return 0.0
    return float(fuzz.partial_ratio(needle, haystack)) / 100.0


def verify_quote(
    quote: str,
    cited_chunk_id: str,
    retrieved: list[Chunk],
    *,
    threshold: float = MATCH_THRESHOLD,
) -> QuoteCheck:
    """Confirm a quote occurs in the passage it was attributed to.

    Args:
        quote: The text the model claims to have copied.
        cited_chunk_id: The passage it cited.
        retrieved: The passages that were shown for this field.
        threshold: Minimum similarity to accept.

    Returns:
        The verification outcome. A quote shorter than
        :data:`MIN_QUOTE_CHARS`, or matching nothing retrieved for this field,
        is not verified - and the caller turns that into NOT_FOUND.
    """
    cleaned = quote.strip()
    if len(cleaned) < MIN_QUOTE_CHARS:
        return QuoteCheck(
            verified=False,
            score=0.0,
            reason=f"the quote is too short to verify ({len(cleaned)} characters)",
        )

    by_id = {chunk.chunk_id: chunk for chunk in retrieved}
    cited = by_id.get(cited_chunk_id)
    if cited is not None:
        score = _similarity(cleaned, cited.text)
        if score >= threshold:
            return QuoteCheck(
                verified=True,
                score=score,
                chunk=cited,
                reason=f"quote matches {cited_chunk_id} at {score:.2f}",
            )

    # The quote may belong to another passage shown for this same field, which
    # is a citation slip rather than an invention. Anything outside that set is
    # not accepted, however well it matches.
    best_chunk: Chunk | None = None
    best_score = 0.0
    for chunk in retrieved:
        if chunk.chunk_id == cited_chunk_id:
            continue
        score = _similarity(cleaned, chunk.text)
        if score > best_score:
            best_chunk, best_score = chunk, score

    if best_chunk is not None and best_score >= threshold:
        logger.info(
            "quote_citation_corrected",
            extra={
                "cited": cited_chunk_id,
                "actual": best_chunk.chunk_id,
                "score": round(best_score, 3),
            },
        )
        return QuoteCheck(
            verified=True,
            score=best_score,
            chunk=best_chunk,
            corrected=True,
            reason=(
                f"quote was cited to {cited_chunk_id or 'nothing'} but matches "
                f"{best_chunk.chunk_id} at {best_score:.2f}, which was retrieved "
                "for this field"
            ),
        )

    logger.warning(
        "quote_unverified",
        extra={"cited": cited_chunk_id, "best_score": round(best_score, 3)},
    )
    return QuoteCheck(
        verified=False,
        score=best_score,
        reason=(
            f"the quote does not occur in any passage retrieved for this field "
            f"(best match {best_score:.2f} < {threshold:.2f})"
        ),
    )
