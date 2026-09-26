"""Choosing between several readings of the same page.

A PDF page can be read in more than one way, and on real ACBA documents the best
way differs per document: `loans-tariffs.pdf` parses cleanly, while the mortgage
summary's text layer is broken in three different ways and OCR reads it at 92%
confidence. So OCR is a **peer strategy, not a fallback**, and the choice is made
by measurement rather than by a fixed preference order.

The rule, in one place so it can be stated and tested:

1. Every candidate is scored by the *same* :func:`~tariff_agent.documents.quality.score_text`,
   so the numbers are comparable.
2. Tesseract's mean confidence is a **gate, not a score**: below the configured
   minimum an OCR candidate is ineligible however good its text looks. The two
   scales measure different things and must not be added together.
3. The highest text score wins.
4. OCR must win by a **margin**, not by a hair. Tesseract's Armenian model
   misreads «և» as «ն» and, on the real tariff book, «չի» as «sh» - turning
   «չի գանձվում» ("is not charged") into something unreadable. The quality
   score cannot see those corruptions: it measures whether text reads like
   text, not whether it says what the page says. On page 2 of the tariff book
   OCR scored 1.00 against the parser's 0.92 and won, and the negation that
   made a fee zero was lost with it. Requiring a clear margin keeps OCR where
   it is genuinely needed - the mortgage summary, where the parser scores 0.2 -
   without letting it displace a working parse.
"""

from __future__ import annotations

from dataclasses import dataclass

from tariff_agent.config import DocumentSettings
from tariff_agent.documents.document import ExtractionMethod
from tariff_agent.documents.quality import TextQuality, score_text
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

OCR_MARGIN = 0.10
"""How much better OCR must score before it displaces a parsed reading."""


@dataclass(frozen=True, slots=True)
class TextCandidate:
    """One reading of a page, with everything needed to judge it.

    Attributes:
        method: How this text was obtained.
        text: The text itself.
        quality: Its score on the shared scale.
        ocr_confidence: Tesseract's mean word confidence, for OCR candidates.
    """

    method: ExtractionMethod
    text: str
    quality: TextQuality
    ocr_confidence: float | None = None

    @property
    def is_ocr(self) -> bool:
        """Whether this candidate came from OCR."""
        return self.method is ExtractionMethod.OCR


def make_candidate(
    method: ExtractionMethod,
    text: str,
    settings: DocumentSettings,
    ocr_confidence: float | None = None,
) -> TextCandidate:
    """Score a piece of text and wrap it as a candidate.

    Args:
        method: How the text was obtained.
        text: The text.
        settings: Provides the page-length expectation.
        ocr_confidence: Tesseract's mean word confidence, for OCR.

    Returns:
        The scored candidate.
    """
    return TextCandidate(
        method=method,
        text=text,
        quality=score_text(text, min_chars=settings.min_chars_per_page),
        ocr_confidence=ocr_confidence,
    )


def choose_best_text(
    candidates: list[TextCandidate], settings: DocumentSettings, *, page: int = 0
) -> TextCandidate | None:
    """Pick the most usable reading of a page.

    Args:
        candidates: The readings to choose between.
        settings: Provides the OCR confidence gate.
        page: Page number, for the log line.

    Returns:
        The winning candidate, or ``None`` when there is nothing to choose from.
    """
    if not candidates:
        return None

    eligible = [
        candidate
        for candidate in candidates
        if not candidate.is_ocr
        or (candidate.ocr_confidence or 0.0) >= settings.ocr_min_confidence
    ]
    rejected = [candidate for candidate in candidates if candidate not in eligible]
    for candidate in rejected:
        logger.info(
            "ocr_below_confidence_gate",
            extra={
                "page": page,
                "confidence": candidate.ocr_confidence,
                "gate": settings.ocr_min_confidence,
            },
        )
    if not eligible:
        return None

    best = max(eligible, key=lambda candidate: (candidate.quality.score, not candidate.is_ocr))
    if best.is_ocr:
        parsed = [candidate for candidate in eligible if not candidate.is_ocr]
        rival = max(parsed, key=lambda candidate: candidate.quality.score, default=None)
        if rival is not None and best.quality.score - rival.quality.score < OCR_MARGIN:
            logger.info(
                "ocr_margin_not_met",
                extra={
                    "page": page,
                    "ocr_score": round(best.quality.score, 3),
                    "parsed_score": round(rival.quality.score, 3),
                    "margin": OCR_MARGIN,
                },
            )
            best = rival
    logger.info(
        "page_strategy_chosen",
        extra={
            "page": page,
            "method": best.method.value,
            "ocr_confidence": best.ocr_confidence,
            "scores": {
                candidate.method.value: round(candidate.quality.score, 3)
                for candidate in candidates
            },
            "defects": {
                candidate.method.value: list(candidate.quality.defects)
                for candidate in candidates
                if candidate.quality.defects
            },
        },
    )
    return best
