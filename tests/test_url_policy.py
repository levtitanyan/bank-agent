"""Tests for URL policy: which URLs may be fetched at all.

The rule is exact-host membership over https. These tests exist mainly to pin
the hostile cases - the lookalike hosts that a suffix check would let through.
"""

from __future__ import annotations

import pytest

from tariff_agent.config import Allowlist
from tariff_agent.errors import DomainNotAllowedError
from tariff_agent.http.url_policy import (
    assert_allowed,
    filter_allowed,
    is_allowed_url,
    normalize_url,
)

ALLOWLIST = Allowlist(allowed_schemes=("https",), domains=("acba.am", "www.acba.am"))


@pytest.mark.parametrize(
    "url",
    [
        "https://acba.am/hy/individual/loans/consumer-loans",
        "https://www.acba.am/files/loans-tariffs.pdf",
        "https://ACBA.AM/hy",
        "https://acba.am./hy",
    ],
)
def test_allowed_urls(url: str) -> None:
    """Official ACBA hosts over https are fetchable, however they are written."""
    assert is_allowed_url(url, ALLOWLIST)


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("https://acba.am.evil.com/hy", "lookalike suffix"),
        ("https://evil-acba.am/hy", "lookalike prefix"),
        ("https://files.acba.am/x.pdf", "subdomain not listed"),
        ("http://acba.am/hy", "plain http"),
        ("ftp://acba.am/x.pdf", "wrong scheme"),
        ("https://acba.am:8443/hy", "non-default port"),
        ("/hy/individual", "relative with no base"),
        ("javascript:alert(1)", "not a fetchable URL"),
    ],
)
def test_blocked_urls(url: str, why: str) -> None:
    """Everything else is refused, including the near-misses."""
    assert not is_allowed_url(url, ALLOWLIST), why


def test_assert_allowed_returns_the_normalized_url() -> None:
    """Callers get back the canonical form, so caching and logging agree."""
    assert assert_allowed("https://ACBA.AM/hy#section", ALLOWLIST) == "https://acba.am/hy"


def test_assert_allowed_names_the_host_it_refused() -> None:
    """The error must tell a reviewer what was blocked and why."""
    with pytest.raises(DomainNotAllowedError, match="evil.com"):
        assert_allowed("https://acba.am.evil.com/x", ALLOWLIST)


def test_percent_encoding_is_preserved() -> None:
    """'loan%20info.pdf' must stay encoded or the request 404s."""
    url = "https://www.acba.am/files/loan%20info.pdf"
    assert normalize_url(url) == url


def test_relative_links_resolve_against_their_page() -> None:
    """Discovery harvests relative hrefs and must resolve them per RFC 3986.

    The last path segment of the base is a document, not a directory, so
    '../../' from /hy/individual/loans/consumer-loans lands in /hy/.
    """
    base = "https://acba.am/hy/individual/loans/consumer-loans"
    assert (
        normalize_url("../../files/loans-tariffs.pdf", base)
        == "https://acba.am/hy/files/loans-tariffs.pdf"
    )
    assert (
        normalize_url("/files/loans-tariffs.pdf", base)
        == "https://acba.am/files/loans-tariffs.pdf"
    )
    assert normalize_url("5G-loan", base) == "https://acba.am/hy/individual/loans/5G-loan"


def test_fragments_are_dropped_but_queries_kept() -> None:
    """Fragments never reach a server; query strings identify the document."""
    assert normalize_url("https://acba.am/hy?tab=rates#top") == "https://acba.am/hy?tab=rates"


def test_filter_allowed_keeps_order_drops_duplicates_and_off_domain() -> None:
    """Link harvesting drops foreign links quietly rather than raising."""
    base = "https://acba.am/hy/individual/loans/consumer-loans"
    links = [
        "/hy/individual/loan/5G-loan",
        "https://www.facebook.com/acba",
        "/hy/individual/loan/5G-loan",
        "https://www.acba.am/files/loans-tariffs.pdf",
        "mailto:info@acba.am",
    ]
    assert filter_allowed(links, ALLOWLIST, base) == [
        "https://acba.am/hy/individual/loan/5G-loan",
        "https://www.acba.am/files/loans-tariffs.pdf",
    ]


def test_url_without_a_host_is_refused_by_normalization() -> None:
    """A hostless URL cannot be checked, so it cannot be fetched."""
    with pytest.raises(DomainNotAllowedError, match="no host"):
        normalize_url("mailto:info@acba.am")
