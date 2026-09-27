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

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from tariff_agent.observability.logging import get_logger
from tariff_agent.rag.chunking import Chunk

logger = get_logger(__name__)

MATCH_THRESHOLD = 0.90
"""How closely a quote must match the passage. Below this it is not a quote."""

NUMBER = re.compile(r"\d[\d.,\s]*\d|\d")
"""Digit runs, including ACBA's comma, dot and space grouping."""

MIN_QUOTE_CHARS = 8
"""Below this length a quote is accepted only on an exact match.

`partial_ratio` scores a short needle against almost any haystack - «1%» is
0.90-similar to half a tariff book - so fuzzy matching cannot be trusted here.
Rejecting short quotes outright was worse, though: the currency field's whole
answer is «ՀՀ դրամ», seven characters, and a correct verbatim quote of it was
being discarded as unverifiable. Whether the field survived depended on whether
the model had happened to include the label, which made the result differ
between runs over an unchanged page. A short quote that occurs *exactly* is
stronger evidence than a long one at 0.91, so exactness is what is required.
"""


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


def _occurs_exactly(quote: str, text: str) -> bool:
    """Report whether a quote occurs verbatim in a passage.

    Whitespace is normalized on both sides, because a PDF breaks lines where
    the model does not; nothing else is relaxed.

    Args:
        quote: The quoted text.
        text: The passage.

    Returns:
        True when the quote is a literal substring of the passage.
    """
    needle = " ".join(quote.split())
    haystack = " ".join(text.split())
    return bool(needle) and needle in haystack


def _digits(text: str) -> list[str]:
    """Extract the numbers a passage or quote states, as bare digit strings.

    Grouping is discarded, because ACBA writes «50,000», «50.000» and
    «50 000» for the same amount - sometimes in one document.

    Args:
        text: Quote or passage.

    Returns:
        One digit-only string per number found, in order.
    """
    found = []
    for match in NUMBER.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if digits:
            found.append(digits)
    return found


def _numbers_are_real(quote: str, text: str) -> bool:
    """Whether every number the quote states also occurs in the passage.

    This is what stops the attack fuzzy matching cannot see. A tariff quote is
    mostly boilerplate and a few digits, and the digits are the whole payload:
    «Տևողություն 9-600 ամիս» against a document saying 9-60 scores **0.98** on
    `partial_ratio`, and «…պայմաններով 4.9%» against 20.1-21.6% scores 0.94.
    Both would have been reported as verified tariffs. Similarity is the right
    test for wording and the wrong one for numbers, so the numbers are checked
    exactly and separately.

    Args:
        quote: The text the model claims to have copied.
        text: The passage it is attributed to.

    Returns:
        True when the quote states no numbers the passage does not.
    """
    available = _digits(text)
    return all(number in available for number in _digits(quote))


def _matches(quote: str, text: str, threshold: float) -> float:
    """Score a quote against a passage, refusing invented numbers outright.

    Args:
        quote: The quoted text.
        text: The passage.
        threshold: Minimum similarity to accept.

    Returns:
        The similarity, or 0.0 when the quote states a number the passage does
        not - a near-miss on digits is not a near-miss, it is a different fact.
    """
    score = _similarity(quote, text)
    if score >= threshold and not _numbers_are_real(quote, text):
        return 0.0
    return score


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
    by_id = {chunk.chunk_id: chunk for chunk in retrieved}
    if len(cleaned) < MIN_QUOTE_CHARS:
        return _verify_short(cleaned, cited_chunk_id, retrieved, by_id)


    cited = by_id.get(cited_chunk_id)
    if cited is not None:
        score = _matches(cleaned, cited.text, threshold)
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
        score = _matches(cleaned, chunk.text, threshold)
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
            f"(best match {best_score:.2f} < {threshold:.2f}); a quote stating a "
            "number the passage does not is refused however closely the wording matches"
        ),
    )


def _verify_short(
    quote: str,
    cited_chunk_id: str,
    retrieved: list[Chunk],
    by_id: dict[str, Chunk],
) -> QuoteCheck:
    """Verify a quote too short for fuzzy matching, by requiring an exact one.

    The citation-correction rule is the same as for a long quote: the passage
    the quote is actually found in must be one retrieved for this field.

    Args:
        quote: The quoted text, already stripped.
        cited_chunk_id: The passage the model cited.
        retrieved: The passages shown for this field.
        by_id: ``retrieved`` indexed by chunk id.

    Returns:
        Verified against whichever retrieved passage contains the quote
        verbatim, preferring the cited one; otherwise not verified.
    """
    cited = by_id.get(cited_chunk_id)
    if cited is not None and _occurs_exactly(quote, cited.text):
        return QuoteCheck(
            verified=True,
            score=1.0,
            chunk=cited,
            reason=f"short quote occurs verbatim in {cited_chunk_id}",
        )
    for chunk in retrieved:
        if chunk.chunk_id != cited_chunk_id and _occurs_exactly(quote, chunk.text):
            logger.info(
                "quote_citation_corrected",
                extra={"cited": cited_chunk_id, "actual": chunk.chunk_id, "score": 1.0},
            )
            return QuoteCheck(
                verified=True,
                score=1.0,
                chunk=chunk,
                corrected=True,
                reason=(
                    f"short quote was cited to {cited_chunk_id or 'nothing'} but occurs "
                    f"verbatim in {chunk.chunk_id}, which was retrieved for this field"
                ),
            )
    logger.warning(
        "quote_unverified_short",
        extra={"cited": cited_chunk_id, "chars": len(quote)},
    )
    return QuoteCheck(
        verified=False,
        score=0.0,
        reason=(
            f"the quote is only {len(quote)} characters, too short to match fuzzily, "
            "and does not occur verbatim in any passage retrieved for this field"
        ),
    )
