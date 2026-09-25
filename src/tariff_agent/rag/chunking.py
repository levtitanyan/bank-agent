"""Cutting documents into retrievable, quotable pieces.

Four rules, each of which exists to protect the evidence trail rather than to
improve ranking:

* **A chunk never spans a page.** Evidence cites one page; a chunk that crosses a
  boundary makes that citation false for half its text.
* **Offsets are exact.** ``char_start``/``char_end`` index into ``Page.text``, so
  Phase 6 can locate a quote, verify it really occurs, and resolve which section
  it sits under. Without offsets, verification degrades to substring guessing.
* **Tables are their own chunks**, and carry their section title *inside the
  text*. A row reading «0.5% | ամսական» is meaningless on its own; under the
  heading «Սպասարկման վճար» it is a service fee. Metadata alone would not reach
  the model, which sees only chunk text.
* **Splits never fall inside a number.** «1,000,000» cut across two chunks
  becomes two wrong values rather than one right one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from tariff_agent.documents.document import Document, DocumentKind, Page, Table
from tariff_agent.models import Language
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

# Preferred split points, strongest first: paragraph, line, Armenian full stop
# or Latin sentence end.
_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[։.!?])\s+")
_DIGIT_RUN = re.compile(r"[0-9][0-9.,%  ]*[0-9%]")


class ChunkType(StrEnum):
    """What kind of content a chunk holds."""

    TEXT = "text"
    TABLE = "table"


class SourceRole(StrEnum):
    """Whether the chunk came from the product's primary source.

    Retrieval keeps the two apart so that Phase 6 can compare what they say and
    raise a conflict when they disagree - as the 2023 mortgage summary and the
    current product page do about the nominal rate.
    """

    PRIMARY = "primary"
    SUPPORTING = "supporting"


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable passage, carrying everything evidence needs.

    Attributes:
        chunk_id: Stable identifier, unique within a document.
        doc_id: Content hash of the source document.
        text: The passage itself. For a table, the section title followed by the
            rendered rows.
        page: 1-based page. For HTML this is 1, and the evidence layer reports
            no page number at all.
        section: Nearest heading, when one was detected.
        char_start: Offset into ``Page.text`` where this passage begins, or
            ``None`` for a table, whose text is a rendering rather than a span.
        char_end: Offset just past the passage.
        chunk_type: Text or table.
        source_role: Primary or supporting source.
        document_kind: Whether the source was a PDF or an HTML page. Evidence
            from HTML reports no page number, because it has none, and the
            chunk is the only thing the extraction layer sees.
        document_name: Title, for evidence and for the reviewer.
        source_url: Where the document came from.
        language: Document language.
        retrieved_at: When the document was downloaded.
        document_date: The date the document states for itself, when it does.
            Carried here so a conflict between two sources can show that one of
            them is years older than the other.
    """

    chunk_id: str
    doc_id: str
    text: str
    page: int
    chunk_type: ChunkType
    source_role: SourceRole
    document_kind: DocumentKind
    document_name: str
    source_url: str
    language: Language
    retrieved_at: datetime
    section: str | None = None
    document_date: date | None = None
    char_start: int | None = None
    char_end: int | None = None

    @property
    def evidence_page(self) -> int | None:
        """The page number to cite, or None when the source has no pages."""
        return None if self.document_kind is DocumentKind.HTML else self.page

    @property
    def is_table(self) -> bool:
        """Whether this chunk renders a table."""
        return self.chunk_type is ChunkType.TABLE

    def citation(self) -> str:
        """Render the provenance line shown with this chunk in a prompt.

        Returns:
            A compact description naming document, page and section.
        """
        parts = [self.document_name]
        if self.page and self.chunk_type is not ChunkType.TABLE or self.page > 1:
            parts.append(f"page {self.page}")
        elif self.page:
            parts.append(f"page {self.page}")
        if self.section:
            parts.append(self.section)
        return " → ".join(parts)


def _safe_split_point(text: str, target: int) -> int:
    """Find a split point at or before ``target`` that does not cut a number.

    Args:
        text: The text being split.
        target: The preferred offset.

    Returns:
        An offset that is not inside a digit run.
    """
    if target >= len(text):
        return len(text)
    for match in _DIGIT_RUN.finditer(text):
        if match.start() < target < match.end():
            return match.start()
    return target


def _split_span(text: str, max_chars: int, overlap: int) -> list[tuple[int, int]]:
    """Split a span into windows, preferring natural boundaries.

    Args:
        text: The span's text.
        max_chars: Maximum window size.
        overlap: How much each window repeats of the previous one.

    Returns:
        ``(start, end)`` offsets into ``text``.
    """
    if len(text) <= max_chars:
        return [(0, len(text))]

    windows: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            window = text[start:end]
            cut = max(
                (match.end() for match in _PARAGRAPH.finditer(window)),
                default=max((match.end() for match in _SENTENCE.finditer(window)), default=0),
            )
            if cut > max_chars // 3:
                end = start + cut
            end = start + _safe_split_point(text[start:], end - start)
        windows.append((start, end))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return windows


def _sections_on(document: Document, page_number: int) -> list[tuple[int, int, str | None]]:
    """Return the section spans covering one page.

    Args:
        document: The processed document.
        page_number: The page to describe.

    Returns:
        ``(start, end, title)`` spans covering the whole page text, including an
        untitled span before the first heading.
    """
    page = next(p for p in document.pages if p.number == page_number)
    sections = [s for s in document.sections if s.page == page_number]
    if not sections:
        return [(0, len(page.text), None)]

    spans: list[tuple[int, int, str | None]] = []
    if sections[0].start > 0:
        spans.append((0, sections[0].start, None))
    for section in sections:
        spans.append((section.start, max(section.end, section.start), section.title))
    return [span for span in spans if span[1] > span[0]]


def _table_text(table: Table, section: str | None) -> str:
    """Render a table with the heading it belongs under.

    A fee row is meaningless without its section: «0.5% | ամսական» only becomes a
    service fee under «Սպասարկման վճար». The model sees chunk text, not
    metadata, so the title has to be inside it.

    Args:
        table: The table.
        section: The nearest heading, if known.

    Returns:
        The chunk text for this table.
    """
    heading = section or table.section
    return f"{heading}\n{table.as_text()}" if heading else table.as_text()


def _split_table(table: Table, max_chars: int) -> list[tuple[tuple[str, ...], ...]]:
    """Split a long table into row groups, repeating the header on each.

    Args:
        table: The table to split.
        max_chars: Budget per chunk.

    Returns:
        Row groups, each beginning with the header row.
    """
    if not table.rows:
        return []
    header, *body = table.rows
    groups: list[tuple[tuple[str, ...], ...]] = []
    current: list[tuple[str, ...]] = [header]
    size = len(" | ".join(header))
    for row in body:
        row_size = len(" | ".join(row)) + 1
        if size + row_size > max_chars and len(current) > 1:
            groups.append(tuple(current))
            current = [header]
            size = len(" | ".join(header))
        current.append(row)
        size += row_size
    groups.append(tuple(current))
    return groups


def chunk_document(
    document: Document,
    *,
    source_role: SourceRole = SourceRole.PRIMARY,
    max_chars: int = 1200,
    overlap: int = 150,
) -> list[Chunk]:
    """Cut a processed document into retrievable chunks.

    Args:
        document: The processed document.
        source_role: Whether this is the product's primary source.
        max_chars: Target chunk size.
        overlap: Characters repeated between consecutive text chunks.

    Returns:
        The chunks, in document order: text chunks per page followed by that
        page's tables.
    """
    chunks: list[Chunk] = []
    for page in document.pages:
        chunks.extend(_chunk_page(document, page, source_role, max_chars, overlap))
    logger.info(
        "document_chunked",
        extra={
            "doc_id": document.doc_id[:12],
            "chunks": len(chunks),
            "tables": sum(1 for chunk in chunks if chunk.is_table),
            "role": source_role.value,
        },
    )
    return chunks


def _chunk_page(
    document: Document,
    page: Page,
    source_role: SourceRole,
    max_chars: int,
    overlap: int,
) -> list[Chunk]:
    """Chunk one page's text and tables.

    Args:
        document: The owning document.
        page: The page to chunk.
        source_role: Whether this is the product's primary source.
        max_chars: Target chunk size.
        overlap: Characters repeated between consecutive text chunks.

    Returns:
        The page's chunks.
    """

    def build(
        text: str,
        chunk_type: ChunkType,
        section: str | None,
        start: int | None,
        end: int | None,
    ) -> Chunk:
        return Chunk(
            chunk_id=f"{document.doc_id[:8]}-p{page.number}-{len(chunks):03d}",
            doc_id=document.doc_id,
            text=text,
            page=page.number,
            chunk_type=chunk_type,
            source_role=source_role,
            document_kind=document.kind,
            document_name=document.document_name,
            source_url=document.source_url,
            language=document.language,
            retrieved_at=document.retrieved_at,
            document_date=document.document_date,
            section=section,
            char_start=start,
            char_end=end,
        )

    chunks: list[Chunk] = []
    for span_start, span_end, title in _sections_on(document, page.number):
        span = page.text[span_start:span_end]
        for window_start, window_end in _split_span(span, max_chars, overlap):
            text = span[window_start:window_end].strip()
            if not text:
                continue
            absolute = span_start + window_start
            chunks.append(
                build(text, ChunkType.TEXT, title, absolute, span_start + window_end)
            )

    for table in page.tables:
        section = table.section or _section_title_for_table(document, page)
        for rows in _split_table(table, max_chars):
            piece = Table(page=page.number, rows=rows, section=section)
            chunks.append(build(_table_text(piece, section), ChunkType.TABLE, section, None, None))
    return chunks


def _section_title_for_table(document: Document, page: Page) -> str | None:
    """Guess which section a table belongs to.

    PyMuPDF does not report where on the page a table sits relative to the
    headings, so the last heading on the page is used. Imperfect, and better
    than leaving a fee table unlabelled.

    Args:
        document: The owning document.
        page: The page the table is on.

    Returns:
        The section title, if the page has one.
    """
    titles = [section.title for section in document.sections if section.page == page.number]
    return titles[-1] if titles else None
