"""Cleaning rules for extracted text. Pure functions, one rule each.

Every rule here can corrupt a tariff if it is too eager, so each is deliberately
conservative and separately tested. Three in particular were written against
what the real ACBA documents actually contain:

* **«և» is a letter, not punctuation.** It appears 144 times across the two real
  PDFs; «եւ» appears zero times. Treating it as a space would break
  «տեղեկատվական» and every synonym that contains it, so it is preserved exactly.
  Any equivalence between the two spellings belongs to retrieval-time folding,
  not to the stored text.
* **Numbers are barely touched.** «10 59 10 10» in the mortgage summary is a
  *phone number*, and «13, 5» may be a list. Only unambiguous thousand groups
  are rejoined; understanding what a number means is Phase 6's job.
* **Tables are never passed through here.** Joining wrapped lines and dropping
  repeated blocks are right for prose and destructive for a table, where a
  repeated short line is «0%» in another row.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

# A private-use bullet used by the real mortgage summary (72 occurrences).
_PUA_BULLET = ""
_PUA_RANGE = re.compile(r"[-]")
_ODD_SPACES = re.compile(r"[       ]")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_BLANK_LINES = re.compile(r"\n{3,}")

# Digit groups that are unambiguously thousand separators: 1-3 digits, then one
# or more groups of exactly three. "10 59 10 10" (a phone number) does not match,
# because its groups are two digits long.
_THOUSAND_GROUPS = re.compile(r"(?<![\d,.])(\d{1,3})((?: \d{3})+)(?![\d,.])")

_PAGE_NUMBER_PATTERNS = (
    re.compile(r"^\s*\d{1,3}\s*$"),
    re.compile(r"^\s*\d{1,3}\s*/\s*\d{1,3}\s*$"),
    re.compile(r"^\s*էջ\s*\d{1,3}\s*(?:/\s*\d{1,3})?\s*$", re.IGNORECASE),
    re.compile(r"^\s*page\s*\d{1,3}\s*(?:of\s*\d{1,3})?\s*$", re.IGNORECASE),
    re.compile(r"^\s*-\s*\d{1,3}\s*-\s*$"),
)

# A line that continues on the next one: no terminal punctuation, and the next
# line does not start something new.
_SENTENCE_END = re.compile(r"[.!?:։՝;]\s*$")
_LIST_START = re.compile(r"^\s*(?:[•\-–—*]|\d{1,2}[.)]|[ա-ֆ][.)])\s+")


def normalize_chars(text: str) -> str:
    """Put text into the single character form the rest of the code expects.

    Applies NFC so composed and decomposed Armenian compare equal, maps the
    private-use bullets real ACBA PDFs contain to «•», replaces exotic space
    characters with an ordinary space, and collapses runs of spaces and blank
    lines.

    «և» is left exactly as it is: it is a letter, and NFC does not decompose it.

    Args:
        text: Raw extracted text.

    Returns:
        Normalized text.
    """
    text = unicodedata.normalize("NFC", text)
    text = text.replace(_PUA_BULLET, "•")
    text = _PUA_RANGE.sub("", text)
    text = _ODD_SPACES.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTI_SPACE.sub(" ", text)
    return _BLANK_LINES.sub("\n\n", text)


def repair_split_numbers(text: str) -> str:
    """Rejoin digit groups that are unmistakably one number.

    Only thousand separators are touched - «10 000 000» becomes «10000000»'s
    readable form «10 000 000» with a non-breaking join, i.e. the spaces are
    removed so the value reads as one token. A phone number written «10 59 10
    10» is left alone, because its groups are not three digits long, and
    «13, 5» is left alone because interpreting it is Phase 6's job.

    Args:
        text: Normalized text.

    Returns:
        Text with thousand-grouped numbers joined.
    """

    def _join(match: re.Match[str]) -> str:
        return match.group(1) + match.group(2).replace(" ", "")

    return _THOUSAND_GROUPS.sub(_join, text)


def strip_page_numbers(text: str) -> str:
    """Remove lines that are only a page number.

    Covers the bare number, ``3/12``, the Armenian «էջ 1/9» used by the real
    tariff document, ``Page 3 of 9`` and ``- 3 -``.

    Args:
        text: Page text.

    Returns:
        The text without page-number lines.
    """
    kept = [
        line
        for line in text.split("\n")
        if not any(pattern.match(line) for pattern in _PAGE_NUMBER_PATTERNS)
    ]
    return "\n".join(kept)


def strip_repeated_furniture(
    pages: Sequence[str], *, ratio: float = 0.6, window: int = 3, min_pages: int = 3
) -> list[str]:
    """Remove headers and footers that repeat across a document.

    A line appearing at the top or bottom of at least ``ratio`` of the pages is
    furniture rather than content. Requires at least ``min_pages`` pages: in a
    two-page document a repeated line is as likely to be a real heading.

    Args:
        pages: Page texts, in order.
        ratio: Fraction of pages a line must appear on to count as furniture.
        window: Upper bound on how many lines at each end to consider. The
            actual window shrinks on short pages, so that body text is never
            inside the edge on every page.
        min_pages: Below this many pages, nothing is removed.

    Returns:
        The page texts with furniture lines removed.
    """
    if len(pages) < min_pages:
        return list(pages)

    counts: Counter[str] = Counter()
    for page in pages:
        lines = [line.strip() for line in page.split("\n") if line.strip()]
        # The window has to scale with the page: on a short page a fixed window
        # of three covers the whole thing, and body text gets counted as
        # furniture because it "repeats at the edge" of every page.
        edge = max(1, min(window, len(lines) // 3))
        edges = {*lines[:edge], *lines[-edge:]}
        counts.update(line for line in edges if len(line) > 2)

    threshold = max(2, int(len(pages) * ratio))
    furniture = {line for line, count in counts.items() if count >= threshold}
    if not furniture:
        return list(pages)

    cleaned: list[str] = []
    for page in pages:
        kept = [line for line in page.split("\n") if line.strip() not in furniture]
        cleaned.append("\n".join(kept))
    return cleaned


def join_broken_lines(text: str) -> str:
    """Join lines that a PDF wrapped mid-sentence.

    Two cases: a line ending in a hyphen continues without a space, and a line
    with no terminal punctuation continues with one. Lines that start a list
    item or a new sentence are left alone.

    Never applied to table cells - a table's line breaks are structure.

    Args:
        text: Page text.

    Returns:
        Text with wrapped lines rejoined.
    """
    lines = text.split("\n")
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not out or not line:
            out.append(line)
            continue
        previous = out[-1]
        if not previous.strip():
            out.append(line)
            continue
        if _LIST_START.match(line) or _SENTENCE_END.search(previous):
            out.append(line)
            continue
        if previous.endswith("-"):
            out[-1] = previous[:-1] + line.lstrip()
        else:
            out[-1] = f"{previous} {line.lstrip()}"
    return "\n".join(out)


def dedupe_long_paragraphs(text: str, *, min_chars: int = 120) -> str:
    """Drop repeated long paragraphs, keeping short repeats.

    Tariff documents legitimately repeat «0%» and «չի գանձվում» in section after
    section; those are data. A repeated paragraph of 120 characters or more is a
    duplicated block, usually a boilerplate notice.

    Args:
        text: Document text.
        min_chars: Minimum length for a paragraph to be considered for removal.

    Returns:
        Text with long duplicate paragraphs removed.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for paragraph in text.split("\n\n"):
        key = " ".join(paragraph.split())
        if len(key) >= min_chars:
            if key in seen:
                continue
            seen.add(key)
        kept.append(paragraph)
    return "\n\n".join(kept)


def prepare_for_scoring(text: str) -> str:
    """Apply only the rules that cannot hide an extraction defect.

    Scoring has to happen **before** lines are joined. Line joining is what
    makes one-word-per-line text look like prose, so cleaning first would raise
    the very signal that detects the defect - a page extracted one word per
    line, with digits dropped, scored a perfect 1.00 until this split existed.

    Args:
        text: Raw extracted text.

    Returns:
        Text with characters normalized and page numbers removed, and its line
        structure untouched.
    """
    return strip_page_numbers(normalize_chars(text)).strip()


def finish_page_text(text: str) -> str:
    """Apply the rules that make text readable, once a strategy has won.

    Args:
        text: Text that has already been scored.

    Returns:
        Text with wrapped lines joined and thousand groups repaired.
    """
    return repair_split_numbers(join_broken_lines(text)).strip()


def clean_page_text(text: str) -> str:
    """Run the full per-page pipeline.

    Kept for callers that do not need to score, such as tests of the rules
    themselves; the processing path scores between the two halves.

    Args:
        text: Raw page text.

    Returns:
        Cleaned page text.
    """
    return finish_page_text(prepare_for_scoring(text))


def clean_cell(text: str) -> str:
    """Normalize one table cell, without any of the prose rules.

    Args:
        text: Raw cell text.

    Returns:
        The cell with characters normalized and whitespace collapsed. Line
        joining, page-number stripping and deduplication are deliberately not
        applied: in a table they destroy structure.
    """
    return " ".join(normalize_chars(text).split())
