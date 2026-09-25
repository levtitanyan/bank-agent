"""Finding the date a document was issued or last updated.

A tariff document's age is part of its evidence. ACBA's mortgage information
summary carries «Թարմացվել է առ՝ 15.05.2023թ.» while the product page describing
the same loan is current, and the two state different nominal rates. Without the
date, a reviewer handed that disagreement sees two official sources; with it,
they see a three-year-old summary and a current page, which usually settles it.

Armenian documents date themselves in several ways, and OCR damages all of them,
so the patterns are deliberately forgiving about internal spacing.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Final

from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

_MONTHS: Final[dict[str, int]] = {
    "հունվար": 1, "փետրվար": 2, "մարտ": 3, "ապրիլ": 4, "մայիս": 5, "հունիս": 6,
    "հուլիս": 7, "օգոստոս": 8, "սեպտեմբեր": 9, "հոկտեմբեր": 10, "նոյեմբեր": 11,
    "դեկտեմբեր": 12,
}

# «Թարմացվել է առ՝ 15.05.2023թ.» - OCR often inserts a space after a dot.
_DOTTED = re.compile(r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})")
# «Հաստատման ամսաթիվ 23/04/26»
_SLASHED = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})")
# «Ուժի մեջ է 2026թ. ապրիլի 29-ից»
_ARMENIAN = re.compile(
    r"(\d{4})\s*թ\.?\s*([ա-ֆԱ-Ֆ]+?)(?:ի|in)?\s*(\d{1,2})", re.IGNORECASE
)

_MARKERS: Final[tuple[str, ...]] = (
    "թարմացվել", "ուժի մեջ", "հաստատման", "updated", "effective",
)


def parse_document_date(text: str, *, window: int = 220) -> date | None:
    """Find the document's own date, preferring text near a dating phrase.

    Args:
        text: The document's text, or its first pages.
        window: How many characters after a dating phrase to search first.

    Returns:
        The date, or ``None`` when the document does not state one. A wrong
        date is worse than none - it would make a current document look stale -
        so only well-formed, plausible dates are accepted.
    """
    lowered = text.casefold()
    for marker in _MARKERS:
        position = lowered.find(marker)
        while position != -1:
            found = _first_date(text[position : position + window])
            if found is not None:
                logger.info(
                    "document_date_found",
                    extra={"date": found.isoformat(), "marker": marker},
                )
                return found
            position = lowered.find(marker, position + 1)
    return None


def _first_date(fragment: str) -> date | None:
    """Return the first plausible date in a fragment.

    Args:
        fragment: Text to scan.

    Returns:
        The date, or None.
    """
    match = _DOTTED.search(fragment)
    if match:
        return _build(int(match.group(3)), int(match.group(2)), int(match.group(1)))

    match = _ARMENIAN.search(fragment)
    if match:
        month = _month_number(match.group(2))
        if month is not None:
            return _build(int(match.group(1)), month, int(match.group(3)))

    match = _SLASHED.search(fragment)
    if match:
        year = int(match.group(3))
        year += 2000 if year < 100 else 0
        return _build(year, int(match.group(2)), int(match.group(1)))
    return None


def _month_number(name: str) -> int | None:
    """Map an Armenian month name to its number.

    Args:
        name: The month name, possibly inflected.

    Returns:
        The month number, or None when the word is not a month.
    """
    folded = name.casefold()
    for stem, number in _MONTHS.items():
        if folded.startswith(stem):
            return number
    return None


def _build(year: int, month: int, day: int) -> date | None:
    """Build a date, rejecting implausible ones.

    Args:
        year: Four-digit year.
        month: Month number.
        day: Day of month.

    Returns:
        The date, or None when it is out of range. Bank documents are not from
        1990 and not from 2100; a value outside that is a misparse.
    """
    if not (2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None
