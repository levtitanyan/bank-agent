"""Reading PDFs with PyMuPDF, in more than one way.

Two text strategies are produced for every page, because reconnaissance on the
real documents showed neither is reliable alone:

* **flat** (``get_text()``) - what most PDFs need. On ACBA's mortgage summary it
  returns one word per line.
* **word positions** (``get_text("words")``) - lines rebuilt from PyMuPDF's own
  block and line numbering, which keeps columns apart. Grouping by vertical
  position instead was tried and merged text across columns.

A third strategy, OCR, is added by :mod:`tariff_agent.documents.processing`.
This module does not decide which wins.
"""

from __future__ import annotations

from typing import Any

import pymupdf

from tariff_agent.config import DocumentSettings
from tariff_agent.documents.cleaning import prepare_for_scoring
from tariff_agent.documents.document import ExtractionMethod
from tariff_agent.documents.strategies import TextCandidate, make_candidate
from tariff_agent.errors import PdfParseError
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)


def open_pdf(content: bytes) -> Any:
    """Open a PDF from bytes.

    Args:
        content: The raw file.

    Returns:
        An open PyMuPDF document.

    Raises:
        PdfParseError: If the file cannot be opened, is encrypted, or has no
            pages. Each is a real condition worth distinguishing in a log, and
            none should surface as a bare library exception.
    """
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise PdfParseError(f"could not open the PDF: {exc}") from exc
    if document.is_encrypted and not document.authenticate(""):
        document.close()
        raise PdfParseError("the PDF is encrypted and cannot be read")
    if document.page_count == 0:
        document.close()
        raise PdfParseError("the PDF has no pages")
    return document


def flat_text(page: Any) -> str:
    """Extract a page's text in reading order.

    Args:
        page: A PyMuPDF page.

    Returns:
        The page text, uncleaned.
    """
    text: str = page.get_text()
    return text


def text_from_word_positions(page: Any) -> str:
    """Rebuild a page's lines from word positions.

    Groups words by the block and line numbers PyMuPDF assigns, then orders them
    within a line by word number. That respects the document's own column
    structure; grouping by vertical coordinate merges adjacent columns.

    Args:
        page: A PyMuPDF page.

    Returns:
        The rebuilt text, uncleaned.
    """
    grouped: dict[tuple[int, int], list[tuple[int, str]]] = {}
    for word in page.get_text("words"):
        _, _, _, _, text, block_no, line_no, word_no = word[:8]
        grouped.setdefault((int(block_no), int(line_no)), []).append((int(word_no), str(text)))
    lines = [
        " ".join(word for _, word in sorted(words)) for _, words in sorted(grouped.items())
    ]
    return "\n".join(lines)


def page_candidates(
    page: Any, settings: DocumentSettings, *, page_number: int
) -> list[TextCandidate]:
    """Produce every text reading of a page that PyMuPDF can give.

    Args:
        page: A PyMuPDF page.
        settings: Provides the page-length expectation for scoring.
        page_number: 1-based page number, for logging.

    Returns:
        The scored candidates, carrying text that has been normalized but whose
        line structure is intact - scoring must see the structure, because that
        is where one-word-per-line and glued-word defects show.
    """
    candidates: list[TextCandidate] = []
    for method, extract in (
        (ExtractionMethod.PDF_TEXT, flat_text),
        (ExtractionMethod.PDF_WORDS, text_from_word_positions),
    ):
        try:
            raw = extract(page)
        except Exception as exc:
            logger.info(
                "pdf_strategy_failed",
                extra={
                    "page": page_number,
                    "method": method.value,
                    "error_type": type(exc).__name__,
                },
            )
            continue
        prepared = prepare_for_scoring(raw)
        if prepared:
            candidates.append(make_candidate(method, prepared, settings))
    return candidates
