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


def parse_sitemap_urls(xml: bytes, allowlist: Allowlist, *, max_urls: int = MAX_URLS) -> list[str]:
    """Extract allow-listed page URLs from sitemap XML.

    Handles both a plain ``<urlset>`` and a ``<sitemapindex>`` (one level deep,
    returning the child sitemap URLs for the caller to fetch).

    Args:
        xml: Raw sitemap bytes.
        allowlist: Policy applied to every extracted URL.
        max_urls: Cap on the number of URLs returned.

    Returns:
        Normalized, permitted URLs. Empty when the document cannot be parsed -
        ACBA's real sitemap contains a malformed entry, and one bad line must
        not discard the other 587.
    """
    try:
        root = DefusedET.fromstring(xml)
    except Exception as exc:  # defusedxml raises several unrelated types
        logger.warning("sitemap_unparseable", extra={"error_type": type(exc).__name__})
        return []

    tag = "sitemap" if root.tag.endswith("sitemapindex") else "url"
    raw = [
        element.text.strip()
        for element in root.iter(f"{_SITEMAP_NS}loc")
        if element.text and element.text.strip()
    ]
    urls = filter_allowed(raw[:max_urls], allowlist)
    logger.info(
        "sitemap_parsed",
        extra={"kind": tag, "entries": len(raw), "allowed": len(urls)},
    )
    return urls


def fetch_sitemap_urls(
    client: SafeHttpClient, sitemap_url: str, allowlist: Allowlist, *, max_urls: int = MAX_URLS
) -> list[str]:
    """Fetch and parse a sitemap.

    Args:
        client: The guarded HTTP client.
        sitemap_url: Absolute URL of the sitemap.
        allowlist: Policy applied to every extracted URL.
        max_urls: Cap on the number of URLs returned.

    Returns:
        Permitted URLs, or an empty list if the sitemap is missing or unusable.
    """
    try:
        result = client.fetch(sitemap_url)
    except FetchError as exc:
        logger.warning(
            "sitemap_unavailable",
            extra={"url": sitemap_url, "error_type": type(exc).__name__},
        )
        return []
    return parse_sitemap_urls(result.content, allowlist, max_urls=max_urls)
