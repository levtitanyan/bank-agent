"""Tests for sitemap parsing and source discovery.

The HTML fixtures are trimmed copies of real ACBA pages (their headers record
the source URL and fetch date), so the parser is tested against the markup the
bank actually serves rather than markup invented to satisfy it.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tariff_agent.config import (
    DiscoveryConfig,
    HttpSettings,
    load_allowlist,
    load_discovery_config,
    load_products,
)
from tariff_agent.discovery.sitemap import (
    fetch_sitemap_urls,
    parse_sitemap,
    parse_sitemap_urls,
)
from tariff_agent.discovery.sources import (
    SourceKind,
    SourceRole,
    discover_product_sources,
    score_candidate,
    select_candidate_pages,
)
from tariff_agent.errors import SourceNotFoundError
from tariff_agent.http.client import SafeHttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
ALLOWLIST = load_allowlist()
CATALOG = load_products()
CONFIG = load_discovery_config()
CONSUMER = CATALOG.get("consumer_loan")
MORTGAGE = CATALOG.get("mortgage")
assert CONSUMER is not None and MORTGAGE is not None

CONSUMER_CATEGORY_PAGE = "https://acba.am/hy/individual/loans/consumer-loans"
CONSUMER_PAGE = "https://acba.am/hy/individual/loan/consumer-loan--up-to-10mln"
CONSUMER_SUBPAGE = "https://acba.am/hy/individual/loan/online-consumer-loan"
MORTGAGE_PAGE = "https://acba.am/hy/individual/loan/purchase-mortgage"
RENOVATION_PAGE = "https://acba.am/hy/individual/loan/renovation-mortgage"
SUMMARY_PDF = "https://www.acba.am/files/loan%20info.pdf"
RENOVATION_SUMMARY_PDF = "https://www.acba.am/files/Mortgage%20Loans.pdf"
TARIFFS_PDF = "https://www.acba.am/files/loans-tariffs.pdf"
# ACBA serves the same tariff document from three URLs; discovery must accept
# whichever one the page it crawled happened to link.
TARIFFS_PDF_NAME = "loans-tariffs.pdf"


# --------------------------------------------------------------------------- #
# Sitemap
# --------------------------------------------------------------------------- #


def test_sitemap_keeps_only_allow_listed_urls() -> None:
    """The real sitemap links social media; those are not fetchable sources."""
    urls = parse_sitemap_urls((FIXTURES / "sitemap.xml").read_bytes(), ALLOWLIST)
    assert CONSUMER_PAGE in urls
    assert not any("facebook" in url for url in urls)


def test_sitemap_survives_the_malformed_entry_acba_really_has() -> None:
    """One broken <loc> must not discard the other 587 URLs."""
    urls = parse_sitemap_urls((FIXTURES / "sitemap.xml").read_bytes(), ALLOWLIST)
    assert len(urls) >= 8
    assert not any(url.startswith("(") for url in urls)


def test_unparseable_sitemap_degrades_to_empty() -> None:
    """A missing or broken sitemap is one route lost, not a failed run."""
    assert parse_sitemap_urls(b"<<not xml", ALLOWLIST) == []
    assert parse_sitemap_urls(b"", ALLOWLIST) == []


def test_sitemap_parser_refuses_entity_expansion() -> None:
    """An XML bomb is a few hundred bytes on the wire and gigabytes in memory."""
    bomb = b"""<?xml version="1.0"?>
    <!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>&lol2;</loc></url>
    </urlset>"""
    assert parse_sitemap_urls(bomb, ALLOWLIST) == []


def test_sitemap_respects_the_url_cap() -> None:
    """A huge sitemap must not flood one discovery run."""
    urls = parse_sitemap_urls((FIXTURES / "sitemap.xml").read_bytes(), ALLOWLIST, max_urls=3)
    assert len(urls) <= 3


# --------------------------------------------------------------------------- #
# Page selection
# --------------------------------------------------------------------------- #


def test_page_selection_excludes_other_audiences() -> None:
    """business-mortgage and agro loans live under different path segments."""
    urls = parse_sitemap_urls((FIXTURES / "sitemap.xml").read_bytes(), ALLOWLIST)
    pages = select_candidate_pages(urls, MORTGAGE, CONFIG)
    assert MORTGAGE_PAGE in pages
    assert not any("/business/" in page or "/agro/" in page for page in pages)


def test_page_selection_excludes_the_archive_and_calculators() -> None:
    """Archived tariffs and calculators look relevant but are not sources."""
    urls = parse_sitemap_urls((FIXTURES / "sitemap.xml").read_bytes(), ALLOWLIST)
    pages = select_candidate_pages(urls, MORTGAGE, CONFIG)
    assert not any("archive" in page or "calculator" in page for page in pages)


def test_seed_pages_keep_guaranteed_slots() -> None:
    """A site full of loosely matching pages must not evict the curated one."""
    noise = [f"https://acba.am/hy/individual/loan/consumer-loan-variant-{i}" for i in range(20)]
    pages = select_candidate_pages(noise, CONSUMER, CONFIG)
    assert CONSUMER_PAGE in pages
    assert len(pages) <= CONFIG.limits.max_pages


def test_page_selection_works_without_a_sitemap() -> None:
    """Seeds alone keep discovery running when the sitemap is unavailable.

    The pinned canonical page comes first, then the configured seeds.
    """
    pages = select_candidate_pages([], CONSUMER, CONFIG)
    assert pages[0] == CONSUMER_PAGE
    assert CONSUMER_CATEGORY_PAGE in pages
    assert "https://acba.am/hy/individual/loan/5G-loan" in pages


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def test_information_summary_outranks_the_general_tariff_list() -> None:
    """«տեղեկատվական ամփոփագիր» is the authoritative product document."""
    summary, _, summary_role = score_candidate(
        SUMMARY_PDF, "հիփոթեքային վարկի տեղեկատվական ամփոփագիր", SourceKind.PDF, MORTGAGE, CONFIG
    )
    tariffs, _, tariffs_role = score_candidate(
        TARIFFS_PDF, "Վարկային սակագներ", SourceKind.PDF, MORTGAGE, CONFIG
    )
    assert summary > tariffs
    assert summary_role is SourceRole.PRIMARY
    assert tariffs_role is SourceRole.SUPPORTING


def test_archived_editions_are_pushed_below_the_floor() -> None:
    """An archived tariff PDF is plausible-looking and wrong."""
    score, reasons, _ = score_candidate(
        "https://acba.am/hy/tariffs-archive/loans-tariffs-2019.pdf",
        "Վարկային սակագներ արխիվ",
        SourceKind.PDF,
        CONSUMER,
        CONFIG,
    )
    assert score < CONFIG.scoring.min_score
    assert any("արխիվ" in reason or "archive" in reason for reason in reasons)


def test_business_documents_are_penalized() -> None:
    """The retail products we monitor are not the business ones."""
    score, _, _ = score_candidate(
        "https://acba.am/hy/business/loan/business-mortgage",
        "Բիզնես հիփոթեք",
        SourceKind.HTML,
        MORTGAGE,
        CONFIG,
    )
    assert score < CONFIG.scoring.min_score


def test_shared_documents_need_no_product_slug() -> None:
    """loans-tariffs.pdf legitimately covers every loan product."""
    score, reasons, _ = score_candidate(
        TARIFFS_PDF, "Վարկային սակագներ", SourceKind.PDF, CONSUMER, CONFIG
    )
    assert score >= CONFIG.scoring.min_score
    assert any("shared" in reason for reason in reasons)


def test_a_page_that_states_rates_can_be_primary() -> None:
    """The consumer-loan page links no summary and holds the rates itself."""
    score, _, role = score_candidate(
        CONSUMER_PAGE, "Սպառողական վարկեր", SourceKind.HTML, CONSUMER, CONFIG, rate_mentions=30
    )
    assert role is SourceRole.PRIMARY
    assert score >= CONFIG.scoring.min_score


def test_every_score_carries_its_reasons() -> None:
    """A reviewer choosing between two documents needs the why, not just a number."""
    _, reasons, _ = score_candidate(
        SUMMARY_PDF, "տեղեկատվական ամփոփագիր", SourceKind.PDF, MORTGAGE, CONFIG
    )
    assert reasons
    assert all("(" in reason and ")" in reason for reason in reasons)


# --------------------------------------------------------------------------- #
# Discovery end to end, against the trimmed real pages
# --------------------------------------------------------------------------- #


def serve_fixtures(pages: dict[str, str]) -> tuple[SafeHttpClient, list[str]]:
    """Build a client that serves the fixture pages and records what was fetched."""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        fetched.append(url)
        for page_url, filename in pages.items():
            if url == page_url:
                return httpx.Response(
                    200,
                    content=(FIXTURES / filename).read_bytes(),
                    headers={"content-type": "text/html; charset=utf-8"},
                )
        return httpx.Response(404)

    settings = HttpSettings(respect_robots=False, max_attempts=1, cache_dir=Path("/tmp/no-cache"))
    client = SafeHttpClient(
        ALLOWLIST, settings, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    return client, fetched


def test_mortgage_primary_is_the_information_summary() -> None:
    """The real purchase-mortgage page links its ամփոփագիր among 9 PDFs."""
    client, _ = serve_fixtures({MORTGAGE_PAGE: "purchase_mortgage_page.html"})
    result = discover_product_sources(client, MORTGAGE, CONFIG, [MORTGAGE_PAGE])
    assert result.primary.url == SUMMARY_PDF
    assert result.primary.kind is SourceKind.PDF
    assert result.primary.role is SourceRole.PRIMARY


def test_consumer_primary_is_the_product_page_with_the_tariff_pdf_supporting() -> None:
    """The consumer product links no summary, so its own page is the source.

    This is the case a naive "PDF beats HTML" rule gets wrong: the shared
    tariff list scores higher on keywords but is not this product's source.
    """
    client, _ = serve_fixtures(
        {
            CONSUMER_PAGE: "consumer_loan_10mln_page.html",
            CONSUMER_CATEGORY_PAGE: "consumer_loans_page.html",
            CONSUMER_SUBPAGE: "online_consumer_loan_page.html",
        }
    )
    result = discover_product_sources(
        client, CONSUMER, CONFIG, [CONSUMER_CATEGORY_PAGE, CONSUMER_SUBPAGE, CONSUMER_PAGE]
    )
    assert result.primary.url == CONSUMER_PAGE
    assert result.primary.kind is SourceKind.HTML
    assert any(TARIFFS_PDF_NAME in candidate.url for candidate in result.supporting)


def test_category_page_does_not_outrank_the_product_page() -> None:
    """ACBA's consumer-loans page lists several products and states no EIR.

    Four headline percentages and no «տարեկան փաստացի» cannot yield one honest
    set of ten tariff fields, so the product's own page is pinned in
    products.yaml and must win.
    """
    client, _ = serve_fixtures(
        {
            CONSUMER_PAGE: "consumer_loan_10mln_page.html",
            CONSUMER_CATEGORY_PAGE: "consumer_loans_page.html",
        }
    )
    result = discover_product_sources(
        client, CONSUMER, CONFIG, [CONSUMER_CATEGORY_PAGE, CONSUMER_PAGE]
    )
    assert result.primary.url == CONSUMER_PAGE
    assert CONSUMER_CATEGORY_PAGE in [c.url for c in result.supporting]
    assert any("own page" in reason for reason in result.primary.reasons)


def test_the_canonical_page_is_always_crawled() -> None:
    """A pinned page must never be pushed out of the crawl budget."""
    noise = [f"https://acba.am/hy/individual/loan/consumer-loan-{i}" for i in range(30)]
    pages = select_candidate_pages(noise, CONSUMER, CONFIG)
    assert CONSUMER_PAGE in pages


def test_a_known_neighbouring_product_does_not_stop_every_run() -> None:
    """The renovation summary is official, and known to be the wrong product.

    Escalating to a human on every nightly run over a document we have already
    identified is not human-in-the-loop, it is an alarm nobody will read. The
    mortgage's exclude_terms name it, so it is scored down instead.
    """
    client, _ = serve_fixtures(
        {
            MORTGAGE_PAGE: "purchase_mortgage_page.html",
            RENOVATION_PAGE: "renovation_mortgage_page.html",
        }
    )
    result = discover_product_sources(client, MORTGAGE, CONFIG, [MORTGAGE_PAGE, RENOVATION_PAGE])
    assert result.primary.url == SUMMARY_PDF
    assert not result.requires_review


def test_two_genuinely_close_summaries_still_require_review() -> None:
    """The mechanism must survive: an unrecognised rival still stops the run.

    Modelled on the real pair before the renovation mortgage was named in
    exclude_terms - two «տեղեկատվական ամփոփագիր» documents within ten points,
    where choosing automatically is a coin flip on whose rates get reported.
    """
    unfiltered = MORTGAGE.model_copy(update={"exclude_terms": ()})
    client, _ = serve_fixtures(
        {
            MORTGAGE_PAGE: "purchase_mortgage_page.html",
            RENOVATION_PAGE: "renovation_mortgage_page.html",
        }
    )
    result = discover_product_sources(
        client, unfiltered, CONFIG, [MORTGAGE_PAGE, RENOVATION_PAGE]
    )
    assert result.requires_review
    assert result.primary.url == SUMMARY_PDF
    assert RENOVATION_SUMMARY_PDF in [c.url for c in result.supporting]
    assert any("two plausible official documents" in note for note in result.notes)


def test_a_single_clear_summary_does_not_require_review() -> None:
    """Review is for genuine dilemmas; one clear winner must not stop the run."""
    client, _ = serve_fixtures({MORTGAGE_PAGE: "purchase_mortgage_page.html"})
    result = discover_product_sources(client, MORTGAGE, CONFIG, [MORTGAGE_PAGE])
    assert not result.requires_review


def test_candidates_record_where_they_were_found() -> None:
    """Provenance: every discovered document names the page that linked it."""
    client, _ = serve_fixtures({MORTGAGE_PAGE: "purchase_mortgage_page.html"})
    result = discover_product_sources(client, MORTGAGE, CONFIG, [MORTGAGE_PAGE])
    assert result.primary.discovered_from == MORTGAGE_PAGE


def test_plain_http_links_on_a_real_page_are_dropped() -> None:
    """The real mortgage page links one PDF over http; https-only refuses it."""
    client, fetched = serve_fixtures({MORTGAGE_PAGE: "purchase_mortgage_page.html"})
    result = discover_product_sources(client, MORTGAGE, CONFIG, [MORTGAGE_PAGE])
    assert "tuyjer" not in " ".join(c.url for c in result.all_sources)
    assert not any(url.startswith("http://") for url in fetched)


def test_a_broken_page_does_not_end_the_crawl() -> None:
    """One 404 mid-crawl must not lose the sources found elsewhere."""
    client, _ = serve_fixtures({MORTGAGE_PAGE: "purchase_mortgage_page.html"})
    result = discover_product_sources(
        client, MORTGAGE, CONFIG, ["https://acba.am/hy/gone", MORTGAGE_PAGE]
    )
    assert result.primary.url == SUMMARY_PDF
    assert result.pages_fetched == 1


def test_crawl_budget_is_respected() -> None:
    """Discovery must not expand just because the site is large."""
    noise = [f"https://acba.am/hy/individual/loan/purchase-mortgage-{i}" for i in range(30)]
    client, fetched = serve_fixtures({MORTGAGE_PAGE: "purchase_mortgage_page.html"})
    discover_product_sources(client, MORTGAGE, CONFIG, [*noise, MORTGAGE_PAGE])
    assert len(fetched) <= CONFIG.limits.max_pages


def test_seeds_are_used_and_flagged_when_discovery_finds_nothing() -> None:
    """A reviewer must be able to tell live discovery from a configured fallback."""
    client, _ = serve_fixtures({})  # every page 404s
    result = discover_product_sources(client, MORTGAGE, CONFIG, [])
    assert result.primary.is_seed
    assert any("seed" in note for note in result.notes)


def test_no_sources_at_all_is_an_error_not_an_empty_result() -> None:
    """Missing data is reported; it is never quietly empty."""
    product = MORTGAGE.model_copy(update={"seed_pages": (), "seed_documents": ()})
    client, _ = serve_fixtures({})
    with pytest.raises(SourceNotFoundError, match="no official source"):
        discover_product_sources(client, product, CONFIG, [])


def test_scoring_config_is_policy_a_reviewer_can_change() -> None:
    """Raising the floor drops weak candidates without touching Python."""
    strict = DiscoveryConfig.model_validate(
        {
            "scoring": {**CONFIG.scoring.model_dump(), "min_score": 200},
            "limits": CONFIG.limits.model_dump(),
        }
    )
    client, _ = serve_fixtures({MORTGAGE_PAGE: "purchase_mortgage_page.html"})
    result = discover_product_sources(client, MORTGAGE, strict, [MORTGAGE_PAGE])
    assert result.primary.is_seed  # everything live was dropped, seeds remain


# --------------------------------------------------------------------------- #
# Sitemap index following
# --------------------------------------------------------------------------- #

INDEX_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://acba.am/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://acba.am/sitemap-broken.xml</loc></sitemap>
</sitemapindex>"""

CHILD_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://acba.am/hy/individual/loan/purchase-mortgage</loc></url>
  <url><loc>https://acba.am/hy/individual/loans/consumer-loans</loc></url>
</urlset>"""

NESTED_INDEX_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://acba.am/sitemap-deeper.xml</loc></sitemap>
</sitemapindex>"""


def sitemap_client(pages: dict[str, bytes]) -> tuple[SafeHttpClient, list[str]]:
    """Serve sitemap XML by URL and record what was fetched."""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        fetched.append(url)
        body = pages.get(url)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": "application/xml"})

    settings = HttpSettings(respect_robots=False, max_attempts=1, cache_dir=Path("/tmp/no-cache"))
    client = SafeHttpClient(
        ALLOWLIST, settings, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    return client, fetched


def test_parse_does_not_mistake_an_index_for_a_list_of_pages() -> None:
    """An index lists sitemaps; returning them as pages would crawl XML files."""
    assert parse_sitemap_urls(INDEX_XML, ALLOWLIST) == []
    assert parse_sitemap(INDEX_XML, ALLOWLIST).is_index is True
    assert parse_sitemap(CHILD_XML, ALLOWLIST).is_index is False


def test_an_index_is_followed_one_level_to_its_pages() -> None:
    """The page URLs come from the children, not from the index itself."""
    client, fetched = sitemap_client(
        {
            "https://acba.am/sitemap.xml": INDEX_XML,
            "https://acba.am/sitemap-pages.xml": CHILD_XML,
        }
    )
    urls = fetch_sitemap_urls(client, "https://acba.am/sitemap.xml", ALLOWLIST)
    assert MORTGAGE_PAGE in urls
    assert not any(url.endswith(".xml") for url in urls)
    assert "https://acba.am/sitemap-pages.xml" in fetched


def test_one_unreadable_child_sitemap_does_not_lose_the_others() -> None:
    """A 404 on one child is a lost route, not a failed discovery."""
    client, _ = sitemap_client(
        {
            "https://acba.am/sitemap.xml": INDEX_XML,
            "https://acba.am/sitemap-pages.xml": CHILD_XML,
            # sitemap-broken.xml is absent and answers 404
        }
    )
    urls = fetch_sitemap_urls(client, "https://acba.am/sitemap.xml", ALLOWLIST)
    assert MORTGAGE_PAGE in urls


def test_index_recursion_stops_at_one_level() -> None:
    """An index inside an index is a mistake or a trap; either way, stop."""
    client, fetched = sitemap_client(
        {
            "https://acba.am/sitemap.xml": INDEX_XML,
            "https://acba.am/sitemap-pages.xml": NESTED_INDEX_XML,
        }
    )
    assert fetch_sitemap_urls(client, "https://acba.am/sitemap.xml", ALLOWLIST) == []
    assert "https://acba.am/sitemap-deeper.xml" not in fetched


def test_the_number_of_child_sitemaps_is_capped() -> None:
    """A hostile index must not be able to order an unbounded crawl."""
    many = b"""<?xml version="1.0" encoding="utf-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">""" + b"".join(
        f"<sitemap><loc>https://acba.am/sitemap-{i}.xml</loc></sitemap>".encode()
        for i in range(50)
    ) + b"</sitemapindex>"
    client, fetched = sitemap_client({"https://acba.am/sitemap.xml": many})
    fetch_sitemap_urls(client, "https://acba.am/sitemap.xml", ALLOWLIST, max_children=3)
    assert len([url for url in fetched if url.startswith("https://acba.am/sitemap-")]) == 3
