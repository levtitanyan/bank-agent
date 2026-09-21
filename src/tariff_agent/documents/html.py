"""Turning a bank product page into a document.

HTML is a first-class source here, not a fallback: ACBA's consumer loan links no
information summary and states its rates in the page itself, so the page *is*
the authoritative document for that product.

Real pages are around 400 KB of markup wrapping a few KB of content, so the
navigation, scripts and boilerplate have to go. A dedicated readability library
was considered and rejected: a twenty-line heuristic tested against the actual
fixtures is easier to explain, easier to debug when a page changes, and one
fewer dependency in a project whose whole point is traceability.

An HTML document has exactly one page internally, so every code path downstream
is uniform, but :meth:`Document.evidence_page` reports ``None`` for it - page
numbers in evidence must be real.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from tariff_agent.config import DocumentSettings
from tariff_agent.documents.cleaning import clean_cell, dedupe_long_paragraphs, normalize_chars
from tariff_agent.documents.document import Section, Table
from tariff_agent.documents.tables import is_plausible_table
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

_DROP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg")
_HEADING_TAGS = ("h1", "h2", "h3", "h4")
MIN_CONTAINER_CHARS = 400
"""A semantic container must hold at least this much text to be preferred."""

SEMANTIC_CONTAINER_SHARE = 0.5
"""...and at least this share of the body, or the body itself is used."""


def _strip_chrome(soup: BeautifulSoup) -> None:
    """Remove the parts of a page that are never content.

    Args:
        soup: The parsed page, modified in place.
    """
    for tag_name in _DROP_TAGS:
        for element in soup.find_all(tag_name):
            element.decompose()


def _content_container(soup: BeautifulSoup) -> Tag:
    """Find the element holding the page's actual content.

    Deliberately blunt: the chrome has already been removed, so the body *is*
    essentially the content, and the only refinement worth making is to prefer a
    semantic ``<main>`` or ``<article>`` when the page provides one that holds
    most of the text.

    A cleverer rule was tried first - score containers by their own text,
    discounting text inside nested containers - and it failed badly on the real
    consumer-loan page: it picked a 3.6 KB leaf div out of 10.6 KB of content
    and dropped 19 of the 21 interest-rate mentions. On a page built from nested
    divs, that discount penalises exactly the element that holds the content.
    Losing content silently is far worse than keeping a little boilerplate,
    which retrieval can ignore.

    Args:
        soup: The parsed page, already stripped of chrome.

    Returns:
        The content element.
    """
    body = soup.body or soup
    body_chars = len(body.get_text(" ", strip=True))
    for tag_name in ("main", "article"):
        element = body.find(tag_name)
        if element is None:
            continue
        chars = len(element.get_text(" ", strip=True))
        if chars >= max(MIN_CONTAINER_CHARS, body_chars * SEMANTIC_CONTAINER_SHARE):
            logger.debug("html_container_chosen", extra={"tag": tag_name, "chars": chars})
            return element
    return body


def extract_tables_from_html(container: Tag, settings: DocumentSettings) -> tuple[Table, ...]:
    """Extract the tables from a page.

    Args:
        container: The content element.
        settings: Shape and fill thresholds, shared with the PDF path so both
            kinds of document are judged the same way.

    Returns:
        The tables that passed validation, all on page 1.
    """
    tables: list[Table] = []
    for element in container.find_all("table"):
        rows = tuple(
            tuple(clean_cell(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"]))
            for row in element.find_all("tr")
        )
        rows = tuple(row for row in rows if row)
        if rows and is_plausible_table(rows, settings):
            tables.append(Table(page=1, rows=rows))
    return tuple(tables)


def extract_html(
    html: str, settings: DocumentSettings
) -> tuple[str, tuple[Table, ...], tuple[Section, ...], str]:
    """Parse a page into text, tables, sections and a title.

    Args:
        html: The page source.
        settings: Table thresholds and deduplication length.

    Returns:
        A tuple of ``(text, tables, sections, title)``. The text excludes table
        markup, so a table's contents are not counted twice.
    """
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    _strip_chrome(soup)
    container = _content_container(soup)
    tables = extract_tables_from_html(container, settings)

    for element in container.find_all("table"):
        element.decompose()

    # The container's whole text, not a list of chosen tags: ACBA's pages put
    # most of their content in divs and spans, and an allow-list of p/li/td
    # dropped 74 of 76 interest-rate mentions on the consumer-loan page.
    raw = container.get_text("\n", strip=True)
    lines = [normalize_chars(line).strip() for line in raw.split("\n")]
    text = dedupe_long_paragraphs(
        "\n".join(line for line in lines if line), min_chars=settings.dedupe_min_chars
    )

    sections: list[Section] = []
    cursor = 0
    for heading in container.find_all(_HEADING_TAGS):
        title = normalize_chars(heading.get_text(" ", strip=True)).strip()
        if not title:
            continue
        found = text.find(title, cursor)
        if found < 0:
            continue
        cursor = found + len(title)
        sections.append(Section(title=title, page=1, start=found, end=found))
    sections = _close_sections(sections, len(text))
    logger.info(
        "html_parsed",
        extra={"chars": len(text), "tables": len(tables), "sections": len(sections)},
    )
    return text, tables, tuple(sections), title


def _close_sections(sections: list[Section], total: int) -> list[Section]:
    """Give each section an end offset: the start of the next one.

    Args:
        sections: Sections with start offsets only.
        total: Length of the page text.

    Returns:
        Sections with both offsets set.
    """
    closed: list[Section] = []
    for index, section in enumerate(sections):
        end = sections[index + 1].start if index + 1 < len(sections) else total
        closed.append(
            Section(
                title=section.title,
                page=section.page,
                start=section.start,
                end=max(end, section.start),
            )
        )
    return closed
