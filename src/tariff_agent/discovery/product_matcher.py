"""Resolving a free-form product name to one of the products we monitor.

The approach is hybrid, in two deterministic layers:

* a **curated multilingual synonym dictionary** in ``config/products.yaml``
  carries the semantics - «հիփոթեք», "mortgage" and "ипотека" are the same
  product, and no amount of string similarity would discover that;
* **fuzzy matching** (rapidfuzz ``token_set_ratio``) absorbs the typos, word
  order and extra words that real queries carry.

Gemini is deliberately not involved. Product resolution is the one decision in
the pipeline that must be explainable and reproducible offline, and a wrong
resolution silently reports the wrong product's tariffs.

The third layer is a **penalty for terms we do not monitor**. Pure similarity
resolves "business mortgage" to our retail mortgage with a perfect score,
because the phrase literally contains it; the penalty pushes such a query out of
the resolved band instead.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz

from tariff_agent.config import Product, ProductCatalog
from tariff_agent.models import Language
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

MAX_QUERY_LENGTH = 200
"""Queries longer than this are truncated: they are noise, not product names."""

RESOLVE_THRESHOLD = 85.0
"""At or above this score, with a clear lead, the product is resolved."""

CANDIDATE_THRESHOLD = 60.0
"""Below this score nothing plausible was found."""

AMBIGUITY_LEAD = 10.0
"""The winner must beat the runner-up by this much, or a human decides."""

UNSUPPORTED_PENALTY = 45.0
"""Deducted when the query names a product family we do not monitor."""

_PUNCTUATION = re.compile(r"[«»„“”\"'`՝․,.!?;:()\[\]{}/\\|_—–-]+")
_WHITESPACE = re.compile(r"\s+")


class ResolutionStatus(StrEnum):
    """Outcome of resolving a product query."""

    RESOLVED = "resolved"
    """One product matched clearly enough to act on."""

    AMBIGUOUS = "ambiguous"
    """Several products are plausible, or the best match is weak. A human picks."""

    NOT_FOUND = "not_found"
    """Nothing we monitor resembles the query."""


@dataclass(frozen=True, slots=True)
class ProductMatch:
    """One product's best match against the query.

    Attributes:
        product_id: The product's registry id.
        score: 0-100 similarity after penalties.
        matched_name: The specific name or synonym that produced the score,
            so a reviewer can see *why* it matched.
        language: Which language list that name came from.
    """

    product_id: str
    score: float
    matched_name: str
    language: Language


@dataclass(frozen=True, slots=True)
class ProductResolution:
    """The result of resolving a query, including why it turned out that way.

    Returned rather than raised: the ambiguous case carries the data a reviewer
    needs, and Phase 8 tools must hand the model a status, never an exception.

    Attributes:
        status: See :class:`ResolutionStatus`.
        query: The original query, as typed.
        best: The winning match, when there is one.
        alternatives: Runner-up matches, best first - what the reviewer chooses
            between when the status is ambiguous.
        reason: One human-readable sentence explaining the outcome.
    """

    status: ResolutionStatus
    query: str
    best: ProductMatch | None
    alternatives: tuple[ProductMatch, ...]
    reason: str

    @property
    def product_id(self) -> str | None:
        """The resolved product id, or None when unresolved."""
        if self.best is None or self.status is not ResolutionStatus.RESOLVED:
            return None
        return self.best.product_id


def normalize_query(raw: str) -> str:
    """Reduce a query to the form used for comparison.

    Applies NFC so that composed and decomposed Armenian compare equal, folds
    case, strips the punctuation that surrounds product names in practice
    («», quotes, hyphens) and collapses whitespace. Over-long input is truncated
    rather than rejected, since it is usually a pasted sentence.

    Args:
        raw: The query as typed.

    Returns:
        The normalized query.
    """
    text = unicodedata.normalize("NFC", raw)[:MAX_QUERY_LENGTH]
    text = _PUNCTUATION.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip().casefold()


def _named_variants(product: Product) -> list[tuple[str, Language]]:
    """List every name a product may be referred to by, with its language.

    Args:
        product: The configured product.

    Returns:
        ``(name, language)`` pairs, official names first.
    """
    variants: list[tuple[str, Language]] = [
        (product.name_hy, Language.HY),
        (product.name_en, Language.EN),
    ]
    for code, names in product.synonyms.items():
        try:
            language = Language(code)
        except ValueError:
            logger.warning("unknown_synonym_language", extra={"language": code})
            continue
        variants.extend((name, language) for name in names)
    return variants


def _best_match(query: str, product: Product) -> ProductMatch:
    """Score a query against one product's names.

    Args:
        query: The normalized query.
        product: The product to score.

    Returns:
        The product's best match. ``token_set_ratio`` is used because real
        queries carry extra words ("consumer loan tariffs") and free word order
        («վարկ սպառողական»), both of which it ignores.
    """
    best_score = 0.0
    best_name = product.name_en
    best_language = Language.EN
    for name, language in _named_variants(product):
        score = float(fuzz.token_set_ratio(query, normalize_query(name)))
        if score > best_score:
            best_score, best_name, best_language = score, name, language
    return ProductMatch(
        product_id=product.id,
        score=best_score,
        matched_name=best_name,
        language=best_language,
    )


def _unsupported_terms_in(query: str, catalog: ProductCatalog) -> list[str]:
    """Find terms marking the query as a product family we do not monitor.

    Args:
        query: The normalized query.
        catalog: The product catalog carrying the term list.

    Returns:
        The matching terms, normalized.
    """
    tokens = set(query.split())
    return [term for term in catalog.unsupported_terms if normalize_query(term) in tokens]


def resolve_product(query: str, catalog: ProductCatalog) -> ProductResolution:
    """Decide which monitored product a free-form query refers to.

    Args:
        query: Product name as the user typed it, in any supported language.
        catalog: The configured products.

    Returns:
        A :class:`ProductResolution`. Callers decide what an ambiguous or
        missing result means: the deterministic pipeline raises, the agent
        escalates to a reviewer.
    """
    normalized = normalize_query(query)
    if not normalized:
        return ProductResolution(
            status=ResolutionStatus.NOT_FOUND,
            query=query,
            best=None,
            alternatives=(),
            reason="the query is empty",
        )

    penalty_terms = _unsupported_terms_in(normalized, catalog)
    penalty = UNSUPPORTED_PENALTY if penalty_terms else 0.0

    matches = sorted(
        (
            ProductMatch(
                product_id=match.product_id,
                score=max(0.0, match.score - penalty),
                matched_name=match.matched_name,
                language=match.language,
            )
            for match in (_best_match(normalized, product) for product in catalog.products)
        ),
        key=lambda match: match.score,
        reverse=True,
    )
    best = matches[0]
    runner_up = matches[1] if len(matches) > 1 else None
    lead = best.score - runner_up.score if runner_up else best.score

    resolution = _classify(query, best, matches, lead, penalty_terms)
    logger.info(
        "product_resolved",
        extra={
            "query": query,
            "status": resolution.status.value,
            "product_id": best.product_id,
            "score": round(best.score, 1),
            "lead": round(lead, 1),
            "matched_name": best.matched_name,
            "penalty_terms": penalty_terms,
        },
    )
    return resolution


def _classify(
    query: str,
    best: ProductMatch,
    matches: list[ProductMatch],
    lead: float,
    penalty_terms: list[str],
) -> ProductResolution:
    """Turn scores into a status, with a reason a human can read.

    Args:
        query: The original query.
        best: The highest-scoring match.
        matches: All matches, best first.
        lead: How far ahead the best match is.
        penalty_terms: Unsupported terms found in the query, if any.

    Returns:
        The classified resolution.
    """
    alternatives = tuple(matches[1:])
    if best.score < CANDIDATE_THRESHOLD:
        detail = (
            f" the query mentions {penalty_terms}, which we do not monitor;"
            if penalty_terms
            else ""
        )
        return ProductResolution(
            status=ResolutionStatus.NOT_FOUND,
            query=query,
            best=None,
            alternatives=alternatives,
            reason=(
                f"no monitored product resembles {query!r} "
                f"(best was {best.product_id} at {best.score:.0f}/100);{detail}"
            ).rstrip(";"),
        )
    if best.score >= RESOLVE_THRESHOLD and lead >= AMBIGUITY_LEAD:
        return ProductResolution(
            status=ResolutionStatus.RESOLVED,
            query=query,
            best=best,
            alternatives=alternatives,
            reason=(
                f"matched {best.product_id} at {best.score:.0f}/100 "
                f"via {best.matched_name!r} ({best.language.value})"
            ),
        )
    return ProductResolution(
        status=ResolutionStatus.AMBIGUOUS,
        query=query,
        best=best,
        alternatives=alternatives,
        reason=(
            f"{query!r} is not decisive: {best.product_id} scored {best.score:.0f} "
            f"and the next candidate {lead:.0f} behind; a reviewer should choose"
        ),
    )
