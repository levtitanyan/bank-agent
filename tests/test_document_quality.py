"""Tests for text quality scoring.

The score decides which extraction strategy wins, so it has to separate text
that is merely short from text that is unusable, and it has to catch the two
failures real ACBA documents produce: one word per line, and glued words.
"""

from __future__ import annotations

from tariff_agent.documents.quality import score_text

GOOD = (
    "Անվանական տոկոսադրույք՝ 13,5% տարեկան։ Վարկի ժամկետը մինչև 60 ամիս է, "
    "գումարը 300,000-ից 10,000,000 ՀՀ դրամ։ Ապահովվածությունը՝ երաշխավորություն "
    "կամ գրավ։ Սպասարկման վճար չի գանձվում։\n"
) * 3


def test_clean_armenian_prose_scores_high() -> None:
    """The baseline: text worth quoting from."""
    assert score_text(GOOD).score > 0.8


def test_empty_text_scores_zero() -> None:
    """Nothing to quote, nothing to score."""
    assert score_text("").score == 0.0
    assert score_text("   \n  ").score == 0.0


def test_one_word_per_line_is_detected() -> None:
    """How ACBA's mortgage summary extracts, and it must not win on merit."""
    quality = score_text("\n".join(GOOD.split()))
    assert "one_word_per_line" in quality.defects
    assert quality.score < 0.4
    assert quality.words_per_line < 1.5


def test_glued_words_are_detected() -> None:
    """How the same document's block extraction fails: no word boundaries."""
    quality = score_text("ԱնշարժգույքիձեռքբերմանհիփոթեքայինվարկԱրժույթՀՀդրամ " * 6)
    assert "glued_words" in quality.defects
    assert quality.score < 0.4


def test_mojibake_scores_near_zero() -> None:
    """Replacement characters mean the encoding was lost."""
    quality = score_text("��� �� ��� " * 30)
    assert quality.score < 0.1
    assert "unreadable_characters" in quality.defects


def test_a_fragment_scores_below_prose() -> None:
    """53 characters is what the mortgage summary's structured extraction gives."""
    assert score_text("Արժույթ՝ ՀՀ դրամ").score < score_text(GOOD).score


def test_defects_are_reported_so_a_low_score_can_be_explained() -> None:
    """A number nobody can explain is not useful in a review or a log."""
    quality = score_text("\n".join(GOOD.split()))
    assert quality.defects
    assert "defects" in quality.as_log_fields()
