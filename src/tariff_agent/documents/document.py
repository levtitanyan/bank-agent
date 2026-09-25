"""What a processed document is.

The contract between document processing and everything after it. Three
properties are load-bearing:

* **Tables are separate from text.** Cleaning rules that join wrapped lines or
  drop repeated blocks are right for prose and destructive for a table, where a
  "repeated short line" is «0%» in a different row.
* **Every page records how its text was obtained.** ACBA's mortgage summary
  parses badly and OCRs well, so two pages of one document can legitimately come
  from different strategies, and a reviewer must be able to see which.
* **HTML has no page numbers.** An HTML document is one page internally, so
  every code path is uniform, but :meth:`Document.evidence_page` returns ``None``
  for it - a page number in the evidence must never be a fiction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from tariff_agent.models import Language


class DocumentKind(StrEnum):
    """What sort of document was processed."""

    PDF = "pdf"
    HTML = "html"


class ExtractionMethod(StrEnum):
    """How a page's text was obtained.

    Recorded per page, because the best strategy differs between documents and
    sometimes between pages of one document.
    """

    PDF_TEXT = "pdf_text"
    """PyMuPDF flat text extraction."""

    PDF_WORDS = "pdf_words"
    """Lines rebuilt from word positions, using PyMuPDF's own block and line
    segmentation. Needed where flat extraction yields one word per line."""

    OCR = "ocr"
    """Rendered and read by Tesseract. A peer strategy, not a last resort."""

    HTML = "html"
    """Parsed from markup."""


@dataclass(frozen=True, slots=True)
class Table:
    """One table, kept structured.

    Attributes:
        page: 1-based page the table was found on.
        section: Nearest heading above it, when one was detected.
        rows: Cell text, row by row. Never passed through the text cleaners.
    """

    page: int
    rows: tuple[tuple[str, ...], ...]
    section: str | None = None

    def as_text(self) -> str:
        """Render the table as text for chunking and for prompts.

        Returns:
            One line per row, cells joined by ``|``. Phase 5 chunks a table as a
            single unit, and Phase 6 shows it to the model in this form.
        """
        return "\n".join(" | ".join(cell.strip() for cell in row) for row in self.rows)

    @property
    def shape(self) -> tuple[int, int]:
        """Rows by columns."""
        return len(self.rows), max((len(row) for row in self.rows), default=0)


@dataclass(frozen=True, slots=True)
class Section:
    """A heading and the span of page text beneath it.

    Attributes:
        title: The heading text, cleaned.
        page: 1-based page the heading appears on.
        start: Character offset of the section's text within that page.
        end: Character offset just past the section's text.
    """

    title: str
    page: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a processed document.

    Attributes:
        number: 1-based page number. An HTML document has exactly one page.
        text: Cleaned text, excluding tables.
        tables: Tables found on this page, structured and uncleaned.
        method: Which strategy produced ``text``.
        quality: 0-1 score of that text.
        ocr_confidence: Tesseract's mean word confidence when OCR was used or
            attempted, else ``None``.
        warnings: Anything a reviewer should know about this page.
    """

    number: int
    text: str
    method: ExtractionMethod
    quality: float
    tables: tuple[Table, ...] = ()
    ocr_confidence: float | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Document:
    """A retrieved document, cleaned and structured.

    Attributes:
        doc_id: sha256 of the fetched bytes. Identity is the content, not the
            URL - ACBA serves one tariff PDF from three URLs.
        source_url: Where it was fetched from.
        document_name: Human-readable title for evidence and reports.
        kind: PDF or HTML.
        language: Language of the document body.
        retrieved_at: When these bytes were downloaded.
        checked_at: When the bank last confirmed them current.
        document_date: The date the document states for itself - «Թարմացվել է
            առ՝ 15.05.2023թ.» - or None. Retrieval time says when *we* fetched
            it; this says how old the bank's own content is, which is what
            settles a disagreement between two official sources.
        pages: The pages, in order.
        sections: Detected headings and their spans.
        quality: Worst page quality, weighted by page length.
        needs_review: True when the best available text is still poor, so a
            human should look before any value is trusted.
        warnings: Document-level notes.
    """

    doc_id: str
    source_url: str
    document_name: str
    kind: DocumentKind
    language: Language
    retrieved_at: datetime
    checked_at: datetime
    pages: tuple[Page, ...]
    document_date: date | None = None
    sections: tuple[Section, ...] = ()
    quality: float = 0.0
    needs_review: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def evidence_page(self, page_number: int) -> int | None:
        """Return the page number to cite in evidence, if one is meaningful.

        Args:
            page_number: The internal page number.

        Returns:
            The page number for a PDF; ``None`` for HTML, which has no
            pagination. Phase 6 uses this rather than the raw number, so an
            HTML citation never claims a page that does not exist.
        """
        return None if self.kind is DocumentKind.HTML else page_number

    @property
    def text(self) -> str:
        """All page text joined, in page order."""
        return "\n\n".join(page.text for page in self.pages if page.text)

    @property
    def tables(self) -> tuple[Table, ...]:
        """Every table in the document, in page order."""
        return tuple(table for page in self.pages for table in page.tables)

    @property
    def methods(self) -> tuple[ExtractionMethod, ...]:
        """The distinct strategies used, in page order of first appearance."""
        seen: list[ExtractionMethod] = []
        for page in self.pages:
            if page.method not in seen:
                seen.append(page.method)
        return tuple(seen)

    def section_for(self, page_number: int, offset: int) -> str | None:
        """Find the heading a quoted span sits under.

        Args:
            page_number: Page the quote was found on.
            offset: Character offset of the quote within that page's text.

        Returns:
            The section title, or ``None`` when the span is above the first
            heading or no headings were detected.
        """
        for section in self.sections:
            if section.page == page_number and section.start <= offset < section.end:
                return section.title
        return None
