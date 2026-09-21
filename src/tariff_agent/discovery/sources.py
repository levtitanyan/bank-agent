"""Finding the official pages and documents that state a product's tariffs.

Ranking, not guessing: every candidate carries the reasons it scored what it
did, so a reviewer can see why one PDF was preferred over another.

Two properties of the real site shaped this module:

* **The authoritative source is not always a PDF.** The consumer-loan page links
  no «ամփոփագիր» at all and states its rates in the page itself, while the
  mortgage page links a proper information summary. So discovery returns a
  *primary* source plus *supporting* ones rather than a single winner.
* **Some documents are shared.** ``loans-tariffs.pdf`` covers every loan
  product, so it is not required to match the product's own slug.

Bounded throughout: a fixed number of pages, links per page and candidates. The
crawl cannot expand just because the site is large.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from tariff_agent.config import DiscoveryConfig, Product
from tariff_agent.errors import FetchError, SourceNotFoundError
from tariff_agent.http.client import ContentKind, SafeHttpClient
from tariff_agent.http.url_policy import filter_allowed, normalize_url
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

_RATE_MARKERS = ("տոկոսադրույք", "տարեկան փաստացի", "interest rate")
"""Text that indicates a page actually states rates rather than describing a product."""

_MIN_RATE_MENTIONS = 3
"""How often a rate marker must appear before a page counts as stating rates."""


class SourceKind(StrEnum):
    """What sort of source a candidate is."""

    PDF = "pdf"
    HTML = "html"


class SourceRole(StrEnum):
    """How a candidate is meant to be used."""

    PRIMARY = "primary"
    """The authoritative source for this product's tariffs."""

    SUPPORTING = "supporting"
    """Useful corroboration, e.g. the bank-wide tariff list."""


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """One discovered page or document, with the evidence for its ranking.

    Attributes:
        url: Normalized, allow-listed URL.
        kind: PDF or HTML.
        title: Anchor text, or the page title for a page discovered directly.
        score: Total of all weights applied.
        reasons: Human-readable explanation of each contribution, for the log
            and for a reviewer choosing between two candidates.
        role: Whether the winning signal suggests an authoritative document.
        is_seed: True when this came from configured fallback seeds rather than
            live discovery - a reviewer must be able to tell the difference.
        discovered_from: The page this link was found on, when applicable.
    """

    url: str
    kind: SourceKind
    title: str
    score: float
    reasons: tuple[str, ...]
    role: SourceRole
    is_seed: bool = False
    discovered_from: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """The outcome of discovering sources for one product.

    Attributes:
        product_id: The product these sources belong to.
        primary: The source Phase 4 should extract from.
        supporting: Additional sources worth retrieving, best first.
        requires_review: True when two primary candidates are too close to
            separate automatically - the assignment's "two plausible official
            PDFs" case.
        notes: Anything a reviewer should know, e.g. that seeds were used.
        pages_fetched: How many pages the crawl actually retrieved.
    """

    product_id: str
    primary: SourceCandidate
    supporting: tuple[SourceCandidate, ...] = ()
    requires_review: bool = False
    notes: tuple[str, ...] = ()
    pages_fetched: int = 0

    @property
    def all_sources(self) -> tuple[SourceCandidate, ...]:
        """Primary first, then supporting sources."""
        return (self.primary, *self.supporting)


@dataclass
class _Scored:
    """Mutable accumulator used while scoring one candidate."""

    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    role: SourceRole = SourceRole.SUPPORTING

    def add(self, points: float, reason: str, role: SourceRole | None = None) -> None:
        """Apply one weighted signal.

        Args:
            points: Weight to add (may be negative).
            reason: Why, in words a reviewer can read.
            role: Upgrades the candidate's role when the signal implies it.
        """
        self.score += points
        self.reasons.append(f"{reason} ({points:+.0f})")
        if role is SourceRole.PRIMARY:
            self.role = SourceRole.PRIMARY


def _fold(text: str) -> str:
    """Normalize text for keyword comparison.

    Args:
        text: Raw anchor text, URL or title.

    Returns:
        NFC-normalized, case-folded text.
    """
    return unicodedata.normalize("NFC", text).casefold()


def _slug_tokens(url: str) -> str:
    """Turn a URL path into space-separated words for fuzzy matching.

    Args:
        url: Absolute URL.

    Returns:
        The last two path segments, hyphens and underscores split into words.
    """
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    tail = " ".join(segments[-2:]) if segments else ""
    return tail.replace("-", " ").replace("_", " ").replace("%20", " ").casefold()


def _matches_product(url: str, product: Product) -> float:
    """Score how well a URL's slug matches the product's names.

    Args:
        url: Absolute URL.
        product: The product being discovered.

    Returns:
        0-100 similarity against the closest product name.
    """
    slug = _slug_tokens(url)
    if not slug:
        return 0.0
    return max(float(fuzz.token_set_ratio(slug, _fold(name))) for name in product.all_names)


def _is_shared_document(url: str, anchor: str, config: DiscoveryConfig) -> bool:
    """Report whether a document legitimately covers several products.

    Args:
        url: Absolute URL.
        anchor: Anchor text.
        config: Discovery configuration.

    Returns:
        True when the URL or anchor names a shared document, which is therefore
        exempt from the product-slug requirement.
    """
    haystack = f"{_fold(url)} {_fold(anchor)}"
    return any(_fold(term) in haystack for term in config.scoring.shared_document_keywords)


def score_candidate(
    url: str,
    anchor: str,
    kind: SourceKind,
    product: Product,
    config: DiscoveryConfig,
    *,
    rate_mentions: int = 0,
    is_curated_seed: bool = False,
) -> tuple[float, tuple[str, ...], SourceRole]:
    """Score one discovered URL against the scoring policy.

    Args:
        url: Normalized, allow-listed URL.
        anchor: Anchor text of the link, or the page title.
        kind: PDF or HTML.
        product: The product being discovered.
        config: Weights and thresholds.
        rate_mentions: How often the page states a rate, for HTML candidates.
        is_curated_seed: Whether this URL is listed in ``products.yaml``.

    Returns:
        The total score, the reasons behind it, and the implied role.
    """
    scoring = config.scoring
    folded_url, folded_anchor = _fold(url), _fold(anchor)
    scored = _Scored()

    for keyword in scoring.anchor_keywords:
        if _fold(keyword.text) in folded_anchor:
            role = SourceRole.PRIMARY if keyword.role == "primary" else SourceRole.SUPPORTING
            scored.add(keyword.weight, f"anchor mentions {keyword.text!r}", role)
            break

    for keyword in scoring.url_keywords:
        if _fold(keyword.text) in folded_url:
            role = SourceRole.PRIMARY if keyword.role == "primary" else SourceRole.SUPPORTING
            scored.add(keyword.weight, f"url contains {keyword.text!r}", role)
            break

    for keyword in scoring.negative_keywords:
        if _fold(keyword.text) in folded_url or _fold(keyword.text) in folded_anchor:
            scored.add(keyword.weight, f"mentions {keyword.text!r}")

    for segment, weight in scoring.path_segments.items():
        if segment in folded_url:
            scored.add(weight, f"path contains {segment!r}")

    slug_score = _matches_product(url, product)
    if slug_score >= 80:
        scored.add(scoring.product_slug_match, f"url slug matches the product ({slug_score:.0f})")
    elif _is_shared_document(url, anchor, config):
        scored.add(
            scoring.product_slug_match / 2,
            "shared tariff document, so no product slug is required",
        )

    if is_curated_seed:
        scored.add(scoring.seed_page_bonus, "listed as a curated product page")

    if kind is SourceKind.PDF:
        scored.add(scoring.pdf_bonus, "is a PDF document")
    elif rate_mentions >= _MIN_RATE_MENTIONS:
        scored.add(
            scoring.rate_mention_bonus,
            f"page states rates ({rate_mentions} mentions)",
            SourceRole.PRIMARY,
        )

    return scored.score, tuple(scored.reasons), scored.role


def _kind_of(url: str) -> SourceKind:
    """Classify a URL as a PDF or an HTML page.

    Args:
        url: Absolute URL.

    Returns:
        The source kind implied by the path.
    """
    return SourceKind.PDF if urlsplit(url).path.lower().endswith(".pdf") else SourceKind.HTML


def _count_rate_mentions(text: str) -> int:
    """Count how often a page states a rate.

    Args:
        text: The page's text.

    Returns:
        Total occurrences of the rate markers.
    """
    folded = _fold(text)
    return sum(folded.count(marker) for marker in _RATE_MARKERS)


def select_candidate_pages(
    sitemap_urls: list[str], product: Product, config: DiscoveryConfig
) -> list[str]:
    """Choose which pages are worth fetching for this product.

    Args:
        sitemap_urls: All allow-listed URLs from the sitemap.
        product: The product being discovered.
        config: Weights and limits.

    Returns:
        Page URLs, best first, capped at ``limits.max_pages``. Seed pages are
        always included, so discovery still works when the sitemap is missing.
    """
    scored: list[tuple[float, str]] = []
    for url in sitemap_urls:
        if _kind_of(url) is SourceKind.PDF:
            continue
        slug_score = _matches_product(url, product)
        if slug_score < 70:
            continue
        penalty = sum(
            weight for segment, weight in config.scoring.path_segments.items() if segment in url
        )
        negative = sum(
            keyword.weight
            for keyword in config.scoring.negative_keywords
            if _fold(keyword.text) in _fold(url)
        )
        scored.append((slug_score + penalty + negative, url))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    seeds = [normalize_url(seed) for seed in product.seed_pages]
    # Seeds are curated, so they keep guaranteed slots: a site with many
    # loosely-matching pages must not push the known-good page out of budget.
    budget = max(0, config.limits.max_pages - len(seeds))
    ordered = [url for score, url in scored if score >= config.scoring.page_min_score][:budget]
    for seed in seeds:
        if seed not in ordered:
            ordered.append(seed)
    return ordered[: config.limits.max_pages]


def _harvest_page(
    client: SafeHttpClient, page_url: str, product: Product, config: DiscoveryConfig
) -> list[SourceCandidate]:
    """Fetch one page and score it and the documents it links.

    Args:
        client: The guarded HTTP client.
        page_url: Page to fetch.
        product: The product being discovered.
        config: Weights and limits.

    Returns:
        Candidates found on this page. Empty when the page could not be
        fetched - one broken page must not end the crawl.
    """
    try:
        html = client.fetch_text(page_url, expect=ContentKind.HTML)
    except FetchError as exc:
        logger.warning(
            "discovery_page_unavailable",
            extra={"url": page_url, "error_type": type(exc).__name__},
        )
        return []

    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else page_url
    candidates: list[SourceCandidate] = []
    curated = {normalize_url(seed) for seed in product.seed_pages}

    score, reasons, role = score_candidate(
        page_url,
        title,
        SourceKind.HTML,
        product,
        config,
        rate_mentions=_count_rate_mentions(soup.get_text(" ", strip=True)),
        is_curated_seed=page_url in curated,
    )
    candidates.append(
        SourceCandidate(
            url=page_url,
            kind=SourceKind.HTML,
            title=title,
            score=score,
            reasons=reasons,
            role=role,
        )
    )

    anchors = soup.find_all("a", href=True)[: config.limits.max_links_per_page]
    texts: dict[str, str] = {}
    for anchor in anchors:
        href = str(anchor["href"])
        if not href.lower().split("?")[0].endswith(".pdf"):
            continue
        allowed = filter_allowed([href], client.allowlist, base=page_url)
        if not allowed:
            logger.debug("discovery_link_rejected", extra={"href": href, "page": page_url})
            continue
        texts.setdefault(allowed[0], anchor.get_text(" ", strip=True))

    for url, anchor_text in texts.items():
        score, reasons, role = score_candidate(
            url, anchor_text, SourceKind.PDF, product, config
        )
        candidates.append(
            SourceCandidate(
                url=url,
                kind=SourceKind.PDF,
                title=anchor_text or url.rsplit("/", 1)[-1],
                score=score,
                reasons=reasons,
                role=role,
                discovered_from=page_url,
            )
        )
    return candidates


def discover_product_sources(
    client: SafeHttpClient,
    product: Product,
    config: DiscoveryConfig,
    sitemap_urls: list[str] | None = None,
) -> DiscoveryResult:
    """Find the pages and documents that state this product's tariffs.

    Args:
        client: The guarded HTTP client.
        product: The resolved product.
        config: Weights and limits.
        sitemap_urls: Pre-fetched sitemap URLs; when omitted, only seed pages
            are crawled.

    Returns:
        A primary source plus supporting ones, with the reasons for the ranking.

    Raises:
        SourceNotFoundError: When nothing scored above the floor, even after
            falling back to configured seeds.
    """
    pages = select_candidate_pages(sitemap_urls or [], product, config)
    notes: list[str] = []
    candidates: list[SourceCandidate] = []
    fetched = 0

    for page_url in pages:
        found = _harvest_page(client, page_url, product, config)
        if found:
            fetched += 1
        candidates.extend(found)

    best_by_url: dict[str, SourceCandidate] = {}
    for candidate in candidates:
        current = best_by_url.get(candidate.url)
        if current is None or candidate.score > current.score:
            best_by_url[candidate.url] = candidate

    kept = [c for c in best_by_url.values() if c.score >= config.scoring.min_score]
    if not kept:
        kept = _seed_candidates(product, config)
        if kept:
            notes.append(
                "live discovery found nothing above the score floor; "
                "using configured seed documents, which may be out of date"
            )

    if not kept:
        raise SourceNotFoundError(
            f"no official source found for {product.id} on the allow-listed domains"
        )

    # Role before score: a shared tariff list can outscore a product page on
    # keywords, but it is not that product's authoritative source. Only a
    # candidate carrying a primary signal - an information summary, or a page
    # that states the rates itself - may lead.
    kept.sort(
        key=lambda c: (c.role is SourceRole.PRIMARY, c.score, c.kind is SourceKind.PDF),
        reverse=True,
    )
    kept = kept[: config.limits.max_candidates]
    for candidate in kept:
        logger.info(
            "source_candidate",
            extra={
                "product_id": product.id,
                "url": candidate.url,
                "kind": candidate.kind.value,
                "role": candidate.role.value,
                "score": round(candidate.score, 1),
                "is_seed": candidate.is_seed,
                "reasons": list(candidate.reasons),
            },
        )

    primary, *rest = kept
    # Role decides who leads; score decides how useful the rest are. Sorting the
    # supporting list by role too would bury the shared tariff PDF beneath every
    # sibling product page, and that PDF is what corroborates the page's rates.
    rest.sort(key=lambda c: (c.score, c.kind is SourceKind.PDF), reverse=True)
    requires_review, review_note = _needs_review(primary, rest, config)
    if review_note:
        notes.append(review_note)

    return DiscoveryResult(
        product_id=product.id,
        primary=primary,
        supporting=tuple(rest[: config.limits.max_supporting]),
        requires_review=requires_review,
        notes=tuple(notes),
        pages_fetched=fetched,
    )


def _needs_review(
    primary: SourceCandidate, rest: list[SourceCandidate], config: DiscoveryConfig
) -> tuple[bool, str | None]:
    """Decide whether a human must choose between two close candidates.

    Args:
        primary: The highest-scoring candidate.
        rest: The remaining candidates, best first.
        config: Provides the ambiguity band.

    Returns:
        Whether review is required, and a note explaining why.
    """
    # Restricted to PDFs: sibling product pages routinely score alike and are
    # not a real dilemma, whereas two official information summaries are exactly
    # the case a human must settle.
    rivals = [
        c
        for c in rest
        if c.role is SourceRole.PRIMARY
        and c.kind is SourceKind.PDF
        and primary.score - c.score <= config.limits.ambiguity_band
    ]
    if primary.role is not SourceRole.PRIMARY or primary.kind is not SourceKind.PDF or not rivals:
        return False, None
    rival = rivals[0]
    return True, (
        f"two plausible official documents scored within "
        f"{config.limits.ambiguity_band:.0f} points: "
        f"{primary.url} ({primary.score:.0f}) and {rival.url} ({rival.score:.0f})"
    )


def _seed_candidates(product: Product, config: DiscoveryConfig) -> list[SourceCandidate]:
    """Build candidates from the configured fallback seeds.

    Args:
        product: The product being discovered.
        config: Weights, used to score the seeds consistently.

    Returns:
        Seed candidates, flagged so a reviewer knows discovery did not find them.
    """
    seeds: list[SourceCandidate] = []
    for url in (*product.seed_documents, *product.seed_pages):
        normalized = normalize_url(url)
        kind = _kind_of(normalized)
        score, reasons, role = score_candidate(normalized, "", kind, product, config)
        seeds.append(
            SourceCandidate(
                url=normalized,
                kind=kind,
                title=normalized.rsplit("/", 1)[-1],
                score=score,
                reasons=(*reasons, "configured seed fallback (+0)"),
                role=role,
                is_seed=True,
            )
        )
    return seeds
