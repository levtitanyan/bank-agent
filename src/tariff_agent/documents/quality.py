"""Scoring how usable a piece of extracted text is.

Used twice: to decide whether OCR is worth attempting, and to decide which
strategy's output to keep. Both uses need *one* comparable scale, so every
candidate is scored by the same function.

Three of the five signals exist because reconnaissance on the real ACBA mortgage
summary showed the usual ones miss its failure modes:

* flat extraction returns one word per line - readable characters, unusable
  structure, so **words per line** matters;
* block extraction returns «Անշարժգույքիձեռքբերմանհիփոթեքային...» - correct
  characters with no word boundaries at all, so **glued tokens** matter;
* structured extraction returns 53 of 1,593 characters - so **length** matters.

A high score means "this text is worth quoting from", not "this text is
correct". Correctness is evidence verification in Phase 6.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_WORD = re.compile(r"\S+")
GLUED_TOKEN_CHARS = 25
"""A token longer than this has almost certainly lost its word boundaries."""

IDEAL_WORDS_PER_LINE = 6.0
"""Roughly what a normal text line holds; the real tariff PDF averages 6.75."""

# Fatal defects. Each names a way extraction can produce text that looks
# plausible by the basic measures and is unusable in practice, and each was
# observed on a real ACBA document. A weighted average lets a candidate fail two
# of these and still score respectably, so they are applied as multipliers.
_MIN_USEFUL_CHARS = 50
_GLUED_LIMIT = 0.3
_ONE_WORD_LIMIT = 1.5
_LETTER_LIMIT = 0.3
_REPLACEMENT_LIMIT = 0.05


@dataclass(frozen=True, slots=True)
class TextQuality:
    """The score, the signals behind it, and any fatal defects found.

    Attributes:
        score: 0-1 overall usability.
        chars: Number of characters.
        letter_ratio: Share of non-space characters that are letters.
        words_per_line: Mean tokens per non-empty line.
        glued_ratio: Share of tokens longer than :data:`GLUED_TOKEN_CHARS`.
        replacement_ratio: Share of replacement or control characters.
        defects: Names of the fatal defects that cut the score, e.g.
            ``("one_word_per_line",)``. Reported so a low score can be explained
            in a log line rather than being an opaque number.
    """

    score: float
    chars: int
    letter_ratio: float
    words_per_line: float
    glued_ratio: float
    replacement_ratio: float
    defects: tuple[str, ...] = ()

    def as_log_fields(self) -> dict[str, object]:
        """Return the signals in a form suitable for a structured log line."""
        return {
            "quality": round(self.score, 3),
            "chars": self.chars,
            "letter_ratio": round(self.letter_ratio, 3),
            "words_per_line": round(self.words_per_line, 2),
            "glued_ratio": round(self.glued_ratio, 3),
            "defects": list(self.defects),
        }


def _clamp(value: float) -> float:
    """Clamp a value into 0-1.

    Args:
        value: Any float.

    Returns:
        The value limited to the closed interval 0-1.
    """
    return max(0.0, min(1.0, value))


def score_text(text: str, *, min_chars: int = 200) -> TextQuality:
    """Score how usable extracted text is.

    Args:
        text: The candidate text.
        min_chars: Length at which the length signal is considered satisfied.

    Returns:
        The score and the signals behind it. Empty text scores exactly 0.
    """
    stripped = text.strip()
    if not stripped:
        return TextQuality(0.0, 0, 0.0, 0.0, 0.0, 0.0)

    tokens = _WORD.findall(stripped)
    lines = [line for line in stripped.split("\n") if line.strip()]
    non_space = [ch for ch in stripped if not ch.isspace()]

    letters = sum(1 for ch in non_space if unicodedata.category(ch).startswith("L"))
    letter_ratio = letters / len(non_space) if non_space else 0.0

    bad = sum(
        1
        for ch in stripped
        if ch == "�" or (unicodedata.category(ch) == "Cc" and ch not in "\n\t")
    )
    replacement_ratio = bad / len(stripped)

    words_per_line = len(tokens) / len(lines) if lines else 0.0
    glued = sum(1 for token in tokens if len(token) > GLUED_TOKEN_CHARS)
    glued_ratio = glued / len(tokens) if tokens else 0.0

    length_signal = _clamp(len(stripped) / min_chars)
    letter_signal = _clamp(letter_ratio / 0.7)
    # One word per line scores 0; the ideal and anything above it scores 1.
    line_signal = _clamp((words_per_line - 1.0) / (IDEAL_WORDS_PER_LINE - 1.0))
    base = 0.45 * length_signal + 0.30 * letter_signal + 0.25 * line_signal

    defects: list[str] = []
    penalty = 1.0
    if len(stripped) < _MIN_USEFUL_CHARS:
        defects.append("too_short")
        penalty *= 0.3
    if glued_ratio > _GLUED_LIMIT:
        defects.append("glued_words")
        penalty *= 0.2
    if words_per_line < _ONE_WORD_LIMIT and len(lines) > 5:
        defects.append("one_word_per_line")
        penalty *= 0.3
    if letter_ratio < _LETTER_LIMIT:
        defects.append("few_letters")
        penalty *= 0.3
    if replacement_ratio > _REPLACEMENT_LIMIT:
        defects.append("unreadable_characters")
        penalty *= 0.2

    return TextQuality(
        score=round(_clamp(base * penalty), 4),
        chars=len(stripped),
        letter_ratio=round(letter_ratio, 4),
        words_per_line=round(words_per_line, 3),
        glued_ratio=round(glued_ratio, 4),
        replacement_ratio=round(replacement_ratio, 4),
        defects=tuple(defects),
    )
