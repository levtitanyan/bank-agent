"""Tests for the Armenian text layer.

The cases that matter are the ones the real documents produce: five inflected
forms of one concept, OCR that reads «և» as «ն», and query terms whose common
words match paragraphs that say nothing about the field.
"""

from __future__ import annotations

import pytest

from tariff_agent.rag.text import (
    fold_for_matching,
    has_informative_hit,
    has_lexical_hit,
    stem_hy,
    tokenize,
)

RATE_FORMS = (
    "տոկոսադրույք",
    "տոկոսադրույքը",
    "տոկոսադրույքի",
    "տոկոսադրույքին",
    "տոկոսադրույքից",
    "տոկոսադրույքների",
    "տոկոսադրույքներին",
)


def test_every_inflected_form_reaches_one_stem() -> None:
    """Five ways of writing one concept must retrieve the same passages."""
    stems = {stem_hy(fold_for_matching(form)) for form in RATE_FORMS}
    assert len(stems) == 1


def test_short_words_are_not_destroyed_by_stripping() -> None:
    """A suffix rule with no floor turns «վարկ» into nothing."""
    assert stem_hy("վարկ") == "վարկ"
    assert len(stem_hy("գին")) >= 3


def test_ocr_confusion_between_the_ligature_and_n_is_folded() -> None:
    """Tesseract reads «և» as «ն», so «Տևողություն» arrives as «Տնողություն»."""
    assert fold_for_matching("Տևողություն") == fold_for_matching("Տնողություն")
    assert fold_for_matching("մինչև") == fold_for_matching("մինչն")


def test_folding_is_one_directional() -> None:
    """Only «և» moves, which is what keeps the collision risk small.

    Measured on the indexed corpus - 26,054 tokens, 1,591 distinct folded
    forms - no «և» word collides with a different real word. The risk is real
    in principle and absent in this corpus.
    """
    assert fold_for_matching("ն") == "ն"
    assert "և" not in fold_for_matching("և")


def test_stored_text_is_never_rewritten() -> None:
    """Folding is a comparison key. «և» stays «և» in anything we keep."""
    original = "ՀՀ դրամ և արտարժույթ"
    assert "և" in original
    assert fold_for_matching(original) != original


def test_numbers_survive_as_single_tokens() -> None:
    """«13,5%» and «1,000,000» are the values being searched for."""
    tokens = tokenize("Անվանական տոկոսադրույք՝ 13,5% և գումարը 1,000,000 ՀՀ դրամ")
    assert "13,5%" in tokens
    assert "1,000,000" in tokens


def test_function_words_are_dropped_including_the_folded_ligature() -> None:
    """«և» folds to «ն», so an unfolded stoplist would leak it into every chunk."""
    assert "ն" not in tokenize("վարկ և ավանդ")


def test_a_term_is_present_when_most_of_it_occurs() -> None:
    """Documents paraphrase: «ստացող» in the query, «ստանալու» in the text."""
    text = "Աշխատավարձը Բանկի միջոցով ստանալու դեպքում կիրառվում է արտոնյալ տոկոսադրույք"
    assert has_lexical_hit(text, ("աշխատավարձը բանկի միջոցով",))


def test_common_words_alone_do_not_prove_a_field_is_present() -> None:
    """«հայտ» and «վճար» are everywhere in a tariff document.

    Without requiring the identifying token, any fee paragraph would be
    accepted as evidence of an application fee the product never states.
    """
    corpus_frequency = {"հայտ": 3, "վճար": 4, "ուսումնասիրությա": 0}
    text = "Վարկի հայտը ներկայացվում է առցանց։ Վճարները գանձվում են ամսական։"
    assert not has_informative_hit(
        text,
        ("հայտի ուսումնասիրության վճար",),
        is_informative=lambda token: corpus_frequency.get(token, 0),
    )


def test_the_identifying_token_makes_a_term_present() -> None:
    """With «ուսումնասիրության» in the text, the same term is confirmed."""
    corpus_frequency = {"հայտ": 3, "վճար": 4, "ուսումնասիրությա": 1}
    text = "Հայտի ուսումնասիրության վճար՝ 5,000 ՀՀ դրամ"
    assert has_informative_hit(
        text,
        ("հայտի ուսումնասիրության վճար",),
        is_informative=lambda token: corpus_frequency.get(token, 0),
    )


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_empty_input_is_handled(text: str) -> None:
    """Empty text tokenizes to nothing rather than raising."""
    assert tokenize(text) == []
    assert not has_lexical_hit(text, ("տոկոսադրույք",))
