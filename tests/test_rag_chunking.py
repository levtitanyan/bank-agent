"""Tests for chunking.

Chunk boundaries decide whether evidence can be verified, so the rules here are
about provenance rather than ranking: a chunk must belong to exactly one page,
its offsets must point back at the text it came from, and a table must arrive
with the heading that gives its rows meaning.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tariff_agent.documents.document import (
    Document,
    DocumentKind,
    ExtractionMethod,
    Page,
    Section,
    Table,
)
from tariff_agent.models import Language
from tariff_agent.rag.chunking import ChunkType, SourceRole, chunk_document

LONG = "Անվանական տոկոսադրույքը սահմանվում է պայմանագրով և կարող է փոփոխվել։ "


def build_document(
    pages: list[Page], sections: list[Section] | None = None, kind: DocumentKind = DocumentKind.PDF
) -> Document:
    """Build a processed document for chunking tests."""
    now = datetime.now(UTC)
    return Document(
        doc_id="a" * 64,
        source_url="https://www.acba.am/files/loans-tariffs.pdf",
        document_name="Վարկային սակագներ",
        kind=kind,
        language=Language.HY,
        retrieved_at=now,
        checked_at=now,
        pages=tuple(pages),
        sections=tuple(sections or []),
    )


def text_page(number: int, text: str, tables: tuple[Table, ...] = ()) -> Page:
    """Build one page."""
    return Page(
        number=number,
        text=text,
        method=ExtractionMethod.PDF_TEXT,
        quality=0.9,
        tables=tables,
    )


def test_a_chunk_never_spans_two_pages() -> None:
    """Evidence cites one page; a chunk crossing a boundary makes that false."""
    document = build_document([text_page(1, LONG * 30), text_page(2, LONG * 30)])
    chunks = chunk_document(document, max_chars=400, overlap=50)
    assert len(chunks) > 4
    for chunk in chunks:
        assert chunk.page in (1, 2)
        page_text = document.pages[chunk.page - 1].text
        assert chunk.text.strip()
        assert chunk.text[:40] in page_text


def test_offsets_point_back_at_the_text_they_came_from() -> None:
    """Phase 6 locates a quote by these offsets; wrong offsets break verification."""
    document = build_document([text_page(1, LONG * 20)])
    for chunk in chunk_document(document, max_chars=500, overlap=60):
        assert chunk.char_start is not None and chunk.char_end is not None
        span = document.pages[0].text[chunk.char_start : chunk.char_end]
        assert chunk.text.strip() in span.strip() or span.strip().startswith(chunk.text[:30])


def test_consecutive_chunks_overlap() -> None:
    """A value split across a boundary must survive in one of the two chunks."""
    document = build_document([text_page(1, LONG * 20)])
    chunks = chunk_document(document, max_chars=400, overlap=120)
    assert len(chunks) >= 2
    starts = [chunk.char_start for chunk in chunks]
    ends = [chunk.char_end for chunk in chunks]
    assert any(starts[index + 1] < ends[index] for index in range(len(chunks) - 1))


def test_a_split_never_falls_inside_a_number() -> None:
    """«1,000,000» cut in half becomes two wrong values instead of one right one."""
    filler = "տեքստ " * 60
    document = build_document([text_page(1, f"{filler}գումարը՝ 1,000,000 ՀՀ դրամ {filler}")])
    for chunk in chunk_document(document, max_chars=200, overlap=20):
        assert not chunk.text.endswith("1,")
        assert not chunk.text.startswith("000,000")


def test_a_table_is_its_own_chunk_and_carries_its_heading() -> None:
    """«0.5% | ամսական» means nothing until «Սպասարկման վճար» is above it.

    The model sees chunk text, not metadata, so the section title has to be
    inside the text rather than only recorded beside it.
    """
    table = Table(
        page=1,
        rows=(("Արժույթ", "Տոկոսադրույք"), ("ՀՀ դրամ", "13,5%")),
        section="Սպասարկման վճար",
    )
    document = build_document([text_page(1, LONG * 3, tables=(table,))])
    chunks = chunk_document(document)
    table_chunks = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TABLE]
    assert len(table_chunks) == 1
    assert table_chunks[0].text.startswith("Սպասարկման վճար")
    assert "ՀՀ դրամ | 13,5%" in table_chunks[0].text
    assert table_chunks[0].char_start is None


def test_a_long_table_is_split_with_the_header_repeated() -> None:
    """Half a table without its header is a grid of unlabelled numbers."""
    rows = (("Արժույթ", "Տոկոսադրույք", "Ժամկետ"),) + tuple(
        (f"Արժույթ {index}", f"{index},5%", f"{index} ամիս") for index in range(40)
    )
    document = build_document([text_page(1, LONG, tables=(Table(page=1, rows=rows),))])
    table_chunks = [
        chunk for chunk in chunk_document(document, max_chars=300)
        if chunk.chunk_type is ChunkType.TABLE
    ]
    assert len(table_chunks) > 1
    for chunk in table_chunks:
        assert "Արժույթ | Տոկոսադրույք | Ժամկետ" in chunk.text


def test_sections_become_chunk_metadata() -> None:
    """The section is what evidence reports, so it has to reach the chunk."""
    page_text = "Ներածություն\nտեքստ առաջին\nՏոկոսադրույք\nանվանական 13,5%"
    sections = [
        Section(title="Ներածություն", page=1, start=0, end=26),
        Section(title="Տոկոսադրույք", page=1, start=26, end=len(page_text)),
    ]
    document = build_document([text_page(1, page_text)], sections)
    titles = {chunk.section for chunk in chunk_document(document)}
    assert titles == {"Ներածություն", "Տոկոսադրույք"}


def test_source_role_is_carried_so_conflicts_can_be_seen_later() -> None:
    """Primary and supporting must stay distinguishable all the way to Phase 6."""
    document = build_document([text_page(1, LONG * 3)])
    chunks = chunk_document(document, source_role=SourceRole.SUPPORTING)
    assert all(chunk.source_role is SourceRole.SUPPORTING for chunk in chunks)


def test_a_short_document_yields_one_chunk() -> None:
    """No padding, no empty chunks."""
    document = build_document([text_page(1, "Արժույթ՝ ՀՀ դրամ")])
    chunks = chunk_document(document)
    assert len(chunks) == 1
    assert chunks[0].text == "Արժույթ՝ ՀՀ դրամ"


def test_chunk_metadata_covers_what_retrieval_promises() -> None:
    """Bank, product, document, url, language and time all travel with the text."""
    document = build_document([text_page(1, LONG * 2)], kind=DocumentKind.HTML)
    chunk = chunk_document(document)[0]
    assert chunk.document_name == "Վարկային սակագներ"
    assert chunk.source_url.endswith(".pdf")
    assert chunk.language is Language.HY
    assert chunk.doc_id == "a" * 64
    assert chunk.retrieved_at.tzinfo is not None
