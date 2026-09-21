"""Tests for the text-cleaning rules.

Each rule can corrupt a tariff if it is too eager, so each is tested alone. The
cases that matter most are the ones taken from the real ACBA documents: a phone
number that looks like a split amount, «և» in the middle of words, and repeated
short values that are data rather than duplication.
"""

from __future__ import annotations

from tariff_agent.documents.cleaning import (
    clean_cell,
    dedupe_long_paragraphs,
    finish_page_text,
    join_broken_lines,
    normalize_chars,
    prepare_for_scoring,
    repair_split_numbers,
    strip_page_numbers,
    strip_repeated_furniture,
)


def test_armenian_and_ligature_survive_normalization() -> None:
    """«և» is a letter. Treating it as a space breaks «տեղեկատվական»."""
    text = "Տեղեկատվական ամփոփագիր՝ ՀՀ դրամ և արտարժույթ"
    cleaned = normalize_chars(text)
    assert "և" in cleaned
    assert "Տեղեկատվական" in cleaned
    assert "եւ" not in cleaned


def test_composed_and_decomposed_armenian_become_identical() -> None:
    """NFC, so the same word from two sources compares equal."""
    import unicodedata

    composed = "սպառողական վարկ"
    assert normalize_chars(unicodedata.normalize("NFD", composed)) == normalize_chars(composed)


def test_private_use_bullets_become_real_bullets() -> None:
    """The real mortgage summary contains 72 of these."""
    assert normalize_chars(" վարկի տրամադրում") == "• վարկի տրամադրում"


def test_exotic_spaces_collapse_but_newlines_survive() -> None:
    """Line structure is a signal; stray space characters are not."""
    assert normalize_chars("10 000") == "10 000"
    assert normalize_chars("ա\n\n\n\nբ") == "ա\n\nբ"


def test_thousand_groups_are_rejoined() -> None:
    """«10 000 000 ՀՀ դրամ» is one value, not three."""
    assert repair_split_numbers("գումարը 10 000 000 ՀՀ դրամ") == "գումարը 10000000 ՀՀ դրամ"
    assert repair_split_numbers("300 000-ից") == "300000-ից"


def test_a_phone_number_is_not_treated_as_an_amount() -> None:
    """«10 59 10 10» appears in the real document and is a phone number.

    Its groups are two digits long, so the thousand-separator rule does not
    match it. Getting this wrong would invent the amount 10591010.
    """
    assert repair_split_numbers("Հեռախոս՝ 10 59 10 10") == "Հեռախոս՝ 10 59 10 10"


def test_a_decimal_written_with_a_comma_is_left_alone() -> None:
    """«13, 5» may be a list; deciding is Phase 6's job, not cleaning's."""
    assert repair_split_numbers("13, 5 %") == "13, 5 %"
    assert repair_split_numbers("տոկոսադրույք 13,5%") == "տոկոսադրույք 13,5%"


def test_page_number_lines_are_removed() -> None:
    """Including «էջ 1/9», the form the real tariff document uses."""
    text = "Տոկոսադրույք\n3\nէջ 1/9\n- 7 -\nPage 2 of 9\n12/240\nԳումար"
    kept = strip_page_numbers(text).splitlines()
    assert kept == ["Տոկոսադրույք", "Գումար"]


def test_a_number_inside_a_sentence_is_not_a_page_number() -> None:
    """Only a line that is *only* a number counts."""
    assert "10" in strip_page_numbers("Ժամկետը 10 ամիս")


def test_repeated_headers_and_footers_are_removed() -> None:
    """A line at the edge of most pages is furniture, not content."""
    pages = [f"«ԱԿԲԱ ԲԱՆԿ» ՍԱԿԱԳՆԵՐ\nԲաժին {n}\nբովանդակություն\nէջատակ" for n in range(1, 5)]
    cleaned = strip_repeated_furniture(pages)
    assert all("ԱԿԲԱ ԲԱՆԿ» ՍԱԿԱԳՆԵՐ" not in page for page in cleaned)
    assert all("էջատակ" not in page for page in cleaned)
    assert all("բովանդակություն" in page for page in cleaned)


def test_furniture_removal_needs_enough_pages() -> None:
    """In a two-page document a repeated line is as likely to be a heading."""
    pages = ["Տոկոսադրույք\nմարմին", "Տոկոսադրույք\nմարմին"]
    assert strip_repeated_furniture(pages) == pages


def test_wrapped_lines_are_joined_and_list_items_are_not() -> None:
    """PDF line wraps are noise; list structure is meaning."""
    text = "Անվանական տոկոսադրույքը սահմանվում է\nպայմանագրով։\n• առաջին\n• երկրորդ"
    joined = join_broken_lines(text)
    assert "սահմանվում է պայմանագրով։" in joined
    assert joined.count("•") == 2
    assert "• առաջին • երկրորդ" not in joined


def test_hyphenated_words_are_rejoined_without_a_space() -> None:
    """A trailing hyphen means the word continues."""
    assert "տոկոսադրույք" in join_broken_lines("տոկոսա-\nդրույք")


def test_long_duplicate_paragraphs_go_and_short_repeats_stay() -> None:
    """«0%» repeats legitimately in a tariff table; boilerplate does not."""
    boilerplate = (
        "Ամփոփագրում նշված պայմանները կարող են փոփոխվել բանկի կողմից միակողմանի "
        "կարգով, որի մասին հաճախորդը տեղեկացվում է օրենսդրությամբ սահմանված կարգով։"
    )
    text = f"{boilerplate}\n\n0%\n\nՍպասարկման վճար\n\n0%\n\n{boilerplate}"
    cleaned = dedupe_long_paragraphs(text)
    assert cleaned.count(boilerplate) == 1
    assert cleaned.count("0%") == 2


def test_table_cells_are_spared_the_prose_rules() -> None:
    """Joining lines or dropping repeats inside a table destroys its structure."""
    assert clean_cell("  0%  ") == "0%"
    assert clean_cell("չի\nգանձվում") == "չի գանձվում"
    assert clean_cell(" 10 000 000") == "• 10 000 000"


def test_scoring_stage_keeps_line_structure_and_finishing_joins_it() -> None:
    """Scoring must see the defect; joining must happen only afterwards.

    One word per line is how the real mortgage summary extracts. Joining before
    scoring made that text look like prose and it scored a perfect 1.00.
    """
    one_word_per_line = "Անշարժ\nգույքի\nձեռքբերման\nհիփոթեքային\nվարկ"
    prepared = prepare_for_scoring(one_word_per_line)
    assert prepared.count("\n") == 4
    assert finish_page_text(prepared).count("\n") == 0
