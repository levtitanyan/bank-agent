"""Reading the bank's sitemap to learn which pages exist.

Parsed with :mod:`defusedxml` rather than plain lxml. A sitemap is attacker-
influenced input in the general case, and stock XML parsers expand entities:
a "billion laughs" document is a few hundred bytes on the wire and gigabytes in
memory. defusedxml refuses entity expansion outright.

The sitemap is one of three discovery routes, so every failure here degrades to
an empty list rather than stopping the run: a missing sitemap must not prevent
discovery from using seed pages.
"""

from __future__ import annotations

from dataclasses import dataclass

from defusedxml import ElementTree as DefusedET

from tariff_agent.config import Allowlist
from tariff_agent.errors import FetchError
from tariff_agent.http.client import SafeHttpClient
from tariff_agent.http.url_policy import filter_allowed
from tariff_agent.observability.logging import get_logger

logger = get_logger(__name__)

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
MAX_URLS = 5000
"""Upper bound on URLs taken from a sitemap, so a huge file cannot flood a run."""

MAX_CHILD_SITEMAPS = 10
"""Child sitemaps fetched from an index. Bounds the work a hostile index can cause."""


@dataclass(frozen=True, slots=True)
class SitemapUrls:
    """URLs read from one sitemap document.

    Attributes:
        urls: The ``<loc>`` values, normalized and allow-listed.
        is_index: True when the document was a ``<sitemapindex>``, meaning the
            URLs are *other sitemaps* rather than pages. Callers must not treat
            them as pages - which is the bug this type exists to prevent.
    """

    urls: tuple[str, ...]
    is_index: bool


def parse_sitemap(xml: bytes, allowlist: Allowlist, *, max_urls: int = MAX_URLS) -> SitemapUrls:
    """Extract allow-listed URLs from one sitemap document.

    The result records whether the document was an index, because the two kinds
    look identical otherwise - both are lists of ``<loc>`` elements - and
    treating an index as a list of pages would send the crawler to fetch
    sitemaps as if they were product pages.

    Args:
        xml: Raw sitemap bytes.
        allowlist: Policy applied to every extracted URL.
        max_urls: Cap on the number of URLs returned.

    Returns:
        The URLs and whether they are child sitemaps. Empty when the document
        cannot be parsed - ACBA's real sitemap contains a malformed entry, and
        one bad line must not discard the other 587.
    """
    try:
        root = DefusedET.fromstring(xml)
    except Exception as exc:  # defusedxml raises several unrelated types
        logger.warning("sitemap_unparseable", extra={"error_type": type(exc).__name__})
        return SitemapUrls(urls=(), is_index=False)

    is_index = root.tag.endswith("sitemapindex")
    raw = [
        element.text.strip()
        for element in root.iter(f"{_SITEMAP_NS}loc")
        if element.text and element.text.strip()
    ]
    urls = filter_allowed(raw[:max_urls], allowlist)
    logger.info(
        "sitemap_parsed",
        extra={
            "kind": "sitemapindex" if is_index else "urlset",
            "entries": len(raw),
            "allowed": len(urls),
        },
    )
    return SitemapUrls(urls=tuple(urls), is_index=is_index)


def parse_sitemap_urls(xml: bytes, allowlist: Allowlist, *, max_urls: int = MAX_URLS) -> list[str]:
    """Extract page URLs from a plain ``<urlset>`` document.

    Args:
        xml: Raw sitemap bytes.
        allowlist: Policy applied to every extracted URL.
        max_urls: Cap on the number of URLs returned.

    Returns:
        Page URLs, or an empty list for an index document - whose entries are
        sitemaps, not pages. Use :func:`fetch_sitemap_urls` to follow an index.
    """
    parsed = parse_sitemap(xml, allowlist, max_urls=max_urls)
    return [] if parsed.is_index else list(parsed.urls)


def _fetch_sitemap(
    client: SafeHttpClient, sitemap_url: str, allowlist: Allowlist, max_urls: int
) -> SitemapUrls:
    """Fetch and parse one sitemap document.

    Args:
        client: The guarded HTTP client.
        sitemap_url: Absolute URL of the sitemap.
        allowlist: Policy applied to every extracted URL.
        max_urls: Cap on the number of URLs returned.

    Returns:
        The parsed result, empty when the document could not be fetched.
    """
    try:
        result = client.fetch(sitemap_url)
    except FetchError as exc:
        logger.warning(
            "sitemap_unavailable",
            extra={"url": sitemap_url, "error_type": type(exc).__name__},
        )
        return SitemapUrls(urls=(), is_index=False)
    return parse_sitemap(result.content, allowlist, max_urls=max_urls)


def fetch_sitemap_urls(
    client: SafeHttpClient,
    sitemap_url: str,
    allowlist: Allowlist,
    *,
    max_urls: int = MAX_URLS,
    max_children: int = MAX_CHILD_SITEMAPS,
) -> list[str]:
    """Fetch a sitemap and return the page URLs it leads to.

    A ``<sitemapindex>`` is followed one level: each child sitemap is fetched
    through the same guarded client, and only page URLs are returned. Recursion
    stops there - a sitemap index nested inside a sitemap index is either a
    mistake or an attempt to make us crawl indefinitely.

    Args:
        client: The guarded HTTP client.
        sitemap_url: Absolute URL of the sitemap.
        allowlist: Policy applied to every extracted URL.
        max_urls: Cap on the number of page URLs returned.
        max_children: Cap on how many child sitemaps are fetched from an index.

    Returns:
        Page URLs, de-duplicated in first-seen order. Empty if the sitemap is
        missing or unusable - one lost discovery route, not a failed run.
    """
    root = _fetch_sitemap(client, sitemap_url, allowlist, max_urls)
    if not root.is_index:
        return list(root.urls)

    children = root.urls[:max_children]
    logger.info(
        "sitemap_index_followed",
        extra={
            "url": sitemap_url,
            "children": len(children),
            "skipped": len(root.urls) - len(children),
        },
    )
    pages: list[str] = []
    seen: set[str] = set()
    for child_url in children:
        child = _fetch_sitemap(client, child_url, allowlist, max_urls)
        if child.is_index:
            logger.warning("sitemap_index_nested_too_deep", extra={"url": child_url})
            continue
        for url in child.urls:
            if url not in seen:
                seen.add(url)
                pages.append(url)
            if len(pages) >= max_urls:
                return pages
    return pages
