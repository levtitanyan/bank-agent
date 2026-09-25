"""Finding headings in page text, so evidence can name a section.

PDF pages arrive without structure: PyMuPDF's font metadata is unusable on the
mortgage summary (its structured extraction returns 53 of 1,593 characters), and
OCR output has no font information at all. So headings are recognised from the
shape of the text, which is the only signal both paths share.

A heading here is a short line that does not end like a sentence and is followed
by something longer. That is a heuristic, and it will miss some and invent
others - but an approximate section beats the empty string, which is what
evidence carried before this existed.
"""

from __future__ import annotations

import re

from tariff_agent.documents.document import Section

MAX_HEADING_CHARS = 70
"""Longer than this and it is a sentence, not a heading."""

MIN_BODY_CHARS = 40
"""The line after a heading should be longer, or the pair is just short lines."""

_SENTENCE_END = re.compile(r"[.!?:։]\s*$")
_MOSTLY_DIGITS = re.compile(r"^[\d\s.,/%-]+$")


def detect_sections(page_text: str, page_number: int) -> list[Section]:
    """Detect headings on one page and the spans beneath them.

    Args:
        page_text: The cleaned page text.
        page_number: 1-based page number, recorded on each section.

    Returns:
        Sections covering the page, each running to the next heading.
    """
    lines = page_text.split("\n")
    headings: list[tuple[int, str]] = []
    offset = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if _is_heading(stripped, following):
            headings.append((offset, stripped))
        offset += len(line) + 1

    sections: list[Section] = []
    for position, (start, title) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(page_text)
        sections.append(Section(title=title, page=page_number, start=start, end=end))
    return sections


def _is_heading(line: str, following: str) -> bool:
    """Judge whether a line is a heading.

    Args:
        line: The candidate line.
        following: The line after it.

    Returns:
        True when the line is short, unpunctuated, not numeric, and introduces
        something longer than itself.
    """
    if not (3 <= len(line) <= MAX_HEADING_CHARS):
        return False
    if _SENTENCE_END.search(line) or _MOSTLY_DIGITS.match(line):
        return False
    if line.startswith(("•", "-", "\u2013")):
        return False
    return len(following) >= MIN_BODY_CHARS
