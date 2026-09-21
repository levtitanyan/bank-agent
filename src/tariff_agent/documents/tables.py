"""Extracting tables, and refusing the ones that are not tables.

PyMuPDF's table finder is heuristic, and on a graphics-heavy page it reports
structure that is not there: on ACBA's mortgage summary it returns cells like
``['ն', 'և', '', '']`` - fragments of a word split across invisible column
boundaries. A junk table indexed as evidence is worse than a missing one,
because it will be retrieved and quoted with a page number that makes it look
authoritative.

So a detected table is kept only if it has enough rows, enough columns, and
enough non-empty cells to be a table at all.
"""

from __future__ import annotations

from typing import Any

from tariff_agent.config import DocumentSettings
from tariff_agent.documents.cleaning import clean_cell
from tariff_agent.documents.document import Table
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)


def _rows_from(raw: list[list[str | None]]) -> tuple[tuple[str, ...], ...]:
    """Normalize raw extracted cells.

    Args:
        raw: Rows of possibly-``None`` cells, as PyMuPDF returns them.

    Returns:
        Rows of cleaned cell strings. Cells are cleaned with the table-safe
        rule only: no line joining, no deduplication.
    """
    return tuple(tuple(clean_cell(cell or "") for cell in row) for row in raw)


def is_plausible_table(rows: tuple[tuple[str, ...], ...], settings: DocumentSettings) -> bool:
    """Judge whether detected cells really form a table.

    Args:
        rows: The normalized rows.
        settings: Provides the minimum shape and fill thresholds.

    Returns:
        True when the shape and fill are consistent with a real table.
    """
    if len(rows) < settings.table_min_rows:
        return False
    columns = max((len(row) for row in rows), default=0)
    if columns < settings.table_min_cols:
        return False
    cells = [cell for row in rows for cell in row]
    if not cells:
        return False
    filled = sum(1 for cell in cells if cell.strip())
    return filled / len(cells) >= settings.table_min_filled


def extract_tables(page: Any, page_number: int, settings: DocumentSettings) -> tuple[Table, ...]:
    """Extract the plausible tables from one PDF page.

    Args:
        page: A PyMuPDF page.
        page_number: 1-based page number, recorded on each table.
        settings: Shape and fill thresholds.

    Returns:
        The tables that passed validation. Detection failures are logged and
        treated as "no tables", never raised: a page without tables is normal.
    """
    try:
        found = page.find_tables()
    except Exception as exc:  # PyMuPDF raises various errors on odd layouts
        logger.info(
            "table_detection_failed",
            extra={"page": page_number, "error_type": type(exc).__name__},
        )
        return ()

    kept: list[Table] = []
    rejected = 0
    for table in getattr(found, "tables", []):
        try:
            rows = _rows_from(table.extract())
        except Exception as exc:
            logger.info(
                "table_extraction_failed",
                extra={"page": page_number, "error_type": type(exc).__name__},
            )
            continue
        if is_plausible_table(rows, settings):
            kept.append(Table(page=page_number, rows=rows))
        else:
            rejected += 1

    if rejected:
        logger.info(
            "tables_rejected_as_junk",
            extra={"page": page_number, "rejected": rejected, "kept": len(kept)},
        )
    return tuple(kept)
