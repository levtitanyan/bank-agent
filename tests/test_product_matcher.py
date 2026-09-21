"""Tests for resolving a free-form product name.

The cases that matter are the ones where similarity alone gives the wrong
answer: "business mortgage" literally contains "mortgage", and "loan" fits both
monitored products equally well.
"""

from __future__ import annotations

import pytest

from tariff_agent.config import load_products
from tariff_agent.discovery.product_matcher import (
    ResolutionStatus,
    normalize_query,
    resolve_product,
)
from tariff_agent.models import Language

CATALOG = load_products()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("consumer loan", "consumer_loan"),
        ("Consumer Loan", "consumer_loan"),
        ("սպառողական վարկ", "consumer_loan"),
        ("«սպառողական վարկ»", "consumer_loan"),
        ("потребительский кредит", "consumer_loan"),
        ("potrebkredit", "consumer_loan"),
        ("personal loan", "consumer_loan"),
        ("mortgage", "mortgage"),
        ("հիփոթեքային վարկ", "mortgage"),
        ("ипотека", "mortgage"),
        ("ipoteka", "mortgage"),
        ("home loan", "mortgage"),
        ("consumer loan tariffs 2026", "consumer_loan"),
        ("վարկ սպառողական", "consumer_loan"),
    ],
)
def test_resolves_across_languages_and_phrasings(query: str, expected: str) -> None:
    """Armenian, English and Russian: synonyms, transliterations, extra words."""
    resolution = resolve_product(query, CATALOG)
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.product_id == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [("consumr loan", "consumer_loan"), ("հիփոտեք", "mortgage")],
)
def test_tolerates_typos(query: str, expected: str) -> None:
    """Fuzzy matching exists for exactly this."""
    assert resolve_product(query, CATALOG).product_id == expected


def test_reports_which_synonym_matched_and_its_language() -> None:
    """A reviewer must be able to see why a query resolved as it did."""
    resolution = resolve_product("ипотека", CATALOG)
    assert resolution.best is not None
    assert resolution.best.language is Language.RU
    assert resolution.best.matched_name == "ипотека"
    assert "mortgage" in resolution.reason


def test_bare_loan_is_ambiguous_and_offers_both_products() -> None:
    """'loan' fits both monitored products; guessing would be wrong."""
    resolution = resolve_product("loan", CATALOG)
    assert resolution.status is ResolutionStatus.AMBIGUOUS
    assert resolution.product_id is None
    assert resolution.alternatives
    offered = {
        resolution.best.product_id if resolution.best else "",
        *(alternative.product_id for alternative in resolution.alternatives),
    }
    assert offered == {"consumer_loan", "mortgage"}


@pytest.mark.parametrize(
    "query",
    ["business mortgage", "բիզնես հիփոթեք", "бизнес ипотека", "business loan"],
)
def test_business_products_are_not_our_products(query: str) -> None:
    """ACBA really sells these; we do not monitor them, and must not pretend to.

    Pure similarity resolves "business mortgage" to the retail mortgage with a
    perfect score, because the phrase contains it.
    """
    resolution = resolve_product(query, CATALOG)
    assert resolution.status is ResolutionStatus.NOT_FOUND
    assert resolution.product_id is None


@pytest.mark.parametrize("query", ["credit card", "ավանդ", "deposit account", "pizza", "?????"])
def test_unmonitored_or_nonsense_queries_are_not_forced_to_a_product(query: str) -> None:
    """Never snap an unsupported product to the nearest monitored one."""
    assert resolve_product(query, CATALOG).status is ResolutionStatus.NOT_FOUND


def test_empty_query_is_reported_not_crashed() -> None:
    """Empty input is a user error, handled explicitly."""
    resolution = resolve_product("   ", CATALOG)
    assert resolution.status is ResolutionStatus.NOT_FOUND
    assert "empty" in resolution.reason


def test_long_input_is_truncated_not_rejected() -> None:
    """A pasted sentence should still resolve, not blow up the matcher."""
    resolution = resolve_product("mortgage " + "x" * 5000, CATALOG)
    assert resolution.status in (ResolutionStatus.RESOLVED, ResolutionStatus.AMBIGUOUS)


def test_normalization_makes_armenian_forms_compare_equal() -> None:
    """Composed and decomposed Armenian must not be different products."""
    import unicodedata

    composed = "սպառողական վարկ"
    decomposed = unicodedata.normalize("NFD", composed)
    assert normalize_query(composed) == normalize_query(decomposed)
    assert resolve_product(decomposed, CATALOG).product_id == "consumer_loan"


def test_resolution_is_deterministic() -> None:
    """The same query must always give the same answer - no model, no sampling."""
    results = {resolve_product("ипотека", CATALOG).product_id for _ in range(5)}
    assert results == {"mortgage"}
