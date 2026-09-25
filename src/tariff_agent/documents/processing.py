"""Turning fetched bytes into a :class:`Document`.

The one entry point the rest of the project uses. It routes by content type,
runs every available reading strategy, keeps the best per page, assembles
sections, and decides whether a human should look before any value is trusted.

Identity is the **content hash**, not the URL. ACBA serves one tariff PDF from
three different URLs; without this, the same document would be indexed three
times and a change in one copy would look like a change in three.
"""

from __future__ import annotations

from tariff_agent.config import DocumentSettings
from tariff_agent.documents.cleaning import (
    dedupe_long_paragraphs,
    finish_page_text,
    prepare_for_scoring,
    strip_repeated_furniture,
)
from tariff_agent.documents.dates import parse_document_date, parse_document_edition
from tariff_agent.documents.document import (
    Document,
    DocumentKind,
    ExtractionMethod,
    Page,
    Section,
)
from tariff_agent.documents.html import extract_html
from tariff_agent.documents.ocr import ocr_page
from tariff_agent.documents.pdf import open_pdf, page_candidates
from tariff_agent.documents.sections import detect_sections
from tariff_agent.documents.strategies import choose_best_text, make_candidate
from tariff_agent.errors import DocumentError, PdfParseError
from tariff_agent.http.client import ContentKind, FetchResult
from tariff_agent.models import Language
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)


def process_document(
    fetch: FetchResult,
    settings: DocumentSettings,
    *,
    document_name: str | None = None,
    language: Language = Language.HY,
    use_ocr: bool = True,
) -> Document:
    """Parse a fetched file into a cleaned, structured document.

    Args:
        fetch: The result of fetching the file.
        settings: Parsing, OCR and quality settings.
        document_name: Title for evidence; falls back to the file name.
        language: Language of the document body.
        use_ocr: Whether OCR may compete. Turned off in tests that are not
            about OCR, and by the offline demos.

    Returns:
        The processed document.

    Raises:
        DocumentError: If the file is of an unsupported kind, or produced no
            usable text at all. Missing data is reported, never invented.
    """
    name = document_name or fetch.url.rsplit("/", 1)[-1]
    if "pdf" in fetch.content_type or fetch.content.startswith(b"%PDF-"):
        return _process_pdf(fetch, settings, name, language, use_ocr)
    if "html" in fetch.content_type or "xml" in fetch.content_type:
        return _process_html(fetch, settings, name, language)
    raise DocumentError(f"unsupported content type {fetch.content_type!r} for {fetch.url}")


def _process_html(
    fetch: FetchResult, settings: DocumentSettings, name: str, language: Language
) -> Document:
    """Build a document from an HTML page.

    Args:
        fetch: The fetched page.
        settings: Parsing settings.
        name: Title for evidence, replaced by the page's own title when it has one.
        language: Language of the body.

    Returns:
        A single-page document.

    Raises:
        DocumentError: If the page yields no text.
    """
    html = fetch.content.decode("utf-8", errors="replace")
    text, tables, sections, title = extract_html(html, settings)
    if not text.strip():
        raise DocumentError(f"no readable text in the page at {fetch.url}")

    candidate = make_candidate(ExtractionMethod.HTML, text, settings)
    page = Page(
        number=1,
        text=text,
        method=ExtractionMethod.HTML,
        quality=candidate.quality.score,
        tables=tables,
    )
    return _assemble(
        fetch=fetch,
        name=title or name,
        kind=DocumentKind.HTML,
        language=language,
        pages=[page],
        sections=list(sections),
        settings=settings,
    )


def _process_pdf(
    fetch: FetchResult,
    settings: DocumentSettings,
    name: str,
    language: Language,
    use_ocr: bool,
) -> Document:
    """Build a document from a PDF, choosing the best reading of each page.

    Args:
        fetch: The fetched file.
        settings: Parsing, OCR and quality settings.
        name: Title for evidence.
        language: Language of the body.
        use_ocr: Whether OCR may compete.

    Returns:
        The processed document.

    Raises:
        PdfParseError: If the file cannot be opened.
        DocumentError: If no page yielded usable text.
    """
    from tariff_agent.documents.tables import extract_tables

    document = open_pdf(fetch.content)
    try:
        page_count = min(document.page_count, settings.max_pages)
        if document.page_count > settings.max_pages:
            logger.info(
                "pdf_page_cap_applied",
                extra={"pages": document.page_count, "cap": settings.max_pages},
            )

        raw_pages: list[Page] = []
        for index in range(page_count):
            number = index + 1
            pdf_page = document[index]
            candidates = page_candidates(pdf_page, settings, page_number=number)
            ocr_confidence: float | None = None

            if use_ocr:
                result = ocr_page(pdf_page, settings, page_number=number)
                if result is not None:
                    ocr_confidence = result.mean_confidence
                    candidates.append(
                        make_candidate(
                            ExtractionMethod.OCR,
                            prepare_for_scoring(result.text),
                            settings,
                            ocr_confidence=result.mean_confidence,
                        )
                    )

            best = choose_best_text(candidates, settings, page=number)
            if best is None:
                logger.warning("page_unreadable", extra={"page": number})
                continue

            raw_pages.append(
                Page(
                    number=number,
                    # Still unjoined: repeated headers and footers are matched
                    # line by line, so joining has to wait until after they are
                    # removed - otherwise the header merges into the first body
                    # line and becomes unmatchable.
                    text=best.text,
                    method=best.method,
                    quality=best.quality.score,
                    tables=extract_tables(pdf_page, number, settings),
                    ocr_confidence=best.ocr_confidence if best.is_ocr else ocr_confidence,
                    warnings=best.quality.defects,
                )
            )
    finally:
        document.close()

    if not raw_pages:
        raise DocumentError(f"no readable text in the PDF at {fetch.url}")

    cleaned = strip_repeated_furniture(
        [page.text for page in raw_pages], ratio=settings.furniture_ratio
    )
    pages = [
        Page(
            number=page.number,
            # Furniture is gone; now the wrapped lines can be joined and the
            # thousand groups repaired.
            text=finish_page_text(
                dedupe_long_paragraphs(text, min_chars=settings.dedupe_min_chars)
            ),
            method=page.method,
            quality=page.quality,
            tables=page.tables,
            ocr_confidence=page.ocr_confidence,
            warnings=page.warnings,
        )
        for page, text in zip(raw_pages, cleaned, strict=True)
    ]
    # Headings come from the shape of the text, not from font metadata: the
    # summary's structured extraction is unusable and OCR has no fonts at all.
    # Without this, every PDF's evidence carried an empty section.
    sections = [
        section for page in pages for section in detect_sections(page.text, page.number)
    ]
    return _assemble(
        fetch=fetch,
        name=name,
        kind=DocumentKind.PDF,
        language=language,
        pages=pages,
        sections=sections,
        settings=settings,
    )


def _assemble(
    *,
    fetch: FetchResult,
    name: str,
    kind: DocumentKind,
    language: Language,
    pages: list[Page],
    sections: list[Section],
    settings: DocumentSettings,
) -> Document:
    """Build the document and decide whether it needs review.

    Quality is the length-weighted mean of the pages, so one short bad page in a
    long document does not condemn it, and one good page does not excuse a
    document that is mostly unreadable.

    Args:
        fetch: The fetched file, for identity and timestamps.
        name: Document title.
        kind: PDF or HTML.
        language: Language of the body.
        pages: The processed pages.
        sections: Detected sections.
        settings: Provides the quality floor.

    Returns:
        The assembled document.
    """
    # The date is stated on the first pages when it is stated at all, and
    # scanning the whole document invites a misparse from an unrelated number.
    header = "\n".join(page.text for page in pages[:2])
    document_date = parse_document_date(header)
    document_edition = parse_document_edition(header)

    total = sum(len(page.text) for page in pages)
    if total:
        quality = sum(page.quality * len(page.text) for page in pages) / total
    else:
        quality = 0.0

    warnings: list[str] = []
    needs_review = quality < settings.quality_floor
    if needs_review:
        warnings.append(
            f"text quality {quality:.2f} is below the floor {settings.quality_floor:.2f}"
        )

    document = Document(
        doc_id=fetch.sha256,
        source_url=fetch.url,
        document_name=name,
        kind=kind,
        language=language,
        retrieved_at=fetch.retrieved_at,
        checked_at=fetch.checked_at,
        pages=tuple(pages),
        document_date=document_date,
        document_edition=document_edition,
        sections=tuple(sections),
        quality=round(quality, 4),
        needs_review=needs_review,
        warnings=tuple(warnings),
    )
    logger.info(
        "document_processed",
        extra={
            "doc_id": document.doc_id[:12],
            "url": document.source_url,
            "kind": kind.value,
            "pages": len(pages),
            "tables": len(document.tables),
            "methods": [method.value for method in document.methods],
            "quality": document.quality,
            "needs_review": needs_review,
        },
    )
    return document


__all__ = ["ContentKind", "PdfParseError", "process_document"]
