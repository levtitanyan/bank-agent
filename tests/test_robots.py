"""Tests for robots.txt handling.

The behaviour under test is the split the reviewer asked for: a missing file
means "no rules exist" and we proceed, while a server that fails to answer means
permission is unknown and the run stops.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tariff_agent.config import Allowlist, HttpSettings
from tariff_agent.errors import RobotsDisallowedError, RobotsUnavailableError
from tariff_agent.http.client import ContentKind
from tariff_agent.http.robots import build_client

ALLOWLIST = Allowlist(allowed_schemes=("https",), domains=("acba.am", "www.acba.am"))
PAGE_URL = "https://acba.am/hy/individual/loans/consumer-loans"
PRIVATE_URL = "https://acba.am/internal/secret"
PDF_BYTES = b"%PDF-1.7\nx"

ROBOTS = b"""User-agent: *
Disallow: /internal/
Allow: /
"""


Handler = Callable[[httpx.Request], httpx.Response]


def routed(robots: httpx.Response, page: httpx.Response) -> tuple[Handler, list[str]]:
    """Build a handler answering robots.txt and page requests separately."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return robots if request.url.path == "/robots.txt" else page

    return handler, seen


@pytest.fixture
def settings(tmp_path: Path) -> HttpSettings:
    """Settings with robots checking on and a temporary cache."""
    return HttpSettings(
        max_attempts=2, backoff_base=0.01, respect_robots=True, cache_dir=tmp_path / "cache"
    )


def test_allowed_path_is_fetched_and_robots_is_read_once(settings: HttpSettings) -> None:
    """robots.txt is fetched once per host, then cached for the run."""
    page = httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})
    handler, seen = routed(httpx.Response(200, content=ROBOTS), page)
    client = build_client(
        ALLOWLIST, settings, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    client.fetch(PAGE_URL, expect=ContentKind.HTML)
    client.fetch(PAGE_URL, expect=ContentKind.HTML)
    assert sum(1 for url in seen if url.endswith("/robots.txt")) == 1


def test_disallowed_path_is_refused_before_fetching(settings: HttpSettings) -> None:
    """A path the bank asks crawlers to avoid is never requested."""
    page = httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})
    handler, seen = routed(httpx.Response(200, content=ROBOTS), page)
    client = build_client(
        ALLOWLIST, settings, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    with pytest.raises(RobotsDisallowedError):
        client.fetch(PRIVATE_URL)
    assert not any("/internal/" in url for url in seen)


def test_missing_robots_means_no_rules_exist(settings: HttpSettings) -> None:
    """404 is the normal case for most servers: proceed."""
    page = httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})
    handler, _ = routed(httpx.Response(404), page)
    client = build_client(
        ALLOWLIST, settings, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    assert client.fetch(PAGE_URL, expect=ContentKind.HTML).size > 0


def test_server_error_on_robots_stops_the_run(settings: HttpSettings) -> None:
    """5xx means permission is unknown, so we do not resolve it in our favour."""
    page = httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})
    handler, _ = routed(httpx.Response(503), page)
    client = build_client(
        ALLOWLIST, settings, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    with pytest.raises(RobotsUnavailableError, match="stopping rather than assuming"):
        client.fetch(PAGE_URL, expect=ContentKind.HTML)


def test_robots_can_be_switched_off_for_offline_fixtures(tmp_path: Path) -> None:
    """With respect_robots false, no robots.txt request is made at all."""
    settings = HttpSettings(respect_robots=False, cache_dir=tmp_path / "cache")
    page = httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})
    handler, seen = routed(httpx.Response(200, content=ROBOTS), page)
    client = build_client(
        ALLOWLIST, settings, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    client.fetch(PAGE_URL, expect=ContentKind.HTML)
    assert not any(url.endswith("/robots.txt") for url in seen)
